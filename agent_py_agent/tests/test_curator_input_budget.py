from __future__ import annotations

"""Curator 输入预算缩批（真机 2026-09-24 起记忆整理永久卡死的缺陷修复）。

真机账：9/24 15:39Z 起每次 session_close 都是 CURATOR_INPUT_BUDGET_EXCEEDED，cursor_before 始终同一个、
processed 为 0；此前成功批次的提示长度已贴着 40000 上限（39399、39653）。收集阶段只给模板预留固定
7000 字符、按条目 JSON 计量，身份清单与审计保底都不在预算内；积压一满，组出的提示必然超预算，
提取前直接报错且不缩批，游标不前进，下一轮重建同一批，永远失败。

本测试锁定修复口径：按最终提示实测长度做确定性尾部截断（先消息后审计，各留至少一条），与超时缩批同一
游标契约——被丢弃的尾部留在原游标之后，下一轮重放，零丢失；缩批发生在可选决策标注之前；预算失败不得
复用上一调用者留下的尝试形状。
"""

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_backend import (
    curator_prompt,
    extract_with_retries,
    fit_batch_to_input_budget,
    last_model_attempts,
)
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorAuditInput,
    CuratorInputBatch,
    CuratorMessageInput,
    collect_curator_inputs,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    MemoryCuratorConfig,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import (
    MemoryCuratorState,
    MemoryCuratorStateStore,
)
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore

_MANIFEST = re.compile(r"输入身份清单为 (\{.*?\})。必须逐项复制清单", re.S)


# 函数用途: 假 Curator 模型，只按提示里的输入身份清单逐项回填 processed，并记录每次收到的提示。
class _ManifestEchoBackend:
    name = "manifest-echo"

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.prompts: list[str] = []

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        self.prompts.append(prompt)
        manifest = json.loads(_MANIFEST.search(prompt).group(1))
        message_ids = manifest["message_ids"]
        audit_ids = manifest["audit_event_ids"]
        output = {
            "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
            "daily_events": [],
            "candidates": [],
            "processed_message_refs": [{"message_id": mid} for mid in message_ids],
            "processed_audit_refs": [{"event_id": eid} for eid in audit_ids],
            "unresolved_refs": [],
            "warnings": [],
            "next_cursor": {
                "per_thread_cursors": [{"thread_id": self.thread_id, "message_id": message_ids[-1]}] if message_ids else [],
                "last_audit_event_id": audit_ids[-1] if audit_ids else None,
            },
        }
        return ModelResponse(text=json.dumps(output, ensure_ascii=False), backend=self.name)


# 函数用途: 按真机审计行形态（36 位事件编号、带内容预览的工具调用）写入若干条 owner audit 事件。
def _write_audit(tmp_path: Path, thread_id: str, count: int) -> list[str]:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    event_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(1, count + 1)]
    rows = [
        {"event_id": event_id, "action": "tool_call", "created_at": 20.0 + index, "status": "ok",
         "tool_name": "read_file", "tool_success": True, "tool_call_id": f"call-{index}", "run_id": "run-1",
         "thread_id": thread_id, "request_id": f"request-{index}", "content_hash": "sha256:tool",
         "content_preview": "读取项目说明：" + "构建步骤与目录约定。" * 90}
        for index, event_id in enumerate(event_ids)
    ]
    (audit_dir / "2026-09-24.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return event_ids


# 函数用途: 造一个真实 ConversationStore 线程和若干条用户消息。
def _conversation(tmp_path: Path, count: int):
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
    messages = [
        store.messages.append(
            {
                "thread_id": thread.thread_id,
                "role": "user",
                "content": f"第{index + 1}条：我的个人电脑使用 macOS。",
                "channel": "internal",
                "metadata": {"request_id": f"request-{index + 1}"},
                "now": 11.0 + index,
            }
        )
        for index in range(count)
    ]
    return store, thread, messages


# 函数用途: 按生产同一口径（收集预算 = max_input_chars - 7000 - 正式记忆字符）算出“恰好收满全部积压”的配置。
def _backlog_filling_config(store: ConversationStore, tmp_path: Path) -> MemoryCuratorConfig:
    base = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, timeout_seconds=1, max_retries=0,
                               batch_message_limit=80, max_input_chars=500_000)
    everything = collect_curator_inputs(conversation_store=store, audit_dir=tmp_path / "no-audit",
                                        state=MemoryCuratorState(), config=base)
    message_chars = len(json.dumps([item.to_model() for item in everything.messages], ensure_ascii=False))
    return replace(base, max_input_chars=message_chars + 7_000 + len(json.dumps([])))


