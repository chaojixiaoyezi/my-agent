from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from agent_py_agent.agent.subagents.context_bundle import (
    build_context_bundle,
    context_gate_prompt_lines,
    validate_context_bundle,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import CapabilityGrant


def test_context_bundle_output_contract_separates_required_and_forbidden_files(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal=(
            "写示例站页面。required_files: should-not-count.html"
        ),
        thought="forbidden_files: should-not-count.html",
        plan=["拆页面", "验收文件名"],
        role="worker",
        acceptance_checks=["forbidden_files: should-not-count.html"],
        attributes={
            "required_files": ["index.html", "item-detail.html", "style.css", "app.js"],
            "forbidden_files": ["product.html", "old-product.html", "stale.html", "obsolete.html"],
        },
    )
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))

    assert bundle.output_contract["required_files"] == [
        "index.html",
        "item-detail.html",
        "style.css",
        "app.js",
    ]
    assert bundle.output_contract["forbidden_files"] == [
        "product.html",
        "old-product.html",
        "stale.html",
        "obsolete.html",
    ]
    assert bundle.task_packet["file_contract"]["required_files"] == [
        "index.html",
        "item-detail.html",
        "style.css",
        "app.js",
    ]
    assert bundle.task_packet["file_contract"]["forbidden_files"] == [
        "product.html",
        "old-product.html",
        "stale.html",
        "obsolete.html",
    ]
    assert bundle.output_contract["file_contract_source"] == "attributes_required_forbidden_fields"


def test_context_bundle_file_contract_does_not_parse_task_text_fields(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="required_files: text-only.html",
        thought="forbidden_files: text-only-old.html",
        plan=["写页面"],
        role="worker",
        acceptance_checks=["required_files: text-only-style.css"],
    )
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))

    assert bundle.output_contract["required_files"] == []
    assert bundle.output_contract["forbidden_files"] == []
    assert bundle.task_packet["file_contract"]["required_files"] == []
    assert bundle.task_packet["file_contract"]["forbidden_files"] == []


def test_context_bundle_required_files_include_file_level_write_roots(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    target = tmp_path / "deliverables" / "site-output" / "index.html"
    task = manager.create_run(
        goal=f"在 {target} 创建高端现代家具首页。",
        thought="目标文件来自父级自然语言任务。",
        plan=["写页面", "汇报验收"],
        role="child",
        acceptance_checks=["文件路径正确存在", "HTML 可正常解析"],
    )
    task.allowed_write_roots = [str(task.task_dir), str(target)]
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))
    report = validate_context_bundle(bundle)

    assert "index.html" in bundle.output_contract["required_files"]
    assert "index.html" in bundle.task_packet["file_contract"]["required_files"]
    assert report.ok is True


def test_context_bundle_required_files_include_open_world_file_level_write_roots(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    target = tmp_path / "deliverables" / "analysis" / "metrics.parquet"
    task = manager.create_run(
        goal="生成分析结果文件。",
        thought="目标文件来自父级结构化写入根。",
        plan=["生成文件", "验收文件"],
        role="child",
        acceptance_checks=["文件路径正确存在"],
    )
    task.allowed_write_roots = [str(task.task_dir), str(target)]
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))
    report = validate_context_bundle(bundle)

    assert "metrics.parquet" in bundle.output_contract["required_files"]
    assert "metrics.parquet" in bundle.task_packet["file_contract"]["required_files"]
    assert report.ok is True


def test_context_bundle_ignores_source_markdown_inputs_for_required_files(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal=(
            "读取 data/country_packs/vietnam.md，结合 data/channel_partners.csv，"
            "输出越南市场环境分析，未确认信息标注【未确认】。"
        ),
        thought="vietnam.md 是输入资料，不是需要创建的报告文件。",
        plan=["读取越南资料", "整理市场机会", "输出分析结论"],
        role="worker",
        acceptance_checks=["分析覆盖市场机会、渠道和风险", "不把输入资料当成产物"],
    )
    task.allowed_write_roots = [str(Path(task.task_dir) / "reports")]
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))
    report = validate_context_bundle(bundle)

    assert bundle.output_contract["required_files"] == []
    assert bundle.task_packet["file_contract"]["required_files"] == []
    assert report.ok is True


