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
           "stages_ms": {"prepare_ms": 3, "error": secret, "total_ms": 10 ** 1000}}
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
    home.mkdir(mode=0o700)
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


def test_control_operation_reads_original_runtime_binding(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.agent.tooling.audit_lifecycle_topic import _runtime_evidence

    home = tmp_path / "owner"
    home.mkdir(mode=0o700)
    repo = RuntimeRepository(runtime_db_path(home))
    request = HostCommandRequest("local/main", "actor", "feishu", "thread", "request", "status", "0" * 64)
    binding = repo.register_host_command(request)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home, owner_id="local/main"))
    entry = {"owner_id": "local/main", "operation_id": request.operation_id}
    result = _runtime_evidence(agent, entry)
    assert result["control_operation"]["attempt_id"] == binding.attempt_id
    assert result["attempt"]["status"] == "pending"
    assert "goal" not in result["task"]


def test_tool_calls_unsupported_scopes_offer_valid_explicit_owner_retry():
    from types import SimpleNamespace

    from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger
    from agent_py_agent.agent.tooling.audit_tool_calls_topic import tool_calls_topic

    for scope in ("current_thread", "all_owners"):
        q = AuditQuery("tool_calls", scope, "trusted-thread", 0, 20)
        # 没有home/config也可返回拒绝范围的事实；不构造/读取原账或写索引。
        with patch.object(ToolTelemetryLedger, "__init__", side_effect=AssertionError("source must not start")):
            report = tool_calls_topic(SimpleNamespace(), q)["tool_calls"]
        assert report["coverage"] == {"status": "partial", "reason": "owner_scoped_telemetry_only"}
        assert report["requested_scope"] == scope and report["supported_scopes"] == ["owner"]
        assert report["effect_outcome"] == "not_started" and report["read_only"] is True
        assert report["source_read_started"] is False and report["entries"] == []
        assert report["error_code"] == "AUDIT_TOPIC_SCOPE_UNSUPPORTED"
        assert "本人全部会话" in report["next_action"] and "空 entries 不表示没有" in report["next_action"]
        retry = report["retry_example"]
        assert retry == {"topic": "tool_calls", "scope": "owner", "since_hours": 24, "limit": 20}
        assert isinstance(_parse(retry), tuple)


def test_tool_calls_scope_guidance_preserves_cross_owner_permission_gate():
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.audit_records_tool import AuditRecordsTool

    agent = SimpleNamespace(home_paths=object())
    with (patch("agent_py_agent.agent.tooling.audit_records_tool.current_subagent_run_id", return_value=""),
          patch("agent_py_agent.agent.user_space.owner_admin_controls.read_owner_admin_controls",
                return_value={"audit_allowed": True}),
          patch("agent_py_agent.agent.user_space.owner_admin_controls.cross_owner_audit_allowed", return_value=False)):
        result = AuditRecordsTool(agent).execute({"topic": "tool_calls", "scope": "all_owners"})
    assert not result.ok and result.error_code == "AUDIT_ACCESS_DENIED"
    assert json.loads(result.output)["reason"] == "cross_owner_not_allowed"
    assert result.effect_outcome == "not_started"


def test_audit_schema_explains_topic_scope_differences():
    from agent_py_agent.agent.tooling.audit_records_tool import AuditRecordsTool

    spec = AuditRecordsTool.model_spec
    assert "tool_calls 仅支持scope=owner" in spec.description
    properties = spec.input_schema["properties"]
    assert "tool_calls仅支持owner" in properties["scope"]["description"]
    assert "message_lifecycle" in properties["topic"]["description"]
    assert "仅requests/tool_calls" in properties["cursor"]["description"]
    assert "仅tool_calls且scope=owner" in properties["tool_name"]["description"]


def test_public_audit_cursor_repeated_same_hours_keeps_original_window(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
    from agent_py_agent.agent.observability.telemetry_ledger import PARTS, ToolTelemetryLedger
    from agent_py_agent.agent.tooling.audit_records_tool import AuditRecordsTool

    home = tmp_path / "owner"
    home.mkdir(mode=0o700)
    ledger = ToolTelemetryLedger(home, owner_id="local/main")
    for i in range(4):
        ledger.record_tool_call({"call_id": f"c{i}", "tool_name": "read", "started_at": 999000 + i,
                                 "ended_at": 999001 + i, "status": "ok"})
    ledger._read_index.advance("telemetry", (*PARTS, "tool_calls.jsonl"))
    monkeypatch.setattr(OwnerReadIndex, "advance", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read advanced index")))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home, owner_id="local/main"),
                            config=SimpleNamespace(retention_scan_slice_entries=2))
    with (patch("agent_py_agent.agent.tooling.audit_records_tool.current_subagent_run_id", return_value=""),
          patch("agent_py_agent.agent.user_space.owner_admin_controls.read_owner_admin_controls",
                return_value={"audit_allowed": True})):
        with patch("time.time", return_value=1000000):
            first = AuditRecordsTool(agent).execute({"topic": "tool_calls", "scope": "owner", "since_hours": 24, "limit": 2})
        assert first.ok, first.output
        page1 = json.loads(first.output)
        cursor = page1["tool_calls"]["next_cursor"]
        assert cursor
        with patch("time.time", return_value=1001000):
            second = AuditRecordsTool(agent).execute({"topic": "tool_calls", "scope": "owner", "since_hours": 24,
                                                      "limit": 2, "cursor": cursor})
    assert second.ok, second.output
    page2 = json.loads(second.output)
    assert page1["window"] == page2["window"]
    assert [row["call_id"] for row in page1["tool_calls"]["entries"]] == ["c0", "c1"]
    assert [row["call_id"] for row in page2["tool_calls"]["entries"]] == ["c2", "c3"]


def test_cursor_explicit_changed_window_refuses_before_source():
    import base64

    state = {"token": "opaque-provider-token", "since": 900000, "until": None, "since_hours": 24}
    token = "t:" + base64.urlsafe_b64encode(json.dumps(state).encode()).decode()
    changed = _parse({"topic": "tool_calls", "cursor": token, "since_hours": 12})
    assert not changed.ok and changed.effect_outcome == "not_started"
    assert json.loads(changed.output)["reason"] == "cursor_window_mismatch"
    changed_until = _parse({"topic": "tool_calls", "cursor": token, "until": 999999})
    assert not changed_until.ok and json.loads(changed_until.output)["reason"] == "cursor_window_mismatch"
    omitted = _parse({"topic": "tool_calls", "cursor": token})
    assert omitted[2] == 24 and omitted[4] == 900000


def test_legacy_cursor_missing_duration_requires_omission_and_never_guesses():
    import base64

    from agent_py_agent.agent.tooling.audit_records_tool import _bind_cursor_window

    token = "t:" + base64.urlsafe_b64encode(json.dumps({"token": "old", "since": 900000, "until": None}).encode()).decode()
    explicit = _parse({"topic": "tool_calls", "cursor": token, "since_hours": 24})
    assert not explicit.ok and explicit.effect_outcome == "not_started"
    assert json.loads(explicit.output)["reason"] == "cursor_window_unknown"
    assert "省略 since_hours" in json.loads(explicit.output)["message"]
    omitted = _parse({"topic": "tool_calls", "cursor": token})
    assert isinstance(omitted, tuple) and omitted[4] == 900000
    page = {"tool_calls": {"next_cursor": token}}
    _bind_cursor_window(page, "tool_calls", None)
    assert page["tool_calls"]["next_cursor"] == token
