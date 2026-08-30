from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import (
    CandidateService,
    CandidateTransitionError,
)
from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent, DailyMemoryStore


def _user_observation(*, message_id: str = "message-1") -> CandidateObservation:
    return CandidateObservation(
        candidate_type="long_term_fact",
        content="个人电脑使用 macOS。",
        subject_key="device.personal.os",
        scope=MemoryScope("personal", "personal"),
        origin="user_explicit",
        source_message_refs=(
            {
                "message_id": message_id,
                "thread_id": "thread-1",
                "role": "user",
                "content_hash": "sha256:example",
                "quote": "个人电脑使用 macOS",
            },
        ),
        observed_at="2026-08-04T01:00:00+00:00",
        confidence=0.99,
        promotion_target="long_term",
    )


def test_candidate_replay_is_idempotent_but_independent_sources_increment(tmp_path: Path):
    service = CandidateService(tmp_path / "candidates.jsonl")

    first = service.observe(_user_observation())
    replay = service.observe(_user_observation())
    independent = service.observe(_user_observation(message_id="message-2"))

    assert first.candidate_id == replay.candidate_id == independent.candidate_id
    assert replay.occurrence_count == 1
    assert independent.occurrence_count == 2
    assert len(service.list()) == 1


def test_candidate_same_host_observation_replay_keeps_id_when_model_paraphrases(
    tmp_path: Path,
) -> None:
    service = CandidateService(tmp_path / "candidates.jsonl")
    original = replace(_user_observation(), observation_id="host:message-1:device-os")
    paraphrase = replace(
        original,
        content="这位用户的个人设备操作系统是 macOS。",
        confidence=0.8,
    )

    first = service.observe(original)
    replay = service.observe(paraphrase)

    assert replay.candidate_id == first.candidate_id
    assert replay.content == first.content
    assert replay.occurrence_count == 1
    assert len(service.list()) == 1


def test_candidate_state_machine_rejects_direct_promotion_and_redaction_remains_valid(
    tmp_path: Path,
):
    service = CandidateService(tmp_path / "candidates.jsonl")
    candidate = service.observe(_user_observation())

    with pytest.raises(CandidateTransitionError):
        service.mark_promoted(
            candidate.candidate_id,
            reviewer="promotion-service",
            promotion_ref="memory-entry-1",
        )

    approved = service.transition(
        candidate.candidate_id,
        "approved",
        reviewer="admin",
        review_note="证据已核验。",
    )
    promoted = service.mark_promoted(
        approved.candidate_id,
        reviewer="promotion-service",
        promotion_ref="memory-entry-1",
    )
    assert promoted.status == "promoted"

    assert service.redact_content(content_hashes={promoted.content_hash}) == 1
    redacted = service.get(promoted.candidate_id)
    assert redacted.content == ""
    assert redacted.content_hash == promoted.content_hash
    assert redacted.content_redacted_at


