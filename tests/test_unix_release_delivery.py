"""Keep every declared Unix updater target covered by preview release trust."""
import json
import os
import sys
from pathlib import Path
import pytest
from scripts.check_unix_release_delivery import TARGETS, policy, validate_handoff_strategy, validate_published_identity
from types import SimpleNamespace
from backend.core.update.manifest import TARGETS as UPDATE_TARGETS
from backend.core.update.release_policy import parse_release_policy

ROOT = Path(__file__).resolve().parents[1]

def test_all_declared_unix_targets_are_accounted_for():
    assert TARGETS == {target for target in UPDATE_TARGETS if target[0] != "windows"}


def test_package_support_metadata_covers_the_native_release_matrix():
    from scripts.unix_release_manifest import BASELINES
    assert set(BASELINES) == TARGETS
    assert BASELINES[("macos", "arm64")] == "macOS 26"

@pytest.mark.parametrize("target,arch", sorted(TARGETS))
def test_release_policy_preserves_pinned_key_and_keeps_stable_separate(target, arch):
    base = json.loads((ROOT/"packaging/release-trust/windows-x86_64.json").read_bytes())
    value = policy(ROOT, target, arch)
    assert value["public_keys"] == base["public_keys"]
    assert value["allowed_hosts"] == base["allowed_hosts"]
    assert set(value["channels"]) == {"preview"}
    selected = parse_release_policy(json.dumps(value).encode(), target, arch).channel("preview")
    assert selected.manifest_url.endswith("/updates-preview/"+target+"-"+arch+".manifest.json")
    assert selected.signature_url.endswith("/updates-preview/"+target+"-"+arch+".manifest.sig")

@pytest.mark.parametrize("target,arch", [("macos","x86_64"),("windows","x86_64"),("linux","arm64"),("other","x86_64")])
def test_undeclared_target_is_rejected_before_reading_input(target, arch):
    with pytest.raises(ValueError, match="Unsupported"):
        policy(Path("does-not-exist"), target, arch)


@pytest.mark.parametrize("version", ["1.0.0-alpha.3.1", "1.0.0-alpha.3.2", "1.0.0-alpha.3.3", "2.0.0-beta.1"])
@pytest.mark.parametrize("target,arch", sorted(TARGETS))
def test_published_validation_follows_actual_release_version(version, target, arch):
    manifest = SimpleNamespace(source_commit="a" * 40, release_id="v" + version,
        version=version, scope=("gitgo-terminal", "preview", target, arch))
    validate_published_identity(manifest, "a" * 40, target, arch, version)


@pytest.mark.parametrize("change", ["source_commit", "release_id", "version", "scope"])
def test_new_version_never_relaxes_other_release_identity_checks(change):
    manifest = SimpleNamespace(source_commit="a" * 40, release_id="v1.0.0-alpha.3.1",
        version="1.0.0-alpha.3.1", scope=("gitgo-terminal", "preview", "macos", "arm64"))
    setattr(manifest, change, ("gitgo-terminal", "stable", "macos", "arm64") if change == "scope" else "wrong")
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_published_identity(manifest, "a" * 40, "macos", "arm64", "1.0.0-alpha.3.1")


@pytest.mark.parametrize("platform,api,expected", [
    ("macos", 12, "immediate"), ("macos", 11, "after_reboot"),
    ("macos", 9, "after_reboot"), ("macos", 6, "after_reboot"),
    ("linux", 11, "immediate"), ("linux", 9, "immediate"), ("linux", 6, "immediate"),
])
def test_bootstrap_strategy_matches_real_handoff_including_legacy_mac(monkeypatch, platform, api, expected):
    from backend.core.update import handoff
    # Only this module sees the modeled platform; no global sys/platform edit.
    monkeypatch.setattr(handoff, "sys", SimpleNamespace(platform="darwin" if platform == "macos" else "linux"))
    store = SimpleNamespace(registration={"platform": platform, "bootstrap_api": api})
    actual = handoff.strategy(store)
    assert actual == expected
    assert validate_handoff_strategy(platform, api, actual) == expected
    wrong = "after_reboot" if expected == "immediate" else "immediate"
    with pytest.raises(ValueError, match="Wrong update handoff"):
        validate_handoff_strategy(platform, api, wrong)


