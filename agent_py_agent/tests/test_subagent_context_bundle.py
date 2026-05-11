from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from agent_py_agent.agent.subagents.context_bundle import (
    REQUIRED_CONTEXT_BUNDLE_FIELDS,
    build_context_bundle,
    context_gate_prompt_lines,
    validate_context_bundle,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_context_bundle_v1_captures_task_handoff_fields(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="实现购物车结算页",
        thought="需要让 worker 写页面、测试和验收说明。",
        plan=["读取现有项目", "实现结算页", "写测试"],
        role="worker",
        agent_name="checkout-worker",
    )
    task.parent_id = "parent-1"
    task.root_id = "root-1"
    task.depth = 2
    task.acceptance_checks = ["能从购物车进入结算", "测试覆盖订单总价"]
    task.allowed_tools = ["read_file", "write_file"]
    task.allowed_write_roots = [str(tmp_path / task.id / "artifacts")]
    task.forbidden_write_roots = ["/System"]
    task.context_packs = [{"id": "pack-1", "summary": "购物流程背景"}]
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))
    payload = asdict(bundle)

    assert bundle.schema_version == "subagent_context_bundle.v1"
    assert bundle.run_id == task.id
    assert bundle.root_id == "root-1"
    assert bundle.parent_id == "parent-1"
    assert bundle.role == "worker"
    assert bundle.goal == "实现购物车结算页"
    assert bundle.plan == ["读取现有项目", "实现结算页", "写测试"]
    assert bundle.acceptance_checks == ["能从购物车进入结算", "测试覆盖订单总价"]
    assert bundle.constraints["forbidden_write_roots"] == ["/System"]
    assert bundle.permissions["allowed_tools"] == ["read_file", "write_file"]
    assert bundle.workspace_refs["shared_messages"].endswith("shared/messages.jsonl")
    assert bundle.workspace_refs["agent_run_inbox"].endswith("inbox")
    assert bundle.workspace_refs["agent_run_outbox"].endswith("outbox")
    assert bundle.output_contract["final_report_ref"].endswith("final_report.md")
    assert "task.goal" in bundle.source_refs["goal"]
    assert "task.acceptance_checks" in bundle.source_refs["acceptance_checks"]
    assert set(REQUIRED_CONTEXT_BUNDLE_FIELDS).issubset(payload)


# LLM: test_context_bundle_output_contract_separates_required_and_forbidden_files covers prompt drift.
# 函数用途: context bundle 要把必需产物和禁止反例拆成结构化字段，减少下层模型靠自然语言猜。
def test_context_bundle_output_contract_separates_required_and_forbidden_files(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal=(
            "交付静态购物站，必须包含 index.html、product-detail.html、style.css、app.js。"
            "不允许把 product-detail.html 改名成 product.html 或 old-product.html。"
        ),
        thought="禁止创建 legacy.html。",
        plan=["拆页面", "验收文件名"],
        role="worker",
        acceptance_checks=["必须保留 product-detail.html，不得创建 obsolete.html。"],
    )
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))

    assert bundle.output_contract["required_files"] == [
        "index.html",
        "product-detail.html",
        "style.css",
        "app.js",
    ]
    assert bundle.output_contract["forbidden_files"] == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
    ]
    assert bundle.output_contract["file_contract_source"] == "task_text_positive_negative_extraction"


def test_context_bundle_gate_reports_missing_required_handoff_fields(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="", thought="", plan=[])
    task.acceptance_checks = []
    manager.save(task)

    report = validate_context_bundle(build_context_bundle(manager.load(task.id)))

    assert report.ok is False
    assert "goal" in report.missing_fields
    assert "plan" in report.missing_fields
    assert "acceptance_checks" in report.missing_fields
    assert report.blocking_reason == "missing_required_context_fields"


def test_write_execution_context_persists_context_bundle_files(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="写 proof.txt",
        thought="验证 context bundle 落盘。",
        plan=["写文件"],
        role="worker",
    )
    task.acceptance_checks = ["proof.txt 存在"]
    manager.save(task)

    context = manager.write_execution_context(task.id)
    bundle_json = Path(context.context_bundle_json)
    bundle_md = Path(context.context_bundle_file)
    payload = json.loads(bundle_json.read_text(encoding="utf-8"))

    assert bundle_json.exists()
    assert bundle_md.exists()
    assert payload["schema_version"] == "subagent_context_bundle.v1"
    assert payload["run_id"] == task.id
    assert "## Context Gate" in bundle_md.read_text(encoding="utf-8")
    assert context.context_bundle["gate"]["ok"] is True


