"""LLM: create_subagents must be idempotent only for explicit machine contracts.

函数/模块用途: 覆盖真实 E2E 中 root 重复创建同一批小傻妞的问题；只有 idempotency_contract 才能复用已有 run。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _workspace_agent(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    agent = MagicMock()
    agent.config.enable_subagents = True
    agent.config.max_subagents = 10
    agent.subagents = SubAgentManager(tmp_path / ".my-agent" / "subagents", workspace_root=tmp_path)
    return agent


def test_items_mode_reuses_existing_contract_children_and_returns_dispatch_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(
        tool.execute({"goal": "并行生成周度项目报告", "items": _pipeline_items("weekly_star_data.md")}).output
    )
    second = json.loads(
        tool.execute({"goal": "并行生成周度项目报告", "items": _pipeline_items("weekly_data.md")}).output
    )

    assert first["created_run_ids"] == first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["pending_start_run_ids"] == []
    assert second["auto_start"]["run_ids"] == first["created_run_ids"]
    assert second["next_action"]["action"] == "await_lifecycle_event"
    assert len(agent.subagents.list_runs()) == 3


def test_reused_done_children_are_excluded_from_dispatch_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(
        tool.execute({"goal": "并行生成周度项目报告", "items": _pipeline_items("weekly_star_data.md")}).output
    )
    done = agent.subagents.load(first["created_run_ids"][0])
    done.status = "DONE"
    done.verification_status = "VERIFIED"
    agent.subagents.save(done)
    second = json.loads(
        tool.execute({"goal": "并行生成周度项目报告", "items": _pipeline_items("weekly_data.md")}).output
    )

    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["pending_start_run_ids"] == []
    assert second["auto_start"]["run_ids"] == first["created_run_ids"][1:]
    assert second["next_action"]["action"] == "await_lifecycle_event"


def test_generic_single_worker_reuses_explicit_idempotency_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    params = {
        "goal": "在 artifacts/index.html 写一个现代家具品牌首页。",
        "role": "worker",
        "extra_write_roots": [str(tmp_path / "artifacts")],
        "context_packs": [_idempotency_pack("site-output", "artifacts/index.html")],
    }
    first = json.loads(CreateSubagentsTool(agent).execute(params).output)
    second = json.loads(CreateSubagentsTool(agent).execute(params).output)

    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["pending_start_run_ids"] == []
    assert second["auto_start"]["run_ids"] == first["created_run_ids"]
    assert len(agent.subagents.list_runs()) == 1


def test_generic_worker_reuses_system_derived_output_ref_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({
        "goal": "写一个现代家具品牌首页。",
        "role": "worker",
        "input_refs": ["brief.md"],
        "output_files": ["artifacts/index.html"],
        "extra_write_roots": [str(tmp_path / "artifacts")],
    }).output)
    second = json.loads(tool.execute({
        "goal": "把首页做得更高级，仍然输出同一个文件。",
        "role": "worker",
        "input_refs": ["brief.md"],
        "output_files": ["artifacts/index.html"],
        "extra_write_roots": [str(tmp_path / "artifacts")],
    }).output)

    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert len(agent.subagents.list_runs()) == 1


def test_generic_default_name_does_not_reuse_different_goal(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({"goal": "写 index1.html", "role": "worker"}).output)
    second = json.loads(tool.execute({"goal": "写 index2.html", "role": "worker"}).output)

    assert first["created_run_ids"] != second["created_run_ids"]
    assert second["created_run_ids"] == second["created_run_ids"]
    assert second["reused_run_ids"] == []
    assert len(agent.subagents.list_runs()) == 2


def test_unfinished_sibling_with_same_declared_output_does_not_claim_file_ownership(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({
        "goal": "实现参数展开器。",
        "role": "worker",
        "output_files": ["artifacts/params.py"],
    }).output)
    second_result = tool.execute({
        "goal": "重新实现同一个参数展开器。",
        "role": "coding",
        "output_files": ["artifacts/params.py"],
    })
    second = json.loads(second_result.output)

    assert second_result.ok is True
    assert second["created_run_ids"]
    assert second["created_run_ids"] != first["created_run_ids"]
    assert len(agent.subagents.list_runs()) == 2


def test_overlapping_items_are_created_as_codex_style_shared_workspace_workers(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    result = CreateSubagentsTool(agent).execute({
        "goal": "并行实现两个模块。",
        "items": [
            {"goal": "实现第一版。", "output_files": ["artifacts/shared.py"]},
            {"goal": "实现第二版。", "output_files": ["artifacts/shared.py"]},
        ],
    })
    payload = json.loads(result.output)

    assert result.ok is True
    assert len(payload["created_run_ids"]) == 2
    assert len(agent.subagents.list_runs()) == 2


def test_existing_and_batch_output_overlap_remains_coordination_metadata(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({
        "goal": "实现已有模块。",
        "output_files": ["artifacts/existing.py"],
    }).output)

    result = tool.execute({
        "goal": "并行实现三个独立模块。",
        "items": [
            {"goal": "重写已有模块。", "output_files": ["artifacts/existing.py"]},
            {"goal": "实现新模块 A。", "output_files": ["artifacts/shared.py"]},
            {"goal": "实现新模块 B。", "output_files": ["artifacts/shared.py"]},
        ],
    })
    payload = json.loads(result.output)

    assert result.ok is True
    assert first["created_run_ids"]
    assert len(payload["created_run_ids"]) == 3
    assert len(agent.subagents.list_runs()) == 4


def test_explicit_replacement_may_take_over_same_declared_output(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({
        "goal": "实现第一版。",
        "role": "worker",
        "output_files": ["artifacts/replacement.py"],
    }).output)
    second_result = tool.execute({
        "goal": "接管失败的第一版并修好。",
        "role": "worker",
        "output_files": ["artifacts/replacement.py"],
        "replacement_for_run_ids": first["created_run_ids"],
    })
    second = json.loads(second_result.output)

    assert second_result.ok is True
    assert second["created_run_ids"]
    assert second["created_run_ids"] != first["created_run_ids"]
    assert len(agent.subagents.list_runs()) == 2


def test_background_main_may_add_independent_work_to_active_lineage(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    agent._current_run_params = _run_params("request-1", source="foreground")
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({
        "goal": "审计 Codex。",
        "output_files": ["output/codex.md"],
    }).output)

    agent._current_run_params = _run_params("bg-main-1", source="background_main_agent")
    result = tool.execute({
        "goal": "同时审计 Hermes。",
        "output_files": ["output/hermes.md"],
    })
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["created_run_ids"]
    assert payload["created_run_ids"] != first["created_run_ids"]
    assert len(agent.subagents.list_runs()) == 2


def test_background_main_still_reuses_explicit_idempotency_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    agent._current_run_params = _run_params("request-1", source="foreground")
    tool = CreateSubagentsTool(agent)
    params = {
        "goal": "审计 Codex。",
        "output_files": ["output/codex.md"],
        "context_packs": [_idempotency_pack("codex-audit", "output/codex.md")],
    }
    first = json.loads(tool.execute(params).output)

    agent._current_run_params = _run_params("bg-main-1", source="background_main_agent")
    second = json.loads(tool.execute(params).output)

    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert len(agent.subagents.list_runs()) == 1


def test_foreground_user_turn_may_add_independent_work_to_active_lineage(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    agent._current_run_params = _run_params("request-1", source="foreground")
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({
        "goal": "审计 Codex。",
        "output_files": ["output/codex.md"],
    }).output)
    second = tool.execute({
        "goal": "同时审计 Hermes。",
        "output_files": ["output/hermes.md"],
    })

    assert second.ok is True
    assert json.loads(second.output)["created_run_ids"] != first["created_run_ids"]
    assert len(agent.subagents.list_runs()) == 2


def test_background_main_may_create_after_lineage_ends_or_with_explicit_replacement(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    agent._current_run_params = _run_params("request-1", source="foreground")
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({
        "goal": "审计 Codex。",
        "output_files": ["output/codex.md"],
    }).output)
    run = agent.subagents.load(first["created_run_ids"][0])
    run.status = "DONE"
    run.verification_status = "VERIFIED"
    agent.subagents.save(run)

    agent._current_run_params = _run_params("bg-main-1", source="background_main_agent")
    after_done = tool.execute({
        "goal": "审计 Hermes。",
        "output_files": ["output/hermes.md"],
    })
    assert after_done.ok is True

    replacement = tool.execute({
        "goal": "明确接管仍在执行的 Hermes 审计。",
        "output_files": ["output/hermes-replacement.md"],
        "replacement_for_run_ids": json.loads(after_done.output)["created_run_ids"],
    })
    assert replacement.ok is True
    assert len(agent.subagents.list_runs()) == 3


def test_all_reused_done_children_return_non_dispatch_next_action(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(
        tool.execute({"goal": "并行生成周度项目报告", "items": _pipeline_items("weekly_star_data.md")}).output
    )
    for run_id in first["created_run_ids"]:
        task = agent.subagents.load(run_id)
        task.status = "DONE"
        task.verification_status = "VERIFIED"
        agent.subagents.save(task)

    second = json.loads(
        tool.execute({"goal": "并行生成周度项目报告", "items": _pipeline_items("weekly_data.md")}).output
    )

    assert second["pending_start_run_ids"] == []
    assert second["next_action"]["action"] == "report_creation_state"
    assert second["next_action"]["reason"].startswith("create_subagents 没有可启动")


def _pipeline_items(output_name: str) -> list[dict[str, object]]:
    return [
        {
            "agent_name": "小傻妞-数据收集",
            "goal": f"收集 代码平台 star 数据，写到 data/subagents/subagent_data_collection/{output_name}",
            "role": "worker",
            "context_packs": [_idempotency_pack("task18-data-collection", "data_collection")],
        },
        {
            "agent_name": "小傻妞-内容编写",
            "goal": "基于数据收集结果写中文说明，输出 project_explanations.md",
            "role": "worker",
            "context_packs": [_idempotency_pack("task18-explanations", "project_explanations")],
        },
        {
            "agent_name": "小傻妞-生成报告",
            "goal": "整合前两步生成 xlsx 和 final_report.md",
            "role": "worker",
            "context_packs": [_idempotency_pack("task18-final-report", "final_report")],
        },
    ]


def _idempotency_pack(key: str, *scope_refs: str) -> dict[str, object]:
    return {
        "kind": "idempotency_contract",
        "contract": {
            "schema": "subagent_idempotency_contract.v1",
            "kind": "test",
            "idempotency_key": key,
            "scope_refs": list(scope_refs),
        },
    }


def _run_params(run_id: str, *, source: str) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        request_id=run_id,
        task_id=run_id,
        source=source,
        task_attributes={"conversation_task_id": "root-task-1"},
    )
