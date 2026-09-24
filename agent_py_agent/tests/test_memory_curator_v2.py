from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._finalization_service import _request_memory_curator
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.provider_headers import provider_session_scope, request_headers
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.gateway_parts.http_handlers import handle_ask
from agent_py_agent.agent.memory_archive.control_plane import (
    MemoryControlPlaneQueryOptions,
    query_memory_control_plane,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_backend import curator_prompt
from agent_py_agent.agent.memory_store.curator_formal import CuratorFormalMemoryInput
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorInputBatch,
    CuratorMessageInput,
    CuratorToolReferenceSource,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    MemoryCuratorConfig,
    curator_response_schema,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore


def _tool_reference_query(owner_root: Path, run_id: str, limit: int) -> dict[str, object]:
    return query_memory_control_plane(
        owner_root,
        MemoryControlPlaneQueryOptions(run_id=run_id, limit=limit),
    )


class _StaticStructuredBackend:
    name = "fake-structured"

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0
        self.schemas: list[dict[str, object]] = []
        self.prompts: list[str] = []

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        self.calls += 1
        self.schemas.append(response_schema)
        self.prompts.append(prompt)
        assert "待提炼经历 JSON" in prompt
        assert "tools" not in response_schema
        return ModelResponse(
            text=json.dumps(self.payload, ensure_ascii=False),
            backend=self.name,
        )


class _BlockingBackend(_StaticStructuredBackend):
    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__(payload)
        self.started = threading.Event()
        self.release = threading.Event()

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        self.started.set()
        self.release.wait(timeout=5)
        return super().generate_structured(prompt, response_schema=response_schema)


class _HangingBackend:
    name = "hanging-curator"

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        del prompt, response_schema
        # 必须长于 adaptive 超时上限(base*8):测试输入 prompt 超 2000 字符会触发
        # 超时自适应放大,若 sleep 太短会先等到 AssertionError 而非超时。
        time.sleep(10)
        raise AssertionError("timed-out daemon result must never be committed")


class _FailingBackend:
    name = "failing-curator"

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        del prompt, response_schema
        raise ConnectionError("provider unavailable")


class _LifecycleCurator:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def request(self, reason: str) -> dict[str, object]:
        self.reasons.append(reason)
        return {
            "requested": True,
            "reason": reason,
            "pending_reasons": list(self.reasons),
            "requested_at": "2026-08-04T00:00:00+00:00",
        }


class _LifecycleHandler:
    def __init__(self, event: str) -> None:
        self.body = {
            "kind": "session_lifecycle",
            "event": event,
            "conversation_id": "conversation-1",
            "user_id": "admin",
            "channel": "chat",
        }
        self.headers: dict[str, str] = {}
        self.client_address = ("127.0.0.1", 12345)
        self.responses: list[tuple[int, dict[str, object]]] = []

    def _read_json(self) -> dict[str, object]:
        return dict(self.body)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        self.responses.append((status, payload))


class _FormalMemorySourceProbe:
    def __init__(self) -> None:
        self.calls = 0

    def read(self, *, max_items: int, max_chars: int):
        self.calls += 1
        assert max_items == 32
        assert 1_000 <= max_chars <= 6_000
        return (
            CuratorFormalMemoryInput(
                authority_type="long_term",
                authority_id="memory-old",
                authority_ref="memory/long_term/memory.jsonl#memory-old",
                content_preview="个人电脑使用 Linux。",
                content_hash="sha256:old",
                subject_key="device.personal.os",
                scope_type="personal",
                scope_key="personal",
                updated_at="2026-08-01T00:00:00+00:00",
            ),
        )


def _service(
    tmp_path: Path,
    backend: object,
    store: ConversationStore,
    *,
    config: MemoryCuratorConfig | None = None,
    formal_memory_source: object | None = None,
    provider: str = "fake-structured",
    model: str = "curator-test",
    owner_id: str = "",
    timezone_name: str = "",
    tool_reference_source: CuratorToolReferenceSource | None = None,
) -> MemoryCuratorService:
    return MemoryCuratorService(
        config=config
        or MemoryCuratorConfig(
            interval_seconds=60,
            turn_threshold=1,
            timeout_seconds=2,
            max_retries=0,
        ),
        dependencies=MemoryCuratorDependencies(
            backend=backend,
            conversation_store=store,
            audit_dir=tmp_path / "audit",
            state_store=MemoryCuratorStateStore(
                tmp_path / "memory" / "curator" / "state.json"
            ),
            daily_store=DailyMemoryStore(tmp_path / "memory" / "daily"),
            candidate_service=CandidateService(tmp_path / "memory" / "candidates.jsonl"),
            run_log=CuratorRunLog(tmp_path / "memory" / "curator" / "runs"),
            identity=MemoryCuratorIdentity(
                provider=provider,
                model=model,
                owner_id=owner_id,
                timezone_name=timezone_name,
            ),
            formal_memory_source=formal_memory_source,
            tool_reference_source=tool_reference_source,
        ),
    )


def _conversation(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    message = store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "我的个人电脑使用 macOS。",
            "channel": "internal",
            "metadata": {
                "session_id": "session-1",
                "request_id": "request-1",
                "task_id": "task-1",
                "run_id": "run-1",
            },
            "now": 11.0,
        }
    )
    return store, thread, message


def test_background_curator_defers_same_endpoint_without_consuming_memory(tmp_path):
    from agent_py_agent.agent.backends.request_scope import foreground_model_scope

    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    backend.api_base = "http://shared-model.invalid/v1"
    service = _service(tmp_path, backend, store)
    service.request("task_complete")
    before = service.state_store.load()
    with foreground_model_scope(SimpleNamespace(api_base=backend.api_base)):
        result = service.run_if_due()
        assert result.status == "busy" and backend.calls == 0
        assert service.state_store.load() == before
    assert service.run_if_due().status == "succeeded"
    assert backend.calls == 1


def test_precompact_curator_bypasses_foreground_deferral(tmp_path):
    from agent_py_agent.agent.backends.request_scope import foreground_model_scope

    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    backend.api_base = "http://shared-model.invalid/v1"
    service = _service(tmp_path, backend, store)
    service.request("pre_compact")
    with foreground_model_scope(SimpleNamespace(api_base=backend.api_base)):
        assert service.run_if_due().status == "succeeded"
    assert backend.calls == 1


def _valid_output(thread_id: str, message_id: str, content: str) -> dict[str, object]:
    del content
    ref = {"message_id": message_id}
    return {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [
            {
                "event_type": "conversation",
                "summary": "用户说明个人电脑使用 macOS。",
                "actor": "user",
                "origin": "user_explicit",
                "message_refs": [ref],
                "tool_refs": [],
                "artifact_refs": [],
                "decisions": [],
                "lessons": [],
                "next_actions": [],
            }
        ],
        "candidates": [
            {
                "candidate_type": "long_term_fact",
                "content": "个人电脑使用 macOS。",
                "subject_key": "device.personal.os",
                "scope": {
                    "scope_type": "personal",
                    "scope_key": "personal",
                    "applies_when": "个人电脑",
                    "excludes_when": "公司服务器",
                },
                "origin": "user_explicit",
                "source_message_refs": [ref],
                "source_tool_refs": [],
                "source_artifact_refs": [],
                "observed_at": "1970-01-01T00:00:11+00:00",
                "valid_from": None,
                "valid_until": None,
                "confidence": 0.99,
                "proposed_action": "add",
                "target_entry_id": None,
                "conflicts_with": [],
                "promotion_target": "long_term",
            }
        ],
        "processed_message_refs": [{"message_id": message_id}],
        "processed_audit_refs": [],
        "unresolved_refs": [],
        "warnings": [],
        "next_cursor": {
            "per_thread_cursors": [{"thread_id": thread_id, "message_id": message_id}],
            "last_audit_event_id": None,
        },
    }