def test_context_bundle_ignores_readme_summary_subject_for_required_files(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="读取 fixture_project 目录下的 README.md，在子代理 task_dir/scenario_outputs/ 写入自己的证据报告",
        thought="README.md 是输入证据，不是需要创建的产物。",
        plan=[
            "使用 read_file 读取 /workspace/fixture_project/README.md\n"
            "使用 write_file 写入 task_dir/scenario_outputs/<run_id>.md，报告 README.md 内容摘要和子代理基本信息"
        ],
        role="worker",
        acceptance_checks=[
            "必须有 read_file 证据证明读取了 README.md",
            "必须有 write_file 证据证明写入了 scenario_outputs/<run_id>.md",
        ],
    )
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))
    report = validate_context_bundle(bundle)

    assert bundle.output_contract["required_files"] == []
    assert bundle.task_packet["file_contract"]["required_files"] == []
    assert report.ok is True


def test_context_bundle_gate_reports_missing_required_handoff_fields(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="", thought="", plan=[])
    task.acceptance_checks = []
    manager.save(task)

    report = validate_context_bundle(build_context_bundle(manager.load(task.id)))

    assert report.ok is False
    assert "goal" in report.missing_fields
    assert "acceptance_checks" in report.missing_fields
    assert report.blocking_reason == "missing_required_context_fields"


def test_context_bundle_gate_allows_goal_without_plan(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="打开协作 case 并发起补证据请求。",
        thought="目标足够明确，plan 只作为提示，不是硬门。",
        plan=[],
    )
    task.acceptance_checks = ["有 collaboration case/request 记录"]
    manager.save(task)

    report = validate_context_bundle(build_context_bundle(manager.load(task.id)))

    assert report.ok is True
    assert "plan" not in report.missing_fields


def test_context_bundle_gate_reports_semantic_file_contract_mismatch(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="用单文件 html 做品牌首页。",
        thought="父级要求文件名不能缩水。",
        plan=["写页面", "写样式"],
        role="worker",
        acceptance_checks=["index.html 存在", "style.css 存在"],
        attributes={"required_files": ["index.html", "style.css"]},
    )
    manager.save(task)
    bundle = build_context_bundle(manager.load(task.id))
    broken = bundle.__class__(
        **{
            **asdict(bundle),
            "output_contract": {**bundle.output_contract, "required_files": ["index.html"]},
            "task_packet": {
                **bundle.task_packet,
                "file_contract": {
                    **bundle.task_packet["file_contract"],
                    "required_files": ["index.html"],
                },
            },
            "expected_required_files": ["index.html", "style.css"],
        }
    )

    report = validate_context_bundle(broken)

    assert report.ok is False
    assert "output_contract.required_files:style.css" in report.missing_fields
    assert "task_packet.file_contract.required_files:style.css" in report.missing_fields
    assert report.blocking_reason == "semantic_context_mismatch"


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

    context = manager.runner_context.write_execution_context(task.id)
    bundle_json = Path(context.context_bundle_json)
    bundle_md = Path(context.context_bundle_file)
    payload = json.loads(bundle_json.read_text(encoding="utf-8"))

    assert bundle_json.exists()
    assert bundle_md.exists()
    assert payload["schema_version"] == "subagent_context_bundle.v1"
    assert payload["run_id"] == task.id
    assert "## Context Gate" in bundle_md.read_text(encoding="utf-8")
    assert context.context_bundle["gate"]["ok"] is True


