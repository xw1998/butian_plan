"""Windows job ownership. Called only by an explicitly launched executor.

The trusted bootstrap waits for a gate before spawning any user command. The
bootstrap is assigned to a non-breakaway, kill-on-close job before gate release.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes

_BOOTSTRAP = '''import json,os,subprocess,sys
if os.name=='nt':
    import msvcrt
    msvcrt.setmode(0,os.O_BINARY)
header=bytearray()
while len(header)<1048576:
    byte=os.read(0,1)
    if not byte: sys.exit(125)
    if byte==b'\\n': break
    header.extend(byte)
else:
    sys.exit(125)
request=json.loads(header)
process=subprocess.Popen(request['argv'],stdin=sys.stdin.buffer,stdout=sys.stdout.buffer,stderr=sys.stderr.buffer,cwd=request['cwd'],env=request['env'],shell=False)
sys.exit(process.wait())
'''


class _BasicLimits(ctypes.Structure):
    _fields_ = [('ProcessTime', ctypes.c_int64), ('JobTime', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimits), ('IoInfo', _IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                ('ThisPeriodTotalUserTime', ctypes.c_int64), ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]


class WindowsJob:
    def __init__(self):
        if os.name != 'nt':
            raise ValueError('executor_windows_job_unavailable')
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        definitions = {
            'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in definitions.items():
            operation = getattr(self.api, name)
            operation.argtypes, operation.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        self._denied = False
        if not self.handle:
            self._error('executor_job_create_failed')
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE; no BREAKAWAY flags.
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = OSError(ctypes.get_last_error(), 'executor_job_configure_failed')
            try:
                self.close()
            except OSError as cleanup:
                error.gate_cleanup_error = type(cleanup).__name__
                error.owned_job = self
            raise error

    @staticmethod
    def _error(reason):
        raise OSError(ctypes.get_last_error(), reason)

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            self._error('executor_job_assign_failed')

    def active_count(self) -> int:
        value = _Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
            self._error('executor_job_query_failed')
        return value.ActiveProcesses

    def stop(self):
        if self._denied:
            raise PermissionError('executor_stop_previously_denied')
        if not self.api.TerminateJobObject(self.handle, 1):
            code = ctypes.get_last_error()
            self._denied = True
            raise OSError(code, 'executor_job_stop_failed')

    def close(self):
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                self._error('executor_job_close_failed')
            self.handle = None


def execution_environment():
    environment = {'PATH': os.environ.get('PATH', ''), 'PYTHONIOENCODING': 'utf-8'}
    # Windows needs SystemRoot to initialize Python's cryptographic RNG.
    # Preserve this OS value without inheriting unrelated host credentials.
    if os.name == 'nt':
        system_root = os.environ.get('SystemRoot')
        if not system_root:
            raise ValueError('executor_system_root_missing')
        environment['SystemRoot'] = system_root
    return environment


class OwnedProcess:
    def __init__(self, argv: tuple[str, ...], cwd: str, stdin: bytes):
        environment = execution_environment()
        self.process = None
        try:
            self.job = WindowsJob()
        except OSError as exc:
            owned_job = getattr(exc, 'owned_job', None)
            if owned_job is not None:
                self.job = owned_job
                exc.owned_process = self
            raise
        try:
            self.process = subprocess.Popen([sys.executable, '-I', '-u', '-c', _BOOTSTRAP],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, shell=False, cwd=cwd,
                                            env=environment)
            try:
                self.job.assign(self.process)
            except BaseException as exc:
                # Preserve the original OS error and the bootstrap owner if EOF
                # has not yet produced exit evidence. Never substitute a kill.
                try:
                    self.abort_unreleased()
                except (OSError, subprocess.TimeoutExpired) as cleanup:
                    exc.gate_cleanup_error = type(cleanup).__name__
                raise
            self.header = (json.dumps({'argv': list(argv), 'cwd': cwd,
                                       'env': environment})
                           + '\n').encode('utf-8')
            self.input = stdin
        except BaseException as exc:
            try:
                if self.confirmed_exit():
                    self.close()
                else:
                    exc.owned_process = self
            except (OSError, RuntimeError) as cleanup:
                exc.gate_cleanup_error = type(cleanup).__name__
                exc.owned_process = self
            raise

    def abort_unreleased(self):
        # Closing the unopened gate sends EOF to trusted bootstrap only.
        # It does not invoke a process termination API.
        self.process.stdin.close()
        self.process.wait(timeout=5)

    def prepare_gate(self):
        # No newline: even a complete JSON header cannot release user code.
        if len(self.header) > 1048576:
            raise ValueError('executor_gate_header_too_large')
        self.process.stdin.write(self.header[:-1])
        self.process.stdin.flush()

    def release_gate(self):
        # Only this byte is serialized with revocation by the owner.
        self.process.stdin.write(b'\n')
        self.process.stdin.flush()

    def release_input(self):
        try:
            self.process.stdin.write(self.input)
        except (BrokenPipeError, OSError):
            pass  # Exit and tree evidence, not a broken stdin, determine outcome.
        finally:
            self.process.stdin.close()

    def release(self):
        self.prepare_gate()
        self.release_gate()
        self.release_input()

    def confirmed_exit(self) -> bool:
        return (self.process is None or self.process.poll() is not None) and self.job.active_count() == 0

    def stop(self):
        self.job.stop()

    def close(self):
        if self.process is not None:
            for name in ('stdin', 'stdout', 'stderr'):
                stream = getattr(self.process, name, None)
                if stream is not None:
                    stream.close()
        self.job.close()
