"""生命周期只公开有授权的结构事实，缺证不冒充未投递。"""
import json
from unittest.mock import patch

from agent_py_agent.agent.gateway_parts.request_audit_records import message_lifecycle_records
from agent_py_agent.agent.tooling.audit_lifecycle_topic import metadata_only
from agent_py_agent.agent.tooling.audit_records_tool import AuditQuery, _parse
from agent_py_agent.tests.test_admin_audit_cursor import query, seed


def test_recursive_metadata_drops_source_bodies():
    secret = "UNIQUE-CONTENT-SENTINEL"
    raw = {"task_id": "t1", "title": secret, "goal": secret, "args_summary": secret, "raw_error": secret,
           "reply_receipt_summaries": [{"state": "observed", "recipient": secret, "body": secret,
                                        "components": [{"body": secret}], "duration_ms": 12}],
           "stages_ms": {"prepare_ms": 3, "error": secret}}
    out = metadata_only(raw)
    assert secret not in json.dumps(out)
    assert out["reply_receipt_summaries"] == [{"state": "observed", "duration_ms": 12}]


def test_exact_request_lifecycle_is_owner_scoped_and_missing_input_unknown(tmp_path):
    paths = seed(tmp_path, 1)
    result = message_lifecycle_records(paths, query(request_id="r00000"))
    assert result["entries"][0]["request_id"] == "r00000"
    assert result["entries"][0]["coverage"]["input"]["status"] == "missing"
    assert "BODY-SENTINEL" not in json.dumps(result)
    from dataclasses import replace
    denied = message_lifecycle_records(paths, replace(query(request_id="r00000"), allowed_owners=frozenset({"other"})))
    assert denied["entries"] == []


def test_query_compatibility_and_topic_validation_before_io():
    old = AuditQuery("requests", "owner", "", 0, 20)
    assert old.request_id == "" and old.cursor == ""
    assert not _parse({"topic": "decision", "request_id": "x"}).ok
    assert not _parse({"topic": "message_lifecycle", "request_id": "x", "message_ref": "y"}).ok
    parsed = _parse({"topic": "requests", "request_id": "old"})
    assert parsed[4] == 0
    with patch("time.time", return_value=10):
        assert not _parse({"topic": "tool_calls", "min_duration_ms": float("nan")}).ok


def test_consumed_input_joins_original_target_and_drops_bodies(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.adapter.ingress import GatewayIngressRecord, GatewayIngressStore
    from agent_py_agent.agent.adapter.protocol import provider_message_reference

    paths = seed(tmp_path, 1)
    root = paths.root / "channel-delivery"
    store = GatewayIngressStore(root)
    store.put_if_absent(GatewayIngressRecord(ingress_id="c" * 64, channel="feishu", user_id="SENTINEL", conversation_id="SENTINEL",
        provider_message_id="message", canonical_payload_digest="digest", content="SENTINEL", metadata={"owner_id": "local/main"},
        submission={"request_id": "input", "target_turn_id": "r00000", "operation_id": "operation"}, created_at=1, updated_at=1))
    (paths.root / "input_receipts").mkdir()
    (paths.root / "input_receipts" / "input.json").write_text(json.dumps({"request_id": "input", "state": "consumed",
        "target_turn_id": "r00000", "prepared_request": {"owner_id": "local/main", "prompt": "SENTINEL"}}))
    page = message_lifecycle_records(paths, replace(query(), message_ref=provider_message_reference("message")))
    assert page["entries"][0]["input"]["state"] == "consumed"
    assert page["entries"][0]["target_request"]["request_id"] == "r00000"
    assert "SENTINEL" not in json.dumps(page)


def test_corrupt_input_preserves_observed_request(tmp_path):
    paths = seed(tmp_path, 1)
    (paths.root / "input_receipts").mkdir()
    (paths.root / "input_receipts" / "r00000.json").write_text("{bad json")
    result = message_lifecycle_records(paths, query(request_id="r00000"))
    assert result["entries"][0]["request"]["request_id"] == "r00000"
    assert result["entries"][0]["coverage"]["input"]["status"] == "unavailable"


def test_tool_calls_use_real_retained_reader_and_do_not_expose_summaries(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
    from agent_py_agent.agent.observability.telemetry_ledger import PARTS, ToolTelemetryLedger
    from agent_py_agent.agent.tooling.audit_tool_calls_topic import tool_calls_topic

    home = tmp_path / "owner"
    ledger = ToolTelemetryLedger(home, owner_id="local/main")
    for i in range(4):
        ledger.record_tool_call({"call_id": f"c{i}", "tool_name": "read", "started_at": i, "ended_at": i + 1,
            "status": "error", "error_code": "TOOL_TIMEOUT", "args_summary": {"value": "SENTINEL"}, "artifact_ref": "SENTINEL"})
    ledger._read_index.advance("telemetry", (*PARTS, "tool_calls.jsonl"))
    monkeypatch.setattr(OwnerReadIndex, "advance", lambda *a, **k: (_ for _ in ()).throw(AssertionError("audit backfill")))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home, owner_id="local/main"),
                            config=SimpleNamespace(retention_scan_slice_entries=2))
    from dataclasses import replace
    q = AuditQuery("tool_calls", "owner", "", 0, 2, error_code="TOOL_TIMEOUT")
    page = tool_calls_topic(agent, q)["tool_calls"]
    assert "SENTINEL" not in json.dumps(page)
    assert all(row["error_code"] == "TOOL_TIMEOUT" for row in page["entries"])
    assert page["scanned"] <= 2
    token = page["next_cursor"]
    assert token
    parsed = _parse({"topic": "tool_calls", "error_code": "TOOL_TIMEOUT", "limit": 2, "cursor": token})
    assert parsed[4] == 0
    next_page = tool_calls_topic(agent, replace(q, cursor=token))["tool_calls"]
    assert {row["call_id"] for row in page["entries"]}.isdisjoint(row["call_id"] for row in next_page["entries"])
