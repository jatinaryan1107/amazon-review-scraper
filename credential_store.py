"""Optional Windows Credential Manager storage for this data folder only."""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path


class CredentialStoreError(Exception):
    """Never include a credential or operating-system response in this error."""


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


class WindowsCredentialStore:
    def __init__(self, data_dir):
        if os.name != "nt":
            raise CredentialStoreError("Secure key storage is available on Windows only.")
        scope = os.path.normcase(str(Path(data_dir).resolve()))
        self.target = "Reviewdesk/OpenRouter/" + hashlib.sha256(scope.encode("utf-8")).hexdigest()
        self._api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        pointer = ctypes.POINTER(_Credential)
        self._api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None

    def load(self):
        credential = ctypes.POINTER(_Credential)()
        if not self._api.CredReadW(self.target, 1, 0, ctypes.byref(credential)):
            if ctypes.get_last_error() == 1168:
                return None
            raise CredentialStoreError("Windows could not read the saved Reviewdesk key.")
        try:
            size = credential.contents.CredentialBlobSize
            if not 0 < size <= 2560:
                raise CredentialStoreError("The saved Reviewdesk credential is invalid.")
            raw = ctypes.string_at(credential.contents.CredentialBlob, size)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise CredentialStoreError("The saved Reviewdesk credential is invalid.") from None
        finally:
            self._api.CredFree(credential)

    def save(self, value):
        raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if not 0 < len(raw) <= 2560:
            raise CredentialStoreError("The Reviewdesk credential is too large.")
        buffer = ctypes.create_string_buffer(raw)
        credential = _Credential(Type=1, TargetName=self.target, Comment="Reviewdesk API key and AI preferences",
                                 CredentialBlobSize=len(raw),
                                 CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE)),
                                 Persist=2, UserName="OpenRouter")
        try:
            if not self._api.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialStoreError("Windows could not save the Reviewdesk key.")
        finally:
            ctypes.memset(buffer, 0, len(buffer))

    def delete(self):
        if not self._api.CredDeleteW(self.target, 1, 0) and ctypes.get_last_error() != 1168:
            raise CredentialStoreError("Windows could not forget the saved Reviewdesk key.")


def system_credential_store(data_dir):
    if os.name != "nt":
        return None
    try:
        return WindowsCredentialStore(data_dir)
    except (OSError, AttributeError):
        return None