@pytest.mark.parametrize("platform,api", [
    ("macos", None), ("macos", True), ("macos", "12"), ("macos", 12.0),
    ("macos", 13), ("linux", 12), ("windows", 10),
])
def test_unknown_or_malformed_bootstrap_never_defaults_to_immediate(platform, api):
    with pytest.raises(ValueError, match="Unsupported installed"):
        validate_handoff_strategy(platform, api, "immediate")


@pytest.fixture
def initial_install_check(tmp_path_factory, monkeypatch):
    """Real managed registration and bootstrap hashes, never execute fake payloads."""
    if os.name != "posix":
        pytest.skip("Unix managed registration requires POSIX ownership")
    from backend.core.update import installation
    from backend.core.update.bootstrap_contract import CURRENT_API
    import scripts.check_unix_release_delivery as delivery
    import scripts.smoke_packaged_runtime as smoke
    from tests.test_update_transaction import build_fixture, refresh_build

    platform = "macos" if sys.platform == "darwin" else "linux"
    architecture = "arm64" if platform == "macos" else "x86_64"
    release = tmp_path_factory / "release"
    build_fixture(release, platform=platform, architecture=architecture)
    (release / "release-trust.json").write_text(json.dumps(policy(ROOT, platform, architecture)))
    refresh_build(release, platform=platform, architecture=architecture)
    state = {"client_opened": False, "client_closed": False, "installer_api": CURRENT_API,
             "reported": "immediate", "damage_bootstrap": False}

    def probe(host, profile):
        package = host.parents[2]
        product = json.loads((package / "product.json").read_bytes())
        contract = json.loads((package / "update-data-contract.json").read_bytes())
        return {"runtime": {**product, "data_compatibility": contract["data_compatibility"]},
                "cleanup": {"complete": False}}
    monkeypatch.setattr(installation, "probe_managed_host", probe)

    def install(command, **kwargs):
        assert command[0] == str(release / "install.sh") and "--managed" in command
        root = Path(command[command.index("--install-root") + 1])
        root.mkdir(mode=0o700)
        monkeypatch.setattr(installation.ManagedStore, "BOOTSTRAP_API", state["installer_api"])
        store = installation.initialize_managed(root, release,
            protected_paths=(Path(kwargs["env"]["HOME"]),))
        state["registration"] = store.registration
        if state["damage_bootstrap"]:
            (root / "bootstrap/gitgo-host/gitgo-host").write_bytes(b"changed bootstrap")
        return SimpleNamespace(stdout='{"managed": true}')
    monkeypatch.setattr(delivery, "subprocess", SimpleNamespace(run=install))

    class Client:
        def __init__(self, host, env):
            state["client_opened"] = True

        def call(self, method, params, **kwargs):
            assert method == "update.status"
            return {"ok": True, "result": ({"schema_version": 6, "can_check": True,
                "apply_strategy": state["reported"]} if params["channel"] == "preview"
                else {"can_check": False, "status": "unconfigured"})}

        def close(self):
            state["client_closed"] = True

    monkeypatch.setattr(smoke, "ProtocolClient", Client)
    return state, lambda: delivery.native_check(ROOT, release, "b" * 40, False)


def test_native_delivery_inspects_actual_initial_bootstrap_registration(initial_install_check):
    state, check = initial_install_check
    report = check()
    assert report["bootstrap_api"] == state["registration"]["bootstrap_api"] == state["installer_api"]
    assert report["apply_strategy"] == "immediate"
    assert report["managed_install"] and report["preview_trust"] and report["stable_unconfigured"]
    assert state["client_closed"]


def test_native_delivery_rejects_reboot_report_from_current_initial_install(initial_install_check):
    state, check = initial_install_check
    state["reported"] = "after_reboot"
    with pytest.raises(ValueError, match="Wrong update handoff"):
        check()
    assert state["client_closed"]


def test_initial_install_cannot_silently_provision_legacy_bootstrap(initial_install_check):
    state, check = initial_install_check
    state["installer_api"] = 11 if sys.platform == "darwin" else 9
    with pytest.raises(ValueError, match="did not provision the current bootstrap"):
        check()
    assert not state["client_opened"]


def test_installer_success_does_not_replace_bootstrap_integrity_check(initial_install_check):
    from backend.core.update import UpdateError
    state, check = initial_install_check
    state["damage_bootstrap"] = True
    with pytest.raises(UpdateError, match="UPDATE_CONTROL_INVALID"):
        check()
    assert not state["client_opened"]
