"""User-scoped encrypted secret storage.

Provider metadata is intentionally kept separate from credentials. Windows
uses DPAPI for the current OS user. macOS stores each value in the login
Keychain and writes only an opaque Keychain reference to disk. Neither backend
creates portable credential data or places plaintext in Gitgo state files.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading
import uuid


class SecretStoreError(RuntimeError):
    """Credential storage is unavailable or corrupted."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


class WindowsDPAPIProtector:
    """Small stdlib-only wrapper around current-user Windows DPAPI."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretStoreError("Windows DPAPI is unavailable on this platform")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    def protect(self, plaintext: str) -> str:
        raw = plaintext.encode("utf-8")
        source, keepalive = _blob(raw)
        output = _DataBlob()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(source), "Gitgo provider credential", None, None, None,
            self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
        )
        if not ok:
            raise SecretStoreError(f"DPAPI protect failed ({ctypes.get_last_error()})")
        del keepalive
        try:
            encrypted = ctypes.string_at(output.pbData, output.cbData)
            return base64.b64encode(encrypted).decode("ascii")
        finally:
            self._kernel32.LocalFree(output.pbData)

    def unprotect(self, ciphertext: str) -> str:
        try:
            raw = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise SecretStoreError("Credential ciphertext is not valid base64") from exc
        source, keepalive = _blob(raw)
        output = _DataBlob()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None,
            self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
        )
        if not ok:
            raise SecretStoreError(f"DPAPI unprotect failed ({ctypes.get_last_error()})")
        del keepalive
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretStoreError("Decrypted credential is not UTF-8") from exc
        finally:
            self._kernel32.LocalFree(output.pbData)


class _MacOSSecurityFramework:
    """Minimal ctypes bridge to the native macOS Keychain Services API."""

    _SUCCESS = 0
    _ITEM_NOT_FOUND = -25300

    def __init__(self) -> None:
        security_path = "/System/Library/Frameworks/Security.framework/Security"
        core_foundation_path = (
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        try:
            self._security = ctypes.CDLL(security_path)
            self._core_foundation = ctypes.CDLL(core_foundation_path)
        except OSError as exc:
            raise SecretStoreError("macOS Keychain framework is unavailable") from exc

        self._security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        self._core_foundation.CFRelease.restype = None

    @staticmethod
    def _encoded(value: str) -> bytes:
        return value.encode("utf-8")

    @staticmethod
    def _raise(action: str, status: int) -> None:
        raise SecretStoreError(
            f"macOS Keychain {action} failed (OSStatus {status})"
        )

    def add(self, service: str, account: str, plaintext: str) -> None:
        service_data = self._encoded(service)
        account_data = self._encoded(account)
        password_data = self._encoded(plaintext)
        item_ref = ctypes.c_void_p()
        status = self._security.SecKeychainAddGenericPassword(
            None,
            len(service_data), service_data,
            len(account_data), account_data,
            len(password_data), password_data,
            ctypes.byref(item_ref),
        )
        try:
            if status != self._SUCCESS:
                self._raise("write", status)
        finally:
            if item_ref.value:
                self._core_foundation.CFRelease(item_ref)

    def read(self, service: str, account: str) -> str:
        service_data = self._encoded(service)
        account_data = self._encoded(account)
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        item_ref = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service_data), service_data,
            len(account_data), account_data,
            ctypes.byref(password_length), ctypes.byref(password_data),
            ctypes.byref(item_ref),
        )
        try:
            if status != self._SUCCESS:
                self._raise("read", status)
            try:
                raw = ctypes.string_at(password_data, password_length.value)
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SecretStoreError(
                    "macOS Keychain credential is not UTF-8"
                ) from exc
        finally:
            if password_data.value:
                self._security.SecKeychainItemFreeContent(None, password_data)
            if item_ref.value:
                self._core_foundation.CFRelease(item_ref)

    def delete(self, service: str, account: str) -> None:
        service_data = self._encoded(service)
        account_data = self._encoded(account)
        item_ref = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service_data), service_data,
            len(account_data), account_data,
            None, None, ctypes.byref(item_ref),
        )
        if status == self._ITEM_NOT_FOUND:
            return
        if status != self._SUCCESS:
            self._raise("lookup for deletion", status)
        try:
            delete_status = self._security.SecKeychainItemDelete(item_ref)
            if delete_status not in (self._SUCCESS, self._ITEM_NOT_FOUND):
                self._raise("delete", delete_status)
        finally:
            if item_ref.value:
                self._core_foundation.CFRelease(item_ref)


class MacOSKeychainProtector:
    """Store credentials in Keychain and expose only opaque references."""

    SERVICE = "io.github.truman-duo.gitgo.provider-credentials"
    REFERENCE_PREFIX = "macos-keychain:"

    def __init__(self, keychain=None) -> None:
        if sys.platform != "darwin" and keychain is None:
            raise SecretStoreError("macOS Keychain is unavailable on this platform")
        self._keychain = keychain or _MacOSSecurityFramework()

    def _account(self, reference: str) -> str:
        if not reference.startswith(self.REFERENCE_PREFIX):
            raise SecretStoreError("Credential is not a macOS Keychain reference")
        account = reference[len(self.REFERENCE_PREFIX):]
        try:
            parsed = uuid.UUID(hex=account)
        except (ValueError, AttributeError) as exc:
            raise SecretStoreError("macOS Keychain reference is invalid") from exc
        if parsed.hex != account:
            raise SecretStoreError("macOS Keychain reference is invalid")
        return account

    def protect(self, plaintext: str) -> str:
        account = uuid.uuid4().hex
        self._keychain.add(self.SERVICE, account, plaintext)
        return f"{self.REFERENCE_PREFIX}{account}"

    def unprotect(self, reference: str) -> str:
        return self._keychain.read(self.SERVICE, self._account(reference))

    def delete(self, reference: str) -> None:
        self._keychain.delete(self.SERVICE, self._account(reference))


def default_secret_protector():
    """Select the current OS credential backend without an insecure fallback."""
    if os.name == "nt":
        return WindowsDPAPIProtector()
    if sys.platform == "darwin":
        return MacOSKeychainProtector()
    raise SecretStoreError(
        f"Secure credential storage is unavailable on platform {sys.platform!r}"
    )


class EncryptedSecretStore:
    """Atomic encrypted key/value store.

    The injected protector seam keeps persistence tests deterministic without
    weakening the production backend.
    """

    VERSION = 1

    def __init__(self, path: Path, protector=None) -> None:
        self.path = Path(path)
        self.protector = protector or default_secret_protector()
        self._lock = threading.RLock()

    def _delete_protected(self, value: str) -> None:
        delete = getattr(self.protector, "delete", None)
        if callable(delete):
            try:
                delete(value)
            except SecretStoreError:
                # The durable store no longer references this value. Failure
                # to remove an orphan must not make a successful save appear
                # to have failed or risk rolling metadata back to stale data.
                pass

    def _read_ciphertexts(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreError(f"Cannot read encrypted credential store: {exc}") from exc
        if int(payload.get("version", 0) or 0) != self.VERSION:
            raise SecretStoreError("Unsupported credential store version")
        values = payload.get("secrets", {})
        if not isinstance(values, dict):
            raise SecretStoreError("Credential store has an invalid secrets map")
        return {str(key): str(value) for key, value in values.items()}

    def read_all(self) -> dict[str, str]:
        with self._lock:
            return {
                key: self.protector.unprotect(value)
                for key, value in self._read_ciphertexts().items()
            }

    def upsert(self, values: dict[str, str]) -> None:
        if not values:
            return
        with self._lock:
            encrypted = self._read_ciphertexts()
            replacements: dict[str, tuple[str | None, str]] = {}
            try:
                for key, value in values.items():
                    normalized_key = str(key)
                    protected = self.protector.protect(str(value))
                    replacements[normalized_key] = (
                        encrypted.get(normalized_key), protected,
                    )
                    encrypted[normalized_key] = protected
                self._write_ciphertexts(encrypted)
            except Exception:
                for _old, protected in replacements.values():
                    self._delete_protected(protected)
                raise
            for old, protected in replacements.values():
                if old and old != protected:
                    self._delete_protected(old)

    def retain_only(self, keys: set[str]) -> None:
        with self._lock:
            encrypted = self._read_ciphertexts()
            retained = {key: value for key, value in encrypted.items() if key in keys}
            if retained != encrypted:
                self._write_ciphertexts(retained)
                for key, value in encrypted.items():
                    if key not in retained:
                        self._delete_protected(value)

    def clear(self) -> None:
        """Remove every native credential, then durably empty the reference file.

        Unlike ordinary orphan cleanup, uninstall must fail closed: if the OS
        credential backend refuses a deletion, keep the reference index so the
        operation can be retried instead of silently stranding a secret.
        """
        with self._lock:
            encrypted = self._read_ciphertexts()
            delete = getattr(self.protector, "delete", None)
            if callable(delete):
                for value in encrypted.values():
                    delete(value)
            if encrypted or self.path.exists():
                self._write_ciphertexts({})

    def _write_ciphertexts(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"version": self.VERSION, "secrets": values}, handle,
                          ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
