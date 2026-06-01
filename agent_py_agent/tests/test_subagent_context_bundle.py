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
from agent_py_agent.agent.subagents.models import CapabilityGrant


def test_context_bundle_v1_captures_task_handoff_fields(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="实现流程状态结算页",
        thought="需要让 worker 写页面、测试和验收说明。",
        plan=["读取现有项目", "实现结算页", "写测试"],
        role="worker",
        agent_name="checkout-worker",
    )
    task.parent_id = "parent-1"
    task.root_id = "root-1"
    task.depth = 2
    task.acceptance_checks = ["能从流程状态进入结算", "测试覆盖流程总计"]
    task.allowed_tools = ["read_file", "write_file"]
    task.allowed_write_roots = [str(tmp_path / task.id / "artifacts")]
    task.forbidden_write_roots = ["/System"]
    task.context_packs = [{"id": "pack-1", "summary": "示例流程背景"}]
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))
    payload = asdict(bundle)

    _assert_core_context_bundle(bundle, task)
    _assert_workspace_context_bundle(bundle, task, tmp_path)
    assert set(REQUIRED_CONTEXT_BUNDLE_FIELDS).issubset(payload)


# LLM: test_context_bundle_embeds_task_envelope_and_tool_preflight protects protocol-first handoff.
# 函数用途: runner 开工前要拿到 TaskEnvelope 和 Tool Preflight，不能只靠自然语言 task_packet 猜路径和工具。
def test_context_bundle_embeds_task_envelope_and_tool_preflight(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="写 build/index.html",
        thought="产物应该写到明确交付目录。",
        plan=["写页面"],
        role="worker",
    )
    task.allowed_tools = ["read_file", "write_file", "controlled_exec"]
    task.acceptance_checks = ["build/index.html 存在"]
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))

    assert bundle.task_envelope["schema_version"] == "subagent_task_envelope.v1"
    assert bundle.task_envelope["address"]["run_id"] == task.id
    assert bundle.task_envelope["acceptance"]["checks"] == ["build/index.html 存在"]
    preflight = bundle.tool_preflight
    assert preflight["ok"] is False
    assert [item["code"] for item in preflight["issues"]] == [
        "missing_allowed_write_roots",
        "controlled_exec_grant_missing",
    ]


# LLM: _assert_core_context_bundle groups identity and task contract assertions.
# 函数用途: 检查 context bundle 的身份、目标、计划、验收和权限主字段。
def _assert_core_context_bundle(bundle, task) -> None:
    assert bundle.schema_version == "subagent_context_bundle.v1"
    assert bundle.run_id == task.id
    assert bundle.root_id == "root-1"
    assert bundle.parent_id == "parent-1"
    assert bundle.role == "worker"
    assert bundle.goal == "实现流程状态结算页"
    assert bundle.plan == ["读取现有项目", "实现结算页", "写测试"]
    assert bundle.acceptance_checks == ["能从流程状态进入结算", "测试覆盖流程总计"]
    assert bundle.constraints["forbidden_write_roots"] == ["/System"]
    assert bundle.permissions["allowed_tools"] == ["read_file", "write_file"]


# LLM: _assert_workspace_context_bundle groups filesystem refs and packet contract assertions.
# 函数用途: 检查 task/run workspace 引用、输出合同和 source refs 没有退化。
def _assert_workspace_context_bundle(bundle, task, tmp_path: Path) -> None:
    assert bundle.workspace_refs["shared_messages"].endswith("shared/messages.jsonl")
    assert bundle.workspace_refs["agent_run_inbox"].endswith("inbox")
    assert bundle.workspace_refs["agent_run_outbox"].endswith("outbox")
    assert bundle.workspace_refs["agent_run_task"].endswith("task.md")
    assert bundle.workspace_refs["agent_run_checkpoint"].endswith("checkpoint.json")
    assert bundle.workspace_refs["agent_run_summary"].endswith("summary.md")
    assert bundle.workspace_refs["agent_run_findings"].endswith("findings.jsonl")
    assert bundle.workspace_refs["agent_run_compactions"].endswith("compactions")
    assert bundle.output_contract["final_report_ref"].endswith("final_report.md")
    assert bundle.task_packet["schema_version"] == "subagent_task_packet.v1"
    assert bundle.task_packet["run_id"] == task.id
    assert bundle.task_packet["role"] == "worker"
    assert bundle.task_packet["tool_contract"]["allowed_tools"] == ["read_file", "write_file"]
    write_roots = bundle.task_packet["write_contract"]["allowed_write_roots"]
    assert write_roots[:2] == [bundle.workspace_refs["task_root"], bundle.workspace_refs["agent_work_dir"]]
    assert str(tmp_path / task.id / "artifacts") in write_roots
    assert "task.goal" in bundle.source_refs["goal"]
    assert "task.acceptance_checks" in bundle.source_refs["acceptance_checks"]


