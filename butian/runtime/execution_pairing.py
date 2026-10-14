"""Local pairing material. Secrets never appear in public connection metadata."""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import stat
from pathlib import Path

from .execution_protocol import canonical


def validate_local_path(path: Path, reason: str) -> None:
    """Reject symlinks and Windows reparse points anywhere in a local path."""
    path = Path(path).absolute()
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(reason)

class _Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _protect_windows(data: bytes, *, decrypt: bool = False) -> bytes:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), buffer)
    target = _Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    if decrypt:
        operation = crypt.CryptUnprotectData
        operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    else:
        operation = crypt.CryptProtectData
        operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    operation.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # UI_FORBIDDEN, current-user protection; no machine-wide scope.
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError(ctypes.get_last_error(), 'executor_credential_protection_failed')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def create_pairing(path: Path, executor_id: str) -> dict:
    if not executor_id or len(executor_id) > 128:
        raise ValueError('executor_identity_invalid')
    path = Path(path)
    validate_local_path(path, 'executor_pairing_path_invalid')
    if not path.parent.is_dir() or path.parent.is_symlink() or path.exists() or path.is_symlink():
        raise ValueError('executor_pairing_path_invalid')
    material = {'version': 1, 'executor_id': executor_id, 'key': secrets.token_hex(32)}
    encoded = canonical(material)
    if os.name == 'nt':
        encoded = b'BTDPAPI1\n' + _protect_windows(encoded)
    else:
        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise ValueError('executor_pairing_directory_unprotected')
        encoded = b'BTPOSIX1\n' + encoded
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return {'executor_id': executor_id, 'credential_protection': 'current_user_dpapi' if os.name == 'nt' else 'owner_only_mode'}


def load_pairing(path: Path) -> tuple[str, bytes]:
    path = Path(path)
    validate_local_path(path, 'executor_pairing_path_invalid')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError('executor_pairing_path_invalid')
    encoded = path.read_bytes()
    if os.name == 'nt':
        if not encoded.startswith(b'BTDPAPI1\n'):
            raise ValueError('executor_pairing_protection_invalid')
        encoded = _protect_windows(encoded[9:], decrypt=True)
    else:
        if stat.S_IMODE(path.stat().st_mode) & 0o077 or not encoded.startswith(b'BTPOSIX1\n'):
            raise ValueError('executor_pairing_protection_invalid')
        encoded = encoded[9:]
    try:
        value = json.loads(encoded)
        if set(value) != {'version', 'executor_id', 'key'} or value['version'] != 1 or not isinstance(value['executor_id'], str) or not value['executor_id']:
            raise ValueError()
        key = bytes.fromhex(value['key'])
        if len(key) != 32:
            raise ValueError()
    except (TypeError, ValueError, KeyError):
        raise ValueError('executor_pairing_material_invalid') from None
    return value['executor_id'], key
