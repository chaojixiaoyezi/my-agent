from __future__ import annotations

import json
from pathlib import Path


# LLM: skill candidates are learning suggestions, not automatic new capabilities.
# 函数用途: 验证 owner 私有 skill 候选只进入 drafts 账本，不会安装为正式 skill。
def test_append_owner_skill_candidate_writes_draft_only(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.skill_candidates import (
        SkillCandidate,
        append_owner_skill_candidate,
    )

    paths = ensure_my_agent_home(tmp_path)
    result = append_owner_skill_candidate(
        paths,
        SkillCandidate(
            title="长任务分项目对比",
            summary="多项目分析时先列覆盖清单，再逐项写证据。",
            source_task_id="task-1",
            evidence_refs=["tasks/task-1/report.md"],
        ),
    )

    assert result.path == paths.owner_home_dir / "skills" / ".drafts" / "skill_candidates.jsonl"
    row = json.loads(result.path.read_text(encoding="utf-8").strip())
    assert row["promotion_status"] == "candidate"
    assert row["title"] == "长任务分项目对比"
    assert not (paths.owner_home_dir / "skills" / "长任务分项目对比").exists()
