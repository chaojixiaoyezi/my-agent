from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.action_protocol import RunScope, ToolCallEnvelope
from agent_py_agent.agent.agent_core._finalization_service import _message_tool_deliveries
from agent_py_agent.agent.agent_core.tool_call_archive_record import _compact_result_envelope
from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.capability.channel_message_tool import SendMessageTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryService,
)
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


class _RecordingAdapter:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.files: list[tuple[str, Path, str]] = []
        self.send_ok = True

    def send_message(self, user_id: str, message: object) -> bool:
        self.messages.append((user_id, str(getattr(message, "content", "") or "")))
        return self.send_ok

    def send_file(
        self,
        user_id: str,
        path: Path,
        *,
        idempotency_key: str = "",
    ) -> bool:
        self.files.append((user_id, path, idempotency_key))
        return True


class _ProviderIdempotentAdapter:
    provider_idempotent_delivery = True

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.logical_messages: dict[str, tuple[str, str]] = {}

    def send_message(self, user_id: str, message: object) -> bool:
        metadata = dict(getattr(message, "metadata", {}) or {})
        key = str(metadata.get("delivery_idempotency_key") or "")
        self.attempts.append(key)
        self.logical_messages.setdefault(
            key,
            (user_id, str(getattr(message, "content", "") or "")),
        )
        return True


def _tool(owner_root: Path) -> tuple[SendMessageTool, _RecordingAdapter]:
    channel_registry = ChannelAdapterRegistry()
    agent = SimpleNamespace(
        config=SimpleNamespace(
            feishu_app_id="",
            feishu_app_secret="",
            my_agent_owner_id="ou_current_user",
        ),
        home_paths=SimpleNamespace(
            owner_provider="feishu",
            owner_id="providers/feishu/users/ou_current_user",
            owner_home_dir=owner_root,
        ),
        delivery_service=DeliveryService(channel_registry),
    )
    tool = SendMessageTool(agent)
    adapter = _RecordingAdapter()
    channel_registry.register_adapter(
        "feishu",
        adapter,
        capabilities=ChannelCapabilities(text=True, reply=True, proactive=True, files=True),
    )
    return tool, adapter


