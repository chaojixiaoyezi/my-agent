from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.retention import (
    RETENTION_SCHEMA_VERSION,
    MemoryRetentionService,
)
from agent_py_agent.agent.retrieval.vector_store import VectorStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)
OLD = "2024-01-01T00:00:00+00:00"


class _Embedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, float(len(text) % 7)] for text in texts]


def _runtime(tmp_path: Path, *, conversation_roots: tuple[Path, ...] = ()):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    long_term = JsonlMemory(
        home.owner_memory_long_term_jsonl,
        ops_path=home.owner_memory_ops_jsonl,
        candidate_service=candidates,
    )
    service = MemoryRetentionService(
        home_paths=home,
        candidates=candidates,
        long_term=long_term,
        conversation_roots=conversation_roots,
    )
    return home, candidates, long_term, service


def _set_policy(home, **updates: object) -> dict[str, object]:
    payload = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    payload.update(updates)
    home.owner_retention_json.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return payload


def _task(
    root: Path,
    task_id: str,
    status: str,
    updated_at: str,
    *,
    tool_output: bool = True,
) -> Path:
    task_root = root / "2026-01-01" / task_id
    state = task_root / "work" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {"task_id": task_id, "status": status, "updated_at": updated_at},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (task_root / "output").mkdir(parents=True)
    (task_root / "output" / "result.txt").write_text("结果", encoding="utf-8")
    if tool_output:
        output = task_root / "work" / "blobs" / "tool_outputs" / "large.txt"
        output.parent.mkdir(parents=True)
        output.write_text("大工具输出", encoding="utf-8")
    return task_root


def _old_file(path: Path, content: str = "old") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (1, 1))
    return path


def _reject_old_candidate(candidates: CandidateService):
    candidate = candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="不应继续保留的候选正文。",
            subject_key="retention.rejected",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            promotion_target="long_term",
        )
    )
    rejected = candidates.transition(
        candidate.candidate_id,
        "rejected",
        reviewer="admin",
        review_note="不适合作为长期记忆。",
    )
    old = replace(rejected, updated_at=OLD)
    candidates.path.write_text(
        json.dumps(old.to_record(), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return old


def _conversation(root: Path, *, user: str, at: float):
    store = ConversationStore(root)
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": user,
            "channel": "internal",
            "channel_conversation_id": f"chat-{user}",
            "channel_user_id": user,
            "now": at,
        }
    )
    message = store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": f"{user} 的历史原话",
            "now": at,
        }
    )
    return store, store.load_thread(thread.thread_id), message


def _state(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def test_retention_v2_default_policy_has_all_required_periods(tmp_path: Path):
    home, _candidates, _long_term, service = _runtime(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))

    assert policy["schema_version"] == RETENTION_SCHEMA_VERSION
    assert policy["conversation_days"] == 365
    assert policy["audit_days"] == 180
    assert policy["daily_days"] == 365
    assert policy["tool_output_days_after_terminal"] == 30
    assert policy["rejected_candidate_days"] == 30
    assert policy["curator_run_days"] == 90
    assert policy["compact_days"] == 365
    assert policy["completed_task_days"] == 365
    assert service.plan(now=NOW).ok is True


