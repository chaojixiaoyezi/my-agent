"""agent_core.runner.prompts 单元测试。

测试子代理 runner 提示词模板构建、变量替换和上下文注入功能。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestBuildSubagentRunnerPrompt:
    """测试 _build_subagent_runner_prompt() 函数。"""

    def _make_context(self, run_id: str, goal: str = "测试任务", **kwargs):
        """创建 SubAgentExecutionContext 的辅助方法。"""
        from agent_py_agent.agent.subagents import SubAgentExecutionContext

        defaults = dict(
            generated_at=1234567890.0,
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
        )
        defaults.update(kwargs)
        return SubAgentExecutionContext(run_id=run_id, goal=goal, **defaults)

    def test_prompt_contains_execution_context(self):
        """验证 prompt 包含执行上下文 JSON。"""
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context("run_123", "测试任务", task_dir="/tmp/test")
        prompt = _build_subagent_runner_prompt(context)

        assert "run_123" in prompt
        assert "测试任务" in prompt
        assert "Execution Context JSON" in prompt

    def test_prompt_contains_extra_instruction(self):
        """验证 prompt 使用额外指令。"""
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context("run_456", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context, instruction="优先使用 read_file")

        assert "优先使用 read_file" in prompt

    def test_prompt_uses_default_instruction_when_empty(self):
        """验证空指令时使用默认指令。"""
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context("run_default", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context, instruction="")

        assert "Extra Instruction" in prompt

    def test_prompt_requests_natural_result_without_machine_markers(self):
        """验证 prompt 要求自然回复且不再输出机器结果块。"""
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context("run_result", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context)

        assert "不要输出 SUBAGENT_RESULT" in prompt
        assert "[/SUBAGENT_RESULT]" not in prompt
        assert "简洁最终回复" in prompt

    def test_prompt_describes_role(self):
        """验证 prompt 说明子代理角色。"""
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context("run_role", role="coder", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context)

        assert "父代理" in prompt

    def test_prompt_says_write_file_creates_parent_dirs(self):
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context(
            "run_write_parent",
            role="worker",
            task_dir="/tmp",
            allowed_tools=["write_file", "apply_patch"],
        )
        prompt = _build_subagent_runner_prompt(context)

        assert "自动创建父目录" in prompt
        assert "不要因为目标目录尚未创建就标记 BLOCKED" in prompt

    def test_prompt_routes_single_text_file_deletion_to_apply_patch(self):
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context(
            "run_delete_file",
            role="worker",
            task_dir="/tmp",
            allowed_tools=["run_command", "apply_patch"],
        )
        prompt = _build_subagent_runner_prompt(context)

        assert "*** Delete File" in prompt
        assert "不要改用 rm/rmdir/unlink" in prompt

    def test_prompt_uses_slim_context_summary_instead_of_full_bundle(self):
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        huge_blob = "BLOAT" * 6000
        context = self._make_context(
            "run_slim",
            "写 index.html",
            task_dir="/tmp/task",
            execution_context_json="/tmp/task/execution_context.json",
            execution_context_file="/tmp/task/EXECUTION_CONTEXT.md",
            context_bundle_json="/tmp/task/context_bundle.json",
            context_bundle_file="/tmp/task/CONTEXT_BUNDLE.md",
            context_bundle={
                "task_envelope": {
                    "schema_version": "subagent_task_envelope.v1",
                    "address": {"run_id": "run_slim", "lineage": ["parent", "run_slim"]},
                    "acceptance": {"checks": ["index.html 存在"]},
                },
                "tool_preflight": {"ok": True, "issues": [], "effective_tools": ["write_file"]},
                "oversized_internal_body": huge_blob,
            },
        )

        prompt = _build_subagent_runner_prompt(context)

        assert "Execution Context JSON" in prompt
        assert "/tmp/task/execution_context.json" not in prompt
        assert "/tmp/task/EXECUTION_CONTEXT.md" not in prompt
        assert "/tmp/task/context_bundle.json" in prompt
        assert "subagent_task_envelope.v1" in prompt
        assert "oversized_internal_body" not in prompt
        assert huge_blob[:100] not in prompt
        assert len(prompt) < 12000

    def test_prompt_shows_read_refs_without_dependency_gate_language(self, tmp_path):
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt
        from agent_py_agent.agent.subagents.models import ContextManifest

        workspace = tmp_path / "task"
        workspace.mkdir()
        artifact = workspace / "data_collection.md"
        artifact.write_text("data", encoding="utf-8")
        missing_alias = "data/subagents/subagent-数据收集/data_collection.md"
        context = self._make_context(
            "run_downstream",
            "整合上游结果，生成 final_report.md",
            task_dir=str(workspace / "data" / "subagents" / "run_downstream"),
            context_manifest=ContextManifest(required_read_paths=[missing_alias, "data_collection.md"]),
            write_boundary={"product_write_roots": [str(workspace)]},
            context_bundle={
                "output_contract": {
                    "required_file_refs": [str(workspace / "final_report.md")],
                },
            },
            allowed_tools=["read_file", "write_file"],
        )

        prompt = _build_subagent_runner_prompt(context)

        assert "resolved_read_paths" in prompt
        assert str(artifact) in prompt
        assert missing_alias not in prompt
        assert "data_collection.md" in prompt
        assert str(workspace / "final_report.md") in prompt
        assert "不是启动前置条件" in prompt
        assert "input_contract" not in prompt

    def test_prompt_filters_host_owned_closeout_refs_from_old_bundle(self):
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context(
            "run-old-bundle",
            "实现模块并在最终回复中汇报",
            task_dir="/tmp/task/work/agents/run-old-bundle",
            execution_context_json="/tmp/task/work/agents/run-old-bundle/execution_context.json",
            execution_context_file="/tmp/task/work/agents/run-old-bundle/EXECUTION_CONTEXT.md",
            write_boundary={
                "execution_cwd": "/tmp/product",
                "allowed_write_roots": ["/tmp/product"],
                "output_json": "/tmp/task/work/agents/run-old-bundle/output.json",
                "handoff_file": "/tmp/task/work/agents/run-old-bundle/HANDOFF.md",
            },
            context_bundle={
                "output_contract": {
                    "required_file_refs": ["/tmp/product/module.ts"],
                    "final_report_ref": "/tmp/task/work/agents/run-old-bundle/final_report.md",
                    "agent_run_final_report_ref": "/tmp/task/work/agents/run-old-bundle/final_report.md",
                    "run_closeout_ref": "/tmp/task/work/agents/run-old-bundle/output.json",
                },
                "runner_recovery_preflight": {
                    "recovery_refs": [
                        "/tmp/task/work/agents/run-old-bundle/checkpoint.json",
                        "/tmp/task/work/agents/run-old-bundle/output.json",
                        "/tmp/task/work/agents/run-old-bundle/runner_result.json",
                    ],
                    "runner_instruction": (
                        "先读 /tmp/task/work/agents/run-old-bundle/checkpoint.json，"
                        "再读 /tmp/task/work/agents/run-old-bundle/output.json"
                    ),
                },
                "task_envelope": {
                    "context_refs": {
                        "context_bundle": "/tmp/task/work/agents/run-old-bundle/context_bundle.json",
                        "execution_context": "/tmp/task/work/agents/run-old-bundle/execution_context.json",
                    },
                },
            },
            allowed_tools=["read_file", "write_file"],
        )

        prompt = _build_subagent_runner_prompt(context)

        assert "/tmp/product/module.ts" in prompt
        assert "agent_run_final_report_ref" not in prompt
        assert "/tmp/task/work/agents/run-old-bundle/execution_context.json" not in prompt
        assert "/tmp/task/work/agents/run-old-bundle/EXECUTION_CONTEXT.md" not in prompt
        assert "/tmp/task/work/agents/run-old-bundle/final_report.md" not in prompt
        assert "/tmp/task/work/agents/run-old-bundle/output.json" not in prompt
        assert "/tmp/task/work/agents/run-old-bundle/runner_result.json" not in prompt
        assert "/tmp/task/work/agents/run-old-bundle/HANDOFF.md" not in prompt
        assert "/tmp/task/work/agents/run-old-bundle/checkpoint.json" in prompt
        assert '"execution_cwd": "/tmp/product"' in prompt

    def test_prompt_exposes_targeted_collaboration_clue_packet(self):
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context(
            "run_responder",
            "根据收到的协作请求查找相关证据",
            task_dir="/tmp/task",
            allowed_tools=["inspect_agent_tree", "send_guidance"],
            context_bundle=_targeted_collaboration_bundle(),
        )

        prompt = _build_subagent_runner_prompt(context)

        assert "clue-A42" in prompt
        assert "corroborate_or_refute" in prompt
        assert "artifact://source-a/context" in prompt
        assert "expected_fields" in prompt


def _targeted_collaboration_bundle() -> dict[str, object]:
    return {
        "collaboration": {
            "targeted_request_count": 1,
            "targeted_requests": [_targeted_collaboration_request()],
        }
    }


def _targeted_collaboration_request() -> dict[str, object]:
    return {
        "case_id": "case-001",
        "request_id": "creq-001",
        "case_ref": "collaboration://case/case-001",
        "request_ref": "collaboration://request/creq-001",
        "question": "请按你的数据源查找是否存在同一线索。",
        "problem_statement": "上游代理发现一个需要多源佐证的开放线索。",
        "observed_facts": [{"label": "关键线索", "value": "clue-A42", "source_ref": "artifact://source-a"}],
        "query_intent": {"goal": "corroborate_or_refute"},
        "query_hints": [{"kind": "candidate_lookup", "value": "clue-A42"}],
        "response_contract": {"expected_fields": ["matched", "evidence_refs", "limitations"]},
        "context_refs": ["artifact://source-a/context"],
    }


class TestContextJsonSerialization:
    """测试执行上下文 JSON 序列化。"""

    def _make_context(self, run_id: str, goal: str = "任务", **kwargs):
        """创建 SubAgentExecutionContext 的辅助方法。"""
        from agent_py_agent.agent.subagents import SubAgentExecutionContext

        defaults = dict(
            generated_at=1234567890.0,
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
        )
        defaults.update(kwargs)
        return SubAgentExecutionContext(run_id=run_id, goal=goal, **defaults)

    def test_context_serializes_to_json(self):
        """验证上下文能序列化为 JSON。"""
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context(
            "run_json",
            "JSON 序列化测试",
            task_dir="/tmp/test",
            allowed_tools=["read_file", "write_file"],
        )
        prompt = _build_subagent_runner_prompt(context)

        assert "run_json" in prompt
        assert "JSON 序列化测试" in prompt


class TestPromptOutputRequirements:
    """测试 prompt 输出要求。"""

    def _make_context(self, run_id: str, goal: str = "任务", **kwargs):
        """创建 SubAgentExecutionContext 的辅助方法。"""
        from agent_py_agent.agent.subagents import SubAgentExecutionContext

        defaults = dict(
            generated_at=1234567890.0,
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
        )
        defaults.update(kwargs)
        return SubAgentExecutionContext(run_id=run_id, goal=goal, **defaults)

    def test_prompt_requests_natural_final_report(self):
        """验证 prompt 要求普通最终回复，而不是额外机器结果块。"""
        from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt

        context = self._make_context("run_parse", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context)

        assert "简洁最终回复" in prompt
        assert "不要输出 SUBAGENT_RESULT" in prompt
