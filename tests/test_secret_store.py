"""Cross-platform credential-store contracts."""

from __future__ import annotations

import json

import pytest

from backend.core.secret_store import (
    EncryptedSecretStore,
    MacOSKeychainProtector,
    SecretStoreError,
    WindowsDPAPIProtector,
)


class _FakeKeychain:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def add(self, service: str, account: str, plaintext: str) -> None:
        key = (service, account)
        if key in self.values:
            raise SecretStoreError("duplicate fake keychain item")
        self.values[key] = plaintext

    def read(self, service: str, account: str) -> str:
        try:
            return self.values[(service, account)]
        except KeyError as exc:
            raise SecretStoreError("missing fake keychain item") from exc

    def delete(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


class _ReferenceProtector:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []

    def protect(self, plaintext: str) -> str:
        reference = f"ref-{len(self.values) + 1}"
        self.values[reference] = plaintext
        return reference

    def unprotect(self, reference: str) -> str:
        return self.values[reference]

    def delete(self, reference: str) -> None:
        self.deleted.append(reference)
        self.values.pop(reference, None)


def test_platform_protectors_keep_the_shared_round_trip_interface():
    assert callable(WindowsDPAPIProtector.protect)
    assert callable(WindowsDPAPIProtector.unprotect)
    assert callable(MacOSKeychainProtector.protect)
    assert callable(MacOSKeychainProtector.unprotect)


def test_macos_keychain_protector_keeps_plaintext_behind_opaque_reference():
    keychain = _FakeKeychain()
    protector = MacOSKeychainProtector(keychain=keychain)

    reference = protector.protect("sk-private-value")

    assert reference.startswith("macos-keychain:")
    assert "sk-private-value" not in reference
    assert protector.unprotect(reference) == "sk-private-value"
    protector.delete(reference)
    with pytest.raises(SecretStoreError, match="missing fake keychain item"):
        protector.unprotect(reference)


@pytest.mark.parametrize("reference", ["", "other:abc", "macos-keychain:not-a-uuid"])
def test_macos_keychain_protector_rejects_invalid_references(reference):
    protector = MacOSKeychainProtector(keychain=_FakeKeychain())
    with pytest.raises(SecretStoreError, match="reference"):
        protector.unprotect(reference)


def test_secret_store_removes_replaced_and_unreferenced_native_secrets(
    tmp_path_factory,
):
    path = tmp_path_factory / "provider_secrets.json"
    protector = _ReferenceProtector()
    store = EncryptedSecretStore(path, protector=protector)

    store.upsert({"provider:a": "first"})
    first_reference = json.loads(path.read_text(encoding="utf-8"))["secrets"]["provider:a"]
    store.upsert({"provider:a": "second"})
    second_reference = json.loads(path.read_text(encoding="utf-8"))["secrets"]["provider:a"]

    assert first_reference in protector.deleted
    assert second_reference not in protector.deleted
    assert store.read_all() == {"provider:a": "second"}

    store.retain_only(set())
    assert second_reference in protector.deleted
    assert store.read_all() == {}