def test_retention_plan_is_read_only_and_apply_covers_every_authority(tmp_path: Path):
    conversation_root = (
        tmp_path
        / "home"
        / "owners"
        / "local"
        / "main"
        / "workspace"
        / "runtime"
        / "workspaces"
        / "demo"
        / "conversations"
    )
    home, candidates, _long_term, service = _runtime(
        tmp_path,
        conversation_roots=(conversation_root,),
    )
    _set_policy(
        home,
        conversation_days=30,
        audit_days=30,
        daily_days=30,
        tool_output_days_after_terminal=30,
        rejected_candidate_days=30,
        curator_run_days=30,
        compact_days=30,
        completed_task_days=365,
        legal_hold_task_ids=["held-task"],
    )
    old_audit = _old_file(home.owner_audit_dir / "2024-01-01.jsonl", "{}\n")
    fresh_audit = _old_file(home.owner_audit_dir / "fresh.jsonl", "{}\n")
    os.utime(fresh_audit, (NOW.timestamp(), NOW.timestamp()))
    old_daily = _old_file(home.owner_memory_daily_dir / "2024-01-01.jsonl", "{}\n")
    old_curator = _old_file(home.owner_memory_curator_runs_dir / "2024-01-01.jsonl", "{}\n")
    old_compact = _old_file(home.owner_compact_dir / "old.json", "{}")
    rejected = _reject_old_candidate(candidates)

    completed = _task(home.owner_tasks_dir, "completed-task", "DONE", OLD)
    tool_only = _task(
        home.owner_tasks_dir,
        "tool-only-task",
        "DONE",
        "2026-06-01T00:00:00+00:00",
    )
    active = _task(home.owner_tasks_dir, "active-task", "RUNNING", OLD)
    held = _task(home.owner_tasks_dir, "held-task", "DONE", OLD)

    old_store, old_thread, old_message = _conversation(
        conversation_root,
        user="old-user",
        at=datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp(),
    )
    _fresh_store, fresh_thread, _fresh_message = _conversation(
        conversation_root,
        user="fresh-user",
        at=datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp(),
    )
    tracked = [
        old_audit,
        fresh_audit,
        old_daily,
        old_curator,
        old_compact,
        candidates.path,
        completed / "work" / "state.json",
        tool_only / "work" / "blobs" / "tool_outputs" / "large.txt",
        active / "work" / "blobs" / "tool_outputs" / "large.txt",
        held / "work" / "state.json",
        conversation_root / "threads" / f"{old_thread.thread_id}.json",
        conversation_root / "messages" / f"{old_thread.thread_id}.jsonl",
        conversation_root / "threads" / f"{fresh_thread.thread_id}.json",
    ]
    before = _state(tracked)

    plan = service.plan(now=NOW)

    assert plan.ok is True
    assert plan.applied is False
    assert _state(tracked) == before
    categories = {action.category for action in plan.actions}
    assert {
        "audit",
        "daily",
        "curator_run",
        "compact",
        "rejected_candidate",
        "completed_task",
        "tool_output",
        "conversation",
    } <= categories
    assert not any(action.path == active for action in plan.actions)
    assert not any(str(held) in str(action.path) for action in plan.actions)

    applied = service.apply(now=NOW)

    assert applied.ok is True
    assert applied.applied is True
    assert not old_audit.exists()
    assert fresh_audit.exists()
    assert not old_daily.exists()
    assert not old_curator.exists()
    assert not old_compact.exists()
    assert rejected.candidate_id not in {item.candidate_id for item in candidates.list()}
    assert not completed.exists()
    assert tool_only.exists()
    assert not (tool_only / "work" / "blobs" / "tool_outputs").exists()
    assert (active / "work" / "blobs" / "tool_outputs" / "large.txt").exists()
    assert held.exists()
    assert old_store.load_thread(old_thread.thread_id) is None
    assert not (conversation_root / "messages" / f"{old_thread.thread_id}.jsonl").exists()
    assert old_store.load_thread(fresh_thread.thread_id) is not None
    assert old_message.message_id not in (
        conversation_root / "user_latest_threads.json"
    ).read_text(encoding="utf-8")
    assert all(action.status in {"deleted", "trashed"} for action in applied.actions)


def test_global_legal_hold_prevents_every_retention_action(tmp_path: Path):
    home, _candidates, _long_term, service = _runtime(tmp_path)
    old_audit = _old_file(home.owner_audit_dir / "2024-01-01.jsonl", "{}\n")
    _set_policy(home, audit_days=1, legal_hold=True)

    plan = service.plan(now=NOW)
    applied = service.apply(now=NOW)

    assert plan.legal_hold is True
    assert plan.actions == ()
    assert applied.applied is False
    assert applied.legal_hold is True
    assert old_audit.exists()


