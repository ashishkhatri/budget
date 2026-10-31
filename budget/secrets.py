"""Keep API secrets out of the database in readable form.

On Windows, values are encrypted with DPAPI (the same protection Windows uses for saved
passwords): only your Windows user account on this computer can decrypt them, so a copy of the
database or a backup is useless elsewhere. On other systems (used for development and tests)
values are only obfuscated, and ``protected()`` says so.
"""
from __future__ import annotations

import base64
import sys

_PREFIX_DPAPI = "dpapi:"
_PREFIX_PLAIN = "plain:"


def protected() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _ENTROPY = b"ClaudeBudget/plaid"

    def _blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))      # returned too, so it stays alive
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _call(encrypt: bool, data: bytes) -> bytes:
        (src, _b1), (ent, _b2), out = _blob(data), _blob(_ENTROPY), _Blob()
        fn = _crypt32.CryptProtectData if encrypt else _crypt32.CryptUnprotectData
        # CRYPTPROTECT_UI_FORBIDDEN = 0x1
        if not fn(ctypes.byref(src), ctypes.c_wchar_p("budget") if encrypt else None,
                  ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):
            raise OSError("Windows couldn't encrypt/decrypt the secret (DPAPI).")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def _protect(data: bytes) -> bytes:
        return _call(True, data)

    def _unprotect(data: bytes) -> bytes:
        return _call(False, data)


def seal(value: str) -> str:
    if not value:
        return ""
    raw = value.encode("utf-8")
    if protected():
        try:
            sealed = _protect(raw)
            if _unprotect(sealed) == raw:           # make sure it can be read back before relying on it
                return _PREFIX_DPAPI + base64.b64encode(sealed).decode("ascii")
        except (OSError, ValueError):
            pass
    return _PREFIX_PLAIN + base64.b64encode(raw).decode("ascii")


def is_sealed(stored: str) -> bool:
    """True when the stored value is really encrypted (not just obfuscated)."""
    return bool(stored) and stored.startswith(_PREFIX_DPAPI)


def unseal(stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith(_PREFIX_DPAPI):
        if not protected():
            raise OSError("This secret was encrypted on Windows and can only be read there.")
        return _unprotect(base64.b64decode(stored[len(_PREFIX_DPAPI):])).decode("utf-8")
    if stored.startswith(_PREFIX_PLAIN):
        return base64.b64decode(stored[len(_PREFIX_PLAIN):]).decode("utf-8")
    return stored
