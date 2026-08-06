from __future__ import annotations

import json
from pathlib import Path


def test_append_daily_memory_event_writes_curated_work_journal(tmp_path: Path):
    from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent, append_daily_memory_event

    path = append_daily_memory_event(
        tmp_path,
        DailyMemoryEvent(
            event_type="task_progress",
            summary="完成 home V2 目录初始化",
            actor="main_agent",
            task_id="task-home-v2",
            run_id="run-home-v2",
            artifact_refs=[
                {
                    "artifact_id": "layout-doc",
                    "path": "docs/architecture/MY_AGENT_HOME_LAYOUT.md",
                }
            ],
            lessons=["每日记忆记录可读摘要，raw archive 保留黑盒细节"],
            next_actions=["继续接入 owner resolver"],
            created_at="2026-05-31T01:02:03+00:00",
        ),
    )

    assert path == tmp_path / "2026-05-31.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["summary"] == "完成 home V2 目录初始化"
    assert record["artifact_refs"][0]["artifact_id"] == "layout-doc"
    assert record["sequence"] == 1
    assert record["previous_event_id"] == ""
    assert record["next_actions"] == ["继续接入 owner resolver"]