# 函数用途: 造使用真实 state/daily/candidate/run-log 权威的 Curator 依赖，可选注入前置标注钩子。
def _dependencies(tmp_path: Path, backend: object, store: ConversationStore, annotate=None) -> MemoryCuratorDependencies:
    return MemoryCuratorDependencies(
        backend=backend,
        conversation_store=store,
        annotate_batch=annotate,
        audit_dir=tmp_path / "audit",
        state_store=MemoryCuratorStateStore(tmp_path / "memory" / "curator" / "state.json"),
        daily_store=DailyMemoryStore(tmp_path / "memory" / "daily"),
        candidate_service=CandidateService(tmp_path / "memory" / "candidates.jsonl"),
        run_log=CuratorRunLog(tmp_path / "memory" / "curator" / "runs"),
        identity=MemoryCuratorIdentity(provider="manifest-echo", model="curator-budget-test"),
    )


# 函数用途: 造一个不带前置标注的 Curator 服务。
def _service(tmp_path: Path, backend: object, store: ConversationStore, config: MemoryCuratorConfig) -> MemoryCuratorService:
    return MemoryCuratorService(config=config, dependencies=_dependencies(tmp_path, backend, store))


# 函数用途: 造一条与生产同构的 Curator 消息输入。
def _message(message_id: str, preview_chars: int = 20) -> CuratorMessageInput:
    preview = "我的个人电脑使用 macOS。" * max(1, preview_chars // 10)
    return CuratorMessageInput(message_id=message_id, thread_id="thread-1", role="user", channel="internal",
                               created_at=11.0, full_content=preview, content_preview=preview,
                               content_hash="sha256:test", metadata={"request_id": "request-1"})


# 函数用途: 造一条与生产同构的 Curator 审计输入。
def _audit(event_id: str) -> CuratorAuditInput:
    return CuratorAuditInput(
        event_id=event_id, event_type="tool_call", created_at="1970-01-01T00:00:11+00:00", status="ok",
        operation_id="operation-" + event_id, tool_call_id="call-" + event_id, tool_name="read_file",
        tool_success=True, error_code="", effect_outcome="", session_id="session-1", thread_id="thread-1",
        request_id="request-1", task_id="task-1", run_id="run-1", content_hash="sha256:tool", source_ref="",
        artifact_ref="", artifact_hash="", artifact_size_bytes=0, preview="工具预览",
    )


# 函数用途: 造一批同构消息与审计输入。
def _batch(messages: int, audits: int, preview_chars: int = 20) -> CuratorInputBatch:
    return CuratorInputBatch(
        messages=tuple(_message(f"message-{index:03d}", preview_chars) for index in range(1, messages + 1)),
        audit_events=tuple(_audit(f"event-{index:03d}") for index in range(1, audits + 1)),
    )


def test_full_backlog_is_fitted_committed_as_prefix_and_replayed_without_loss(tmp_path: Path) -> None:
    store, thread, messages = _conversation(tmp_path, 80)
    config = _backlog_filling_config(store, tmp_path)
    event_ids = _write_audit(tmp_path, thread.thread_id, 3)
    # 前置条件即真机形态：消息收满收集预算，审计仍按保底至少进一条，身份清单也不在预算内，最终提示超预算。
    collected = collect_curator_inputs(
        conversation_store=store, audit_dir=tmp_path / "audit", state=MemoryCuratorState(),
        config=replace(config, max_input_chars=config.max_input_chars - 7_000 - len(json.dumps([]))),
    )
    assert len(collected.messages) == 80
    assert collected.audit_events
    assert len(curator_prompt(collected)) > config.max_input_chars

    backend = _ManifestEchoBackend(thread.thread_id)
    service = _service(tmp_path, backend, store, config)
    first = service.run(reason="admin")

    assert first.status == "succeeded", first.failure_code
    assert len(backend.prompts) == 1
    assert len(backend.prompts[0]) <= config.max_input_chars
    sent = json.loads(_MANIFEST.search(backend.prompts[0]).group(1))["message_ids"]
    # 只截尾部：本轮输入是原积压的前缀，游标只推进到前缀最后一条。
    assert 0 < len(sent) < 80
    assert sent == [item.message_id for item in messages[: len(sent)]]
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: sent[-1]}
    audits = len(collected.audit_events)
    assert first.warnings == (f"memory_curator_input_fitted:messages=80->{len(sent)},audit={audits}->{audits}",)
    assert messages[0].content not in first.warnings[0]
    sent_audit = json.loads(_MANIFEST.search(backend.prompts[0]).group(1))["audit_event_ids"]

    # 被截掉的尾部下一轮从原游标之后重放；全部积压被恰好处理一次，没有丢失或重复。
    second = service.run(reason="admin")
    assert second.status == "succeeded", second.failure_code
    replay_manifest = json.loads(_MANIFEST.search(backend.prompts[1]).group(1))
    assert sent + replay_manifest["message_ids"] == [item.message_id for item in messages]
    assert sent_audit + replay_manifest["audit_event_ids"] == event_ids
    assert second.warnings == ()
    state = service.state_store.load()
    assert state.per_thread_cursors == {thread.thread_id: messages[-1].message_id}
    assert state.last_processed_audit_event_id == event_ids[-1]


