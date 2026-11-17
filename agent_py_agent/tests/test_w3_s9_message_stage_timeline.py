"""S9: one read-only stage timeline joins ingress, request, rounds, runtime.db and delivery for a request."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace

from agent_py_agent.agent.adapter.ingress import build_gateway_ingress_record, gateway_ingress_id
from agent_py_agent.agent.observability import message_stage_timeline as timeline_module
from agent_py_agent.agent.observability.message_stage_timeline import message_stage_timeline
from agent_py_agent.agent.tooling.gateway_status import _metadata_projection
from agent_py_agent.cli import gateway_timeline

_REQUEST = "gwreq-msg-0123456789abcdef"
_OWNER = "providers/feishu/users/ou_test"
_T0 = 1_800_000_000.0


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _seed(tmp_path, *, provider_at=_T0 - 0.4, with_ingress=True, owner=_OWNER, timing_extra=None):
    gateway = tmp_path / "gateway"
    owners = tmp_path / "owners"
    timing = {"accepted_at": _T0, "dispatched_at": _T0 + 0.1, "owner_agent_ready_at": _T0 + 0.11,
              "lock_wait_started_at": _T0 + 0.12, "lock_acquired_at": _T0 + 0.125, "queue_wait_ms": 100.0,
              "agent_init_ms": 10.0, "lock_wait_ms": 5.0, **(timing_extra or {})}
    request = {"id": _REQUEST, "request_id": _REQUEST, "created_at": _T0, "completed_at": _T0 + 5.0,
               "owner_id": owner, "host_im_dispatch.v1": {"owner_id": owner}, "request_stage_timing": timing,
               "metadata": {"channel": "feishu", "adapter": "feishu", "message_id": "om_test"}}
    _write(gateway / "requests" / "done" / f"{_REQUEST}.json", request)
    if with_ingress:
        _write(gateway / "channel-delivery" / "ingress" / "records" / f"{gateway_ingress_id('feishu', 'om_test')}.json",
               {"provider_message_id": "om_test", "timestamp": provider_at, "created_at": _T0 - 0.02, "content": "x"})
    _write(gateway / "channel-delivery" / "sent" / f"{hashlib.sha256(_REQUEST.encode()).hexdigest()}.json",
           {"request_id": _REQUEST, "dispatch_started_at": _T0 + 5.2, "sent_at": _T0 + 5.9})
    home = owners.joinpath(*owner.split("/")) if ".." not in owner else owners / "unused"
    rounds = home / "data" / "telemetry" / "task_calls" / f"{_REQUEST}.rounds.jsonl"
    rounds.parent.mkdir(parents=True, exist_ok=True)
    frames = [{"schema": "task_rounds.v1", "generation": "0" * 64, "sequence": 0, "row": None},
              {"schema": "task_rounds.v1", "generation": "0" * 64, "sequence": 1,
               "row": {"started_at": _T0 + 0.2, "model": "m1", "context_assembly_ms": 20.0, "first_token_wait_ms": 1000.0,
                       "duration_ms": 2000.0, "stages_ms": {"compose": 3.5, "bad": "x"}}},
              {"schema": "task_rounds.v1", "generation": "0" * 64, "sequence": 2,
               "row": {"started_at": _T0 + 2.5, "model": "m1", "context_assembly_ms": 30.0, "first_token_wait_ms": 500.0,
                       "duration_ms": 1500.0, "inter_round_gap_ms": 300.0}}]
    rounds.write_text("\n".join(json.dumps(frame) for frame in frames) + "\n{broken\n", encoding="utf-8")
    with sqlite3.connect(home / "runtime.db") as conn:
        conn.execute("CREATE TABLE task_runs (task_run_id TEXT PRIMARY KEY, task_id TEXT, status TEXT, created_at REAL, closed_at REAL)")
        conn.execute("CREATE TABLE agent_runs (agent_run_id TEXT PRIMARY KEY, task_run_id TEXT)")
        conn.execute("CREATE TABLE tool_operations (operation_id TEXT PRIMARY KEY, agent_run_id TEXT, operation_type TEXT, "
                     "status TEXT, created_at REAL, handler_started_at REAL, settled_at REAL)")
        conn.execute("INSERT INTO task_runs VALUES ('tr1', ?, 'COMPLETED', ?, ?)", (_REQUEST, _T0 + 0.13, _T0 + 4.1))
        conn.execute("INSERT INTO agent_runs VALUES ('ar1', 'tr1')")
        conn.execute("INSERT INTO tool_operations VALUES ('op1', 'ar1', 'run_command', 'SUCCEEDED', ?, ?, ?)",
                     (_T0 + 2.25, _T0 + 2.26, _T0 + 2.3))
    return gateway, owners, home


def test_ingress_id_helper_is_the_record_builder_formula():
    record = build_gateway_ingress_record(channel=" feishu ", user_id="u", conversation_id="c", provider_message_id=" om_1 ",
                                          content="hi", metadata={}, timestamp=_T0, now=_T0)
    assert record.ingress_id == gateway_ingress_id("feishu", "om_1")
    assert record.ingress_id == hashlib.sha256(b'["feishu","om_1"]').hexdigest()


def test_timeline_joins_every_source_in_pipeline_order(tmp_path):
    gateway, owners, _home = _seed(tmp_path)
    result = message_stage_timeline(gateway_root=gateway, owners_dir=owners, request_id=_REQUEST)
    assert result["ok"] and result["request_state"] == "done" and result["coverage"] == {} and result["warnings"] == []
    stages = [stage["stage"] for stage in result["stages"]]
    assert stages == ["provider_created", "adapter_received", "gateway_accepted", "dispatched", "owner_agent_ready",
                      "lane_wait_started", "lane_acquired", "turn_started", "model_call_started", "first_token",
                      "model_call_finished", "tool_claimed", "tool_settled", "model_call_started", "first_token",
                      "model_call_finished", "turn_finished", "delivery_started", "delivery_sent"]
    summary = result["summary"]
    assert summary["provider_to_adapter_ms"] == 380.0 and summary["adapter_to_gateway_ms"] == 20.0
    assert summary["model_rounds"] == 2 and summary["model_total_ms"] == 3500.0 and summary["context_assembly_total_ms"] == 50.0
    assert summary["managed_tool_count"] == 1 and summary["managed_tool_total_ms"] == 50.0
    assert summary["turn_ms"] == 3970.0 and summary["finish_to_delivery_ms"] == 1800.0
    assert summary["end_to_end_ms"] == 6300.0 and summary["end_to_end_origin"] == "provider_created"
    first_round = result["stages"][8]
    assert first_round["stages_ms"] == {"compose": 3.5} and first_round["model"] == "m1"
    assert result["origin_stage"] == "adapter_received" and result["stages"][1]["offset_ms"] == 0.0


def test_provider_clock_ahead_is_reported_not_corrected(tmp_path):
    gateway, owners, _home = _seed(tmp_path, provider_at=_T0 + 27.5)
    result = message_stage_timeline(gateway_root=gateway, owners_dir=owners, request_id=_REQUEST)
    assert result["warnings"] == ["provider_clock_ahead_of_host"]
    assert result["summary"]["provider_to_adapter_ms"] == -27520.0
    assert result["summary"]["end_to_end_origin"] == "adapter_received" and result["summary"]["end_to_end_ms"] == 5920.0
    assert result["stages"][0]["stage"] == "provider_created"


def test_pruned_ingress_record_falls_back_to_times_carried_in_the_request(tmp_path):
    gateway, owners, _home = _seed(tmp_path, with_ingress=False,
                                   timing_extra={"provider_created_at": _T0 - 0.5, "adapter_received_at": _T0 - 0.05})
    result = message_stage_timeline(gateway_root=gateway, owners_dir=owners, request_id=_REQUEST)
    assert "ingress" not in result["coverage"] and result["summary"]["provider_to_adapter_ms"] == 450.0


def test_invalid_ids_and_unsafe_owner_paths_are_refused(tmp_path):
    gateway, owners, _home = _seed(tmp_path, owner="providers/../../outside")
    assert message_stage_timeline(gateway_root=gateway, owners_dir=owners, request_id="../x")["error_code"] == "REQUEST_ID_INVALID"
    assert message_stage_timeline(gateway_root=gateway, owners_dir=owners, request_id="gwreq-none")["error_code"] == "REQUEST_NOT_FOUND"
    result = message_stage_timeline(gateway_root=gateway, owners_dir=owners, request_id=_REQUEST)
    assert result["ok"] and result["coverage"]["owner"] == "unresolved"
    assert "turn_started" not in [stage["stage"] for stage in result["stages"]]


def test_missing_sources_are_listed_in_coverage_without_zero_fill(tmp_path):
    gateway, owners, home = _seed(tmp_path)
    (home / "runtime.db").unlink()
    (home / "data" / "telemetry" / "task_calls" / f"{_REQUEST}.rounds.jsonl").unlink()
    result = message_stage_timeline(gateway_root=gateway, owners_dir=owners, request_id=_REQUEST)
    assert result["coverage"] == {"rounds": "missing", "runtime_db": "runtime_db_missing"}
    assert "turn_ms" not in result["summary"] and "accepted_to_turn_start_ms" not in result["summary"]


def test_dispatch_stage_copies_ingress_times_into_the_request_record(tmp_path):
    from agent_py_agent.agent.gateway_parts.request_binding import record_gateway_request_stage

    gateway, _owners, _home = _seed(tmp_path)
    processing = gateway / "requests" / "processing" / f"{_REQUEST}.json"
    request = json.loads((gateway / "requests" / "done" / f"{_REQUEST}.json").read_text())
    request.pop("request_stage_timing")
    request.update(execution_attempt_id="gateway-attempt-1", lease_epoch=1)
    _write(processing, request)
    timing = record_gateway_request_stage(processing, dict(request), "dispatched_at", at=_T0 + 0.1)
    assert timing["provider_created_at"] == _T0 - 0.4 and timing["adapter_received_at"] == _T0 - 0.02
    assert round(timing["provider_to_adapter_ms"], 1) == 380.0 and round(timing["adapter_to_accept_ms"], 1) == 20.0
    stored = json.loads(processing.read_text())["request_stage_timing"]
    assert stored["provider_created_at"] == _T0 - 0.4 and round(stored["queue_wait_ms"], 1) == 100.0
    later = record_gateway_request_stage(processing, dict(request), "owner_agent_ready_at", at=_T0 + 0.2)
    assert later["provider_created_at"] == _T0 - 0.4 and "owner_agent_ready_at" in later


def test_cli_renders_text_and_json_and_picks_the_latest_request(tmp_path, monkeypatch, capsys):
    gateway, owners, _home = _seed(tmp_path)
    monkeypatch.setattr("agent_py_agent.cli.gateway_coordinated_run._cold_gateway_paths", lambda args: SimpleNamespace(root=gateway))
    monkeypatch.setattr("agent_py_agent.agent.user_space.home_layout.home_paths", lambda: SimpleNamespace(owners_dir=owners))
    assert gateway_timeline.cmd_gateway_timeline(SimpleNamespace(request_id="", json=False)) == 0
    text = capsys.readouterr().out
    assert text.startswith(f"timeline request={_REQUEST} state=done") and "delivery_sent" in text and "end_to_end_ms=6300.0" in text
    assert gateway_timeline.cmd_gateway_timeline(SimpleNamespace(request_id=_REQUEST, json=True)) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["model_rounds"] == 2
    assert gateway_timeline.cmd_gateway_timeline(SimpleNamespace(request_id="gwreq-none", json=False)) == 1
    assert "error=REQUEST_NOT_FOUND" in capsys.readouterr().out


def test_status_projection_keeps_only_numeric_timeline_fields(tmp_path):
    gateway, owners, home = _seed(tmp_path)
    full = message_stage_timeline(gateway_root=gateway, owner_home=home, request_id=_REQUEST)
    projected = _metadata_projection({"request_timeline": {**{key: full[key] for key in ("request_id", "ok", "request_state",
                                                                                          "summary", "coverage", "warnings")},
                                                           "stages": full["stages"]}})["request_timeline"]
    assert "stages" not in projected and projected["summary"]["end_to_end_origin"] == "provider_created"
    assert all(isinstance(value, (int, float)) for key, value in projected["summary"].items() if key != "end_to_end_origin")
    assert timeline_module.find_request_record(gateway, "../x") == (None, "")
