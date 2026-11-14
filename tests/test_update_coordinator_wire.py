"""Replaceable engines keep the immutable API 10/11 bootstrap wire contract."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from backend.core.update import coordinator as engine
from backend.core.update.transaction import TransactionResult
from backend.core.update.validation import UpdateError


@pytest.fixture
def invoke_wire(tmp_path_factory, monkeypatch):
    """Exercise the actual reader with simulated process I/O, not a real engine."""
    from backend.core import process_control, process_output
    from backend.core.update import health
    root = tmp_path_factory / "managed"
    (root / "control").mkdir(parents=True)
    store = SimpleNamespace(root=root)
    proc = SimpleNamespace(returncode=0, poll=lambda: 0)
    wire = {"value": ""}
    monkeypatch.setattr(engine, "_environment", lambda _: {})
    monkeypatch.setattr(process_control, "spawn_managed", lambda *a, **kw: proc)
    monkeypatch.setattr(process_control, "attach_kill_job", lambda _: None)
    monkeypatch.setattr(process_control, "close_job", lambda _: None)
    monkeypatch.setattr(process_output, "collect_output", lambda *a, **kw: (wire["value"], "", False))
    monkeypatch.setattr(health, "cleanup_report", lambda *a: {"complete": True})
    return store, wire, lambda: engine._invoke(store, root / "unused-host", [])


@pytest.mark.parametrize("mode,status", [
    ("propose", "idle"), ("propose", "exit_required"),
    ("apply", "committed"), ("apply", "rolled_back"), ("recover", "rolled_back"),
])
def test_engine_main_round_trips_through_immutable_bootstrap_reader(invoke_wire, monkeypatch, capsys, mode, status):
    from backend.core.update import automatic, handoff, installation
    store, wire, invoke = invoke_wire
    name = "gitgo-host.exe" if os.name == "nt" else "gitgo-host"
    cache = store.root / "control/engines/retained"
    program = (store.root / "active" if mode == "propose" else cache) / "internal/gitgo-host" / name
    monkeypatch.setattr(engine, "sys", SimpleNamespace(frozen=True, executable=str(program), platform=sys.platform))
    monkeypatch.setattr(installation, "open_managed", lambda _: store)
    monkeypatch.setattr(engine, "_read", lambda _: ({"deferred": False, "nonce": "a" * 32,
        "exit_code": 75, "cleanup_complete": True}, cache))
    answer = TransactionResult(status, version="1.0.0-alpha.4", error_code="UPDATE_HEALTH_FAILED",
        health_failure={"reason": "private bounded startup diagnostic"})
    store.recover = lambda: answer
    monkeypatch.setattr(handoff, "_consume_request", lambda *a: answer)
    monkeypatch.setattr(automatic, "queue_on_clean_exit", lambda *a: {"queued": True} if status == "exit_required" else None)
    arguments = ["--root", str(store.root)]
    if mode == "propose": arguments += ["--propose", "--nonce", "a" * 32]
    if mode == "recover": arguments += ["--recover"]

    assert engine.main(arguments) == 0
    wire["value"] = capsys.readouterr().out
    result = json.loads(wire["value"])
    assert set(result) == {"status", "version", "error_code"}
    assert result["status"] == status
    assert "private bounded" not in wire["value"]
    received = invoke()
    assert received == TransactionResult(**result)
    assert received.health_failure is None


@pytest.mark.parametrize("field", ["health_failure", "unexpected"])
def test_bootstrap_reader_still_rejects_unnegotiated_result_fields(invoke_wire, field):
    _, wire, invoke = invoke_wire
    wire["value"] = json.dumps({"status": "committed", "version": "1.0.0-alpha.4",
        "error_code": None, field: None})
    with pytest.raises(UpdateError, match="UPDATE_SCHEMA_INVALID"):
        invoke()
