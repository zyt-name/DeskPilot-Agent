from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

_ENTROPY = b"xm-agent-api-key-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))),
        buffer,
    )


def _win32() -> None:
    if sys.platform != "win32":
        raise RuntimeError("API key encryption requires Windows DPAPI")


def _crypt32():
    _win32()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    return crypt32


def _free(pointer) -> None:
    if pointer:
        ctypes.windll.kernel32.LocalFree(pointer)


def _protect(data: bytes) -> bytes:
    crypt32 = _crypt32()
    in_blob, in_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "xm-agent",
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    _ = in_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _free(out_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    crypt32 = _crypt32()
    in_blob, in_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    out_blob = _DataBlob()
    description = wintypes.LPWSTR()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    _ = in_buffer, entropy_buffer
    if description:
        _free(description)
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _free(out_blob.pbData)


def encrypt_secret(secret: str) -> str:
    if not secret:
        return ""
    return base64.b64encode(_protect(secret.encode("utf-8"))).decode("ascii")


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    return _unprotect(base64.b64decode(token)).decode("utf-8")


def secret_hint(secret: str) -> str:
    clean = secret.strip()
    if not clean:
        return ""
    if len(clean) <= 8:
        return "已保存"
    return f"{clean[:3]}…{clean[-4:]}"


__all__ = ["decrypt_secret", "encrypt_secret", "secret_hint"]