def test_curator_response_schema_is_raw_and_recursively_strict():
    schema = curator_response_schema()

    assert schema["type"] == "object"
    assert "schema" not in schema

    def assert_strict_objects(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for value in node.values():
                assert_strict_objects(value)
        elif isinstance(node, list):
            for value in node:
                assert_strict_objects(value)

    assert_strict_objects(schema)
    candidate_properties = schema["properties"]["candidates"]["items"]["properties"]
    assert "evidence_refs" not in candidate_properties
    assert "observation_id" not in candidate_properties
    message_ref_properties = candidate_properties["source_message_refs"]["items"][
        "properties"
    ]
    assert set(message_ref_properties) == {"message_id"}


def test_curator_message_ref_normalizes_gateway_request_identity() -> None:
    item = CuratorMessageInput(
        message_id="message-1",
        thread_id="thread-1",
        role="user",
        channel="gateway-cli",
        created_at=11.0,
        full_content="普通中文消息",
        content_preview="普通中文消息",
        content_hash="sha256:test",
        metadata={"gateway_request_id": "gwreq-1"},
    )

    assert item.ref()["request_id"] == "gwreq-1"
    assert "gateway_request_id" not in item.ref()


def test_curator_prompt_requires_atomic_cross_scope_candidates() -> None:
    message = CuratorMessageInput(
        message_id="message-manifest-1",
        thread_id="thread-manifest-1",
        role="user",
        channel="internal",
        created_at=11.0,
        full_content="普通中文消息",
        content_preview="普通中文消息",
        content_hash="sha256:test",
        metadata={},
    )
    prompt = curator_prompt(CuratorInputBatch(messages=(message,), audit_events=()))

    assert "必须按适用域拆成多条 candidate" in prompt
    assert "个人电脑使用 macOS" in prompt
    assert "公司服务器使用 Linux" in prompt
    assert "一次性要求只能是 session/temporary" in prompt
    assert "不要输出 quote" in prompt
    assert "模型不得输出 promotion_mode" in prompt
    assert "至少要跨不同任务/运行/日期出现 2 个独立证据组" in prompt
    assert "默认进审核，不自动晋升" not in prompt
    assert "独立证据组" in prompt
    assert '"message_ids": ["message-manifest-1"]' in prompt
    assert "不能遗漏、重复或加入清单外 ID" in prompt


def test_curator_response_schema_does_not_expose_host_promotion_mode() -> None:
    """宿主权限字段不属于模型输出合同，provider 不能自行声明或升权。"""
    schema = curator_response_schema()
    candidate = schema["properties"]["candidates"]["items"]

    assert "promotion_mode" not in candidate["properties"]
    assert "promotion_mode" not in candidate["required"]


def test_curator_rejects_provider_attempt_to_author_quote(tmp_path: Path) -> None:
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["daily_events"][0]["message_refs"] = [
        {"message_id": message.message_id, "quote": "用户的电脑使用苹果系统"}
    ]
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    assert service.state_store.load().per_thread_cursors == {}
    assert service.candidate_service.list() == []


def test_curator_text_fallback_rejects_sparse_nested_schema(tmp_path: Path) -> None:
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    del output["daily_events"][0]["decisions"]
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    assert service.state_store.load().per_thread_cursors == {}


def test_curator_rejects_provider_task_and_run_id_fields_as_host_owned(
    tmp_path: Path,
) -> None:
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["candidates"][0]["source_task_ids"] = ["task-not-in-owner-batch"]
    output["candidates"][0]["source_run_ids"] = ["run-not-in-owner-batch"]
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    assert service.candidate_service.list() == []


def test_curator_assigns_observation_id_and_evidence_from_host_snapshot(
    tmp_path: Path,
) -> None:
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    candidate = service.candidate_service.list()[0]
    assert candidate.observation_keys[0].startswith("observation-")
    assert candidate.evidence_refs[0]["message_id"] == message.message_id
    assert candidate.evidence_refs[0]["quote"] == message.content[:300]
    assert candidate.evidence_refs[0]["content_hash"] == (
        "sha256:" + hashlib.sha256(message.content.encode("utf-8")).hexdigest()
    )


def test_curator_rejects_provider_attempt_to_set_host_observation_id(tmp_path: Path) -> None:
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["candidates"][0]["observation_id"] = "provider-random-observation-id"
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    assert service.candidate_service.list() == []


def test_curator_real_service_writes_daily_candidate_and_advances_cursor(tmp_path: Path):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(
        _valid_output(thread.thread_id, message.message_id, message.content)
    )
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert result.daily_events == 1
    assert result.candidates == 1
    state = service.state_store.load()
    assert state.per_thread_cursors[thread.thread_id] == message.message_id
    assert state.active_lease == {}
    candidate = service.candidate_service.list()[0]
    assert candidate.source_task_ids == ["task-1"]
    assert candidate.source_run_ids == ["run-1"]
    assert len(list((tmp_path / "memory" / "daily").glob("*.jsonl"))) == 1
    daily = service.daily_store.list(day="1970-01-01")[0]
    assert daily.session_id == "session-1"
    assert daily.thread_id == thread.thread_id
    assert daily.request_id == "request-1"
    assert daily.task_id == "task-1"
    assert daily.run_id == "run-1"
    assert daily.created_at == "1970-01-01T00:00:11+00:00"
    assert backend.calls == 1


def test_curator_enriches_large_tool_output_ref_without_copying_body(
    tmp_path: Path,
) -> None:
    store, thread, message = _conversation(tmp_path)
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    audit_event_id = "audit-tool-large-1"
    tool_call_id = "call-large-1"
    audit_dir.joinpath("1970-01-01.jsonl").write_text(
        json.dumps(
            {
                "event_id": audit_event_id,
                "action": "tool_call",
                "created_at": "1970-01-01T00:00:12+00:00",
                "status": "ok",
                "tool_name": "read_file",
                "tool_call_id": tool_call_id,
                "tool_success": True,
                "run_id": "run-1",
                "task_id": "task-1",
                "request_id": "request-1",
                "content_preview": "read_file 已成功，完整输出已外置。",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    body_marker = "NEVER_COPY_THIS_TOOL_BODY" * 2_000
    artifact = (
        tmp_path
        / "tasks"
        / "1970-01-01"
        / "task-1"
        / "work"
        / "blobs"
        / "tool_outputs"
        / "read-file-large.json"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(body_marker, encoding="utf-8")
    digest = hashlib.sha256(body_marker.encode("utf-8")).hexdigest()
    artifact.parent.joinpath("index.jsonl").write_text(
        json.dumps(
            {
                "kind": "tool_output",
                "tool": "read_file",
                "call_id": tool_call_id,
                "run_id": "run-1",
                "task_id": "task-1",
                "request_id": "request-1",
                "path": str(artifact),
                "sha256": digest,
                "size_bytes": len(body_marker.encode("utf-8")),
                "status": "ok",
                "ok": True,
                "parameters": {"path": "/private/source-must-not-leak"},
                "output": body_marker,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["daily_events"][0]["tool_refs"] = [{"event_id": audit_event_id}]
    output["daily_events"][0]["artifact_refs"] = [{"artifact_ref": str(artifact)}]
    output["candidates"][0]["source_tool_refs"] = [{"event_id": audit_event_id}]
    output["candidates"][0]["source_artifact_refs"] = [
        {"artifact_ref": str(artifact)}
    ]
    output["processed_audit_refs"] = [{"event_id": audit_event_id}]
    output["next_cursor"]["last_audit_event_id"] = audit_event_id
    backend = _StaticStructuredBackend(output)
    service = _service(
        tmp_path,
        backend,
        store,
        tool_reference_source=CuratorToolReferenceSource(
            tmp_path,
            query=_tool_reference_query,
        ),
    )

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    expected_ref = {
        "artifact_ref": str(artifact.resolve()),
        "event_id": audit_event_id,
        "content_hash": f"sha256:{digest}",
        "size_bytes": len(body_marker.encode("utf-8")),
        "task_id": "task-1",
        "run_id": "run-1",
        "created_at": "1970-01-01T00:00:12+00:00",
    }
    daily = service.daily_store.list(day="1970-01-01")[0]
    candidate = service.candidate_service.list()[0]
    assert daily.artifact_refs == (expected_ref,)
    assert candidate.source_artifact_refs == [expected_ref]
    assert body_marker not in backend.prompts[0]
    assert "/private/source-must-not-leak" not in backend.prompts[0]
    assert len(json.dumps(asdict(daily), ensure_ascii=False)) < 10_000
    assert len(json.dumps(asdict(candidate), ensure_ascii=False)) < 10_000


@pytest.mark.parametrize(
    "claimed_event_id",
    [
        "audit-assistant-tool-round",
        "audit-failed-tool-call",
        "audit-status-only-tool-call",
        "audit-unknown-effect-tool-call",
    ],
)
def test_curator_drops_non_success_tool_verified_evidence(
    tmp_path: Path,
    claimed_event_id: str,
) -> None:
    """assistant_tool_round(status=ok) 和失败 tool_call 都不能伪装成
    tool_verified；Daily 可保留用户经历，但非法 tool_ref 必须被剔除。"""
    store, thread, message = _conversation(tmp_path)
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    audit_rows = [
        {
            "event_id": "audit-assistant-tool-round",
            "action": "assistant_tool_round",
            "created_at": "1970-01-01T00:00:12+00:00",
            "status": "ok",
            "run_id": "run-1",
            "task_id": "task-1",
            "request_id": "request-1",
            "content_preview": "模型准备调用 update_persona。",
        },
        {
            "event_id": "audit-failed-tool-call",
            "action": "tool_call",
            "created_at": "1970-01-01T00:00:13+00:00",
            "status": "error",
            "error_code": "TOOL_ACTION_NOT_REQUIRED",
            "tool_name": "update_persona",
            "tool_call_id": "call-update-persona-1",
            "tool_success": False,
            "effect_outcome": "not_started",
            "run_id": "run-1",
            "task_id": "task-1",
            "request_id": "request-1",
            "content_preview": "update_persona 被宿主拒绝。",
        },
        {
            "event_id": "audit-status-only-tool-call",
            "action": "tool_call",
            "created_at": "1970-01-01T00:00:14+00:00",
            "status": "ok",
            "tool_name": "read_file",
            "tool_call_id": "call-status-only-1",
            "run_id": "run-1",
            "task_id": "task-1",
            "request_id": "request-1",
            "content_preview": "只有 status，没有宿主成功布尔。",
        },
        {
            "event_id": "audit-unknown-effect-tool-call",
            "action": "tool_call",
            "created_at": "1970-01-01T00:00:15+00:00",
            "status": "ok",
            "tool_name": "write_file",
            "tool_call_id": "call-unknown-effect-1",
            "tool_success": True,
            "effect_outcome": "unknown",
            "run_id": "run-1",
            "task_id": "task-1",
            "request_id": "request-1",
            "content_preview": "副作用终态未知。",
        },
    ]
    audit_dir.joinpath("1970-01-01.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in audit_rows),
        encoding="utf-8",
    )
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["daily_events"][0]["origin"] = "tool_verified"
    output["daily_events"][0]["message_refs"] = []
    output["daily_events"][0]["tool_refs"] = [{"event_id": claimed_event_id}]
    candidate = output["candidates"][0]
    candidate["origin"] = "tool_verified"
    candidate["source_message_refs"] = []
    candidate["source_tool_refs"] = [{"event_id": claimed_event_id}]
    output["processed_audit_refs"] = [
        {"event_id": row["event_id"]} for row in audit_rows
    ]
    output["next_cursor"]["last_audit_event_id"] = audit_rows[-1]["event_id"]
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert result.daily_events == 0
    assert result.candidates == 0
    assert service.candidate_service.list() == []
    assert service.daily_store.list(day="1970-01-01") == []
    assert any(
        "candidate:candidate_tool_verified_without_tool" in warning
        for warning in result.warnings
    )
    assert any(
        "daily:daily_tool_verified_without_tool" in warning
        for warning in result.warnings
    )
    assert service.state_store.load().last_processed_audit_event_id == audit_rows[-1][
        "event_id"
    ]


def test_curator_accepts_host_successful_tool_call_evidence(tmp_path: Path) -> None:
    """只有完整工具身份、宿主成功布尔与已知 effect 的真实 tool_call
    才能保留为 tool_verified 引用。"""
    store, thread, message = _conversation(tmp_path)
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    event_id = "audit-successful-tool-call"
    audit_dir.joinpath("1970-01-01.jsonl").write_text(
        json.dumps(
            {
                "event_id": event_id,
                "action": "tool_call",
                "created_at": "1970-01-01T00:00:12+00:00",
                "status": "ok",
                "tool_name": "read_file",
                "tool_call_id": "call-read-file-1",
                "tool_success": True,
                "effect_outcome": "confirmed",
                "operation_id": "tool-operation-read-1",
                "run_id": "run-1",
                "task_id": "task-1",
                "request_id": "request-1",
                "content_preview": "read_file 已成功。",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["daily_events"][0]["tool_refs"] = [{"event_id": event_id}]
    candidate = output["candidates"][0]
    candidate["origin"] = "tool_verified"
    candidate["source_message_refs"] = []
    candidate["source_tool_refs"] = [{"event_id": event_id}]
    output["processed_audit_refs"] = [{"event_id": event_id}]
    output["next_cursor"]["last_audit_event_id"] = event_id
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert result.candidates == 1
    stored = service.candidate_service.list()[0]
    assert stored.origin == "tool_verified"
    assert stored.promotion_mode == "auto_eligible"
    assert stored.source_tool_refs[0]["event_id"] == event_id
    assert stored.source_tool_refs[0]["tool_name"] == "read_file"
    assert stored.source_tool_refs[0]["tool_success"] is True
    assert stored.source_tool_refs[0]["effect_outcome"] == "confirmed"
    daily = service.daily_store.list(day="1970-01-01")[0]
    assert daily.tool_refs[0]["event_id"] == event_id


def test_curator_rejects_tool_artifact_ref_outside_owner_root(tmp_path: Path) -> None:
    store, thread, message = _conversation(tmp_path)
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    audit_dir.joinpath("1970-01-01.jsonl").write_text(
        json.dumps(
            {
                "event_id": "audit-foreign-artifact",
                "action": "tool_call",
                "created_at": "1970-01-01T00:00:12+00:00",
                "status": "ok",
                "tool_call_id": "call-foreign-artifact",
                "run_id": "run-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    foreign_artifact = tmp_path.parent / "foreign-owner-tool-output.json"
    foreign_artifact.write_text("foreign owner body", encoding="utf-8")
    index = (
        tmp_path
        / "tasks"
        / "1970-01-01"
        / "task-1"
        / "work"
        / "blobs"
        / "tool_outputs"
        / "index.jsonl"
    )
    index.parent.mkdir(parents=True)
    index.write_text(
        json.dumps(
            {
                "kind": "tool_output",
                "call_id": "call-foreign-artifact",
                "run_id": "run-1",
                "path": str(foreign_artifact),
                "sha256": hashlib.sha256(b"foreign owner body").hexdigest(),
                "size_bytes": 18,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    backend = _StaticStructuredBackend(
        _valid_output(thread.thread_id, message.message_id, message.content)
    )
    service = _service(
        tmp_path,
        backend,
        store,
        tool_reference_source=CuratorToolReferenceSource(
            tmp_path,
            query=_tool_reference_query,
        ),
    )

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_INPUT_TOOL_REFERENCE_READ_FAILED"
    assert backend.calls == 0
    assert service.state_store.load().per_thread_cursors == {}
    assert service.candidate_service.list() == []


def test_curator_schema_failure_keeps_old_cursor_and_records_failure(tmp_path: Path):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend({"schema_version": "wrong"})
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    state = service.state_store.load()
    assert thread.thread_id not in state.per_thread_cursors
    assert state.last_failure_code == "CURATOR_SCHEMA_INVALID"
    assert list((tmp_path / "memory" / "daily").glob("*.jsonl")) == []


@pytest.mark.parametrize(
    ("backend", "failure_code"),
    [
        (_HangingBackend(), "CURATOR_MODEL_TIMEOUT"),
        (_FailingBackend(), "CURATOR_MODEL_FAILED"),
    ],
)
def test_curator_timeout_and_model_failure_keep_cursor_and_stable_code(
    tmp_path: Path,
    backend: object,
    failure_code: str,
) -> None:
    store, thread, _message = _conversation(tmp_path)
    service = _service(
        tmp_path,
        backend,
        store,
        config=MemoryCuratorConfig(
            interval_seconds=60,
            turn_threshold=1,
            timeout_seconds=1,
            max_retries=0,
        ),
    )

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == failure_code
    state = service.state_store.load()
    assert thread.thread_id not in state.per_thread_cursors
    assert state.last_failure_code == failure_code
    assert state.active_lease == {}
    assert [record.failure_code for record in service.run_log.list()] == [failure_code]


# 函数用途: 按协议把策展输出包成供应商非流式响应体(OpenAI content 字符串 / Anthropic 强制工具块)。
def _provider_structured_response(protocol: str, output: dict[str, object]) -> dict[str, object]:
    if protocol == "anthropic_compatible":
        return {
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "toolu-1", "name": "my_agent_structured_output", "input": output}
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(output, ensure_ascii=False)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


# 函数用途: 起一个回放合法策展输出的本地 HTTP 服务,并记录每次请求实际携带的会话头值。
def _session_header_server(protocol: str, output: dict[str, object]):
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append(str(self.headers.get("x-test-session") or ""))
            encoded = json.dumps(_provider_structured_response(protocol, output), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker, seen


# 函数用途: 真机 2026-09-13 起主 owner 零提取的根因:要求会话头的服务商在发请求前就拒绝无会话请求,
# 而后台线程没有前台 ContextVar。用真实 OpenAI/Anthropic 兼容后端 + 本地传输验证 run 自带会话头。
@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic_compatible"])
def test_curator_binds_host_session_for_session_header_provider(tmp_path: Path, protocol: str) -> None:
    store, thread, message = _conversation(tmp_path)
    server, worker, seen = _session_header_server(
        protocol, _valid_output(thread.thread_id, message.message_id, message.content)
    )
    options = BackendOptions(
        api_base=f"http://127.0.0.1:{server.server_port}/v1",
        api_key="test-key",
        model_name="curator-model",
        session_header="x-test-session",
        stream_enabled=False,
    )
    backend = (
        AnthropicCompatibleBackend(options)
        if protocol == "anthropic_compatible"
        else OpenAICompatibleBackend(options)
    )
    service = _service(tmp_path, backend, store, provider=protocol, owner_id="local/main")
    try:
        result = service.run(reason="admin")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)

    assert result.status == "succeeded", result.failure_code
    assert len(seen) == 1 and seen[0]
    # 会话值只由结构化事实 owner_id + run_id 派生:同一原语在测试线程重放即得同值。
    with provider_session_scope(("local/main",), result.run_id):
        assert seen[0] == request_headers({}, {}, "x-test-session")["x-test-session"]
    with pytest.raises(ValueError):
        request_headers({}, {}, "x-test-session")  # run 结束后当前线程不再持有会话


# 函数用途: 在供应商调用线程里读取当前会话头值,并可按脚本先失败再成功,用来验证重试共享同一会话。
class _SessionRecordingBackend(_StaticStructuredBackend):
    def __init__(self, payload: dict[str, object], *, failures: tuple[BaseException, ...] = ()) -> None:
        super().__init__(payload)
        self.failures = list(failures)
        self.sessions: list[str] = []

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        self.sessions.append(request_headers({}, {}, "x-test-session")["x-test-session"])
        if self.failures:
            raise self.failures.pop(0)
        return super().generate_structured(prompt, response_schema=response_schema)


def test_curator_session_is_shared_by_retries_and_distinct_per_run_and_owner(tmp_path: Path) -> None:
    store, thread, message = _conversation(tmp_path)
    backend = _SessionRecordingBackend(
        _valid_output(thread.thread_id, message.message_id, message.content),
        failures=(ConnectionError("provider unavailable"),),
    )
    config = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, timeout_seconds=2, max_retries=1)
    service = _service(tmp_path / "owner-a", backend, store, config=config, owner_id="owner-a")

    first = service.run(reason="admin")

    assert first.status == "succeeded"
    assert backend.sessions[0] and backend.sessions == [backend.sessions[0]] * 2  # 同 run 两次调用同一会话
    with provider_session_scope(("owner-a",), first.run_id):
        assert backend.sessions[0] == request_headers({}, {}, "x-test-session")["x-test-session"]
    with pytest.raises(ValueError):
        request_headers({}, {}, "x-test-session")  # run 结束后 ContextVar 已复位

    second_message = store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "公司服务器使用 Linux。",
            "channel": "internal",
            "metadata": {"session_id": "session-1", "request_id": "request-2", "task_id": "task-1", "run_id": "run-1"},
            "now": 12.0,
        }
    )
    backend.payload = _valid_output(thread.thread_id, second_message.message_id, second_message.content)
    second = service.run(reason="admin")
    assert second.status == "succeeded" and second.run_id != first.run_id
    assert backend.sessions[2] != backend.sessions[0]  # 不同 run 不同会话

    other_store, other_thread, other_message = _conversation(tmp_path / "owner-b")
    other_backend = _SessionRecordingBackend(
        _valid_output(other_thread.thread_id, other_message.message_id, other_message.content)
    )
    other = _service(tmp_path / "owner-b", other_backend, other_store, owner_id="owner-b").run(reason="admin")
    assert other.status == "succeeded"
    with provider_session_scope(("owner-b",), other.run_id):
        assert other_backend.sessions == [request_headers({}, {}, "x-test-session")["x-test-session"]]
    with provider_session_scope(("owner-a",), other.run_id):
        assert other_backend.sessions[0] != request_headers({}, {}, "x-test-session")["x-test-session"]  # owner 参与派生


def test_curator_rejects_model_claim_of_host_owned_review_origin(tmp_path: Path) -> None:
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["candidates"][0]["origin"] = "reviewed"
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    assert service.candidate_service.list() == []
    assert service.state_store.load().per_thread_cursors == {}


def test_curator_rejects_daily_claim_of_host_owned_review_origin(tmp_path: Path) -> None:
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["daily_events"][0]["origin"] = "reviewed"
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    assert list((tmp_path / "memory" / "daily").glob("*.jsonl")) == []


def test_curator_failure_is_not_retried_on_every_gateway_tick(tmp_path: Path):
    store, _thread, _message = _conversation(tmp_path)
    backend = _StaticStructuredBackend({"schema_version": "wrong"})
    service = _service(tmp_path, backend, store)

    first = service.run_if_due()
    second = service.run_if_due()

    assert first.status == "failed"
    assert second.status == "not_due"
    assert backend.calls == 1


def test_curator_owner_lease_allows_only_one_running_model_call(tmp_path: Path):
    store, thread, message = _conversation(tmp_path)
    backend = _BlockingBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    service = _service(tmp_path, backend, store)
    results: list[object] = []

    worker = threading.Thread(target=lambda: results.append(service.run(reason="admin")))
    worker.start()
    assert backend.started.wait(timeout=2)
    second = service.run(reason="admin")
    backend.release.set()
    worker.join(timeout=5)

    assert second.status == "busy"
    assert results[0].status == "succeeded"
    assert backend.calls == 1


def test_curator_success_preserves_distinct_reason_requested_during_run(
    tmp_path: Path,
) -> None:
    store, thread, message = _conversation(tmp_path)
    backend = _BlockingBackend(
        _valid_output(thread.thread_id, message.message_id, message.content)
    )
    service = _service(tmp_path, backend, store)
    results: list[object] = []

    worker = threading.Thread(
        target=lambda: results.append(service.run(reason="turn_threshold"))
    )
    worker.start()
    assert backend.started.wait(timeout=2)

    service.request("task_complete")
    backend.release.set()
    worker.join(timeout=5)

    assert results[0].status == "succeeded"
    state_after_first = service.state_store.load()
    assert state_after_first.pending_reasons == ["task_complete"]
    assert state_after_first.pending_requested_at

    follow_up = service.run_if_due()

    assert follow_up.status == "succeeded"
    assert follow_up.reason == "task_complete"
    assert follow_up.warnings == ("no_new_experience",)
    state_after_follow_up = service.state_store.load()
    assert state_after_follow_up.pending_reasons == []
    assert state_after_follow_up.pending_requested_at == ""


def test_curator_success_preserves_same_reason_generation_requested_during_run(
    tmp_path: Path,
) -> None:
    store, thread, message = _conversation(tmp_path)
    backend = _BlockingBackend(
        _valid_output(thread.thread_id, message.message_id, message.content)
    )
    service = _service(tmp_path, backend, store)
    results: list[object] = []

    worker = threading.Thread(
        target=lambda: results.append(service.run(reason="task_complete"))
    )
    worker.start()
    assert backend.started.wait(timeout=2)

    service.request("task_complete")
    backend.release.set()
    worker.join(timeout=5)

    assert results[0].status == "succeeded"
    state_after_first = service.state_store.load()
    assert state_after_first.pending_reasons == ["task_complete"]
    assert state_after_first.pending_reason_generations["task_complete"] == 2

    follow_up = service.run_if_due()

    assert follow_up.status == "succeeded"
    assert follow_up.reason == "task_complete"
    assert follow_up.warnings == ("no_new_experience",)
    state_after_follow_up = service.state_store.load()
    assert state_after_follow_up.pending_reasons == []
    assert state_after_follow_up.pending_reason_generations["task_complete"] == 2


def test_curator_turn_threshold_uses_same_service(tmp_path: Path):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(
        _valid_output(thread.thread_id, message.message_id, message.content)
    )
    service = _service(tmp_path, backend, store)

    result = service.run_if_due()

    assert result.reason == "turn_threshold"
    assert result.status == "succeeded"


@pytest.mark.parametrize(
    ("event", "reason"),
    [("close", "session_close"), ("reset", "reset")],
)
def test_gateway_session_lifecycle_requests_same_curator_state_reason(
    event: str,
    reason: str,
) -> None:
    curator = _LifecycleCurator()
    agent = SimpleNamespace(
        config=SimpleNamespace(gateway_per_user_owner_scoping=False),
        memory_curator=curator,
    )
    handler = _LifecycleHandler(event)

    handle_ask(handler, SimpleNamespace(agent=agent), lambda: "unused")

    assert handler.responses == [
        (
            202,
            {
                "event": event,
                "reason": reason,
                "requested": True,
                "pending_reasons": [reason],
                "requested_at": "2026-08-04T00:00:00+00:00",
                "status": "accepted",
                "disposition": "memory_curator_request",
            },
        )
    ]
    assert curator.reasons == [reason]


def test_compact_task_and_lifecycle_triggers_share_one_durable_state(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    service = _service(tmp_path, _StaticStructuredBackend({}), store)
    agent = SimpleNamespace(memory_curator=service)

    _request_memory_curator(agent, "pre_compact")
    _request_memory_curator(agent, "task_complete")
    service.request("session_close")
    service.request("reset")

    state = service.state_store.load()
    assert service.state_store.path == tmp_path / "memory" / "curator" / "state.json"
    assert state.pending_reasons == [
        "pre_compact",
        "task_complete",
        "session_close",
        "reset",
    ]


def test_curator_dependency_contract_has_no_model_facing_tools(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    service = _service(tmp_path, _StaticStructuredBackend({}), store)
    forbidden = {
        "tools",
        "tool_registry",
        "shell",
        "write_file",
        "edit_file",
        "remember",
        "update_persona",
        "skill_installer",
    }

    assert forbidden.isdisjoint(MemoryCuratorDependencies.__dataclass_fields__)
    assert forbidden.isdisjoint(vars(service))


def test_curator_reads_bounded_formal_memory_as_non_instruction_context(tmp_path: Path):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(
        _valid_output(thread.thread_id, message.message_id, message.content)
    )
    formal = _FormalMemorySourceProbe()
    service = _service(tmp_path, backend, store, formal_memory_source=formal)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert formal.calls == 1
    prompt = backend.prompts[0]
    assert '"authority_id": "memory-old"' in prompt
    assert "formal_memories 是当前 active 正式记忆的有界只读投影" in prompt
    assert "不能直接修改 USER.md" in prompt


def test_curator_disk_failure_rolls_back_whole_batch_and_retry_is_idempotent(
    tmp_path: Path,
):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(
        _valid_output(thread.thread_id, message.message_id, message.content)
    )
    service = _service(tmp_path, backend, store)
    write_target = service.committer._write_target
    writes = 0

    def fail_second_write(path: Path, content: str) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected disk failure")
        write_target(path, content)

    service.committer._write_target = fail_second_write
    failed = service.run(reason="admin")

    assert failed.status == "failed"
    assert failed.failure_code == "CURATOR_COMMIT_FAILED"
    state = service.state_store.load()
    assert state.per_thread_cursors == {}
    assert state.candidate_count == 0
    assert state.daily_event_count == 0
    assert service.candidate_service.list() == []
    assert list((tmp_path / "memory" / "daily").glob("*.jsonl")) == []
    assert [item.status for item in service.run_log.list()] == ["failed"]

    service.committer._write_target = write_target
    retried = service.run(reason="admin")

    assert retried.status == "succeeded"
    assert len(service.candidate_service.list()) == 1
    assert service.candidate_service.list()[0].occurrence_count == 1
    daily_path = next((tmp_path / "memory" / "daily").glob("*.jsonl"))
    daily = service.daily_store.list(day=daily_path.stem)
    assert len(daily) == 1
    assert daily[0].sequence == 1
    assert daily[0].previous_event_id == ""


def test_curator_restart_waits_for_live_lease_then_recovers_expired_partial_commit(
    tmp_path: Path,
):
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    crashed_service = _service(tmp_path, _StaticStructuredBackend(output), store)
    write_target = crashed_service.committer._write_target
    writes = 0

    def crash_after_first_write(path: Path, content: str) -> None:
        nonlocal writes
        write_target(path, content)
        writes += 1
        if writes == 1:
            raise SystemExit("simulated process stop")

    crashed_service.committer._write_target = crash_after_first_write
    with pytest.raises(SystemExit, match="simulated process stop"):
        crashed_service.run(reason="admin")

    restarted = _service(tmp_path, _StaticStructuredBackend(output), store)
    live_attempt = restarted.run(reason="admin")
    assert live_attempt.status == "busy"
    assert restarted.state_store.load().active_lease

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    recovered = restarted.run(reason="admin", now=future)

    assert recovered.status == "succeeded"
    state = restarted.state_store.load()
    assert state.active_lease == {}
    assert state.last_committed_run_id == recovered.run_id
    assert state.per_thread_cursors[thread.thread_id] == message.message_id
    candidates = restarted.candidate_service.list()
    assert len(candidates) == 1
    assert candidates[0].occurrence_count == 1
    daily_path = next((tmp_path / "memory" / "daily").glob("*.jsonl"))
    daily = restarted.daily_store.list(day=daily_path.stem)
    assert len(daily) == 1
    assert daily[0].sequence == 1
    records = restarted.run_log.list()
    assert [item.status for item in records] == ["recovered_rollback", "succeeded"]
    assert records[-1].recovery["kind"] == "expired_lease"
    assert records[-1].recovery["previous_run_id"] != recovered.run_id


def test_curator_provider_model_switch_keeps_identical_schema(tmp_path: Path):
    store_a, thread_a, message_a = _conversation(tmp_path / "a")
    store_b, thread_b, message_b = _conversation(tmp_path / "b")
    backend_a = _StaticStructuredBackend(
        _valid_output(thread_a.thread_id, message_a.message_id, message_a.content)
    )
    backend_b = _StaticStructuredBackend(
        _valid_output(thread_b.thread_id, message_b.message_id, message_b.content)
    )
    service_a = _service(
        tmp_path / "a",
        backend_a,
        store_a,
        provider="provider-a",
        model="model-a",
    )
    service_b = _service(
        tmp_path / "b",
        backend_b,
        store_b,
        provider="provider-b",
        model="model-b",
    )

    result_a = service_a.run(reason="admin")
    result_b = service_b.run(reason="admin")

    assert (result_a.provider, result_a.model) == ("provider-a", "model-a")
    assert (result_b.provider, result_b.model) == ("provider-b", "model-b")
    assert backend_a.schemas == backend_b.schemas


def test_curator_interval_and_daily_finalize_use_same_state_and_service(tmp_path: Path):
    interval_store, interval_thread, interval_message = _conversation(tmp_path / "interval")
    interval_backend = _StaticStructuredBackend(
        _valid_output(
            interval_thread.thread_id,
            interval_message.message_id,
            interval_message.content,
        )
    )
    config = MemoryCuratorConfig(
        interval_seconds=60,
        turn_threshold=5,
        timeout_seconds=2,
        max_retries=0,
        daily_finalize_hour=23,
    )
    interval_service = _service(
        tmp_path / "interval",
        interval_backend,
        interval_store,
        config=config,
    )
    interval = interval_service.run_if_due(
        now=datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
    )
    assert interval.status == "succeeded"
    assert interval.reason == "interval"

    empty_store = ConversationStore(tmp_path / "daily" / "conversations")
    daily_service = _service(
        tmp_path / "daily",
        _StaticStructuredBackend({}),
        empty_store,
        config=config,
        timezone_name="UTC",
    )
    finalized = daily_service.run_if_due(
        now=datetime(2026, 8, 4, 23, tzinfo=timezone.utc)
    )
    assert finalized.status == "succeeded"
    assert finalized.reason == "daily_finalize"
    assert finalized.warnings == ("no_new_experience",)
    assert daily_service.state_store.load().last_daily_finalize_date == "2026-08-04"


def test_pending_promotable_candidate_ids_only_picks_host_authorized_auto(
    tmp_path: Path,
) -> None:
    """兜底选择正式用户事实与 lesson，自始至终排除无事实权威的模型猜测。"""
    from agent_py_agent.agent.memory_store.candidate_models import (
        CandidateObservation,
        MemoryScope,
    )

    service = _service(tmp_path, _StaticStructuredBackend({}), ConversationStore(tmp_path / "c"))
    candidates = service.candidate_service

    user_auto = candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="个人电脑使用 macOS。",
            subject_key="device.personal.os",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            promotion_target="long_term",
            promotion_mode="auto_eligible",
            source_message_refs=({"message_id": "msg-1"},),
        )
    )
    user_legacy_manual = candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="个人电脑使用 Linux。",
            subject_key="device.personal.alt",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            promotion_target="long_term",
            source_message_refs=({"message_id": "msg-2"},),
        )
    )
    lesson = candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="做 X 任务应先备份再修改。",
            subject_key="lesson.task_x_backup_first",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            promotion_target="lesson",
            source_run_ids=("run-1",),
        )
    )
    inferred_fact = candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="模型猜测用户喜欢蓝色。",
            subject_key="preference.color",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            promotion_target="long_term",
        )
    )

    ids = service._pending_promotable_candidate_ids()

    assert user_auto.candidate_id in ids
    assert user_legacy_manual.candidate_id in ids
    assert lesson.candidate_id in ids
    assert inferred_fact.candidate_id not in ids  # model_inferred 事实:不捞


class _RecordingPromotion:
    """记录被 Curator 提交自动晋升的候选 ID(不模拟闸,闸的真实性由 promotion 层测试证明)。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, candidate_id: str) -> object:
        self.calls.append(candidate_id)
        return SimpleNamespace(promoted=True)


def test_curator_qualified_user_fact_still_auto_promotes(tmp_path: Path) -> None:
    """1227 正例:合格 user_explicit 新 long_term 事实经 curator 提炼 → 宿主赋 auto_eligible →
    落库 → 兜底 selector 捞起并提交自动晋升(真实提炼路径,含 _prepare_outputs 赋权)。"""
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(
        _valid_output(thread.thread_id, message.message_id, message.content)
    )
    service = _service(tmp_path, backend, store)
    recorder = _RecordingPromotion()
    service.promotion_callback = recorder

    result = service.run_if_due(now=datetime(2026, 8, 4, 12, tzinfo=timezone.utc))

    assert result.status == "succeeded"
    candidate = service.candidate_service.list()[0]
    assert candidate.promotion_mode == "auto_eligible"  # 宿主确定性赋权已持久化
    assert candidate.candidate_id in recorder.calls  # 兜底/提交后都提交自动晋升


def test_curator_lesson_extraction_enters_autonomous_threshold_chain(tmp_path: Path) -> None:
    """Curator 提炼 lesson 后提交自主晋升；真实 Promotion 层负责重复证据门槛。"""
    store, thread, message = _conversation(tmp_path)
    payload = _valid_output(thread.thread_id, message.message_id, message.content)
    payload["candidates"] = [
        {
            "candidate_type": "lesson",
            "content": "做 X 任务应先备份再修改。",
            "subject_key": "lesson.task_x_backup_first",
            "scope": {
                "scope_type": "personal",
                "scope_key": "personal",
                "applies_when": "",
                "excludes_when": "",
            },
            "origin": "model_inferred",
            "source_message_refs": [{"message_id": message.message_id}],
            "source_tool_refs": [],
            "source_artifact_refs": [],
            "observed_at": "1970-01-01T00:00:11+00:00",
            "valid_from": None,
            "valid_until": None,
            "confidence": 0.9,
            "proposed_action": "add",
            "target_entry_id": None,
            "conflicts_with": [],
            "promotion_target": "lesson",
        }
    ]
    backend = _StaticStructuredBackend(payload)
    service = _service(tmp_path, backend, store)
    recorder = _RecordingPromotion()
    service.promotion_callback = recorder

    result = service.run_if_due(now=datetime(2026, 8, 4, 12, tzinfo=timezone.utc))

    assert result.status == "succeeded"
    candidate = service.candidate_service.list()[0]
    assert candidate.promotion_mode == "auto_eligible"
    assert recorder.calls == [candidate.candidate_id]
    assert candidate.status == "pending_review"


@pytest.mark.parametrize(
    ("candidate_changes", "expected_status", "expected_callback"),
    [
        ({"conflicts_with": ["memory-old"]}, "blocked_conflict", False),
        (
            {
                "scope": {
                    "scope_type": "temporary",
                    "scope_key": "temporary:session-1",
                    "applies_when": "本次会话",
                    "excludes_when": "会话结束后",
                },
                "valid_until": None,
            },
            "pending_review",
            True,
        ),
    ],
)
def test_curator_incomplete_auto_conditions_stay_blocked_by_structured_gate(
    tmp_path: Path,
    candidate_changes: dict[str, object],
    expected_status: str,
    expected_callback: bool,
) -> None:
    """自主权限不绕过冲突/过期门；已结构化阻塞的候选不会进入回调。"""
    store, thread, message = _conversation(tmp_path)
    payload = _valid_output(thread.thread_id, message.message_id, message.content)
    payload["candidates"][0].update(candidate_changes)
    service = _service(tmp_path, _StaticStructuredBackend(payload), store)
    recorder = _RecordingPromotion()
    service.promotion_callback = recorder

    result = service.run_if_due(now=datetime(2026, 8, 4, 12, tzinfo=timezone.utc))

    assert result.status == "succeeded"
    candidate = service.candidate_service.list()[0]
    assert candidate.promotion_mode == "auto_eligible"
    assert candidate.status == expected_status
    assert (candidate.candidate_id in recorder.calls) is expected_callback


def test_reinjection_chain_never_grows_formal_memory(tmp_path: Path) -> None:
    """防回灌闭环核验(注入→复述→提炼):提炼出的复述候选如实保留来源属性,
    且提炼输入不含注入信封;后续晋升由自动闸+写路径查重兜底(各自已单测)。

    三段机制:①curator 提炼输入只来自 conversation_store(注入信封不在 store,
    只有模型复述进对话);②复述候选 origin=model_inferred → 自动晋升闸拦截;
    ③user_explicit 重复要求 → 写路径查重合并不新增。
    """
    store, thread, message = _conversation(tmp_path)
    # 模型回复逐字复述注入内容(会进对话,是真实发生的话)。
    echoed = store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "<memory-context>祥子买了两次车。</memory-context>请告诉我更多。",
            "channel": "internal",
            "now": 12.0,
        }
    )
    ref = {"message_id": message.message_id}
    backend = _StaticStructuredBackend(
        {
            "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
            "daily_events": [],
            "candidates": [
                {
                    "candidate_type": "long_term_fact",
                    "content": "祥子买了两次车。",
                    "subject_key": "xiangzi.car",
                    "scope": {"scope_type": "personal", "scope_key": "personal", "applies_when": "", "excludes_when": ""},
                    "origin": "user_explicit",
                    "source_message_refs": [ref],
                    "source_tool_refs": [],
                    "source_artifact_refs": [],
                    "observed_at": "1970-01-01T00:00:11+00:00",
                    "valid_from": None,
                    "valid_until": None,
                    "confidence": 0.99,
                    "proposed_action": "add",
                    "target_entry_id": None,
                    "conflicts_with": [],
                    "promotion_target": "long_term",
                },
                {
                    "candidate_type": "long_term_fact",
                    "content": "祥子买了两次车。",
                    "subject_key": "xiangzi.car.repeated",
                    "scope": {"scope_type": "personal", "scope_key": "personal", "applies_when": "", "excludes_when": ""},
                    "origin": "model_inferred",
                    "source_message_refs": [ref],
                    "source_tool_refs": [],
                    "source_artifact_refs": [],
                    "observed_at": "1970-01-01T00:00:11+00:00",
                    "valid_from": None,
                    "valid_until": None,
                    "confidence": 0.99,
                    "proposed_action": "add",
                    "target_entry_id": None,
                    "conflicts_with": [],
                    "promotion_target": "long_term",
                },
            ],
            "processed_message_refs": [
                {"message_id": message.message_id},
                {"message_id": echoed.message_id},
            ],
            "processed_audit_refs": [],
            "unresolved_refs": [],
            "warnings": [],
            "next_cursor": {
                "per_thread_cursors": [{"thread_id": thread.thread_id, "message_id": echoed.message_id}],
                "last_audit_event_id": None,
            },
        }
    )
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")
    assert result.status == "succeeded"
    by_id = {item.candidate_id: item for item in service.candidate_service.list()}
    assert len(by_id) == 2
    # 复述候选如实保留 model_inferred 来源 → 自动晋升闸在 promotion 层拦截(见
    # test_model_inferred_never_auto_promotes);user_explicit 重复走写路径查重
    # （只有精确重复去重，相似事实不覆盖）。本测试核验
    # curator 提炼环节不把复述伪装成用户要求。
    repeated = next(item for item in by_id.values() if item.origin == "model_inferred")
    assert repeated.content == "祥子买了两次车。"
    explicit = next(item for item in by_id.values() if item.origin == "user_explicit")
    assert explicit.candidate_id != repeated.candidate_id


def test_curator_drops_only_evidence_invalid_daily_and_candidate(tmp_path: Path) -> None:
    """问题7真机根因回归(2026-08-09):deepseek-v4-flash 在 candidate 证据里编造
    batch 外 message_id(32位hex)。修复前一个幻觉引用让整批 CURATOR_EVIDENCE_INVALID、
    游标不推进、每6分钟重试同一批39连败;修复后出界引用被剔除,该条丢弃,
    其余 daily/candidate 与游标照常入库——覆盖声明仍严格。"""
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    # 真机形态:candidate[0] 的 source_message_refs 里混入编造的 batch 外 ID
    output["candidates"][0]["source_message_refs"] = [
        {"message_id": "msg-5510de6a5edd21c53dad6066799b07e7"},  # 模型编造(32位hex)
    ]
    # 第二个 candidate 正常
    output["candidates"].append(
        {
            "candidate_type": "long_term_fact",
            "content": "个人电脑使用 macOS。",
            "subject_key": "device.personal.os",
            "scope": {
                "scope_type": "personal",
                "scope_key": "personal",
                "applies_when": "个人电脑",
                "excludes_when": "公司服务器",
            },
            "origin": "user_explicit",
            "source_message_refs": [{"message_id": message.message_id}],
            "source_tool_refs": [],
            "source_artifact_refs": [],
            "observed_at": "1970-01-01T00:00:11+00:00",
            "valid_from": None,
            "valid_until": None,
            "confidence": 0.99,
            "proposed_action": "add",
            "target_entry_id": None,
            "conflicts_with": [],
            "promotion_target": "long_term",
        }
    )
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    # 整批成功、游标推进、只丢证据无效的那条 candidate
    assert result.status == "succeeded"
    assert service.state_store.load().per_thread_cursors == {
        thread.thread_id: message.message_id
    }
    candidates = service.candidate_service.list()
    assert len(candidates) == 1
    assert candidates[0].evidence_refs[0]["message_id"] == message.message_id
    assert any("dropped_evidence:candidate" in w for w in result.warnings), result.warnings


def test_curator_dropped_daily_and_candidate_warn_but_advance_cursor(tmp_path: Path) -> None:
    """宽容边界:全部引用都出界的 daily/candidate 丢弃,但覆盖声明正确时整批仍推进。"""
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    # 两条都只引用 batch 外 ID → 双双丢弃
    output["daily_events"][0]["message_refs"] = [
        {"message_id": "msg-fabricated-daily-001"}
    ]
    output["candidates"][0]["source_message_refs"] = [
        {"message_id": "msg-fabricated-candidate-001"}
    ]
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert service.state_store.load().per_thread_cursors == {
        thread.thread_id: message.message_id
    }
    assert service.candidate_service.list() == []
    assert any("dropped_evidence:" in w for w in result.warnings), result.warnings
    assert "daily" in " ".join(result.warnings)
    assert "candidate" in " ".join(result.warnings)


def test_curator_drops_raw_add_scope_without_rolling_back_valid_outputs(
    tmp_path: Path,
) -> None:
    """模型给出 raw company key 时只丢该候选；合法 Candidate、Daily 和游标仍提交。"""
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    invalid = dict(output["candidates"][0])
    invalid["content"] = "公司服务器使用 Linux。"
    invalid["subject_key"] = "device.company.os"
    invalid["scope"] = {
        "scope_type": "company",
        "scope_key": "default",
        "applies_when": "公司服务器",
        "excludes_when": "个人电脑",
    }
    output["candidates"].insert(0, invalid)
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert result.failure_code == ""
    assert service.state_store.load().per_thread_cursors == {
        thread.thread_id: message.message_id
    }
    candidates = service.candidate_service.list()
    assert len(candidates) == 1
    assert candidates[0].scope["scope_key"] == "personal"
    assert len(service.daily_store.list(day="1970-01-01")) == 1
    assert "dropped_evidence:candidate:candidate_scope_invalid" in result.warnings


def test_curator_canonicalizes_task_alias_before_candidate_commit(tmp_path: Path) -> None:
    """Curator 验证和 Candidate 提交共用 scope 合同，task alias 只落 project 正式键。"""
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["candidates"][0]["scope"] = {
        "scope_type": "project",
        "scope_key": "task:alpha",
        "applies_when": "alpha 项目",
        "excludes_when": "",
    }
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    candidates = service.candidate_service.list()
    assert len(candidates) == 1
    assert candidates[0].scope["scope_key"] == "project:alpha"
    assert not any("candidate_scope_invalid" in item for item in result.warnings)


def test_curator_keeps_raw_scope_for_targeted_legacy_remove(tmp_path: Path) -> None:
    """replace/remove 仍可精确引用并清理历史 raw scope，不套用新 add 的严格门。"""
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    output["candidates"][0].update(
        {
            "content": "移除旧公司环境记录。",
            "subject_key": "device.company.os",
            "scope": {
                "scope_type": "company",
                "scope_key": "default",
                "applies_when": "旧公司环境",
                "excludes_when": "",
            },
            "proposed_action": "remove",
            "target_entry_id": "legacy-company-os",
        }
    )
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    candidates = service.candidate_service.list()
    assert len(candidates) == 1
    assert candidates[0].proposed_action == "remove"
    assert candidates[0].target_entry_id == "legacy-company-os"
    assert candidates[0].scope["scope_key"] == "default"
    assert not any("candidate_scope_invalid" in item for item in result.warnings)


def test_curator_unprocessed_claims_do_not_fail_batch(tmp_path: Path) -> None:
    """漏声明/出界声明不再整批失败:游标按连续 processed 前缀推进,未声明输入停在
    游标前、下轮重放(数据零丢失);daily/candidate 真实证据照常入库。精确相等校验
    曾把「漏声明+出界声明」打成整批失败(真机 2026-08-09 deepseek-v4-flash 39 连败
    0 产出),游标机制本就独立承担防漏。"""
    store, thread, message = _conversation(tmp_path)
    output = _valid_output(thread.thread_id, message.message_id, message.content)
    # 模型漏声明真实消息、只声明了 batch 外不存在的消息
    output["processed_message_refs"] = [
        {"message_id": "msg-someone-elses"}
    ]
    service = _service(tmp_path, _StaticStructuredBackend(output), store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    # 真实消息未声明 → 游标不推进,下轮 batch 重放
    assert service.state_store.load().per_thread_cursors == {}
    # 真实证据引用(daily/candidate 引用本批真实 message)照常入库
    assert len(service.candidate_service.list()) == 1


def test_curator_observation_id_normalizes_task_alias_scope() -> None:
    """steward 1207：同一证据以 task:alpha/project:alpha 双写法重放，宿主 observation_id 必须一致。"""
    from agent_py_agent.agent.memory_store.candidate_models import (
        CandidateObservation,
        MemoryScope,
    )
    from agent_py_agent.agent.memory_store.curator_validation import (
        _canonical_observation_id,
    )

    def observation_for(scope_key: str) -> CandidateObservation:
        return CandidateObservation(
            candidate_type="long_term_fact",
            content="个人电脑使用 macOS。",
            subject_key="device.personal.os",
            scope=MemoryScope("project", scope_key),
            origin="user_explicit",
            promotion_target="long_term",
            source_message_refs=({"message_id": "msg-1"},),
        )

    task_alias = _canonical_observation_id(
        observation_for("task:alpha"),
        message_refs=[],
        tool_refs=[],
        artifact_refs=[],
        task_ids=(),
        run_ids=(),
    )
    canonical = _canonical_observation_id(
        observation_for("project:alpha"),
        message_refs=[],
        tool_refs=[],
        artifact_refs=[],
        task_ids=(),
        run_ids=(),
    )
    assert task_alias == canonical
    # 坏值 scope（legacy 占位）不抛且保持原样，fail-closed 隔离，不阻断 curator 链路
    legacy = _canonical_observation_id(
        observation_for("legacy"),
        message_refs=[],
        tool_refs=[],
        artifact_refs=[],
        task_ids=(),
        run_ids=(),
    )
    assert isinstance(legacy, str) and legacy.startswith("curator-observation-")


def test_curator_task_alias_replay_does_not_duplicate_occurrence(tmp_path: Path) -> None:
    """steward 1207：task:alpha 与 project:alpha 双写法只产生一个候选、occurrence 不重复计。"""
    from agent_py_agent.agent.memory_store.candidate_models import (
        CandidateObservation,
        MemoryScope,
    )
    from agent_py_agent.agent.memory_store.curator_validation import (
        _canonical_observation_id,
    )

    def observation_for(scope_key: str, observation_id: str = "") -> CandidateObservation:
        return CandidateObservation(
            candidate_type="long_term_fact",
            content="个人电脑使用 macOS。",
            subject_key="device.personal.os",
            scope=MemoryScope("project", scope_key),
            origin="user_explicit",
            promotion_target="long_term",
            source_message_refs=({"message_id": "msg-1"},),
            observation_id=observation_id,
        )

    task_alias = observation_for("task:alpha")
    canonical = observation_for("project:alpha")
    task_id = _canonical_observation_id(
        task_alias, message_refs=[], tool_refs=[], artifact_refs=[], task_ids=(), run_ids=()
    )
    project_id = _canonical_observation_id(
        canonical, message_refs=[], tool_refs=[], artifact_refs=[], task_ids=(), run_ids=()
    )
    assert task_id == project_id

    service = CandidateService(tmp_path / "memory" / "candidates.jsonl")
    service.observe_many(
        [observation_for("task:alpha", task_id), observation_for("project:alpha", project_id)]
    )

    current = service.list()
    assert len(current) == 1
    assert current[0].occurrence_count == 1


# LLM: 通用失败码（如 CURATOR_MODEL_FAILED）必须能带出机器可判定的形状，否则真机故障无法定位；
# 同时不得落供应商异常正文。这条锁住诊断字段的存在与边界。
# 函数用途: 验证失败诊断只含异常类名与（可选）HTTP 状态码。
def test_curator_failure_diagnostic_records_shape_and_redacted_message() -> None:
    import agent_py_agent.agent.memory_store.curator as curator_module
    from agent_py_agent.agent.memory_store.curator import _failure_diagnostic

    class _Rejected(RuntimeError):
        http_status = 429

    # 正文经共用日志脱敏后落账:已知密钥形状被遮蔽,可归因的其余文字保留。
    diagnostic = _failure_diagnostic(_Rejected("provider said: sk-abcdefghijklmnopqrstuvwxyz"))
    assert diagnostic["error_type"] == "_Rejected"
    assert diagnostic["provider_http_status"] == 429
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(diagnostic, ensure_ascii=False)
    assert str(diagnostic["message"]).startswith("provider said: ")

    assert _failure_diagnostic(RuntimeError("boom")) == {"error_type": "RuntimeError", "message": "boom"}
    assert _failure_diagnostic(RuntimeError("")) == {"error_type": "RuntimeError"}
    assert len(str(_failure_diagnostic(RuntimeError("x" * 500))["message"])) == 200

    # 诊断只能走既有 warnings 字段：新增 dataclass 字段会让 from_record 的严格 v2 键集校验
    # 拒绝所有历史行，把失败记账变成 CURATOR_RUN_AUDIT_FAILED（真机踩过）。
    from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunRecord

    assert "failure_diagnostic" not in CuratorRunRecord.__dataclass_fields__
    encoded = curator_module._failure_diagnostic_warning(diagnostic)
    assert encoded.startswith("failure_diagnostic=")
    assert json.loads(encoded.split("=", 1)[1])["provider_http_status"] == 429
    # 200 字正文经 JSON 转义可能突破单条 warning 300 字符上限:只缩短 message,整条仍可解析,
    # 否则失败记账本身会变成 CURATOR_RUN_AUDIT_FAILED。
    quoted = curator_module._failure_diagnostic_warning(_failure_diagnostic(RuntimeError('"' * 200)))
    assert len(quoted) <= 300
    assert json.loads(quoted.split("=", 1)[1])["error_type"] == "RuntimeError"
