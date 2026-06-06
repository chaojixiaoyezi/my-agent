from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.agent_core.orchestration.child_result_index import child_result_index
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentBoardOptions
from agent_py_agent.agent.subagents.services.agent_run_state import STATE_LOCATOR_SCHEMA_VERSION


def test_subagent_load_prefers_canonical_agent_run_state(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="确认权威状态读取",
        thought="旧 task.json 只做定位，canonical state 才是详细事实。",
        plan=["保存", "修改 canonical", "读取"],
    )
    task.status = "RUNNING"
    manager.save(task)

    locator_payload = json.loads((tmp_path / task.id / "task.json").read_text(encoding="utf-8"))
    assert locator_payload["schema_version"] == STATE_LOCATOR_SCHEMA_VERSION
    assert "quality_contract" not in locator_payload
    assert "status" not in locator_payload

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


def test_subagent_projections_read_canonical_state_not_legacy_locator(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="投影统一事实源",
        thought="tree、board、child index 都只能信 canonical。",
        plan=["保存", "污染 locator", "读取投影"],
    )
    task.status = "RUNNING"
    task.progress = 0.2
    task.current_tool = "read_file"
    task.artifact_refs = ["draft.md"]
    manager.save(task)

    canonical_state = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    canonical_payload = json.loads(canonical_state.read_text(encoding="utf-8"))
    canonical_payload.update({
        "status": "DONE",
        "progress": 1.0,
        "current_tool": "write_file",
        "artifact_refs": ["final.md"],
        "attributes": {
            **dict(canonical_payload.get("attributes") or {}),
            "artifact_registry_refs": [{"artifact_id": "art-final", "path": "final.md"}],
        },
    })
    canonical_state.write_text(json.dumps(canonical_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    locator = json.loads((tmp_path / task.id / "task.json").read_text(encoding="utf-8"))
    locator.update({"status": "FAILED", "artifact_refs": ["stale.md"], "progress": 0.0})
    (tmp_path / task.id / "task.json").write_text(
        json.dumps(locator, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    loaded = manager.load(task.id)
    snapshot = manager.kernel_snapshot()
    board = manager.board.build_board(options=SubAgentBoardOptions(include_child_status_counts=False))
    index = child_result_index(object(), [loaded])

    assert loaded.status == "DONE"
    assert snapshot.runs[0].status == "DONE"
    assert snapshot.runs[0].artifact_refs == ["final.md"]
    assert board.items[0].status == "DONE"
    assert board.items[0].artifact_refs == ["final.md"]
    assert index[0]["primary_artifact_refs"] == ["final.md"]
    assert index[0]["artifact_registry_refs"][0]["artifact_id"] == "art-final"


def test_child_result_index_surfaces_broken_output_json(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="坏 output.json 不能被伪装成没有结果",
        thought="child result index 要暴露可恢复读取错误。",
        plan=["写坏 JSON", "读取索引"],
    )
    Path(task.output_json).write_text("{ broken", encoding="utf-8")

    index = child_result_index(object(), [task])

    assert index[0]["run_id"] == task.id
    assert index[0]["output_load_error"]["context"] == "child_result.output_json"
    assert index[0]["output_load_error"]["category"] == "data_parse"
    assert "读取失败" in index[0]["output_load_error"]["model_message"]
