"""正式 Lesson 陈旧提示只读持久 metadata.updated_at，不依赖文件 mtime。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_py_agent.agent.memory_store.lessons import LESSON_SCHEMA_VERSION, LessonRecord
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope, routed_lesson_records


class _Repository:
    def __init__(self, record: LessonRecord) -> None:
        self.record = record

    def list(self):
        return [self.record]


def _lesson(updated_at: str) -> LessonRecord:
    return LessonRecord(
        schema_version=LESSON_SCHEMA_VERSION,
        lesson_id="lesson-1",
        candidate_id="candidate-1",
        subject_key="lesson.age",
        scope={"scope_type": "global", "scope_key": "global"},
        content="历史教训正文。",
        occurrence_count=2,
        evidence_groups=("task:a", "task:b"),
        source_task_ids=("a", "b"),
        source_run_ids=(),
        created_at=updated_at,
        updated_at=updated_at,
        path="memory/lessons/age.md",
    )


def _recall(record: LessonRecord, *, now: datetime, stale_days: float = 7.0):
    return routed_lesson_records(
        _Repository(record),
        read_paths=[record.path],
        scope=MemoryRecallScope.from_runtime(),
        stale_days=stale_days,
        now=now,
    )[0].content


def test_fresh_lesson_has_no_caveat() -> None:
    now = datetime.now(timezone.utc)

    assert "memory-age-caveat" not in _recall(_lesson((now - timedelta(days=1)).isoformat()), now=now)


def test_stale_lesson_gets_caveat() -> None:
    now = datetime.now(timezone.utc)
    content = _recall(_lesson((now - timedelta(days=10)).isoformat()), now=now)

    assert "memory-age-caveat" in content
    assert "10 天" in content


def test_invalid_metadata_time_gets_fail_closed_caveat() -> None:
    content = _recall(_lesson("not-an-iso-time"), now=datetime.now(timezone.utc))

    assert "更新时间不可验证" in content


def test_custom_stale_threshold() -> None:
    now = datetime.now(timezone.utc)
    lesson = _lesson((now - timedelta(days=3)).isoformat())

    assert "memory-age-caveat" in _recall(lesson, now=now, stale_days=2.0)
    assert "memory-age-caveat" not in _recall(lesson, now=now, stale_days=5.0)


def test_zero_threshold_disables_caveat() -> None:
    now = datetime.now(timezone.utc)

    assert "memory-age-caveat" not in _recall(
        _lesson((now - timedelta(days=100)).isoformat()),
        now=now,
        stale_days=0.0,
    )
