"""原 checkpoint 账本、唯一 CAS 与局部摘要视图的合同测试。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.live_tool_compact import (
    LiveToolCompactBinding,
    LiveToolCompactCommitRequest,
    commit_live_tool_compact,
)
from agent_py_agent.agent.conversation.models import ConversationCompactCommit


@pytest.fixture
def compact_case(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1.0})
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
        backend=SimpleNamespace(name="test", model_name="test-model"),
    )
    policy = RuntimeCompactPolicy(
        context_window_tokens=1000, trigger_percent=90, trigger_tokens=900,
        recovery_target_percent=60, recovery_target_tokens=600,
        allow_persistent_apply=True, recent_tail_max_turns=4, recent_tail_tokens=100,
        failure_threshold=3, failure_cooldown_seconds=300.0,
    )
    clock = [10.0]
    monkeypatch.setattr(checkpoints, "time", SimpleNamespace(time=lambda: clock[0]))
    return SimpleNamespace(store=store, thread_id=thread.thread_id, agent=agent, policy=policy,
                           clock=clock, root=tmp_path)


def _message(case, content):
    return case.store.messages.append({
        "thread_id": case.thread_id, "role": "user", "content": content,
        "now": case.clock[0],
    })


def _commit_transcript(case, *, scope, summary, content, publish, base=None):
    thread = case.store.threads.require(case.thread_id)
    row = _message(case, content)
    checkpoint_id = checkpoints.write_compact_checkpoint(
        case.agent,
        checkpoints.CompactCheckpointRequest(
            thread=thread, summary=summary, operation_evidence={"summary": summary},
            compact_rows=(row,), retained_tail=(), source_end_byte_offset=100 * (thread.compact_generation + 1),
            projected_tokens_before=900, projected_tokens_after=300, policy=case.policy,
            forced=False, scope=scope, summary_base_checkpoint_id=base,
        ),
    )
    updated = case.store.threads.update_compact_state(
        case.thread_id,
        commit=ConversationCompactCommit(
            summary=summary, operation_evidence={"summary": summary}, checkpoint_id=checkpoint_id,
            compacted_through_message_id=row.message_id,
            compacted_through_byte_offset=100 * (thread.compact_generation + 1),
            source_messages=thread.compact_source_messages + 1,
            source_tool_pairs=thread.compact_source_tool_pairs,
            publish_thread_view=publish,
        ),
        expected_generation=thread.compact_generation, now=case.clock[0],
    )
    return updated, row, checkpoint_id


def _checkpoint_path(case):
    return case.root / "compact" / "conversations" / f"{case.thread_id}.jsonl"


def _replace_head_record(case, change):
    """在测试中模拟已提交账本损坏，同时保持 v3 内容身份一致。"""
    path = _checkpoint_path(case)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    change(rows[-1])
    new_id = checkpoints.scoped_compact_checkpoint_id(rows[-1])
    rows[-1]["checkpoint_id"] = new_id
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    case.store.threads.update_atomic(
        case.thread_id, lambda current: replace(current, compact_checkpoint_id=new_id),
    )


def test_interleaved_task_summaries_follow_only_applicable_bases(compact_case):
    case = compact_case
    task_a = CompactScope(kind="task", task_id="A", task_ids=("A",),
                          anchor_message_id="anchor-A", created_at=15.0)
    task_b = CompactScope(kind="task", task_id="B", task_ids=("B",),
                          anchor_message_id="anchor-B", created_at=35.0)
    first, message_1, checkpoint_1 = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="thread-1", content="m1", publish=True,
    )
    assert first.summary == "thread-1" and first.compacted_through_message_id == message_1.message_id
    assert first.compact_updated_at == 10.0

    case.clock[0] = 20.0
    second, message_a1, checkpoint_a1 = _commit_transcript(
        case, scope=task_a, summary="A-1", content="A1", publish=False, base=checkpoint_1,
    )
    assert second.compact_generation == 2 and second.compact_checkpoint_id == checkpoint_a1
    assert (second.summary, second.compacted_through_message_id, second.compacted_through_byte_offset,
            second.compact_updated_at, second.compact_operation_evidence) == (
                first.summary, first.compacted_through_message_id, first.compacted_through_byte_offset,
                first.compact_updated_at, first.compact_operation_evidence,
            )

    case.clock[0] = 30.0
    third, message_2, checkpoint_2 = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="thread-2", content="m2", publish=True,
        base=checkpoint_1,
    )
    assert third.summary == "thread-2" and third.compacted_through_message_id == message_2.message_id

    case.clock[0] = 40.0
    _fourth, message_b1, checkpoint_b1 = _commit_transcript(
        case, scope=task_b, summary="B-1", content="B1", publish=False, base=checkpoint_2,
    )
    case.clock[0] = 50.0
    fifth, message_a2, checkpoint_a2 = _commit_transcript(
        case, scope=task_a, summary="A-2", content="A2", publish=False, base=checkpoint_a1,
    )

    thread_view = resolve_compact_summary_view(case.agent, fifth, THREAD_COMPACT_SCOPE)
    a_view = resolve_compact_summary_view(case.agent, fifth, task_a)
    b_view = resolve_compact_summary_view(case.agent, fifth, task_b)
    assert (thread_view.checkpoint_id, thread_view.summary, thread_view.source_message_ids) == (
        checkpoint_2, "thread-2", frozenset({message_1.message_id, message_2.message_id}),
    )
    assert (a_view.checkpoint_id, a_view.summary, a_view.source_message_ids) == (
        checkpoint_a2, "A-2", frozenset({message_1.message_id, message_a1.message_id, message_a2.message_id}),
    )
    assert (b_view.checkpoint_id, b_view.summary, b_view.source_message_ids) == (
        checkpoint_b1, "B-1", frozenset({message_1.message_id, message_2.message_id, message_b1.message_id}),
    )
    assert fifth.summary == "thread-2" and fifth.compacted_through_message_id == message_2.message_id
    assert fifth.compact_updated_at == 30.0 and fifth.compact_generation == 5


def test_candidate_identity_scope_coverage_orphan_and_losing_cas(compact_case):
    case = compact_case
    task_a = CompactScope(kind="task", task_id="A", created_at=15.0)
    task_b = CompactScope(kind="task", task_id="B", created_at=15.0)
    first, _row, first_id = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="base", content="base", publish=True,
    )
    case.clock[0] = 20.0
    prepared = first
    candidate_ids = []
    for scope, content in ((task_a, "A"), (task_b, "A"), (task_a, "B")):
        row = _message(case, content)
        candidate_ids.append(checkpoints.write_compact_checkpoint(
            case.agent,
            checkpoints.CompactCheckpointRequest(
                thread=prepared, summary="same summary", operation_evidence={},
                compact_rows=(row,), retained_tail=(), source_end_byte_offset=200,
                projected_tokens_before=900, projected_tokens_after=300, policy=case.policy,
                forced=False, scope=scope, summary_base_checkpoint_id=first_id,
            ),
        ))
    assert len(set(candidate_ids)) == 3
    assert resolve_compact_summary_view(case.agent, first, task_a).checkpoint_id == first_id
    assert resolve_compact_summary_view(case.agent, first, THREAD_COMPACT_SCOPE).checkpoint_id == first_id

    winner = case.store.threads.update_compact_state(
        case.thread_id,
        commit=ConversationCompactCommit(
            summary="same summary", operation_evidence={}, checkpoint_id=candidate_ids[0],
            compacted_through_message_id="ignored", compacted_through_byte_offset=200,
            source_messages=2, source_tool_pairs=0, publish_thread_view=False,
        ),
        expected_generation=1, now=20.0,
    )
    with pytest.raises(RuntimeError, match="generation changed"):
        case.store.threads.update_compact_state(
            case.thread_id,
            commit=ConversationCompactCommit(
                summary="loser", operation_evidence={}, checkpoint_id=candidate_ids[1],
                compacted_through_message_id="ignored", compacted_through_byte_offset=200,
                source_messages=2, source_tool_pairs=0, publish_thread_view=False,
            ),
            expected_generation=1, now=21.0,
        )
    current = case.store.threads.require(case.thread_id)
    assert current == winner and current.compact_checkpoint_id == candidate_ids[0]
    assert resolve_compact_summary_view(case.agent, current, task_a).checkpoint_id == candidate_ids[0]
    assert resolve_compact_summary_view(case.agent, current, task_b).checkpoint_id == first_id
    assert resolve_compact_summary_view(case.agent, current, THREAD_COMPACT_SCOPE).checkpoint_id == first_id


def test_live_checkpoint_preserves_precise_tool_identity(compact_case):
    case = compact_case
    turn = CompactScope(kind="turn", turn_id="turn-1", task_id="A")
    thread = case.store.threads.require(case.thread_id)
    request = dict(
        thread=thread, summary="tool summary", source_tool_call_ids=("same-call",),
        retained_tool_call_ids=(), projected_tokens_before=900, projected_tokens_after=300,
        policy=case.policy, request_id="summary-request", attempt_id="summary-attempt",
        scope=turn,
    )
    first_id = checkpoints.write_live_tool_compact_checkpoint(
        case.agent, checkpoints.LiveToolCompactCheckpointRequest(
            **request, source_tool_refs=({"run_id": "run-1", "attempt_id": "source-attempt",
                                          "turn_id": "turn-1", "call_id": "same-call"},),
        ),
    )
    other_id = checkpoints.write_live_tool_compact_checkpoint(
        case.agent, checkpoints.LiveToolCompactCheckpointRequest(
            **request, source_tool_refs=({"run_id": "run-2", "attempt_id": "source-attempt",
                                          "turn_id": "turn-1", "call_id": "same-call"},),
        ),
    )
    assert first_id != other_id
    current = case.store.threads.update_compact_state(
        case.thread_id,
        commit=ConversationCompactCommit(
            summary="tool summary", operation_evidence={}, checkpoint_id=first_id,
            compacted_through_message_id="", compacted_through_byte_offset=0,
            source_messages=0, source_tool_pairs=1, publish_thread_view=False,
        ),
        expected_generation=0, now=11.0,
    )
    view = resolve_compact_summary_view(case.agent, current, turn)
    assert view.checkpoint_id == first_id
    assert view.source_tool_refs == ({"run_id": "run-1", "attempt_id": "source-attempt",
                                      "turn_id": "turn-1", "call_id": "same-call"},)
    assert resolve_compact_summary_view(case.agent, current, THREAD_COMPACT_SCOPE).checkpoint_id == ""
    assert resolve_compact_summary_view(case.agent, current, CompactScope(kind="turn", turn_id="turn-2")).checkpoint_id == ""


def test_same_call_id_in_distinct_attempts_keeps_source_and_retained_boundaries(compact_case):
    case = compact_case
    turn = CompactScope(kind="turn", turn_id="current-turn")
    source_refs = (
        {"run_id": "same-run", "attempt_id": "attempt-1", "turn_id": "turn-1", "call_id": "reused"},
        {"run_id": "same-run", "attempt_id": "attempt-2", "turn_id": "turn-2", "call_id": "reused"},
    )
    retained_refs = (
        {"run_id": "same-run", "attempt_id": "attempt-3", "turn_id": "turn-3", "call_id": "reused"},
    )
    before = case.store.threads.require(case.thread_id)
    current = commit_live_tool_compact(
        case.agent,
        LiveToolCompactBinding(store=case.store, thread=before, scope=turn),
        LiveToolCompactCommitRequest(
            summary="two replaced calls", source_tool_call_ids=("reused", "reused"),
            retained_tool_call_ids=("reused",), projected_tokens_before=900,
            projected_tokens_after=300, policy=case.policy, request_id="summary-request",
            attempt_id="summary-attempt", source_tool_refs=source_refs,
            retained_tool_refs=retained_refs,
        ),
    )
    assert current.compact_generation == 1 and current.compact_source_tool_pairs == 2
    assert current.summary == "" and current.compacted_through_message_id == ""
    committed = checkpoints.committed_compact_checkpoint_chain(case.agent, current)
    assert len(committed) == 1
    assert committed[0]["source_tool_call_ids"] == ["reused", "reused"]
    assert committed[0]["retained_tool_call_ids"] == ["reused"]
    assert tuple(committed[0]["source_tool_refs"]) == source_refs
    assert tuple(committed[0]["retained_tool_refs"]) == retained_refs
    view = resolve_compact_summary_view(case.agent, current, turn)
    assert view.source_tool_refs == source_refs
    assert retained_refs[0] not in view.source_tool_refs
    assert resolve_compact_summary_view(case.agent, current, THREAD_COMPACT_SCOPE).checkpoint_id == ""


def test_v3_turn_checkpoint_cannot_be_downgraded_to_legacy_thread(compact_case):
    case = compact_case
    turn = CompactScope(kind="turn", turn_id="audit-turn")
    committed, _message_row, checkpoint_id = _commit_transcript(
        case, scope=turn, summary="private turn", content="audit only", publish=False,
    )
    assert checkpoint_id.startswith("compact-v3-")
    path = _checkpoint_path(case)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[-1]["schema"] = "conversation_compact_checkpoint.v2"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    assert case.store.threads.require(case.thread_id).compact_checkpoint_id == checkpoint_id
    with pytest.raises(OSError, match="version identity"):
        resolve_compact_summary_view(case.agent, committed, THREAD_COMPACT_SCOPE)


def test_writer_rejects_wrong_summary_base(compact_case):
    case = compact_case
    thread, _row, _id = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="base", content="base", publish=True,
    )
    task = CompactScope(kind="task", task_id="A", created_at=15.0)
    case.clock[0] = 20.0
    row = _message(case, "A")
    with pytest.raises(ValueError, match="summary base changed"):
        checkpoints.write_compact_checkpoint(
            case.agent, checkpoints.CompactCheckpointRequest(
                thread=thread, summary="A", operation_evidence={}, compact_rows=(row,),
                retained_tail=(), source_end_byte_offset=200, projected_tokens_before=900,
                projected_tokens_after=300, policy=case.policy, forced=False, scope=task,
                summary_base_checkpoint_id="missing",
            ),
        )


@pytest.mark.parametrize("tamper", ["base", "digest", "coverage"])
def test_resealed_bad_checkpoint_is_rejected(compact_case, tamper):
    case = compact_case
    _first, _row, _id = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="base", content="base", publish=True,
    )
    task_a = CompactScope(kind="task", task_id="A", created_at=15.0)
    case.clock[0] = 20.0
    _second, _row, task_id = _commit_transcript(
        case, scope=task_a, summary="A", content="A", publish=False,
    )
    case.clock[0] = 30.0
    thread, _row, _id = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="thread", content="thread", publish=True,
    )
    def change(row):
        if tamper == "base":
            row["summary_base_checkpoint_id"] = task_id
        elif tamper == "digest":
            row["summary_sha256"] = "0" * 64
        else:
            row["source_message_ids"] = [42]

    _replace_head_record(case, change)
    with pytest.raises(OSError):
        resolve_compact_summary_view(case.agent, case.store.threads.require(case.thread_id), THREAD_COMPACT_SCOPE)
    assert thread.compact_generation == 3


def test_legacy_v1_v2_use_original_thread_semantics(compact_case):
    case = compact_case
    path = _checkpoint_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    old_message = _message(case, "legacy transcript")
    common = {
        "event": "conversation_compact_checkpoint",
        "status": "validated_candidate",
        "commit_authority": "conversation_thread.compact_checkpoint_id",
        "source_start_byte_offset": 0,
        "projected_tokens_before": 900,
        "projected_tokens_after": 300,
        "context_window_tokens": 1000,
        "trigger_percent": 90,
        "trigger_tokens": 900,
        "recovery_target_tokens": 600,
        "forced": False,
        "backend": "test",
        "model": "test-model",
    }
    old_rows = [
        {
            **common,
            "schema": "conversation_compact_checkpoint.v1", "checkpoint_id": "old-v1",
            "thread_id": case.thread_id, "generation": 1, "previous_generation": 0,
            "previous_checkpoint_id": "", "created_at": 10.0,
            "source_kind": "transcript", "source_start_message_id": old_message.message_id,
            "source_end_message_id": old_message.message_id, "source_end_byte_offset": 10,
            "source_messages": 1, "source_messages_total": 1,
            "source_tool_pairs": 0, "source_tool_pairs_total": 0,
            "retained_tail_start_message_id": "", "retained_tail_end_message_id": "",
            "retained_tail_message_ids": [], "retained_tail_messages": 0,
            "summary": "old transcript",
            "summary_sha256": hashlib.sha256(b"old transcript").hexdigest(),
            "operation_evidence": {"old": 1},
        },
        {
            **common,
            "schema": "conversation_compact_checkpoint.v2", "checkpoint_id": "old-v2",
            "thread_id": case.thread_id, "generation": 2, "previous_generation": 1,
            "previous_checkpoint_id": "old-v1",
            "created_at": 20.0, "source_kind": "live_tool_ir", "source_tool_call_ids": ["old-call"],
            "source_start_message_id": "", "source_end_message_id": "",
            "source_start_byte_offset": 10, "source_end_byte_offset": 10,
            "source_messages": 0, "source_messages_total": 1,
            "source_tool_pairs": 1, "source_tool_pairs_total": 1,
            "retained_tool_call_ids": [], "retained_tool_pairs": 0,
            "request_id": "old-request", "attempt_id": "old-attempt",
            "summary": "old tools", "summary_sha256": hashlib.sha256(b"old tools").hexdigest(),
            "operation_evidence": {"old": 2},
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in old_rows), encoding="utf-8")
    case.store.threads.update_compact_state(
        case.thread_id,
        commit=ConversationCompactCommit(
            summary="old transcript", operation_evidence={}, checkpoint_id="old-v1",
            compacted_through_message_id=old_message.message_id, compacted_through_byte_offset=10,
            source_messages=1, source_tool_pairs=0,
        ), expected_generation=0, now=10.0,
    )
    thread = case.store.threads.update_compact_state(
        case.thread_id,
        commit=ConversationCompactCommit(
            summary="old tools", operation_evidence={}, checkpoint_id="old-v2",
            compacted_through_message_id=old_message.message_id, compacted_through_byte_offset=10,
            source_messages=1, source_tool_pairs=1,
        ), expected_generation=1, now=20.0,
    )
    thread_view = resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
    task = CompactScope(kind="task", task_id="A", created_at=15.0)
    task_view = resolve_compact_summary_view(case.agent, thread, task)
    assert thread_view.checkpoint_id == "old-v2" and thread_view.legacy_message_end_ids == (old_message.message_id,)
    assert thread_view.source_message_ids == frozenset()
    assert thread_view.source_tool_refs == ({
        "legacy": True, "call_id": "old-call", "request_id": "old-request",
        "attempt_id": "old-attempt",
    },)
    assert task_view.checkpoint_id == "old-v1" and task_view.source_tool_refs == ()
    assert task_view.legacy_message_end_ids == (old_message.message_id,)
    assert resolve_compact_summary_view(case.agent, thread, CompactScope(kind="turn", turn_id="audit")).checkpoint_id == ""


def test_scope_payload_rejects_missing_or_extra_identity_fields():
    scope = CompactScope(kind="task", task_id="A", task_ids=("A",),
                         anchor_message_id="m", created_at=15.0)
    assert CompactScope.from_dict(scope.to_dict()) == scope
    with pytest.raises(ValueError):
        CompactScope.from_dict({**scope.to_dict(), "other": "untrusted"})
    with pytest.raises(ValueError):
        CompactScope.from_dict({"kind": "task", "task_id": "A"})


@pytest.mark.parametrize("schema", ["conversation_compact_checkpoint.v1", "conversation_compact_checkpoint.v2"])
def test_legacy_summary_corruption_cannot_be_used_as_base(compact_case, schema):
    case = compact_case
    row = {
        "schema": schema, "checkpoint_id": "legacy-checkpoint", "thread_id": case.thread_id,
        "generation": 1, "previous_checkpoint_id": "", "created_at": 10.0,
        "summary": "corrupted old body", "summary_sha256": hashlib.sha256(b"original body").hexdigest(),
        "operation_evidence": {}, "source_kind": "transcript" if schema.endswith("v1") else "live_tool_ir",
        "source_end_message_id": "legacy-end", "source_tool_call_ids": ["legacy-call"],
        "request_id": "legacy-request", "attempt_id": "legacy-attempt",
    }
    path = _checkpoint_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    thread = case.store.threads.update_compact_state(case.thread_id, commit=ConversationCompactCommit(
        summary="original body", operation_evidence={}, checkpoint_id="legacy-checkpoint",
        compacted_through_message_id="", compacted_through_byte_offset=0,
        source_messages=0, source_tool_pairs=0,
    ), expected_generation=0)
    with pytest.raises(OSError, match="digest"):
        resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
