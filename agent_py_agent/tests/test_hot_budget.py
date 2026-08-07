from __future__ import annotations

"""HOT 注入预算硬顶与写时降级的确定性测试。

覆盖 P0 记忆稳定化:5000 字符硬顶下,注入侧按活跃度截断(新提升+高频在前),
写侧将超预算的冷规则降级回 lessons(lesson 正文不动,零信息丢失)。
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate
from agent_py_agent.agent.memory_store.lessons import (
    HOT_SCHEMA_VERSION,
    LESSON_SCHEMA_VERSION,
    HOT_MAX_INJECT_CHARS,
    HotRuleRecord,
    HotRuleRepository,
    LessonRecord,
    _demote_hot_excess_unlocked,
    _render_hot,
    hot_records_within_budget,
)
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope, hot_memory_records

_NOW = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)


class _ListRepository:
    def __init__(self, records: list[object]) -> None:
        self.records = records

    def list(self) -> list[object]:
        return list(self.records)


def _lesson(now: datetime = _NOW) -> LessonRecord:
    return LessonRecord(
        schema_version=LESSON_SCHEMA_VERSION,
        lesson_id="lesson-1",
        candidate_id="candidate-lesson",
        subject_key="testing.lesson",
        scope={"scope_type": "project", "scope_key": "project:alpha"},
        content="先验证失败路径，再宣称完成。",
        occurrence_count=3,
        evidence_groups=("task:a", "task:b"),
        source_task_ids=("a", "b"),
        source_run_ids=(),
        created_at=(now - timedelta(days=30)).isoformat(),
        updated_at=(now - timedelta(days=30)).isoformat(),
        path="memory/lessons/testing-lesson.md",
    )


def _hot(now: datetime, *, index: int, rule_len: int, occurrences: int = 3) -> HotRuleRecord:
    return HotRuleRecord(
        schema_version=HOT_SCHEMA_VERSION,
        hot_id=f"hot-{index:02d}",
        candidate_id=f"candidate-hot-{index:02d}",
        lesson_id="lesson-1",
        lesson_ref="memory/lessons/testing-lesson.md",
        rule=("规则%d：" % index + "记" * rule_len)[: rule_len],
        occurrence_count=occurrences,
        evidence_groups=("task:a", "task:b"),
        promoted_at=now.isoformat(),
    )


def _hot_pile() -> list[HotRuleRecord]:
    """25 条混合长度 HOT,总字符必超 5000(每条 120~280,合计约 5000+)。"""
    return [
        _hot(
            _NOW - timedelta(days=25 - index),
            index=index,
            rule_len=120 + index * 7,  # 120..288 字符
            occurrences=3 + (index % 8),
        )
        for index in range(25)
    ]


def _hot_candidate(*, content: str) -> MemoryCandidate:
    return MemoryCandidate(
        schema_version="my-agent.candidate.v1",
        candidate_id="candidate-fresh",
        candidate_type="hot_rule",
        content=content,
        subject_key="testing.fresh",
        scope={"scope_type": "project", "scope_key": "project:alpha"},
        origin="reviewed",
        evidence_refs=[],
        source_message_refs=[],
        source_tool_refs=[],
        source_artifact_refs=[],
        source_task_ids=["a", "b"],
        source_run_ids=["r1", "r2"],
        observed_at=_NOW.isoformat(),
        last_observed_at=_NOW.isoformat(),
        valid_from=_NOW.isoformat(),
        valid_until="",
        occurrence_count=4,
        confidence=0.9,
        proposed_action="promote",
        target_entry_id="lesson-1",
        conflicts_with=[],
        promotion_target="hot",
        status="approved",
        reviewer="admin",
        review_note="",
        created_at=_NOW.isoformat(),
        updated_at=_NOW.isoformat(),
        promoted_at="",
        promotion_ref="",
    )


def test_inject_truncates_to_budget_keeping_newest_first() -> None:
    pile = _hot_pile()
    kept = hot_records_within_budget(pile)
    assert kept, "至少保留一条"
    total = sum(len(record.rule or "") for record in kept)
    assert total <= HOT_MAX_INJECT_CHARS
    assert len(kept) < len(pile), "超预算时必须截断"
    # 活跃度排序:新提升的在前(截断保留的是最近活跃的规则)。
    kept_ids = [record.hot_id for record in kept]
    expected = sorted(pile, key=lambda r: (r.promoted_at or "", r.occurrence_count), reverse=True)
    assert kept_ids == [record.hot_id for record in expected[: len(kept)]]


def test_inject_keeps_everything_within_budget() -> None:
    small = [_hot(_NOW, index=index, rule_len=100) for index in range(5)]
    assert hot_records_within_budget(small) == small


def test_demote_cold_rules_rewrites_file_within_budget() -> None:
    path = Path("/tmp/hot-budget-demote.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    repo = HotRuleRepository(path)
    pile = _hot_pile()
    path.write_text(_render_hot(pile), encoding="utf-8")

    demoted = repo.demote_cold_rules()
    assert demoted, "超预算必须降级出结果"
    # 被降级的是最冷的(最早的 promoted_at)。
    cold_ids = {record.hot_id for record in demoted}
    assert "hot-00" in cold_ids and "hot-24" not in cold_ids

    after = repo.list()
    total = sum(len(record.rule or "") for record in after)
    assert total <= HOT_MAX_INJECT_CHARS
    assert {record.hot_id for record in after}.isdisjoint(cold_ids)
    # 幂等:再跑一次无降级。
    assert repo.demote_cold_rules() == []


def test_promote_demotes_excess_and_keeps_new_rule() -> None:
    path = Path("/tmp/hot-budget-promote.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    repo = HotRuleRepository(path)
    path.write_text(_render_hot(_hot_pile()), encoding="utf-8")

    lesson = _lesson()
    candidate = _hot_candidate(content="新规则:提交前必须跑一次全量测试。")
    record = repo.promote(candidate, lesson=lesson)

    after = repo.list()
    assert any(item.hot_id == record.hot_id for item in after), "新规则必须保留"
    total = sum(len(item.rule or "") for item in after)
    assert total <= HOT_MAX_INJECT_CHARS


def test_hot_records_within_budget_survives_empty_and_tiny() -> None:
    assert hot_records_within_budget([]) == []
    one = _hot(_NOW, index=1, rule_len=50)
    assert hot_records_within_budget([one]) == [one]
    assert _demote_hot_excess_unlocked(Path("/tmp/does-not-exist-hot.md")) == []


def test_recall_inject_respects_budget() -> None:
    pile = _hot_pile()
    records = hot_memory_records(
        _ListRepository(pile),
        _ListRepository([_lesson()]),
        scope=MemoryRecallScope(keys=(("project", "project:alpha"),)),
    )
    total = sum(len(record.content or "") for record in records)
    assert total <= HOT_MAX_INJECT_CHARS
    assert len(records) < len(pile), "超预算注入必须被截断"
    injected_ids = [record.entry_id for record in records]
    newest_ids = sorted(
        pile, key=lambda r: (r.promoted_at or "", r.occurrence_count), reverse=True
    )
    assert injected_ids == [record.hot_id for record in newest_ids[: len(records)]]
