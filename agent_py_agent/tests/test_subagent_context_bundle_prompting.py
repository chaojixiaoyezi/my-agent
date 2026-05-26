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
            "forbidden_files": ["product.html", "old-product.html", "legacy.html", "obsolete.html"],
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
        "legacy.html",
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
        "legacy.html",
        "obsolete.html",
    ]
    assert bundle.output_contract["file_contract_source"] == "attributes_required_forbidden_fields"


# LLM: Context bundle file contracts must ignore task prose fields.
# 函数用途: goal/thought/acceptance_checks 中的 required_files/forbidden_files 文本不能成为机器文件合同。
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


# LLM: Real E2E showed file-level write roots can be present while text extraction misses index.html.
# 函数用途: 产物写入根指向具体 HTML 文件时，required_files 必须包含该文件名，避免 Context Gate 误挡子代理。
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


# LLM: file-level write roots are open-world deliverables, not a fixed web/text suffix list.
# 函数用途: 验证 xlsx/parquet 等新格式文件级写入授权也会进入 required_files。
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


# LLM: Context Gate should not block when a task reads a Markdown data pack as input.
# 函数用途: 复现 Task17 里 `读取 .../vietnam.md` 被误判成必需输出文件，导致市场子代理无法开工。
def test_context_bundle_ignores_source_markdown_inputs_for_required_files(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal=(
            "读取 data/country_packs/vietnam.md，结合 data/channel_partners.csv，"
            "输出越南市场环境分析，未确认信息标注【未确认】。"
        ),
        thought="vietnam.md 是输入资料，不是需要创建的报告文件。",
        plan=["读取越南资料", "整理市场机会", "输出分析结论"],
        role="leaf_worker",
        acceptance_checks=["分析覆盖市场机会、渠道和风险", "不把输入资料当成产物"],
    )
    task.allowed_write_roots = [str(Path(task.task_dir) / "reports")]
    manager.save(task)

    bundle = build_context_bundle(manager.load(task.id))
    report = validate_context_bundle(bundle)

    assert bundle.output_contract["required_files"] == []
    assert bundle.task_packet["file_contract"]["required_files"] == []
    assert report.ok is True


# LLM: Live Lab runner plans mention README.md as report subject, not deliverable.
# 函数用途: 防止“写报告，说明 README.md 内容摘要”误触发 required_files，导致 runner 已完成仍被 Context Gate BLOCKED。
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
            "reserved": {"expected_required_files": ["index.html", "style.css"]},
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
    assert "优先按 context_bundle.task_packet" in prompt


# LLM: test_runner_prompt_includes_task_envelope_and_preflight_status guards model-facing protocol hints.
# 函数用途: 子代理 prompt 要直接告诉模型先读 TaskEnvelope，并展示 preflight issue，避免模型从摘要里猜。
def test_runner_prompt_includes_task_envelope_and_preflight_status(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

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
    context = manager.write_execution_context(task.id)

    prompt = _build_subagent_runner_prompt(context)

    assert "TaskEnvelope: subagent_task_envelope.v1" in prompt
    assert "Tool Preflight: ISSUE" in prompt
    assert "missing_allowed_write_roots" in prompt
    assert "controlled_exec_grant_missing" in prompt


def test_runner_prompt_describes_scoped_capability_request_loop(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

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
    context = manager.write_execution_context(task.id)

    prompt = _build_subagent_runner_prompt(context)

    assert '"capability_type": "shell|tool|skill|mcp|network|generic"' in prompt
    assert '"requested_commands": ["python3"]' in prompt
    assert '"requested_mcp_tools": []' in prompt
    assert '"path_scope": ["任务内需要访问的目录"]' in prompt
    assert '"output_budget": {"stdout_bytes": 65536, "stderr_bytes": 32768}' in prompt
    assert "controlled_exec 只能使用 controlled_exec_grants 里的父级 grant" in prompt