# LLM: This regression keeps user deliverables separate from agent-run internal reports.
# 函数用途: 验证 final_report.md 这类用户产物会绑定到 product root，而不是内部 agent_run final_report。
def test_context_bundle_maps_required_file_to_product_root(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / ".my-agent" / "subagents")
    product_root = tmp_path / "product"
    task = manager.create_run(
        goal="整合上游结果。",
        thought="最终报告给用户看。",
        plan=["读取上游", "写报告"],
        role="coordinator",
        extra_write_roots=[str(product_root)],
        attributes={"required_files": ["final_report.md"]},
    )
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))
    expected = str(product_root / "final_report.md")

    assert bundle.output_contract["product_write_roots"] == [str(product_root)]
    assert bundle.output_contract["required_file_refs"] == [expected]
    assert bundle.output_contract["final_report_ref"] == expected
    assert bundle.output_contract["agent_run_final_report_ref"].endswith("final_report.md")
    assert bundle.task_packet["file_contract"]["required_file_refs"] == [expected]
    assert bundle.task_packet["write_contract"]["product_write_roots"] == [str(product_root)]


# LLM: Parent-declared output_files must be visible to the child runner contract.
# 函数用途: create_subagents 的 output_files 被验收当作交付目标时，context bundle 也必须把它传给 runner。
def test_context_bundle_maps_declared_output_files_to_required_refs(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / ".my-agent" / "subagents")
    product_root = tmp_path / "product"
    target = product_root / "summary_result.txt"
    task = manager.create_run(
        goal="读取资料并写一句摘要。",
        thought="父级声明了输出目标。",
        plan=["读资料", "写摘要"],
        role="worker",
        extra_write_roots=[str(product_root)],
        attributes={"output_files": [str(target)]},
    )
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))

    assert bundle.output_contract["declared_output_refs"] == [str(target)]
    assert bundle.output_contract["required_file_refs"] == [str(target)]
    assert bundle.task_packet["file_contract"]["declared_output_refs"] == [str(target)]
    assert bundle.task_packet["file_contract"]["required_file_refs"] == [str(target)]
    assert bundle.task_packet["write_contract"]["declared_output_refs"] == [str(target)]


# LLM: Logical output_refs are result keys, not filesystem deliverables.
# 函数用途: 允许父级用 output_refs 标注结构化结果字段；没有路径形态时不进入 required_file_refs 硬门。
def test_context_bundle_keeps_logical_output_refs_out_of_required_files(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / ".my-agent" / "subagents")
    task = manager.create_run(
        goal="读取资料并返回结构化摘要。",
        thought="父级声明了结果键。",
        plan=["读资料", "写摘要"],
        role="worker",
        attributes={"output_refs": ["source_file", "subagent_summary"]},
    )
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))

    assert bundle.output_contract["declared_output_refs"] == ["source_file", "subagent_summary"]
    assert bundle.output_contract["required_file_refs"] == []
    assert bundle.task_packet["file_contract"]["declared_output_refs"] == [
        "source_file",
        "subagent_summary",
    ]
    assert bundle.task_packet["file_contract"]["required_file_refs"] == []


# LLM: This regression covers product refs that already include the product root basename.
# 函数用途: 防止 `site/index.html` 在 product root `/tmp/site` 下被拼成 `/tmp/site/site/index.html`。
def test_context_bundle_strips_product_root_basename_from_required_ref(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / ".my-agent" / "subagents")
    product_root = tmp_path / "site"
    task = manager.create_run(
        goal="写高端家具首页。",
        thought="需要生成完整 index.html。",
        plan=["写页面"],
        role="worker",
        extra_write_roots=[str(product_root)],
        acceptance_checks=["site/index.html 存在"],
        attributes={"required_files": ["site/index.html"]},
    )
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))

    assert bundle.output_contract["required_file_refs"] == [str(product_root / "index.html")]
    assert bundle.task_packet["file_contract"]["required_file_refs"] == [str(product_root / "index.html")]


def test_context_bundle_exposes_controlled_exec_grant_refs(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="分析任务目录日志",
        thought="需要父级授权后才能跑 shell。",
        plan=["读取日志", "汇总结论"],
        role="worker",
    )
    task.capability_grants = [
        CapabilityGrant(
            id="grant-shell-1",
            request_id="req-shell-1",
            grant_to_run_id=task.id,
            grant_type="shell",
            tools=["controlled_exec"],
            command_allowlist=["pwd", "python3"],
            path_scope=[str(tmp_path / "workspace")],
            network_scope=["api.example.com"],
            output_budget={"max_stdout_bytes": 4096},
            constraints={"delete_policy": "trash_only"},
        )
    ]
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))

    assert bundle.permissions["controlled_exec_grants"] == [
        {
            "grant_id": "grant-shell-1",
            "request_id": "req-shell-1",
            "run_id": task.id,
            "command_allowlist": ["pwd", "python3"],
            "path_scope": [str(tmp_path / "workspace")],
            "network_scope": ["api.example.com"],
            "output_budget": {"max_stdout_bytes": 4096},
            "risk_level": "",
            "constraints": {"delete_policy": "trash_only"},
            "delete_policy": {
                "mode": "task_trash",
                "commands": ["rm", "rmdir", "unlink"],
                "requires_apply": True,
                "command_allowlist_required": False,
                "completion_requires": ["moved=true", "trash_manifest_ref"],
            },
        }
    ]


# LLM: test_context_bundle_output_contract_separates_required_and_forbidden_files covers prompt drift.
# 函数用途: context bundle 要把必需产物和禁止反例拆成结构化字段，减少下层模型靠自然语言猜。
