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
    assert bundle.goal == "实现购物车结算页"
    assert bundle.plan == ["读取现有项目", "实现结算页", "写测试"]
    assert bundle.acceptance_checks == ["能从购物车进入结算", "测试覆盖订单总价"]
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
    assert bundle.task_packet["write_contract"]["allowed_write_roots"] == [str(tmp_path / task.id / "artifacts")]
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
def test_context_bundle_output_contract_separates_required_and_forbidden_files(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal=(
            "写购物站页面。required_files: should-not-count.html"
        ),
        thought="forbidden_files: should-not-count.html",
        plan=["拆页面", "验收文件名"],
        role="worker",
        acceptance_checks=["forbidden_files: should-not-count.html"],
        attributes={
            "required_files": ["index.html", "product-detail.html", "style.css", "app.js"],
            "forbidden_files": ["product.html", "old-product.html", "legacy.html", "obsolete.html"],
        },
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
    assert bundle.task_packet["file_contract"]["required_files"] == [
        "index.html",
        "product-detail.html",
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
    target = tmp_path / "deliverables" / "furniture-home" / "index.html"
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
    assert "plan" in report.missing_fields
    assert "acceptance_checks" in report.missing_fields
    assert report.blocking_reason == "missing_required_context_fields"


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