def test_fit_runs_before_optional_decision_annotation(tmp_path: Path) -> None:
    store, thread, _messages = _conversation(tmp_path, 80)
    config = _backlog_filling_config(store, tmp_path)
    _write_audit(tmp_path, thread.thread_id, 3)
    seen: list[CuratorInputBatch] = []

    def annotate(batch, run_id, deadline):
        seen.append(batch)
        return batch, ()

    service = MemoryCuratorService(
        config=config, dependencies=_dependencies(tmp_path, _ManifestEchoBackend(thread.thread_id), store, annotate),
    )
    result = service.run(reason="admin")

    assert result.status == "succeeded", result.failure_code
    # 标注只能看到已裁进预算的批次，不为超预算的尾部浪费决策调用。
    assert len(seen) == 1
    assert len(curator_prompt(seen[0])) <= config.max_input_chars
    assert len(seen[0].messages) < 80


def test_fit_drops_message_tail_before_audit_and_keeps_prefixes() -> None:
    batch = _batch(messages=6, audits=4)
    full = len(curator_prompt(batch))
    two_messages = len(curator_prompt(replace(batch, messages=batch.messages[:2])))

    fitted, warnings = fit_batch_to_input_budget(batch, max_chars=two_messages)

    assert fitted.messages == batch.messages[:2]
    assert fitted.audit_events == batch.audit_events
    assert len(curator_prompt(fitted)) <= two_messages < full
    assert warnings == ("memory_curator_input_fitted:messages=6->2,audit=4->4",)

    one_each = len(curator_prompt(replace(batch, messages=batch.messages[:1], audit_events=batch.audit_events[:1])))
    fitted, warnings = fit_batch_to_input_budget(batch, max_chars=one_each)
    assert fitted.messages == batch.messages[:1]
    assert fitted.audit_events == batch.audit_events[:1]
    assert warnings == ("memory_curator_input_fitted:messages=6->1,audit=4->1",)


def test_fit_leaves_a_batch_within_budget_untouched() -> None:
    batch = _batch(messages=3, audits=2)

    fitted, warnings = fit_batch_to_input_budget(batch, max_chars=len(curator_prompt(batch)))

    assert fitted is batch
    assert warnings == ()


def test_budget_smaller_than_one_item_keeps_the_typed_budget_failure() -> None:
    batch = _batch(messages=3, audits=2)
    config = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, max_input_chars=2_000)

    fitted, _warnings = fit_batch_to_input_budget(batch, max_chars=config.max_input_chars)

    # 保底各留一条仍超出（预算小于模板本身）：不缩成空批，交回提取前检查报原失败码。
    assert (len(fitted.messages), len(fitted.audit_events)) == (1, 1)
    with pytest.raises(ValueError, match="CURATOR_INPUT_BUDGET_EXCEEDED"):
        extract_with_retries(_ManifestEchoBackend("thread-1"), config, fitted)


def test_budget_failure_does_not_report_previous_attempt_shapes() -> None:
    batch = _batch(messages=2, audits=0)
    roomy = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, timeout_seconds=1, max_retries=0)
    extract_with_retries(_ManifestEchoBackend("thread-1"), roomy, batch)
    assert len(last_model_attempts()) == 1

    with pytest.raises(ValueError, match="CURATOR_INPUT_BUDGET_EXCEEDED"):
        extract_with_retries(_ManifestEchoBackend("thread-1"), replace(roomy, max_input_chars=2_000), batch)

    # 同一线程上一次成功调用的形状不能被当成这次预算失败的证据。
    assert last_model_attempts() == ()