def test_corrupt_policy_or_task_state_fails_closed_for_unrelated_files(tmp_path: Path):
    home, _candidates, _long_term, service = _runtime(tmp_path)
    old_audit = _old_file(home.owner_audit_dir / "2024-01-01.jsonl", "{}\n")
    policy = _set_policy(home, audit_days="30")

    invalid_policy = service.apply(now=NOW)

    assert invalid_policy.applied is False
    assert {error.error_code for error in invalid_policy.errors} == {
        "MEMORY_RETENTION_POLICY_INVALID"
    }
    assert old_audit.exists()

    policy["audit_days"] = 30
    home.owner_retention_json.write_text(json.dumps(policy), encoding="utf-8")
    state = home.owner_tasks_dir / "2026-01-01" / "broken" / "work" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{broken", encoding="utf-8")

    invalid_state = service.apply(now=NOW)

    assert invalid_state.applied is False
    assert "MEMORY_RETENTION_TASK_STATE_INVALID" in {
        error.error_code for error in invalid_state.errors
    }
    assert old_audit.exists()


def test_apply_revalidates_terminal_state_changed_after_plan(
    tmp_path: Path,
    monkeypatch,
):
    from agent_py_agent.agent.memory_store import retention as retention_module

    home, _candidates, _long_term, service = _runtime(tmp_path)
    task = _task(
        home.owner_tasks_dir,
        "changing-task",
        "DONE",
        "2026-06-01T00:00:00+00:00",
    )
    _set_policy(home, completed_task_days=365, tool_output_days_after_terminal=30)
    state_path = task / "work" / "state.json"
    original = retention_module.execute_retention_plan

    def _change_then_execute(**kwargs):
        state_path.write_text(
            json.dumps(
                {
                    "task_id": "changing-task",
                    "status": "RUNNING",
                    "updated_at": "2026-08-04T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        return original(**kwargs)

    monkeypatch.setattr(retention_module, "execute_retention_plan", _change_then_execute)
    result = service.apply(now=NOW)

    assert result.applied is True
    assert result.ok is False
    assert result.actions[0].status == "state_changed"
    assert "MEMORY_RETENTION_STATE_CHANGED" in {
        error.error_code for error in result.errors
    }
    assert (task / "work" / "blobs" / "tool_outputs" / "large.txt").exists()


def test_nonterminal_task_protects_tool_outputs_even_when_files_are_old(tmp_path: Path):
    home, _candidates, _long_term, service = _runtime(tmp_path)
    task = _task(home.owner_tasks_dir, "running-task", "RUNNING", OLD)
    output = task / "work" / "blobs" / "tool_outputs" / "large.txt"
    os.utime(output, (1, 1))
    _set_policy(home, tool_output_days_after_terminal=1)

    plan = service.plan(now=NOW)
    applied = service.apply(now=NOW)

    assert not any(action.category == "tool_output" for action in plan.actions)
    assert applied.ok is True
    assert output.exists()


def test_hard_delete_scrubs_authority_indexes_candidate_and_not_conversation(tmp_path: Path):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    local = LocalStore(
        tmp_path / "local" / "local.db",
        files_dir=tmp_path / "local" / "files",
        events_path=tmp_path / "local" / "events.jsonl",
        enable_fts=True,
    )
    memory = JsonlMemory(
        home.owner_memory_long_term_jsonl,
        local_store=local,
        ops_path=home.owner_memory_ops_jsonl,
        candidate_service=candidates,
        embedder=_Embedder(),
    )
    content = "需要被完整清除的正式长期记忆。"
    candidate = candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content=content,
            subject_key="privacy.delete",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            promotion_target="long_term",
        )
    )
    record = memory.add(
        "system",
        content,
        kind="fact",
        source=f"candidate:{candidate.candidate_id}",
        attributes={
            "candidate_id": candidate.candidate_id,
            "subject_key": "privacy.delete",
            "scope_type": "personal",
            "scope_key": "personal",
        },
    )
    conversation = tmp_path / "conversation-sentinel.jsonl"
    conversation.write_text("用户历史真实消息\n", encoding="utf-8")
    conversation_before = conversation.read_bytes()
    record_id = local.make_record_id("memory", record.entry_id)
    assert local.get_record(record_id) is not None
    assert record.entry_id in VectorStore(
        home.owner_memory_long_term_dir / "memory_vectors.json"
    ).ids()
    service = MemoryRetentionService(
        home_paths=home,
        candidates=candidates,
        long_term=memory,
    )

    tombstone = service.hard_delete_long_term(
        record.entry_id,
        expected_version=record.version,
    )

    assert tombstone.action == "remove"
    assert memory.all() == []
    assert local.get_record(record_id) is None
    assert not any(path.read_text(encoding="utf-8") == content for path in local.files_dir.glob("*.txt"))
    assert record.entry_id not in VectorStore(
        home.owner_memory_long_term_dir / "memory_vectors.json"
    ).ids()
    redacted = candidates.get(candidate.candidate_id)
    assert redacted.content == ""
    assert redacted.content_hash
    assert "remove" in home.owner_memory_long_term_jsonl.read_text(encoding="utf-8")
    assert content not in home.owner_memory_long_term_jsonl.read_text(encoding="utf-8")
    assert content not in home.owner_memory_ops_jsonl.read_text(encoding="utf-8")
    assert conversation.read_bytes() == conversation_before
