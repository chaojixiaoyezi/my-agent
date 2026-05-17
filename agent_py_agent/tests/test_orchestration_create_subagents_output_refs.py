"""Focused tests for create_subagents output-root binding."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


# LLM: _mock_workspace_agent keeps output-ref tests small and independent from the large create tool suite.
# 函数用途: 构造拥有真实 SubAgentManager 的最小 agent，用于检查 payload 和持久化 task 是否一致。
def _mock_workspace_agent(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.subagents = SubAgentManager(tmp_path / ".my-agent" / "subagents", workspace_root=tmp_path)
    return mock_agent


# LLM: This regression covers the real E2E bug where a stale guessed run id entered a new child goal.
# 函数用途: 确认 create_subagents 返回给模型的 payload 和 task.json 都使用真实 run_id 作为自写产物目录。
def test_items_mode_payload_rebinds_stale_self_output_run_id(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    stale_id = "subagent-0000000000-stale"
    mock_agent = _mock_workspace_agent(tmp_path)

    result = CreateSubagentsTool(mock_agent).execute({
        "items": [{
            "goal": f"收集项目数据，请把结果写到 data/subagents/{stale_id}/data_collection.md",
            "agent_name": "小傻妞-数据收集",
            "role": "worker",
        }],
    })
    payload = json.loads(result.output)
    run_id = payload["ids"][0]
    loaded = mock_agent.subagents.load(run_id)

    assert result.ok is True
    assert stale_id not in payload["tasks"][0]["goal"]
    assert f"data/subagents/{run_id}/data_collection.md" in payload["tasks"][0]["goal"]
    assert loaded.attributes["output_ref_rebindings"][0]["to"].endswith("/data_collection.md")


# LLM: This keeps the workspace-root default behavior out of the oversized create tool test file.
# 函数用途: 验证已有真实任务工作区时，模糊交付 worker 默认获得 workspace_root 写入根。
def test_vague_deliverable_worker_defaults_to_workspace_root():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
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
        "goal": "在目标目录生成一个完整文件 index.html，并报告路径。",
        "role": "writer",
    })
    params = mock_agent.subagents.create_run.call_args.kwargs["params"]

    assert result.ok is True
    assert params.extra_write_roots == [str(Path("/tmp/project").resolve(strict=False))]


# LLM: repair tasks that mention a file need the product workspace, not only a private run dir.
# 函数用途: 验证“修复 index.html”这类自然语言任务默认拿到项目写入根，避免 repair 写到私有工单目录。
def test_repair_file_task_defaults_to_workspace_root():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
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
        "goal": "修复 index1.html 的页面内链接问题。",
        "role": "repair",
    })
    params = mock_agent.subagents.create_run.call_args.kwargs["params"]

    assert result.ok is True
    assert params.extra_write_roots == [str(Path("/tmp/project").resolve(strict=False))]


# LLM: Repair create calls with target refs should write to the target artifact directory, not workspace root.
# 函数用途: 复现真实 E2E 中修复 worker 读对了 lab_outputs/index.html 却写到项目根 index.html 的路径漂移。
def test_repair_task_uses_required_read_target_as_product_root(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    target = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    report = tmp_path / ".my-agent" / "subagents" / "child-a" / "reports" / "test_execution.json"

    result = CreateSubagentsTool(agent).execute({
        "goal": "修复购物站 index.html 的结构验证问题，并满足 required_dom_ids。",
        "agent_name": "小傻妞-验收修复",
        "role": "repair_worker",
        "required_read_paths": [str(report), str(target)],
    })
    payload = json.loads(result.output)
    task = agent.subagents.load(payload["ids"][0])

    assert result.ok is True
    assert task.allowed_write_roots == [
        str(task.task_dir),
        str(target.parent.resolve(strict=False)),
    ]


# LLM: Absolute target files in natural goals should become directory write roots.
# 函数用途: 防止 create_subagents 从“目标文件:/.../index.html”提取出文件本身作为 allowed_write_root。
def test_goal_absolute_target_file_normalizes_write_root_to_parent(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    target = tmp_path / "lab_outputs" / "shop-demo" / "index.html"

    result = CreateSubagentsTool(agent).execute({
        "goal": f"修复购物站验收失败问题。目标文件：{target}",
        "agent_name": "小傻妞-修复购物站",
        "role": "worker",
    })
    payload = json.loads(result.output)
    task = agent.subagents.load(payload["ids"][0])

    assert result.ok is True
    assert task.allowed_write_roots == [
        str(task.task_dir),
        str(target.parent.resolve(strict=False)),
    ]


# LLM: Repair suggested tool calls must survive create_subagents into the runner context.
# 函数用途: 验证 repair_contract 第一片的 required refs/context packs 会写入真实 task，而不是只停留在父级工具输出里。
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
    task = agent.subagents.load(payload["ids"][0])

    assert result.ok is True
    assert task.context_manifest.required_read_paths == [test_ref]
    assert task.context_packs[0]["kind"] == "repair_contract"


# LLM: same display name is not enough to merge different repair scopes.
# 函数用途: 两个验收修复小傻妞如果 repair_contract 指向不同失败 run/产物，必须创建不同任务。
def test_repair_contract_idempotency_does_not_merge_different_scope(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute(_repair_create_params("child-a", "a.xlsx")).output)
    second = json.loads(tool.execute(_repair_create_params("child-b", "b.xlsx")).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"]
    assert second["reused_run_ids"] == []
    assert first["ids"] != second["ids"]


# LLM: same repair contract should reuse even when the model rewrites the natural-language goal.
# 函数用途: 同一个失败 run/目标产物被重复派修复时，复用已有 repair owner，避免拆成修复/执行/验证多段链。
def test_repair_contract_idempotency_reuses_same_scope_with_reworded_goal(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute(_repair_create_params("child-a", "a.xlsx", goal="修复 xlsx 生成脚本")).output)
    second = json.loads(tool.execute(_repair_create_params("child-a", "a.xlsx", goal="继续修复并执行 xlsx 生成")).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["dispatch_run_ids"] == first["created_run_ids"]


# LLM: fallback repair identity covers live runs where the model omitted repair_contract fields.
# 函数用途: 自然语言“修复/精准修复/收尾修复”只要指向同一产物，就复用同一个 repair owner。
def test_repair_goal_fallback_reuses_same_target_without_contract(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    artifact = tmp_path / "index2.html"
    first = json.loads(tool.execute({
        "goal": f"修复 {artifact} 的 HTML 闭合标签，并验证页面完整。",
        "agent_name": "小傻妞-修复",
        "role": "leaf_worker",
    }).output)
    second = json.loads(tool.execute({
        "goal": f"收尾修复文件：{artifact}，确保 </body></html> 是最后内容。",
        "agent_name": "小傻妞-收尾修复",
        "role": "leaf_worker",
    }).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["dispatch_run_ids"] == first["created_run_ids"]


# LLM: fallback repair identity must not merge unrelated file repairs just because both say repair.
# 函数用途: 两个不同目标文件的自然语言修复任务应保持独立 repair owner。
def test_repair_goal_fallback_keeps_different_targets_separate(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _mock_workspace_agent(tmp_path)
    tool = CreateSubagentsTool(agent)
    first = json.loads(tool.execute({
        "goal": f"修复 {tmp_path / 'index1.html'} 的锚点链接。",
        "agent_name": "小傻妞-修复",
        "role": "leaf_worker",
    }).output)
    second = json.loads(tool.execute({
        "goal": f"修复 {tmp_path / 'index2.html'} 的 HTML 闭合标签。",
        "agent_name": "小傻妞-修复",
        "role": "leaf_worker",
    }).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"]
    assert second["reused_run_ids"] == []
    assert first["ids"] != second["ids"]


# LLM: schedule_child_subagents should persist repair context into the child task, not drop it at parsing.
# 函数用途: runner 内父节点创建修复 child 时，repair_contract 的 required refs/context pack 必须进入真实 task。
def test_schedule_child_repair_contract_fields_are_persisted_to_child_context(tmp_path):
    from agent_py_agent.agent.agent_core.hierarchy_tools import ScheduleChildSubagentsTool
    from agent_py_agent.agent.config import AgentConfig
    from agent_py_agent.agent.core import SimpleAgent

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    parent = agent.subagents.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)
    agent._current_subagent_run_id = parent.id
    result = ScheduleChildSubagentsTool(agent).execute({
        "apply": True,
        "children": [_repair_create_params("child-a", "a.xlsx")],
    })
    payload = json.loads(result.output)
    task = agent.subagents.load(payload["created_run_ids"][0])

    assert result.ok is True
    assert task.context_manifest.required_read_paths == ["reports/child-a/test_execution.json"]
    assert task.context_packs[0]["kind"] == "repair_contract"


# LLM: runner-context repair scheduling should reuse the same repair owner by contract, not exact prose.
# 函数用途: 同一个父级重复派同一 repair_contract，即使 goal 改写，也不能拆出第二个执行/验证 child。
def test_schedule_child_repair_contract_reuses_same_scope_with_reworded_goal(tmp_path):
    from agent_py_agent.agent.agent_core.hierarchy_tools import ScheduleChildSubagentsTool
    from agent_py_agent.agent.config import AgentConfig
    from agent_py_agent.agent.core import SimpleAgent

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    parent = agent.subagents.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)
    agent._current_subagent_run_id = parent.id
    tool = ScheduleChildSubagentsTool(agent)

    first = json.loads(tool.execute({"apply": True, "children": [_repair_create_params("child-a", "a.xlsx")]}).output)
    second = json.loads(tool.execute({
        "apply": True,
        "children": [_repair_create_params("child-a", "a.xlsx", goal="继续修复并执行 xlsx 生成")],
    }).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["dispatch_run_ids"] == first["created_run_ids"]


# LLM: _repair_create_params mirrors the repair suggested_tool_call shape used by parent acceptance.
# 函数用途: 生成带同 run 修复合同的 create/schedule 参数，供顶层和 runner-context 测试复用。
def _repair_create_params(run_id: str, artifact: str, *, goal: str = "修复父级验收失败") -> dict[str, object]:
    contract = {
        "schema": "subagent_repair_contract.v1",
        "kind": "parent_acceptance",
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