@pytest.mark.parametrize(
    "terminal_status",
    [
        "rejected",
        "superseded",
        "expired",
        "blocked_missing_evidence",
        "blocked_conflict",
    ],
)
def test_candidate_state_machine_covers_every_review_side_path(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    service = CandidateService(tmp_path / terminal_status / "candidates.jsonl")
    candidate = service.observe(_user_observation())

    transitioned = service.transition(
        candidate.candidate_id,
        terminal_status,
        reviewer="memory-review-test",
        review_note="覆盖统一状态机旁路。",
    )

    assert [event["status"] for event in transitioned.status_history] == [
        "observed",
        "pending_review",
        terminal_status,
    ]
    assert transitioned.reviewer == "memory-review-test"
    with pytest.raises(CandidateTransitionError):
        service.transition(
            candidate.candidate_id,
            "approved",
            reviewer="memory-review-test",
        )


def test_candidate_keeps_large_tool_body_only_in_artifact_authority(tmp_path: Path) -> None:
    service = CandidateService(tmp_path / "candidates.jsonl")
    valid = service.observe(
        CandidateObservation(
            candidate_type="project",
            content="构建产物已生成，完整日志见 artifact。",
            subject_key="project.build.output",
            scope=MemoryScope("project", "project:demo"),
            origin="tool_verified",
            source_tool_refs=({"operation_id": "operation-1", "status": "succeeded"},),
            source_artifact_refs=(
                {
                    "artifact_ref": "artifact://tool-output/1",
                    "sha256": "abc",
                    "size": 2_000_000,
                    "preview": "构建成功",
                },
            ),
            promotion_target="long_term",
        )
    )
    assert valid.source_artifact_refs[0]["size"] == 2_000_000
    assert "完整正文" not in json.dumps(valid.to_record(), ensure_ascii=False)

    with pytest.raises(ValueError, match="raw content"):
        service.observe(
            CandidateObservation(
                candidate_type="project",
                content="非法复制工具正文。",
                subject_key="project.build.raw-output",
                scope=MemoryScope("project", "project:demo"),
                origin="tool_verified",
                source_artifact_refs=({"output": "x" * 10_000},),
                promotion_target="long_term",
            )
        )


def test_daily_store_assigns_authoritative_chain_and_replay_is_idempotent(tmp_path: Path):
    store = DailyMemoryStore(tmp_path / "daily")
    event = DailyMemoryEvent(
        event_type="task_progress",
        summary="完成候选状态机。",
        actor="main_agent",
        origin="model_inferred",
        task_id="task-memory",
        curator_run_id="curator-run-1",
        message_refs=(
            {"message_id": "message-1", "thread_id": "thread-1", "role": "user"},
        ),
        created_at="2026-08-04T01:00:00+00:00",
        extracted_at="2026-08-04T02:00:00+00:00",
    )

    first = store.append(event)
    replay = store.append(event)
    second = store.append(
        DailyMemoryEvent(
            event_type="decision",
            summary="统一使用 owner 级候选账本。",
            actor="main_agent",
            curator_run_id="curator-run-1",
            created_at="2026-08-04T01:01:00+00:00",
            extracted_at="2026-08-04T02:00:00+00:00",
        )
    )

    assert replay.event_id == first.event_id
    assert second.sequence == 2
    assert second.previous_event_id == first.event_id
    assert [item.sequence for item in store.list(day="2026-08-04")] == [1, 2]
    rows = (tmp_path / "daily" / "2026-08-04.jsonl").read_text(encoding="utf-8")
    assert len([line for line in rows.splitlines() if line]) == 2


def test_daily_replay_identity_ignores_model_paraphrase_and_curator_run(tmp_path: Path) -> None:
    store = DailyMemoryStore(tmp_path / "daily")
    original = DailyMemoryEvent(
        event_type="conversation",
        summary="用户说明个人电脑使用 macOS。",
        actor="user",
        origin="user_explicit",
        thread_id="thread-1",
        message_refs=({"message_id": "message-1", "thread_id": "thread-1"},),
        created_at="2026-08-04T01:00:00+00:00",
        curator_run_id="curator-run-before-crash",
    )
    paraphrase = replace(
        original,
        summary="用户明确表示其个人设备运行 macOS。",
        curator_run_id="curator-run-after-restart",
    )

    first = store.append(original)
    replay = store.append(paraphrase)

    assert replay.event_id == first.event_id
    assert replay.summary == first.summary
    assert len(store.list(day="2026-08-04")) == 1


def test_daily_store_rejects_large_output_copied_into_reference(tmp_path: Path):
    store = DailyMemoryStore(tmp_path / "daily")
    event = DailyMemoryEvent(
        event_type="tool_result",
        summary="工具执行成功，完整结果保存在 artifact。",
        actor="tool",
        origin="tool_verified",
        tool_refs=({"tool_call_id": "call-1", "status": "success", "output": "raw"},),
    )

    with pytest.raises(ValueError, match="raw content"):
        store.append(event)


def test_daily_json_contains_summary_and_refs_not_generic_content(tmp_path: Path):
    store = DailyMemoryStore(tmp_path / "daily")
    committed = store.append(
        DailyMemoryEvent(
            event_type="tool_result",
            summary="工具执行成功，详情见 artifact。",
            actor="tool",
            origin="tool_verified",
            tool_refs=({"tool_call_id": "call-1", "status": "success"},),
            artifact_refs=({"artifact_id": "artifact-1", "sha256": "abc", "size": 4096},),
            created_at="2026-08-04T01:00:00+00:00",
        )
    )

    path = tmp_path / "daily" / "2026-08-04.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["event_id"] == committed.event_id
    assert payload["summary"]
    assert "content" not in payload
    assert payload["artifact_refs"][0]["artifact_id"] == "artifact-1"


def test_daily_concurrent_restart_replay_commits_one_event(tmp_path: Path) -> None:
    root = tmp_path / "daily"
    stores = (DailyMemoryStore(root), DailyMemoryStore(root))
    event = DailyMemoryEvent(
        event_type="decision",
        summary="并发提交仍只形成一个每日事件。",
        actor="main_agent",
        origin="model_inferred",
        thread_id="thread-1",
        message_refs=({"message_id": "message-1", "thread_id": "thread-1"},),
        created_at="2026-08-04T03:00:00+00:00",
        extracted_at="2026-08-04T03:01:00+00:00",
        curator_run_id="curator-restart-1",
    )
    barrier = threading.Barrier(2)
    committed: list[DailyMemoryEvent] = []

    def append(store: DailyMemoryStore) -> None:
        barrier.wait(timeout=2)
        committed.append(store.append(event))

    workers = [threading.Thread(target=append, args=(store,)) for store in stores]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)

    restarted = DailyMemoryStore(root)
    rows = restarted.list(day="2026-08-04")
    assert len(committed) == 2
    assert committed[0].event_id == committed[1].event_id == rows[0].event_id
    assert [(item.sequence, item.previous_event_id) for item in rows] == [(1, "")]


