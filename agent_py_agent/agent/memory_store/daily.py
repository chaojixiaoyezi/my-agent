# LLM: Daily memory is the human-readable work journal layer above raw JSONL archive.
# 模块用途: 写入按天分片的工作记忆，记录进展、教训、下一步和引用，而不是替代 raw archive。

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from ..io import append_jsonl


# LLM: DailyMemoryEvent is deliberately generic so research, coding, monitoring, and chat tasks share one journal shape.
# 类用途: 承载每日工作记忆事件，供 owner/task/run/agent 运行中追加可读摘要。
@dataclass(frozen=True)
class DailyMemoryEvent:
    event_type: str
    summary: str
    task_id: str = ""
    run_id: str = ""
    agent_id: str = ""
    session_id: str = ""
    refs: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_record(self) -> dict[str, object]:
        payload = asdict(self)
        if not payload["created_at"]:
            payload["created_at"] = datetime.now(timezone.utc).isoformat()
        return payload


def daily_memory_path(daily_dir: str | Path, *, day: date | str | None = None) -> Path:
    day_key = day.isoformat() if isinstance(day, date) else day or date.today().isoformat()
    return Path(daily_dir) / f"{day_key}.jsonl"


def append_daily_memory_event(daily_dir: str | Path, event: DailyMemoryEvent) -> Path:
    record = event.to_record()
    path = daily_memory_path(daily_dir, day=str(record["created_at"])[:10])
    append_jsonl(path, record, sort_keys=True)
    return path


__all__ = ["DailyMemoryEvent", "append_daily_memory_event", "daily_memory_path"]
