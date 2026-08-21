"""Focused tests for create_subagents output-root binding."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _mock_workspace_agent(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.subagents = SubAgentManager(tmp_path / ".my-agent" / "subagents", workspace_root=tmp_path)
    return mock_agent


def test_items_mode_payload_rebinds_stale_self_output_run_id(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    stale_id = "subagent-0000000000-stale"
    mock_agent = _mock_workspace_agent(tmp_path)

    result = CreateSubagentsTool(mock_agent).execute({
        "goal": "收集项目数据",
        "items": [{
            "goal": "收集项目数据。",
            "output_files": [f"data/subagents/{stale_id}/data_collection.md"],
            "agent_name": "小傻妞-数据收集",
            "role": "worker",
        }],
    })
    payload = json.loads(result.output)
    run_id = payload["created_run_ids"][0]
    loaded = mock_agent.subagents.load(run_id)

    assert result.ok is True
    assert stale_id not in payload["tasks"][0]["attributes"]["output_files"][0]
    assert payload["tasks"][0]["attributes"]["output_files"][0].endswith("data_collection.md")
    assert loaded.attributes["output_ref_rebindings"][0]["to"].endswith("/data_collection.md")


def test_structured_output_worker_defaults_to_workspace_root():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    mock_task = MagicMock()
    mock_task.id = "writer_001"
    mock_task.goal = ""
    mock_task.status = "PLANNING"
    mock_task.verification_status = "UNVERIFIED"
    mock_task.task_dir = "/tmp/writer_001"
    mock_agent.subagents.create_run.return_value = mock_task

    result = CreateSubagentsTool(mock_agent).execute({
        "goal": "生成一个完整文件。",
        "role": "writer",
        "output_files": ["index.html"],
    })
    params = mock_agent.subagents.create_run.call_args.kwargs["params"]

    assert result.ok is True
    assert params.extra_write_roots == [str(Path("/tmp/project").resolve(strict=False))]


def test_ordinary_child_inherits_workspace_without_output_files():
    """普通 child 不需重复声明 output_files 才能写父工作区。"""
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        resolved_extra_write_roots,
    )

    agent = MagicMock()
    agent._current_run_params = None
    agent.subagents.workspace_root = Path("/tmp/project")
    agent.subagents.workspace_roots = [Path("/tmp/project")]

    roots = resolved_extra_write_roots(agent, {"goal": "实现页面"}, "实现页面")

    assert roots == [str(Path("/tmp/project").resolve(strict=False))]


def test_descendant_inherits_only_direct_parent_product_roots():
    """孙代理从直接父级继承窄写区，不回退到 owner 大根。"""
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        resolved_extra_write_roots,
    )

    parent_dir = Path("/tmp/task-parent")
    parent = SimpleNamespace(
        id="subagent-parent",
        task_dir=str(parent_dir),
        agent_run_workspace_dir=str(parent_dir / "run"),
        task_workspace_dir=str(parent_dir.parent),
        allowed_write_roots=[str(parent_dir), "/tmp/project/narrow"],
    )
    agent = MagicMock()
    agent._current_run_params = SimpleNamespace(run_id=parent.id)
    agent.subagents.load.side_effect = lambda run_id: parent if run_id == parent.id else None
    agent.subagents.workspace_root = Path("/tmp/project")
    agent.subagents.workspace_roots = [Path("/tmp/project")]

    roots = resolved_extra_write_roots(agent, {"goal": "继续实现"}, "继续实现")

    assert roots == [str(Path("/tmp/project/narrow").resolve(strict=False))]


def test_descendant_workspace_inheritance_prefers_runner_identity():
    """并发 runner 的线程身份优先，不能被外层 Gateway 请求身份带偏。"""
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        resolved_extra_write_roots,
    )
    from agent_py_agent.agent.agent_core.runner.context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    parent_dir = Path("/tmp/task-parent")
    parent = SimpleNamespace(
        id="subagent-parent",
        task_dir=str(parent_dir),
        agent_run_workspace_dir=str(parent_dir / "run"),
        task_workspace_dir=str(parent_dir.parent),
        allowed_write_roots=["/tmp/project/narrow"],
    )
    agent = MagicMock()
    agent._current_run_params = SimpleNamespace(run_id="gateway-root-request")
    agent.subagents.load.side_effect = (
        lambda run_id: parent if run_id == parent.id else (_ for _ in ()).throw(FileNotFoundError(run_id))
    )
    agent.subagents.workspace_root = Path("/tmp/project")
    agent.subagents.workspace_roots = [Path("/tmp/project")]
    previous = set_current_subagent_context(agent, run_id=parent.id)
    try:
        roots = resolved_extra_write_roots(agent, {"goal": "继续实现"}, "继续实现")
    finally:
        restore_current_subagent_context(agent, previous)

    assert roots == [str(Path("/tmp/project/narrow").resolve(strict=False))]


def test_exact_scope_child_does_not_inherit_parent_workspace():
    """Audit/exact worker 仍保持精确空写根。"""
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        resolved_extra_write_roots,
    )

    agent = MagicMock()
    agent._current_run_params = None
    agent.subagents.workspace_root = Path("/tmp/project")
    agent.subagents.workspace_roots = [Path("/tmp/project")]

    roots = resolved_extra_write_roots(
        agent,
        {"goal": "只读来源", "_exact_allowed_tools": True, "extra_write_roots": []},
        "只读来源",
    )

    assert roots == []


def test_repair_file_task_with_output_ref_defaults_to_workspace_root():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    mock_task = MagicMock()
    mock_task.id = "repair_001"
    mock_task.goal = ""
    mock_task.status = "PLANNING"
    mock_task.verification_status = "UNVERIFIED"
    mock_task.task_dir = "/tmp/repair_001"
    mock_agent.subagents.create_run.return_value = mock_task

    result = CreateSubagentsTool(mock_agent).execute({
        "goal": "修复页面内链接问题。",
        "role": "repair",
        "output_files": ["index1.html"],
    })
    params = mock_agent.subagents.create_run.call_args.kwargs["params"]

    assert result.ok is True
    assert params.extra_write_roots == [str(Path("/tmp/project").resolve(strict=False))]


def test_repair_task_uses_required_read_target_as_product_root(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    target = tmp_path / "lab_outputs" / "site-demo" / "index.html"
    report = tmp_path / ".my-agent" / "subagents" / "child-a" / "reports" / "test_execution.json"

    result = CreateSubagentsTool(agent).execute({
        "goal": "修复示例站 index.html 的结构验证问题，并满足 required_dom_ids。",
        "agent_name": "小傻妞-验收修复",
        "role": "worker",
        "required_read_paths": [str(report), str(target)],
        "context_packs": [
            {
                "kind": "repair_contract",
                "summary": "根据验收报告修复目标文件",
                "contract": {
                    "schema": "subagent_repair_contract.v1",
                    "failed_run_ids": ["child-a"],
                    "required_read_paths": [str(report), str(target)],
                    "target_artifact_refs": [str(target)],
                },
            }
        ],
        "repair_contract": {
            "schema": "subagent_repair_contract.v1",
            "failed_run_ids": ["child-a"],
            "required_read_paths": [str(report), str(target)],
            "target_artifact_refs": [str(target)],
        },
    })
    payload = json.loads(result.output)
    task = agent.subagents.load(payload["created_run_ids"][0])

    assert result.ok is True
    assert task.allowed_write_roots == [
        str(task.task_dir),
        str(target.parent.resolve(strict=False)),
        str(tmp_path.resolve(strict=False)),
    ]


def test_goal_absolute_target_file_normalizes_write_root_to_parent(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    target = tmp_path / "lab_outputs" / "site-demo" / "index.html"

    result = CreateSubagentsTool(agent).execute({
        "goal": "修复示例站验收失败问题。",
        "agent_name": "小傻妞-修复示例站",
        "role": "worker",
        "extra_write_roots": [str(target)],
    })
    payload = json.loads(result.output)
    task = agent.subagents.load(payload["created_run_ids"][0])

    assert result.ok is True
    assert task.allowed_write_roots == [
        str(task.task_dir),
        str(target.parent.resolve(strict=False)),
        str(tmp_path.resolve(strict=False)),
    ]


def test_repair_contract_fields_are_persisted_to_child_context(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    test_ref = str(tmp_path / "reports" / "test_execution.json")
    agent = _mock_workspace_agent(tmp_path)

    result = CreateSubagentsTool(agent).execute({
        "goal": "修复失败报告中的 xlsx 生成问题。",
        "agent_name": "小傻妞-验收修复",
        "role": "worker",
        "required_read_paths": [test_ref],
        "context_packs": [{"kind": "repair_contract", "summary": "同一个 run 内修复、执行、验证"}],
    })
    payload = json.loads(result.output)
    task = agent.subagents.load(payload["created_run_ids"][0])

    assert result.ok is True
    assert task.context_manifest.required_read_paths == [test_ref]
    assert task.context_packs[0]["kind"] == "repair_contract"


def test_repair_contract_idempotency_does_not_merge_different_scope(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute(_repair_create_params("child-a", "a.xlsx")).output)
    second = json.loads(tool.execute(_repair_create_params("child-b", "b.xlsx")).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"]
    assert second["reused_run_ids"] == []
    assert first["created_run_ids"] != second["created_run_ids"]


def test_repair_contract_idempotency_reuses_same_scope_with_reworded_goal(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute(_repair_create_params("child-a", "a.xlsx", goal="修复 xlsx 生成脚本")).output)
    second = json.loads(tool.execute(_repair_create_params("child-a", "a.xlsx", goal="继续修复并执行 xlsx 生成")).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["pending_start_run_ids"] == []
    assert second["auto_start"]["run_ids"] == first["created_run_ids"]


def test_generic_worker_without_idempotency_contract_does_not_reuse_by_goal_text(tmp_path):
    """LLM: Repeating identical goal prose is not a machine fact for create_subagents reuse."""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)

    first = json.loads(tool.execute({"goal": "写一个家具品牌首页", "role": "worker"}).output)
    second = json.loads(tool.execute({"goal": "写一个家具品牌首页", "role": "worker"}).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"]
    assert second["reused_run_ids"] == []
    assert first["created_run_ids"] != second["created_run_ids"]


def test_generic_worker_reuses_structured_idempotency_contract_despite_reworded_goal(tmp_path):
    """LLM: Reuse requires an explicit idempotency contract, not the natural-language goal."""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    contract = {
        "schema": "subagent_idempotency_contract.v1",
        "kind": "implementation_slice",
        "idempotency_key": "home-page-worker",
        "scope_refs": ["deliverables/home/index.html"],
    }

    first = json.loads(tool.execute({
        "goal": "写一个家具品牌首页",
        "role": "worker",
        "context_packs": [{"kind": "idempotency_contract", "contract": contract}],
    }).output)
    second = json.loads(tool.execute({
        "goal": "继续完成高端家具首页",
        "role": "worker",
        "context_packs": [{"kind": "idempotency_contract", "contract": contract}],
    }).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]


def test_structured_idempotency_does_not_reuse_completed_alias(tmp_path):
    """COMPLETED is a historical raw value, not a current reusable terminal state."""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    contract = {
        "schema": "subagent_idempotency_contract.v1",
        "kind": "implementation_slice",
        "idempotency_key": "home-page-worker",
        "scope_refs": ["deliverables/home/index.html"],
    }

    first = json.loads(tool.execute({
        "goal": "写一个家具品牌首页",
        "role": "worker",
        "context_packs": [{"kind": "idempotency_contract", "contract": contract}],
    }).output)
    child = agent.subagents.load(first["created_run_ids"][0])
    child.status = "COMPLETED"
    agent.subagents.save(child)
    second = json.loads(tool.execute({
        "goal": "继续完成高端家具首页",
        "role": "worker",
        "context_packs": [{"kind": "idempotency_contract", "contract": contract}],
    }).output)

    assert second["created_run_ids"]
    assert second["reused_run_ids"] == []
    assert second["created_run_ids"] != first["created_run_ids"]


def test_generic_worker_reuses_same_structured_io_scope_without_goal_text_key(tmp_path):
    """同一父级同一 input/output 范围复用已有 child；这不是按 goal 文案合并。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)

    first = json.loads(tool.execute({
        "goal": "整理第一批项目。",
        "role": "worker",
        "input_refs": [str(tmp_path / "inputs" / "batch4")],
        "output_files": [str(tmp_path / "outputs" / "batch4.md")],
    }).output)
    second = json.loads(tool.execute({
        "goal": "继续处理那一批资料并写报告。",
        "role": "worker",
        "input_refs": [str(tmp_path / "inputs" / "batch4")],
        "output_files": [str(tmp_path / "outputs" / "batch4.md")],
    }).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["child_result_index"][0]["expected_outputs"] == [str(tmp_path / "outputs" / "batch4.md")]
    assert second["tasks"][0]["attributes"]["work_scope_key"]


def test_items_without_output_files_get_task_local_child_output_ref(tmp_path):
    """模型没填 output_files 时，运行时给子代理一个任务内默认结果槽。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    agent._current_run_task_workspace = str(tmp_path / "tasks" / "2026-06-06" / "all-agent-架构分析")
    tool = CreateSubagentsTool(agent)

    payload = json.loads(tool.execute({
        "goal": "并行分析两个项目",
        "items": [
            {
                "goal": "分析 ECC-main 并写报告。",
                "role": "worker",
                "agent_name": "ECC analyzer",
            },
            {
                "goal": "分析 pi-main 并写报告。",
                "role": "worker",
                "agent_name": "pi analyzer",
            },
        ],
    }).output)

    first = payload["tasks"][0]["attributes"]["output_files"][0]
    second = payload["tasks"][1]["attributes"]["output_files"][0]
    first_id = payload["tasks"][0]["id"]
    second_id = payload["tasks"][1]["id"]
    assert first.endswith(f"/work/child_outputs/{first_id}/01-ecc-analyzer-1.md")
    assert second.endswith(f"/work/child_outputs/{second_id}/02-pi-analyzer-2.md")
    assert payload["child_result_index"][0]["expected_outputs"] == [first]
    assert payload["child_result_index"][0]["read_order"] == []
    assert payload["child_output_read_order"][0]["expected_outputs"] == [first]
    assert payload["child_output_read_order"][0]["read_order"] == payload["child_result_index"][0]["read_order"]
    assert payload["next_action"]["action"] == "await_lifecycle_event"
    assert "status_tool_call" not in payload
    assert "wait_tool_call" not in payload
    assert "subagent_workspace" not in payload
    assert "agent_work_dir" not in payload["tasks"][0]
    assert payload["tasks"][0]["attributes"]["system_default_output_ref"] is True


def test_background_turn_uses_structured_run_workspace_for_default_child_outputs(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    task_root = tmp_path / "tasks" / "2026-07-19" / "background-continuation"
    agent._current_run_task_workspace = ""
    agent._current_run_params = SimpleNamespace(
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "work_dir": str(task_root / "work"),
                "output_dir": str(task_root / "output"),
            }
        }
    )

    payload = json.loads(
        CreateSubagentsTool(agent).execute(
            {
                "goal": "后台续接时并行分析两部分",
                "items": [
                    {"goal": "分析第一部分", "agent_name": "first"},
                    {"goal": "分析第二部分", "agent_name": "second"},
                ],
            }
        ).output
    )

    outputs = [item["attributes"]["output_files"][0] for item in payload["tasks"]]
    run_ids = [item["id"] for item in payload["tasks"]]
    assert outputs == [
        str(task_root / "work" / "child_outputs" / run_ids[0] / "01-first-1.md"),
        str(task_root / "work" / "child_outputs" / run_ids[1] / "02-second-2.md"),
    ]
    assert all(item["attributes"]["system_default_output_ref"] for item in payload["tasks"])


def test_system_default_output_templates_rebind_to_unique_run_directories(tmp_path):
    from agent_py_agent.agent.subagents.services.base import rebind_task_output_refs_to_run

    template = str(tmp_path / "task" / "work" / "child_outputs" / "01-worker.md")
    first = SimpleNamespace(
        id="subagent-first",
        attributes={
            "output_files": [template],
            "system_default_output_ref": True,
        },
    )
    second = SimpleNamespace(
        id="subagent-second",
        attributes={
            "output_files": [template],
            "system_default_output_ref": True,
        },
    )

    rebind_task_output_refs_to_run(first)
    rebind_task_output_refs_to_run(second)

    first_ref = first.attributes["output_files"][0]
    second_ref = second.attributes["output_files"][0]
    assert first_ref != second_ref
    assert first_ref.endswith("/child_outputs/subagent-first/01-worker.md")
    assert second_ref.endswith("/child_outputs/subagent-second/01-worker.md")


def test_output_prefixed_task_output_file_does_not_duplicate_output_dir(tmp_path):
    """output/foo.md 已经是任务 output 下路径，不能再拼成 output/output/foo.md。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    task_root = tmp_path / "tasks" / "2026-06-09" / "path-normalization"
    agent._current_run_task_workspace = str(task_root)
    tool = CreateSubagentsTool(agent)

    payload = json.loads(tool.execute({
        "goal": "写一份摘要。",
        "role": "worker",
        "output_files": ["output/report.md"],
    }).output)

    output_ref = payload["tasks"][0]["attributes"]["output_files"][0]
    assert output_ref == str((task_root / "output" / "report.md").resolve(strict=False))
    assert "/output/output/" not in output_ref


def test_model_invented_owner_home_output_is_rebased_into_current_task(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    owner_home = tmp_path / "owners" / "user-a"
    task_root = owner_home / "tasks" / "2026-07-15" / "science-plan"
    agent = _mock_workspace_agent(tmp_path)
    agent.home_paths = SimpleNamespace(owner_home_dir=owner_home, owner_id="users/user-a")
    agent._current_run_task_workspace = str(task_root)
    invented = owner_home / "02_mentors_volunteers.md"

    payload = json.loads(
        CreateSubagentsTool(agent).execute(
            {
                "goal": f"完成导师计划并保存到 {invented}",
                "role": "worker",
                "output_files": [str(invented)],
            }
        ).output
    )

    child = agent.subagents.load(payload["created_run_ids"][0])
    expected = str((task_root / "output" / invented.name).resolve(strict=False))
    assert child.attributes["output_files"] == [expected]
    assert expected in child.goal
    assert str(invented) not in child.goal


def test_same_output_without_structured_inputs_rejects_concurrent_different_work(tmp_path):
    """不同任务既不能因同一输出被误合并，也不能并发取得同一文件写权。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)

    first = json.loads(tool.execute({
        "goal": "分析项目 A 并追加到总报告。",
        "role": "worker",
        "output_files": [str(tmp_path / "outputs" / "summary.md")],
    }).output)
    second = json.loads(tool.execute({
        "goal": "分析项目 B 并追加到总报告。",
        "role": "worker",
        "output_files": [str(tmp_path / "outputs" / "summary.md")],
    }).output)

    assert first["created_run_ids"]
    assert second["ok"] is False
    assert second["error_code"] == "SUBAGENT_OUTPUT_SCOPE_CONFLICT"
    assert second["existing_run_ids"] == first["created_run_ids"]


def test_repair_goal_without_contract_does_not_guess_same_target(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    artifact = tmp_path / "index2.html"
    first = json.loads(tool.execute({
        "goal": f"修复 {artifact} 的 HTML 闭合标签，并验证页面完整。",
        "agent_name": "小傻妞-修复",
        "role": "worker",
    }).output)
    second = json.loads(tool.execute({
        "goal": f"收尾修复文件：{artifact}，确保 </body></html> 是最后内容。",
        "agent_name": "小傻妞-收尾修复",
        "role": "worker",
    }).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"]
    assert second["reused_run_ids"] == []


def test_repair_goal_without_contract_keeps_different_targets_separate(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({
        "goal": f"修复 {tmp_path / 'index1.html'} 的锚点链接。",
        "agent_name": "小傻妞-修复",
        "role": "worker",
    }).output)
    second = json.loads(tool.execute({
        "goal": f"修复 {tmp_path / 'index2.html'} 的 HTML 闭合标签。",
        "agent_name": "小傻妞-修复",
        "role": "worker",
    }).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"]
    assert second["reused_run_ids"] == []
    assert first["created_run_ids"] != second["created_run_ids"]


def test_nested_create_repair_contract_fields_are_persisted_to_child_context(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    parent = agent.subagents.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)
    agent._current_subagent_run_id = parent.id
    result = CreateSubagentsTool(agent).execute(_repair_create_params("child-a", "a.xlsx"))
    payload = json.loads(result.output)
    task = agent.subagents.load(payload["created_run_ids"][0])

    assert result.ok is True
    assert task.context_manifest.required_read_paths == ["reports/child-a/test_execution.json"]
    assert task.context_packs[0]["kind"] == "repair_contract"


def test_nested_create_repair_contract_reuses_same_scope_with_reworded_goal(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    parent = agent.subagents.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)
    agent._current_subagent_run_id = parent.id
    tool = CreateSubagentsTool(agent)

    first = json.loads(tool.execute(_repair_create_params("child-a", "a.xlsx")).output)
    second = json.loads(tool.execute(
        _repair_create_params("child-a", "a.xlsx", goal="继续修复并执行 xlsx 生成")
    ).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["pending_start_run_ids"] == []


def test_nested_create_without_idempotency_contract_does_not_reuse_by_goal_text(tmp_path):
    """LLM: Recursive create_subagents cannot use matching goal prose as a reuse key."""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    parent = agent.subagents.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)
    agent._current_subagent_run_id = parent.id
    tool = CreateSubagentsTool(agent)

    first = json.loads(tool.execute({
        "goal": "写一个家具品牌首页",
        "role": "worker",
        "agent_name": "小小傻妞-worker",
    }).output)
    second = json.loads(tool.execute({
        "goal": "写一个家具品牌首页",
        "role": "worker",
        "agent_name": "小小傻妞-worker",
    }).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"]
    assert second["reused_run_ids"] == []


def _repair_create_params(run_id: str, artifact: str, *, goal: str = "修复最终收口失败") -> dict[str, object]:
    contract = {
        "schema": "subagent_repair_contract.v1",
        "kind": "final_closeout",
        "failed_run_ids": [run_id],
        "required_read_paths": [f"reports/{run_id}/test_execution.json"],
        "target_artifact_refs": [artifact],
        "same_run_required_actions": [
            "read_failure_refs",
            "repair_named_scope",
            "execute_generated_scripts_or_commands_if_needed",
            "verify_target_artifacts",
            "report_artifact_and_test_refs",
        ],
    }
    return {
        "goal": goal,
        "agent_name": "小傻妞-验收修复",
        "role": "worker",
        "required_read_paths": list(contract["required_read_paths"]),
        "context_packs": [{"kind": "repair_contract", "summary": "同一个 run 内修复、执行、验证", "contract": contract}],
        "repair_contract": contract,
    }
