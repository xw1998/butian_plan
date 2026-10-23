"""建立“重定向点”的测试辅助：符号链接优先，无权限时退化为目录联接。

备份/还原的源码树扫描要求“不跟随重定向”，而 Windows 上创建符号链接需要
开发者模式或管理员权限（winerror 1314）。发版闸门要求全量测试零 skip，
所以这里不再 skip：拿不到符号链接权限时改用目录联接（junction），它同样是
重定向点（FILE_ATTRIBUTE_REPARSE_POINT），走的是被测代码的同一条判定分支。

联接用 DeviceIoControl(FSCTL_SET_REPARSE_POINT) 建立，不依赖 cmd.exe 或
mklink，因此在受限终端里也能跑。
"""
from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes as _wt
from pathlib import Path

_GENERIC_WRITE = 0x40000000
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FSCTL_SET_REPARSE_POINT = 0x000900A4
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_ERROR_PRIVILEGE_NOT_HELD = 1314


def _make_junction(link: Path, target: Path) -> None:
    link = Path(link)
    target = Path(os.path.abspath(str(target)))
    if not target.is_dir():
        raise ValueError("junction_target_must_be_directory")
    if link.exists() or link.is_symlink():
        raise FileExistsError(str(link))
    link.mkdir(parents=True)
    substitute = ("\\??\\" + str(target)).encode("utf-16-le")
    printable = str(target).encode("utf-16-le")
    buffer = substitute + b"\x00\x00" + printable + b"\x00\x00"
    payload = ctypes.create_string_buffer(8 + 8 + len(buffer))
    struct.pack_into(
        "<IHHHHHH", payload, 0,
        _IO_REPARSE_TAG_MOUNT_POINT, 8 + len(buffer), 0,
        0, len(substitute), len(substitute) + 2, len(printable),
    )
    payload[16:16 + len(buffer)] = buffer
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = _wt.HANDLE
    kernel32.CreateFileW.argtypes = [_wt.LPCWSTR, _wt.DWORD, _wt.DWORD, _wt.LPVOID,
                                     _wt.DWORD, _wt.DWORD, _wt.HANDLE]
    handle = kernel32.CreateFileW(str(link), _GENERIC_WRITE, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
                                  _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle in (None, _wt.HANDLE(-1).value, -1):
        raise OSError(ctypes.get_last_error(), "create_junction_open_failed")
    try:
        returned = _wt.DWORD(0)
        if not kernel32.DeviceIoControl(_wt.HANDLE(handle), _FSCTL_SET_REPARSE_POINT,
                                        payload, len(payload), None, 0, ctypes.byref(returned), None):
            raise OSError(ctypes.get_last_error(), "create_junction_set_reparse_failed")
    finally:
        kernel32.CloseHandle(_wt.HANDLE(handle))


def make_redirect(link, target) -> str:
    """在 link 处建立指向 target 的重定向点，返回 "symlink" 或 "junction"。"""
    link = Path(link)
    target = Path(target)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return "symlink"
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != _ERROR_PRIVILEGE_NOT_HELD:
            raise
    _make_junction(link, target)
    return "junction"
