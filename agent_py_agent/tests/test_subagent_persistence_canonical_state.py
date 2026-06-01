from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_subagent_load_prefers_canonical_agent_run_state(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="确认权威状态读取",
        thought="旧 task.json 只做定位，canonical state 才是详细事实。",
        plan=["保存", "修改 canonical", "读取"],
    )
    task.status = "RUNNING"
    manager.save(task)

    canonical_state = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    canonical_payload = json.loads(canonical_state.read_text(encoding="utf-8"))
    canonical_payload["status"] = "BLOCKED"
    canonical_payload["blockers"] = ["canonical 状态优先生效"]
    canonical_state.write_text(json.dumps(canonical_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    legacy_payload = json.loads((tmp_path / task.id / "task.json").read_text(encoding="utf-8"))
    legacy_payload["status"] = "DONE"
    (tmp_path / task.id / "task.json").write_text(
        json.dumps(legacy_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    loaded = manager.load(task.id)

    assert loaded.status == "BLOCKED"
    assert loaded.blockers == ["canonical 状态优先生效"]

