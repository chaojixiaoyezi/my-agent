"""LLM: create_subagents must be idempotent only for explicit machine contracts.

函数/模块用途: 覆盖真实 E2E 中 root 重复创建同一批小傻妞的问题；只有 idempotency_contract 才能复用已有 run。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


def _workspace_agent(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    agent = MagicMock()
    agent.config.enable_subagents = True
    agent.config.max_subagents = 10
    agent.config.subagent_workflow_mode = "off"
    agent.subagents = SubAgentManager(tmp_path / ".my-agent" / "subagents", workspace_root=tmp_path)
    return agent


def test_items_mode_reuses_existing_contract_children_and_returns_dispatch_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({"items": _pipeline_items("weekly_star_data.md")}).output)
    second = json.loads(tool.execute({"items": _pipeline_items("weekly_data.md")}).output)

    assert first["created_run_ids"] == first["ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["ids"]
    assert second["dispatch_run_ids"] == []
    assert second["auto_start"]["run_ids"] == first["ids"]
    assert second["next_action"]["tool"] == "inspect_agent_tree"
    assert len(agent.subagents.list_runs()) == 3


def test_reused_done_children_are_excluded_from_dispatch_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({"items": _pipeline_items("weekly_star_data.md")}).output)
    done = agent.subagents.load(first["ids"][0])
    done.status = "DONE"
    done.verification_status = "VERIFIED"
    agent.subagents.save(done)
    second = json.loads(tool.execute({"items": _pipeline_items("weekly_data.md")}).output)

    assert second["reused_run_ids"] == first["ids"]
    assert second["dispatch_run_ids"] == []
    assert second["auto_start"]["run_ids"] == first["ids"][1:]
    assert second["next_action"]["tool"] == "inspect_agent_tree"


def test_count_fanout_creates_requested_number_of_sibling_runs(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    payload = json.loads(CreateSubagentsTool(agent).execute({
        "goal": "隔离场景测试：实现 fixture 功能并产出证据",
        "count": 2,
        "role": "worker",
        "agent_name": "小傻妞-隔离测试",
    }).output)

    assert payload["created_run_ids"] == payload["ids"]
    assert len(payload["ids"]) == 2
    assert len(agent.subagents.list_runs()) == 2


def test_items_mode_fixture_worker_names_do_not_trigger_repair_dedupe(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    payload = json.loads(CreateSubagentsTool(agent).execute({
        "items": [
            _fixture_worker_item("fixture-worker-1"),
            _fixture_worker_item("fixture-worker-2"),
        ],
        "count": 2,
    }).output)

    assert payload["created_run_ids"] == payload["ids"]
    assert payload["reused_run_ids"] == []
    assert len(set(payload["ids"])) == 2
    assert len(agent.subagents.list_runs()) == 2


def test_items_mode_indexed_generic_names_create_distinct_siblings(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    payload = json.loads(CreateSubagentsTool(agent).execute({
        "items": [
            _fixture_worker_item("小傻妞-worker-1"),
            _fixture_worker_item("小傻妞-worker-2"),
        ],
        "count": 2,
    }).output)

    assert len(payload["ids"]) == 2
    assert len(set(payload["ids"])) == 2
    assert payload["created_run_ids"] == payload["ids"]
    assert payload["reused_run_ids"] == []


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
    assert second["reused_run_ids"] == first["ids"]
    assert second["dispatch_run_ids"] == []
    assert second["auto_start"]["run_ids"] == first["ids"]
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
    assert second["reused_run_ids"] == first["ids"]
    assert len(agent.subagents.list_runs()) == 1


def test_generic_default_name_does_not_reuse_different_goal(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({"goal": "写 index1.html", "role": "worker"}).output)
    second = json.loads(tool.execute({"goal": "写 index2.html", "role": "worker"}).output)

    assert first["ids"] != second["ids"]
    assert second["created_run_ids"] == second["ids"]
    assert second["reused_run_ids"] == []
    assert len(agent.subagents.list_runs()) == 2


def test_repeated_count_fanout_reuses_indexed_children_with_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    params = {
        "goal": "隔离场景测试：实现 fixture 功能并产出证据",
        "count": 2,
        "role": "worker",
        "agent_name": "小傻妞-隔离测试",
        "context_packs": [_idempotency_pack("fixture-fanout", "fixture-output")],
    }
    first = json.loads(CreateSubagentsTool(agent).execute(params).output)
    second = json.loads(CreateSubagentsTool(agent).execute(params).output)

    assert len(first["ids"]) == 2
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["ids"]
    assert second["dispatch_run_ids"] == []
    assert second["auto_start"]["run_ids"] == first["ids"]
    assert len(agent.subagents.list_runs()) == 2


def test_all_reused_done_children_return_non_dispatch_next_action(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({"items": _pipeline_items("weekly_star_data.md")}).output)
    for run_id in first["ids"]:
        task = agent.subagents.load(run_id)
        task.status = "DONE"
        task.verification_status = "VERIFIED"
        agent.subagents.save(task)

    second = json.loads(tool.execute({"items": _pipeline_items("weekly_data.md")}).output)

    assert second["dispatch_run_ids"] == []
    assert second["next_action"]["tool"] == "inspect_agent_tree"
    assert second["next_action"]["reason"].startswith("create_subagents 没有可调度")


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


def _fixture_worker_item(agent_name: str) -> dict[str, object]:
    return {
        "agent_name": agent_name,
        "goal": "在隔离 fixture 项目中读取 README.md，并在子代理 task_dir/scenario_outputs/ 写入自己的证据报告",
        "role": "worker",
        "acceptance_checks": ["必须有 read_file 证据", "必须有 write_file 证据", "必须等待普通收口"],
        "plan": "读取 README.md；写入 task_dir/scenario_outputs/<run_id>.md；等待收口",
    }


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