def test_context_gate_prompt_lines_block_missing_required_fields(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="", thought="", plan=[])
    manager.save(task)
    context = manager.write_execution_context(task.id)

    lines = context_gate_prompt_lines(context.context_bundle)
    rendered = "\n".join(lines)

    assert "Context Gate: BLOCKED" in rendered
    assert "missing_required_context_fields" in rendered
    assert "goal" in rendered
    assert "不要继续执行业务实现" in rendered


def test_runner_prompt_includes_context_gate_status(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="写 proof.txt", thought="生成证明文件", plan=["写文件"])
    task.acceptance_checks = ["proof.txt 存在"]
    manager.save(task)
    context = manager.write_execution_context(task.id)

    prompt = _build_subagent_runner_prompt(context)

    assert "## Context Bundle Gate" in prompt
    assert "Context Gate: PASS" in prompt
    assert "context_bundle.json" in prompt


def test_context_bundle_is_mirrored_into_agent_run_workspace(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="把 context bundle 放进运行工作位",
        thought="接管代理应该从 agent run workspace 找到它。",
        plan=["写 bundle", "检查 refs"],
    )
    task.acceptance_checks = ["agent workspace 有 context_bundle.json"]
    manager.save(task)

    context = manager.write_execution_context(task.id)
    agent_workspace = Path(context.context_bundle["workspace_refs"]["agent_run_workspace"])
    workspace_json = agent_workspace / "context_bundle.json"
    workspace_md = agent_workspace / "CONTEXT_BUNDLE.md"
    execution_context_md = Path(context.execution_context_file).read_text(encoding="utf-8")

    assert workspace_json.exists()
    assert workspace_md.exists()
    assert json.loads(workspace_json.read_text(encoding="utf-8"))["run_id"] == task.id
    assert "## Context Bundle" in execution_context_md
    assert str(workspace_json) in execution_context_md
    assert "Context Gate: PASS" in execution_context_md


def test_context_bundle_records_multilevel_lineage_refs(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    root, child, grandchild, great_grandchild = _create_context_bundle_hierarchy(manager)
    payloads = _write_and_load_context_bundle_payloads(manager, [root, child, grandchild, great_grandchild])

    assert payloads[root.id]["lineage"]["depth"] == 0
    assert payloads[root.id]["lineage"]["parent_context_bundle_ref"] == ""
    for task, parent in [(child, root), (grandchild, child), (great_grandchild, grandchild)]:
        _assert_context_bundle_lineage(payloads[task.id], task, parent, root.id)


def _create_context_bundle_hierarchy(manager: SubAgentManager):
    root = manager.create_run(
        goal="根代理拆购物网站任务",
        thought="负责拆分和汇总。",
        plan=["拆任务", "看状态"],
        acceptance_checks=["所有子树有交接包"],
    )
    child = manager.create_run(
        goal="子代理负责账号链路",
        thought="子代理要继续拆分。",
        plan=["拆账号模块", "汇总孙代理结果"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        acceptance_checks=["孙代理交付注册和登录"],
    )
    grandchild = manager.create_run(
        goal="孙代理负责注册页",
        thought="孙代理要继续拆叶子任务。",
        plan=["拆 UI", "拆测试"],
        parent_id=child.id,
        root_id=root.id,
        depth=2,
        acceptance_checks=["孙孙代理交付注册 UI 和测试"],
    )
    great_grandchild = manager.create_run(
        goal="孙孙代理实现注册表单校验",
        thought="叶子节点只做一个具体实现。",
        plan=["改代码", "跑测试"],
        parent_id=grandchild.id,
        root_id=root.id,
        depth=3,
        acceptance_checks=["注册表单错误提示可见"],
    )
    return root, child, grandchild, great_grandchild


def _write_and_load_context_bundle_payloads(manager: SubAgentManager, tasks) -> dict[str, dict[str, object]]:
    contexts = {task.id: manager.write_execution_context(task.id) for task in tasks}
    return {
        run_id: json.loads(Path(context.context_bundle_json).read_text(encoding="utf-8"))
        for run_id, context in contexts.items()
    }


def _assert_context_bundle_lineage(payload: dict[str, object], task, parent, root_id: str) -> None:
    lineage = payload["lineage"]
    parent_ref = lineage["parent_context_bundle_ref"]
    assert lineage["root_id"] == root_id
    assert lineage["parent_id"] == parent.id
    assert lineage["depth"] == task.depth
    assert parent_ref == str(Path(parent.agent_run_workspace_dir) / "context_bundle.json")
    assert Path(parent_ref).exists()
    assert payload["gate"]["ok"] is True