def test_execution_context_uses_canonical_agent_workspace_for_model_boundary(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / ".my-agent" / "subagents")
    task_root = tmp_path / "owners" / "local" / "main" / "tasks" / "2026-06-09" / "inspect-project"
    task = manager.create_run(
        goal="读项目并写报告。",
        thought="子代理应该只看到当前任务 workspace 的工作区。",
        plan=["读源码", "写报告"],
        role="worker",
        attributes={
            "run_workspace": {"task_root": str(task_root)},
            "output_files": ["[任务目录]/helper_report.md"],
        },
    )
    old_locator = str(manager.workspace / task.id)
    task.allowed_write_roots = [
        old_locator,
        str(tmp_path / "fixture_project" / "[任务目录]"),
    ]
    manager.save(task)

    context = manager.runner_context.build_execution_context(task.id)

    old_path = Path(old_locator)
    canonical_agent_dir = manager.load(task.id).agent_run_workspace_dir
    assert context.task_dir == canonical_agent_dir
    assert context.execution_context_file == str(Path(canonical_agent_dir) / "EXECUTION_CONTEXT.md")
    assert context.execution_context_json == str(Path(canonical_agent_dir) / "execution_context.json")
    assert context.context_bundle_file == str(Path(canonical_agent_dir) / "CONTEXT_BUNDLE.md")
    assert context.context_bundle_json == str(Path(canonical_agent_dir) / "context_bundle.json")
    assert context.write_boundary["task_dir"] == canonical_agent_dir
    assert old_locator not in context.write_boundary["allowed_write_roots"]
    assert all("[任务目录]" not in root for root in context.write_boundary["allowed_write_roots"])
    assert "[任务目录]" not in json.dumps(context.context_bundle, ensure_ascii=False)
    assert (old_path / "task.json").exists()
    assert (old_path / "run.json").exists()
    assert not (old_path / "STATUS.md").exists()
    assert not (old_path / "thought.md").exists()
    assert not (old_path / "projection_ledger.jsonl").exists()
    assert not (old_path / "output.json").exists()
    assert not (old_path / "reports" / "runner_result.json").exists()


def test_context_gate_prompt_lines_block_missing_required_fields(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="", thought="", plan=[])
    manager.save(task)
    context = manager.runner_context.write_execution_context(task.id)

    lines = context_gate_prompt_lines(context.context_bundle)
    rendered = "\n".join(lines)

    assert "Context Gate: BLOCKED" in rendered
    assert "missing_required_context_fields" in rendered
    assert "goal" in rendered
    assert "不要继续执行业务实现" in rendered


def test_runner_prompt_includes_context_gate_status(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="写 proof.txt", thought="生成证明文件", plan=["写文件"])
    task.acceptance_checks = ["proof.txt 存在"]
    manager.save(task)
    context = manager.runner_context.write_execution_context(task.id)

    prompt = _build_subagent_runner_prompt(context)

    assert "## Context Bundle Gate" in prompt
    assert "Context Gate: PASS" in prompt
    assert "context_bundle.json" in prompt
    assert "优先按 context_bundle.task_packet" in prompt


def test_runner_prompt_includes_task_envelope_and_preflight_status(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="写 build/index.html",
        thought="测试 preflight 提示。",
        plan=["写文件"],
        role="worker",
    )
    task.allowed_tools = ["read_file", "write_file", "controlled_exec"]
    task.acceptance_checks = ["build/index.html 存在"]
    manager.save(task)
    context = manager.runner_context.write_execution_context(task.id)

    prompt = _build_subagent_runner_prompt(context)

    assert "TaskEnvelope: subagent_task_envelope.v1" in prompt
    assert "Tool Preflight: ISSUE" in prompt
    # 子代理有可写工作区,preflight 不再误报 missing_allowed_write_roots(详见 protocol_preflight 修复);
    # 仍正确报缺失的 controlled_exec grant —— 证明 preflight status 照常写进 prompt。
    assert "missing_allowed_write_roots" not in prompt
    assert "controlled_exec_grant_missing" in prompt


def test_runner_prompt_describes_scoped_capability_request_loop(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="需要在授权后运行任务目录命令",
        thought="缺 shell 时要上抛 scope，不要自授权。",
        plan=["先申请能力", "再引用结果"],
    )
    task.acceptance_checks = ["缺工具时写 capability_request"]
    task.allowed_tools = ["controlled_exec"]
    task.capability_grants = [
        CapabilityGrant(
            id="grant-shell-1",
            request_id="req-shell-1",
            grant_to_run_id=task.id,
            grant_type="shell",
            tools=["controlled_exec"],
            command_allowlist=["pwd"],
            path_scope=[str(tmp_path)],
        )
    ]
    manager.save(task)
    context = manager.runner_context.write_execution_context(task.id)

    prompt = _build_subagent_runner_prompt(context)

    assert '"capability_type": "shell|tool|skill|mcp|network|generic"' in prompt
    assert '"requested_mcp_tools": []' in prompt
    assert "expected_output" in prompt
    assert "不要把父级授权细节、grant、path_scope、output_budget 当成普通任务步骤" in prompt
    assert "controlled_exec 只能使用 controlled_exec_grants 里的父级 grant" in prompt