# ---- promotion_mode 宿主自主权限合同 ----
# 权限不进入 candidate_id/observation key；模型或旧记录携带的合法值不具备授权力，
# 每次都由 typed target/type/origin/action 重算，SOUL 固定人工。


def test_promotion_mode_is_recomputed_from_host_fields_on_every_replay(
    tmp_path: Path,
) -> None:
    """同一正式事实即使携带旧 manual 值，也由当前宿主策略恢复自主权限。"""
    service = CandidateService(tmp_path / "candidates.jsonl")
    auto = replace(_user_observation(), promotion_mode="auto_eligible")
    manual = replace(auto, promotion_mode="manual_required")

    first = service.observe(auto)
    assert first.promotion_mode == "auto_eligible"
    absorbed = service.observe(manual)
    assert absorbed.promotion_mode == "auto_eligible"
    downgrade_attempt = service.observe(auto)
    assert downgrade_attempt.promotion_mode == "auto_eligible"
    assert downgrade_attempt.candidate_id == first.candidate_id


def test_promotion_mode_not_in_identity_so_replay_keeps_occurrence(tmp_path: Path) -> None:
    """矩阵7:权限不进 candidate_id/observation key——同源同 key 重放不增 occurrence。"""
    service = CandidateService(tmp_path / "candidates.jsonl")
    auto = replace(_user_observation(), promotion_mode="auto_eligible")
    manual = replace(auto, promotion_mode="manual_required")

    first = service.observe(auto)
    replay = service.observe(manual)

    assert replay.occurrence_count == 1
    assert len(replay.observation_keys) == 1


def test_supplied_manual_cannot_disable_host_autonomous_candidate(tmp_path: Path) -> None:
    """promotion_mode 不是模型或旧入口的策略开关，正式用户事实仍按宿主合同自主。"""
    service = CandidateService(tmp_path / "candidates.jsonl")
    manual = replace(_user_observation(), promotion_mode="manual_required")
    auto = replace(manual, promotion_mode="auto_eligible")

    first = service.observe(manual)
    assert first.promotion_mode == "auto_eligible"
    upgraded = service.observe(auto)

    assert upgraded.promotion_mode == "auto_eligible"


def test_legacy_record_without_promotion_mode_uses_current_host_policy(
    tmp_path: Path,
) -> None:
    """旧行缺权限字段时重算策略，但不改 candidate 身份、证据或状态历史。"""
    from agent_py_agent.agent.memory_store.candidate_models import (
        CANDIDATE_SCHEMA_VERSION,
    )

    service = CandidateService(tmp_path / "candidates.jsonl")
    original = service.observe(replace(_user_observation(), promotion_mode="auto_eligible"))
    path = tmp_path / "candidates.jsonl"
    # 模拟旧版本写入的无权限行:从落库记录中删掉 promotion_mode 字段。
    legacy_rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        row.pop("promotion_mode", None)
        legacy_rows.append(row)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in legacy_rows) + "\n",
        encoding="utf-8",
    )

    loaded = service.list()[0]

    assert loaded.promotion_mode == "auto_eligible"
    assert loaded.candidate_id == original.candidate_id  # ID 不重写
    assert loaded.schema_version == CANDIDATE_SCHEMA_VERSION  # 版本不 bump
    assert loaded.source_message_refs == original.source_message_refs  # refs 保留
    assert loaded.status_history == original.status_history  # 状态历史保留
    assert loaded.occurrence_count == 1


def test_soul_candidate_remains_manual_regardless_of_supplied_mode(tmp_path: Path) -> None:
    service = CandidateService(tmp_path / "candidates.jsonl")
    soul = replace(
        _user_observation(),
        candidate_type="soul_change",
        subject_key="persona.soul.voice",
        promotion_target="soul",
        promotion_mode="auto_eligible",
    )

    candidate = service.observe(soul)

    assert candidate.promotion_mode == "manual_required"


def test_invalid_promotion_mode_is_rejected(tmp_path: Path) -> None:
    """定版:非空非法 promotion_mode 严格拒绝(观察入口 + 账本读取两层 fail-closed)。"""
    from agent_py_agent.agent.memory_store.candidates import CandidateStoreCorruptError

    service = CandidateService(tmp_path / "candidates.jsonl")
    with pytest.raises(ValueError, match="promotion_mode"):
        service.observe(replace(_user_observation(), promotion_mode="model_controlled"))

    # 账本读取层:落库后改写一行非法权限 → 整账本拒绝(不静默跳过坏行)。
    service.observe(replace(_user_observation(), promotion_mode="auto_eligible"))
    path = tmp_path / "candidates.jsonl"
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        row["promotion_mode"] = "model_controlled"
        rows.append(row)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CandidateStoreCorruptError, match="failed schema validation"):
        service.list()
