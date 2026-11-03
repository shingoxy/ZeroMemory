"""ZeroMemory's dedicated DeepSeek key in the current Windows user's vault."""

import ctypes as ct
import sys
from ctypes import wintypes as wt

TARGET = "ZeroMemory/DeepSeek/api.deepseek.com"
GENERIC = 1
LOCAL_MACHINE = 2  # Persistent for this Windows user on this computer, with no roaming.


class Credential(ct.Structure):
    _fields_ = [
        ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ct.POINTER(ct.c_ubyte)), ("Persist", wt.DWORD),
        ("AttributeCount", wt.DWORD), ("Attributes", ct.c_void_p),
        ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
    ]


def read_deepseek_key():
    if sys.platform != "win32":
        return None
    api = ct.WinDLL("Advapi32", use_last_error=True)
    read = api.CredReadW
    read.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.POINTER(ct.POINTER(Credential))]
    read.restype = wt.BOOL
    free = api.CredFree
    free.argtypes = [ct.c_void_p]
    free.restype = None
    credential = ct.POINTER(Credential)()
    if not read(TARGET, GENERIC, 0, ct.byref(credential)):
        error = ct.get_last_error()
        if error == 1168:  # ERROR_NOT_FOUND
            return None
        raise ct.WinError(error)
    try:
        value = credential.contents
        return ct.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("ascii")
    finally:
        free(credential)


def save_deepseek_key(key):
    if sys.platform != "win32":
        raise OSError("Windows Credential Manager is unavailable")
    if not isinstance(key, str) or not 1 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
        raise ValueError("Expected a single printable ASCII API key")
    api = ct.WinDLL("Advapi32", use_last_error=True)
    write = api.CredWriteW
    write.argtypes = [ct.POINTER(Credential), wt.DWORD]
    write.restype = wt.BOOL
    blob = (ct.c_ubyte * len(key))(*map(ord, key))
    credential = Credential(Type=GENERIC, TargetName=TARGET, CredentialBlobSize=len(key),
                            CredentialBlob=blob, Persist=LOCAL_MACHINE, UserName="ZeroMemory")
    try:
        if not write(ct.byref(credential), 0):
            raise ct.WinError(ct.get_last_error())
    finally:
        ct.memset(blob, 0, len(blob))