def test_send_message_uses_task_registry_and_native_attachment_api(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "2026-07-13" / "weekly-report"
    artifact = task_root / "output" / "weekly.xlsx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"verified workbook")
    register_artifact(
        ArtifactRegistration(
            workspace_root=task_root,
            path=artifact,
            artifact_id="weekly_report",
            status="ready",
            source="test",
        )
    )
    tool, adapter = _tool(owner_root)
    store = LocalStore(owner_root / "data" / "local.db", enable_fts=False)
    registry = _message_registry(tmp_path, store, tool)
    params = ToolCallEnvelope(
        call_id="call-1",
        source="model_tool_call",
        tool_name="send_message",
        input={
            "message": "给你周报。",
            "attachments": [str(artifact)],
        },
        scope=RunScope(
            request_id="gw-1",
            task_id="run-1",
            run_id="run-1",
            owner_type="user",
            owner_id="providers/feishu/users/ou_current_user",
        ),
    )

    first = registry.execute_call(params)
    second = registry.execute_call(params)

    assert first.ok is True
    assert second.ok is True
    assert adapter.messages == [("ou_current_user", "给你周报。")]
    assert len(adapter.files) == 1
    assert adapter.files[0][:2] == ("ou_current_user", artifact)
    assert adapter.files[0][2]
    payload = json.loads(first.output)
    assert payload["delivery_status"] == "sent"
    assert payload["attachments"][0]["artifact_id"] == "weekly_report"
    assert str(owner_root) not in first.output
    assert "ou_current_user" not in first.output
    assert second.output == first.output
    evidence = first.result_envelope["delivery_evidence"]
    assert evidence == {
        "schema_version": "message_tool_delivery.v1",
        "delivery_status": "sent",
        "source_owner_delivery": True,
        "channel": "feishu",
        "content": "给你周报。",
        "receipt_id": payload["receipt_id"],
        "deduplicated": False,
        "evidence_refs": [],
        "attachments": [
            {
                "artifact_id": "weekly_report",
                "path": str(artifact),
                "name": "weekly.xlsx",
                "kind": "xlsx",
                "sha256": payload["attachments"][0]["sha256"],
                "size_bytes": len(b"verified workbook"),
                "ok": True,
            }
        ],
    }
    compact = _compact_result_envelope(first)
    assert compact["delivery_evidence"] == evidence
    ctx = SimpleNamespace(
        archive_tool_calls=[
            {
                "tool": "send_message",
                "ok": True,
                "tool_result_envelope": compact,
            }
        ]
    )
    assert _message_tool_deliveries(ctx) == [evidence]
    assert second.result_envelope["delivery_evidence"]["deduplicated"] is True
    assert second.result_envelope["tool_operation"]["replayed"] is True


def test_successful_send_records_exact_audit_source_refs(tmp_path: Path) -> None:
    from agent_py_agent.agent.ingestion import harvester as hv
    from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state

    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    state = new_state(
        owner_root,
        "http://source.example/pull",
        {"full_read_per_pull": 1},
    )
    state.audit_guarantee = True
    state.source_envelope = {
        "mode": "cursor",
        "record_boundary": "array_item",
        "record_list_key": "items",
        "cursor_field": "next_cursor",
        "cursor_semantics": "next_position",
        "request": {
            "method": "GET",
            "cursor_binding": {"location": "query", "name": "since", "initial": 0},
            "page_size_binding": {"location": "query", "name": "limit"},
        },
        "valid": True,
    }
    persist_state(state)

    def fetch(request):
        from urllib.parse import parse_qs, urlsplit

        url = request.url
        since = int(parse_qs(urlsplit(url).query).get("since", ["0"])[0])
        items = [{"seq": 0, "value": "evidence"}] if since <= 0 else []
        return True, {"items": items, "next_cursor": 1}, ""

    assert hv._harvest_cycle(state, fetch)
    records, delivery = hv.read_spool_records(
        state,
        max_candidates=1,
        consumer="judge",
    )
    candidate = records[0]["candidates"][0]
    ack_id = candidate["ack_id"]
    source_ref = candidate["source_ref"]
    assert hv.submit_verdicts(
        state,
        consumer="judge",
        delivery_ref=delivery["delivery_ref"],
        verdicts=[
            {
                "ack_id": ack_id,
                "verdict": "hit",
                "score": 90,
                "note": "测试判断理由",
                "finding": {
                    "claim": "测试审计发现",
                    "requires_llm_report": True,
                    "evidence_refs": [source_ref],
                },
            }
        ],
    )["acked_now"] == 1
    tool, adapter = _tool(owner_root)

    result = tool.execute(
        {
            "message": "这是已送达的审计发现。",
            "evidence_refs": [source_ref],
            "__run_scope": {
                "request_id": "request-audit-report",
                "task_id": "task-audit-report",
            },
        }
    )

    assert result.ok is True
    assert adapter.messages == [
        ("ou_current_user", "这是已送达的审计发现。")
    ]
    payload = json.loads(result.output)
    assert payload["reported_source_refs"] == [source_ref]
    inspected = hv.inspect_audit_record(state, ack_id)
    assert inspected["processing_status"]["reported"] is True
    assert inspected["processing_status"]["delivery_receipt_ids"] == [
        payload["receipt_id"]
    ]


def test_background_evidence_scope_blocks_wrong_report_before_channel_side_effect(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    tool, adapter = _tool(owner_root)
    required = [
        "audit://watch-1/candidate/1:0",
        "audit://watch-1/candidate/2:0",
    ]
    scope = {
        "request_id": "request-audit-report",
        "task_id": "task-audit-report",
        "delivery_evidence_refs": required,
    }

    for provided in (
        [],
        [required[0]],
        [required[0], "audit://watch-2/candidate/9:0"],
        [*required, "audit://watch-2/candidate/9:0"],
    ):
        rejected = tool.execute(
            {
                "message": "不能发送证据不完整或夹带其他事件的报告。",
                "evidence_refs": provided,
                "__run_scope": scope,
            }
        )
        assert rejected.ok is False
        assert rejected.error_code == "TOOL_INVALID_ARGUMENTS"

    assert adapter.messages == []

    accepted = tool.execute(
        {
            "message": "这条报告只引用当前事件的完整证据。",
            "evidence_refs": list(reversed(required)),
            "__run_scope": scope,
        }
    )

    assert accepted.ok is True
    assert adapter.messages == [
        ("ou_current_user", "这条报告只引用当前事件的完整证据。")
    ]


def test_background_send_canonicalizes_scoped_evidence_misplaced_as_attachments(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    tool, adapter = _tool(owner_root)
    store = LocalStore(owner_root / "data" / "local.db", enable_fts=False)
    registry = _message_registry(tmp_path, store, tool)
    required = (
        "audit://watch-1/candidate/1:0",
        "audit://watch-1/candidate/2:0",
    )
    envelope = ToolCallEnvelope(
        call_id="call-audit-report",
        source="model_tool_call",
        tool_name="send_message",
        input={
            "message": "这是当前事件的审计报告。",
            "attachments": list(required),
        },
        scope=RunScope(
            request_id="request-audit-report",
            task_id="task-audit-report",
            run_id="task-audit-report",
            owner_type="user",
            owner_id="providers/feishu/users/ou_current_user",
            delivery_evidence_refs=required,
        ),
    )

    result = registry.execute_call(envelope)
    corrected = registry.execute_call(
        ToolCallEnvelope(
            call_id="call-audit-report-retry",
            source="model_tool_call",
            tool_name="send_message",
            input={
                "message": "这是当前事件的审计报告。",
                "evidence_refs": list(required),
            },
            scope=envelope.scope,
        )
    )

    assert result.ok is True
    assert corrected.ok is False
    assert corrected.error_code == "TOOL_OPERATION_IDENTITY_CONFLICT"
    assert adapter.messages == [
        ("ou_current_user", "这是当前事件的审计报告。")
    ]
    assert adapter.files == []
    payload = json.loads(result.output)
    assert payload["attachments"] == []
    assert payload["evidence_refs"] == list(required)
    assert result.result_envelope["delivery_evidence"]["evidence_refs"] == list(
        required
    )
    assert corrected.handler_executed is False


def test_send_message_business_key_blocks_new_call_id_in_same_request(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    tool, adapter = _tool(owner_root)
    store = LocalStore(owner_root / "data" / "local.db", enable_fts=False)
    registry = _message_registry(tmp_path, store, tool)

    def envelope(request_id: str, run_id: str, call_id: str) -> ToolCallEnvelope:
        return ToolCallEnvelope(
            call_id=call_id,
            source="model_tool_call",
            tool_name="send_message",
            input={"message": "同一条真实发送"},
            scope=RunScope(
                request_id=request_id,
                task_id=run_id,
                run_id=run_id,
                owner_type="user",
                owner_id="providers/feishu/users/ou_current_user",
            ),
        )

    first = registry.execute_call(envelope("gw-1", "run-1", "call-1"))
    same_request_new_call = registry.execute_call(
        envelope("gw-1", "run-1", "call-2")
    )
    new_request = registry.execute_call(envelope("gw-2", "run-2", "call-3"))

    assert first.ok and same_request_new_call.ok and new_request.ok
    assert adapter.messages == [
        ("ou_current_user", "同一条真实发送"),
        ("ou_current_user", "同一条真实发送"),
    ]
    assert same_request_new_call.result_envelope["tool_operation"]["replayed"] is True
    assert new_request.result_envelope["tool_operation"]["replayed"] is False


def test_ambiguous_send_failure_is_not_executed_again(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    tool, adapter = _tool(owner_root)
    adapter.send_ok = False
    store = LocalStore(owner_root / "data" / "local.db", enable_fts=False)
    registry = _message_registry(tmp_path, store, tool)

    def envelope(call_id: str) -> ToolCallEnvelope:
        return ToolCallEnvelope(
            call_id=call_id,
            source="model_tool_call",
            tool_name="send_message",
            input={"message": "只应尝试一次"},
            scope=RunScope(
                request_id="gw-ambiguous",
                task_id="run-ambiguous",
                run_id="run-ambiguous",
                owner_type="user",
                owner_id="providers/feishu/users/ou_current_user",
            ),
        )

    first = registry.execute_call(envelope("call-1"))
    second = registry.execute_call(envelope("call-2"))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert adapter.messages == [("ou_current_user", "只应尝试一次")]
    record = store.list_tool_operations(owner_id="providers/feishu/users/ou_current_user")
    assert len(record) == 1 and record[0].status == "unknown"


def test_provider_send_then_operation_store_crash_recovers_without_duplicate_message(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    channel_registry = ChannelAdapterRegistry()
    adapter = _ProviderIdempotentAdapter()
    channel_registry.register_adapter(
        "feishu",
        adapter,
        capabilities=ChannelCapabilities(
            text=True,
            reply=True,
            proactive=True,
            provider_idempotency=True,
        ),
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(
            feishu_app_id="",
            feishu_app_secret="",
            my_agent_owner_id="ou_current_user",
        ),
        home_paths=SimpleNamespace(
            owner_provider="feishu",
            owner_id="providers/feishu/users/ou_current_user",
            owner_home_dir=owner_root,
        ),
        delivery_service=DeliveryService(channel_registry),
    )
    tool = SendMessageTool(agent)
    real_store = LocalStore(owner_root / "data" / "local.db", enable_fts=False)

    class _FinishOnceUnavailableStore:
        def __init__(self) -> None:
            self.failures_left = 1

        def claim_tool_operation(self, request):
            return real_store.claim_tool_operation(request)

        def finish_tool_operation(self, request):
            if self.failures_left:
                self.failures_left -= 1
                raise OSError("simulated crash before terminal operation write")
            return real_store.finish_tool_operation(request)

        def __getattr__(self, name: str):
            return getattr(real_store, name)

    registry = _message_registry(
        tmp_path,
        _FinishOnceUnavailableStore(),
        tool,
    )

    def envelope(run_id: str, call_id: str) -> ToolCallEnvelope:
        return ToolCallEnvelope(
            call_id=call_id,
            source="model_tool_call",
            tool_name="send_message",
            input={"message": "同一条崩溃恢复通知"},
            scope=RunScope(
                request_id="gw-provider-crash",
                task_id=run_id,
                run_id=run_id,
                owner_type="user",
                owner_id="providers/feishu/users/ou_current_user",
            ),
        )

    first = registry.execute_call(envelope("run-before-crash", "call-1"))
    with patch(
        "agent_py_agent.agent.local_storage.tool_operations._operation_holder_is_live",
        return_value=False,
    ):
        recovered = registry.execute_call(envelope("run-after-crash", "call-2"))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert recovered.ok is True
    assert len(adapter.attempts) == 2
    assert adapter.attempts[0] == adapter.attempts[1]
    assert list(adapter.logical_messages.values()) == [
        ("ou_current_user", "同一条崩溃恢复通知")
    ]
    records = real_store.list_tool_operations(
        owner_id="providers/feishu/users/ou_current_user"
    )
    assert len(records) == 1
    assert records[0].status == "succeeded"
    assert records[0].generation == 2


def _message_registry(
    root: Path,
    store: object,
    tool: SendMessageTool,
) -> ToolRegistry:
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            operation_store=store,
            operation_store_required=True,
            operation_owner_id="providers/feishu/users/ou_current_user",
        )
    )
    registry.register(tool)
    return registry


def test_send_message_rejects_unregistered_or_cross_owner_file(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    outside = tmp_path / "other-owner" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("secret", encoding="utf-8")
    tool, adapter = _tool(owner_root)

    result = tool.execute({"attachments": [str(outside)]})

    assert result.ok is False
    assert result.error_code == "ARTIFACT_NOT_REGISTERED"
    assert adapter.messages == []
    assert adapter.files == []


def test_send_message_rejects_file_changed_after_registration(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    task_root = owner_root / "tasks" / "2026-07-13" / "report"
    artifact = task_root / "output" / "report.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"first")
    register_artifact(
        ArtifactRegistration(
            workspace_root=task_root,
            path=artifact,
            artifact_id="report",
            status="ready",
        )
    )
    artifact.write_bytes(b"changed")
    tool, adapter = _tool(owner_root)

    result = tool.execute({"attachments": [str(artifact)]})

    assert result.ok is False
    assert result.error_code == "ARTIFACT_VALIDATION_FAILED"
    assert adapter.files == []


def test_send_message_is_registered_and_retrieved_for_plain_user_language(tmp_path: Path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )

    names = {spec.name for spec in agent.tools.specs(include_orchestration=True)}
    relevant = {spec.name for spec in agent.tools.find_relevant_specs("把刚才生成的文件发我")}

    assert "send_message" in names
    assert "send_message" in relevant
    assert agent.tools.tools["send_message"]._delivery is agent.delivery_service
    assert (
        agent.tools.tools["list_capabilities"].sources.channel_registry
        is agent.channel_registry
    )
