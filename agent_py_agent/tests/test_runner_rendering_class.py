"""Runner 渲染模块单元测试。

测试 agent_py_agent/agent/subagents/runner_rendering.py 中的渲染函数。
包括执行上下文、runner 结果、通道探测等渲染功能。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.subagents.models import (
    ChannelProbeCheck,
    ChannelProbeReport,
    ChannelProbeResult,
    ContextManifest,
    QualityContract,
    SubAgentExecutionContext,
    SubAgentRunnerResult,
)
from agent_py_agent.agent.subagents.runner_rendering import (
    _render_runner_item_line,
    render_channel_probe_markdown,
    render_execution_context_markdown,
    render_runner_result_markdown,
    render_single_channel_probe_markdown,
)


class TestExecutionContextRendering:
    """测试执行上下文渲染功能。"""

    def test_render_execution_context_markdown_basic(self):
        """测试基本执行上下文渲染。

        验证执行上下文可以正确渲染为 Markdown。
        """
        context = SubAgentExecutionContext(
            run_id="run-001",
            generated_at=time.time(),
            goal="测试目标",
            thought="测试思考",
            plan=["步骤1", "步骤2"],
            status="RUNNING",
            verification_status="UNVERIFIED",
            channel_status="healthy",
            agent_name="test-agent",
            role="general",
        )

        result = render_execution_context_markdown(context)

        assert "# SUBAGENT EXECUTION CONTEXT" in result
        assert "run_id: run-001" in result
        assert "测试目标" in result
        assert "测试思考" in result
        assert "## Goal" in result
        assert "## Plan" in result

    def test_render_execution_context_explains_delegate_report_writes(self):
        """测试委托写入边界会说明报告类文件仍可写。

        验证 tester/bug_finder 不会因为 delegate 策略误以为自己不能写验收报告。
        """
        context = SubAgentExecutionContext(
            run_id="run-report-boundary",
            generated_at=time.time(),
            goal="写测试报告",
            thought="思考",
            plan=["检查产物", "写报告"],
            role="bug_finder",
            write_boundary={
                "task_dir": "/tmp/task",
                "allowed_write_roots": ["/tmp/task", "/tmp/deliverables"],
                "product_write_roots": ["/tmp/deliverables"],
                "product_write_policy": "delegate",
            },
        )

        result = render_execution_context_markdown(context)

        assert "- product_write_policy: delegate" in result
        assert "- allowed_write_roots: /tmp/task, /tmp/deliverables" in result

    # LLM: Declared output refs should be visible before a runner writes only output.json.
    # 函数用途: 父级声明 output_files 时，执行上下文必须明确告诉 runner 这些是用户产物路径。
    def test_render_execution_context_highlights_declared_output_refs(self):
        context = SubAgentExecutionContext(
            run_id="run-output-target",
            generated_at=time.time(),
            goal="读取资料并写摘要",
            thought="思考",
            plan=["读资料", "写摘要"],
            role="worker",
            context_bundle={
                "output_contract": {
                    "required_file_refs": ["/tmp/work/summary_result.txt"],
                    "declared_output_refs": ["/tmp/work/summary_result.txt"],
                }
            },
        )

        result = render_execution_context_markdown(context)

        assert "## Declared Output Targets" in result
        assert "/tmp/work/summary_result.txt" in result
        assert "内部 output.json 只能作为运行报告" in result

    def test_render_execution_context_markdown_empty_fields(self):
        """测试空字段执行上下文渲染。

        验证空字段时渲染不会报错。
        """
        context = SubAgentExecutionContext(
            run_id="run-002",
            generated_at=time.time(),
            goal="",
            thought="",
            plan=[],
            status="PLANNING",
        )

        result = render_execution_context_markdown(context)

        assert "# SUBAGENT EXECUTION CONTEXT" in result
        assert "goal:" in result
        assert "未设置" in result

    def test_render_execution_context_markdown_with_quality_contract(self):
        """测试带质量契约的执行上下文渲染。

        验证质量契约信息正确渲染。
        """
        context = SubAgentExecutionContext(
            run_id="run-003",
            generated_at=time.time(),
            goal="带质量要求的任务",
            thought="思考",
            plan=["计划"],
            quality_contract=QualityContract(
                user_visible_goal="交付高质量代码",
                quality_bar="代码可运行",
            ),
        )

        result = render_execution_context_markdown(context)

        assert "## Quality Contract" in result
        assert "交付高质量代码" in result
        assert "代码可运行" in result

    def test_render_execution_context_markdown_with_repair_contract_pack(self):
        """测试 repair context pack 会展示关键合同字段。"""
        context = SubAgentExecutionContext(
            run_id="run-repair",
            generated_at=time.time(),
            goal="修复缺失产物",
            thought="读取 refs 后修复",
            plan=["读报告", "修复", "验证"],
            context_packs=[{
                "kind": "repair_contract",
                "summary": "同一个 run 内修复、执行、验证",
                "contract": {
                    "schema": "subagent_repair_contract.v1",
                    "kind": "final_closeout",
                    "same_run_required_actions": ["repair_named_scope", "verify_target_artifacts"],
                    "target_artifact_refs": ["/tmp/out/report.xlsx"],
                },
            }],
        )

        result = render_execution_context_markdown(context)

        assert "contract.schema: subagent_repair_contract.v1" in result
        assert "repair_named_scope, verify_target_artifacts" in result
        assert "/tmp/out/report.xlsx" in result

    def test_render_execution_context_markdown_with_granted_cards(self):
        """测试带授权卡片执行上下文渲染。

        验证授权卡片正确渲染。
        """
        context = SubAgentExecutionContext(
            run_id="run-004",
            generated_at=time.time(),
            goal="测试",
            thought="思考",
            plan=["计划"],
            granted_cards=[
                {
                    "kind": "skill",
                    "name": "web_search",
                    "risk_level": "low",
                    "source": "parent",
                    "description": "网络搜索能力",
                }
            ],
        )

        result = render_execution_context_markdown(context)

        assert "## Granted Cards" in result
        assert "web_search" in result
        assert "low" in result

    def test_render_execution_context_markdown_with_evidence(self):
        """测试带证据执行上下文渲染。

        验证证据列表正确渲染。
        """
        context = SubAgentExecutionContext(
            run_id="run-005",
            generated_at=time.time(),
            goal="测试",
            thought="思考",
            plan=["计划"],
            evidence=[
                {
                    "kind": "test_result",
                    "summary": "测试通过",
                    "ok": True,
                    "command": "pytest",
                    "path": "/tests",
                }
            ],
        )

        result = render_execution_context_markdown(context)

        assert "## Evidence" in result
        assert "测试通过" in result
        assert "pytest" in result


class TestRunnerResultRendering:
    """测试 Runner 结果渲染功能。"""

    def test_render_runner_result_markdown_success(self):
        """测试成功 runner 结果渲染。

        验证成功结果正确渲染。
        """
        result = SubAgentRunnerResult(
            run_id="run-result-001",
            created_at=time.time(),
            dry_run=False,
            ok=True,
            status="completed",
            verification_status="VERIFIED",
            message="任务完成",
            backend="openai",
            tool_rounds=3,
            runner_attempts=1,
        )

        output = render_runner_result_markdown(result)

        assert "# SUBAGENT RUNNER RESULT" in output
        assert "run_id: run-result-001" in output
        assert "OK" in output
        assert "execute" in output
        assert "completed" in output

    def test_render_runner_result_markdown_failure(self):
        """测试失败 runner 结果渲染。

        验证失败结果正确渲染。
        """
        result = SubAgentRunnerResult(
            run_id="run-result-002",
            created_at=time.time(),
            dry_run=False,
            ok=False,
            status="failed",
            verification_status="UNVERIFIED",
            message="执行失败",
            runner_last_error="网络超时",
        )

        output = render_runner_result_markdown(result)

        assert "# SUBAGENT RUNNER RESULT" in output
        assert "FAIL" in output
        assert "failed" in output
        assert "网络超时" in output

    def test_render_runner_result_markdown_dry_run(self):
        """测试 dry-run 模式 runner 结果渲染。

        验证 dry-run 模式正确显示。
        """
        result = SubAgentRunnerResult(
            run_id="run-result-003",
            created_at=time.time(),
            dry_run=True,
            ok=True,
            status="dry_run",
            verification_status="UNVERIFIED",
            message="dry-run 结果",
        )

        output = render_runner_result_markdown(result)

        assert "mode: dry-run" in output

    def test_render_runner_result_markdown_with_structured_output(self):
        """测试带结构化输出的 runner 结果渲染。

        验证结构化输出字段正确渲染。
        """
        result = SubAgentRunnerResult(
            run_id="run-result-004",
            created_at=time.time(),
            dry_run=False,
            ok=True,
            status="completed",
            verification_status="VERIFIED",
            message="完成",
            structured_output_found=True,
            structured_output_ok=True,
            structured_summary="任务摘要",
            blocked_reason="",
            structured_parse_error="",
        )

        output = render_runner_result_markdown(result)

        assert "## Structured Output" in output
        assert "summary: 任务摘要" in output

    def test_render_runner_result_markdown_with_artifact_repair_action(self):
        """产物结构失败时，报告要告诉父级派修复子代理而不是亲自改文件。"""
        result = SubAgentRunnerResult(
            run_id="run-result-artifact-blocked",
            created_at=time.time(),
            dry_run=False,
            ok=True,
            status="BLOCKED",
            verification_status="UNVERIFIED",
            message="等待父级处理",
            structured_output_found=True,
            structured_output_ok=True,
            structured_summary="artifact integrity check failed; repair listed files",
            blocked_reason="artifact_integrity_failed:/tmp/workspace/index.html:placeholder_hash_link",
            output_json="/tmp/workspace/runtime/subagents/worker/output.json",
        )

        output = render_runner_result_markdown(result)

        assert "## Parent Next Action" in output
        assert "不要直接改业务产物" in output
        assert "repair worker" in output
        assert "output_json" in output

    def test_render_runner_result_markdown_with_counts(self):
        """测试带计数的 runner 结果渲染。

        验证各种计数字段正确渲染。
        """
        result = SubAgentRunnerResult(
            run_id="run-result-005",
            created_at=time.time(),
            dry_run=False,
            ok=True,
            status="completed",
            verification_status="VERIFIED",
            message="完成",
            evidence_count=5,
            capability_request_count=2,
            artifact_count=3,
            test_count=10,
            patch_count=2,
            lesson_count=1,
        )

        output = render_runner_result_markdown(result)

        assert "evidence_count: 5" in output
        assert "test_count: 10" in output
        assert "patch_count: 2" in output


class TestChannelProbeRendering:
    """测试通道探测渲染功能。"""

    def test_render_channel_probe_markdown_basic(self):
        """测试基本通道探测报告渲染。

        验证通道探测报告正确渲染。
        """
        report = ChannelProbeReport(
            generated_at=time.time(),
            summary={"total": 1, "healthy": 1},
            results=[
                ChannelProbeResult(
                    run_id="probe-001",
                    channel_status="healthy",
                    checks=[
                        ChannelProbeCheck(
                            name="network",
                            ok=True,
                            summary="网络正常",
                            severity="P1",
                        )
                    ],
                    goal="测试目标",
                )
            ],
        )

        output = render_channel_probe_markdown(report)

        assert "# SUBAGENT CHANNEL PROBE" in output
        assert "probe-001" in output
        assert "healthy" in output

    def test_render_channel_probe_markdown_empty(self):
        """测试空通道探测报告渲染。

        验证没有结果时报告正确渲染。
        """
        report = ChannelProbeReport(
            generated_at=time.time(),
            summary={"total": 0},
            results=[],
        )

        output = render_channel_probe_markdown(report)

        assert "# SUBAGENT CHANNEL PROBE" in output
        assert "暂无可检查的子代理记录" in output

    def test_render_single_channel_probe_markdown_basic(self):
        """测试单个通道探测渲染。

        验证单个探测结果正确渲染。
        """
        result = ChannelProbeResult(
            run_id="single-probe-001",
            channel_status="healthy",
            created_at=time.time(),
            task_dir="/tmp/probe",
            checks=[
                ChannelProbeCheck(
                    name="network_check",
                    ok=True,
                    summary="网络连接正常",
                    severity="P1",
                    evidence_path="/tmp/network.log",
                )
            ],
        )

        output = render_single_channel_probe_markdown(result)

        assert "# CHANNEL PROBE" in output
        assert "single-probe-001" in output
        assert "healthy" in output
        assert "network_check" in output

    def test_render_single_channel_probe_markdown_with_failures(self):
        """测试带失败的通道探测渲染。

        验证失败检查项正确渲染。
        """
        result = ChannelProbeResult(
            run_id="probe-fail-001",
            channel_status="broken",
            created_at=time.time(),
            task_dir="/tmp",
            checks=[
                ChannelProbeCheck(
                    name="network",
                    ok=False,
                    summary="网络连接失败",
                    severity="P1",
                    error="Connection refused",
                )
            ],
        )

        output = render_single_channel_probe_markdown(result)

        assert "FAIL" in output
        assert "Connection refused" in output


class TestRunnerItemLineRendering:
    """测试 runner 条目行渲染功能。"""

    def test_render_runner_item_line_with_path(self):
        """测试带路径的条目行渲染。

        验证路径作为标题正确渲染。
        """
        item = {
            "path": "/tmp/file.py",
            "kind": "file",
            "status": "modified",
        }

        result = _render_runner_item_line(item)

        assert "/tmp/file.py" in result
        assert "kind=file" in result
        assert "status=modified" in result

    def test_render_runner_item_line_with_name(self):
        """测试带名称的条目行渲染。

        验证名称作为标题正确渲染。
        """
        item = {
            "name": "test_file.py",
            "ok": True,
        }

        result = _render_runner_item_line(item)

        assert "test_file.py" in result
        assert "ok=True" in result

    def test_render_runner_item_line_empty(self):
        """测试空条目行渲染。

        验证空字典时有默认值。
        """
        item = {}

        result = _render_runner_item_line(item)

        assert "- item" in result


class TestRunnerResultEdgeCases:
    """测试 Runner 结果边界场景。"""

    def test_render_runner_result_with_long_message(self):
        """测试超长消息渲染。

        验证长消息可以正确渲染。
        """
        long_message = "A" * 1000

        result = SubAgentRunnerResult(
            run_id="run-long-msg",
            created_at=time.time(),
            dry_run=False,
            ok=True,
            status="completed",
            verification_status="VERIFIED",
            message=long_message,
        )

        output = render_runner_result_markdown(result)

        assert "## Message" in output
        assert long_message in output

    def test_render_runner_result_with_empty_backend(self):
        """测试空后端渲染。

        验证 backend 为空时显示 none。
        """
        result = SubAgentRunnerResult(
            run_id="run-no-backend",
            created_at=time.time(),
            dry_run=False,
            ok=True,
            status="completed",
            verification_status="VERIFIED",
            message="完成",
            backend="",
        )

        output = render_runner_result_markdown(result)

        assert "backend: none" in output

    def test_render_execution_context_with_long_goal(self):
        """测试超长目标渲染。

        验证长目标可以正确渲染。
        """
        long_goal = "B" * 500

        context = SubAgentExecutionContext(
            run_id="run-long-goal",
            generated_at=time.time(),
            goal=long_goal,
            thought="思考",
            plan=["计划"],
        )

        output = render_execution_context_markdown(context)

        assert long_goal in output
