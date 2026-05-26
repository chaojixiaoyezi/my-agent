"""runner_prompts.py 单元测试。

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
        from agent_py_agent.agent.subagent import SubAgentExecutionContext

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
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

        context = self._make_context("run_123", "测试任务", task_dir="/tmp/test")
        prompt = _build_subagent_runner_prompt(context)

        assert "run_123" in prompt
        assert "测试任务" in prompt
        assert "Execution Context JSON" in prompt

    def test_prompt_contains_extra_instruction(self):
        """验证 prompt 使用额外指令。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

        context = self._make_context("run_456", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context, instruction="优先使用 read_file")

        assert "优先使用 read_file" in prompt

    def test_prompt_uses_default_instruction_when_empty(self):
        """验证空指令时使用默认指令。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

        context = self._make_context("run_default", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context, instruction="")

        assert "Extra Instruction" in prompt

    def test_prompt_contains_result_block_markers(self):
        """验证 prompt 包含结果块标记。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

        context = self._make_context("run_result", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context)

        assert "[SUBAGENT_RESULT]" in prompt
        assert "[/SUBAGENT_RESULT]" in prompt
        assert '"evidence_packets"' in prompt
        assert "artifact_refs" in prompt
        assert "evidence_refs" in prompt

    def test_prompt_describes_role(self):
        """验证 prompt 说明子代理角色。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

        context = self._make_context("run_role", role="coder", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context)

        assert "父代理" in prompt

    # LLM: Real leaf workers must know write_file can create missing product directories.
    # 函数用途: 防止叶子节点因为 build 目录不存在而误报缺少 mkdir/shell 能力。
    def test_prompt_says_write_file_creates_parent_dirs(self):
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

        context = self._make_context(
            "run_write_parent",
            role="leaf_worker",
            task_dir="/tmp",
            allowed_tools=["write_file", "apply_patch"],
        )
        prompt = _build_subagent_runner_prompt(context)

        assert "自动创建父目录" in prompt
        assert "不要因为目标目录尚未创建就标记 BLOCKED" in prompt

    # LLM: Real MiniMax runner E2E timed out when the prompt embedded full execution_context/context_bundle JSON.
    # 函数用途: 锁住 runner prompt 的瘦身边界：模型看摘要和 refs，不直接吞完整大 JSON。
    def test_prompt_uses_slim_context_summary_instead_of_full_bundle(self):
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

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
        assert "/tmp/task/execution_context.json" in prompt
        assert "/tmp/task/context_bundle.json" in prompt
        assert "subagent_task_envelope.v1" in prompt
        assert "oversized_internal_body" not in prompt
        assert huge_blob[:100] not in prompt
        assert len(prompt) < 12000

    # LLM: Read refs are visible to runners without becoming startup dependencies.
    # 函数用途: 路径线索进入 prompt，但语义是可读线索，不再要求 runner 先满足输入依赖门。
    def test_prompt_shows_read_refs_without_dependency_gate_language(self, tmp_path):
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt
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
        assert missing_alias in prompt
        assert "不是启动前置条件" in prompt
        assert "input_contract" not in prompt

    # LLM: Addressed collaboration requests must expose generic clue content, not just request ids.
    # 函数用途: 防止响应子代理只看到 case/request 引用却看不到开放世界线索、查询意图和响应形状。
    def test_prompt_exposes_targeted_collaboration_clue_packet(self):
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

        context = self._make_context(
            "run_responder",
            "根据收到的协作请求查找相关证据",
            task_dir="/tmp/task",
            allowed_tools=["case_status", "submit_evidence", "update_collaboration_request"],
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


class TestBuildSubagentRunnerRepairPrompt:
    """测试 _build_subagent_runner_repair_prompt() 函数。"""

    def _make_context(self, run_id: str, goal: str = "任务", **kwargs):
        """创建 SubAgentExecutionContext 的辅助方法。"""
        from agent_py_agent.agent.subagent import SubAgentExecutionContext

        defaults = dict(
            generated_at=1234567890.0,
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
        )
        defaults.update(kwargs)
        return SubAgentExecutionContext(run_id=run_id, goal=goal, **defaults)

    def test_repair_prompt_contains_parse_error(self):
        """验证修复 prompt 包含解析错误信息。"""
        from agent_py_agent.agent.agent_core.runner_prompts import (
            _build_subagent_runner_repair_prompt,
        )

        context = self._make_context("run_repair", task_dir="/tmp")
        prompt = _build_subagent_runner_repair_prompt(
            context,
            original_prompt="原始 prompt",
            original_response="原始响应",
            parse_error="缺少 status 字段",
        )

        assert "缺少 status 字段" in prompt
        assert "原始 prompt" in prompt
        assert "原始响应" in prompt

    def test_repair_prompt_default_error_message(self):
        """验证使用默认错误信息。"""
        from agent_py_agent.agent.agent_core.runner_prompts import (
            _build_subagent_runner_repair_prompt,
        )

        context = self._make_context("run_default_error", task_dir="/tmp")
        prompt = _build_subagent_runner_repair_prompt(
            context,
            original_prompt="prompt",
            original_response="response",
        )

        assert "上一轮回复缺少" in prompt or "Previous" in prompt

    def test_repair_prompt_includes_execution_context(self):
        """验证修复 prompt 包含执行上下文。"""
        from agent_py_agent.agent.agent_core.runner_prompts import (
            _build_subagent_runner_repair_prompt,
        )

        context = self._make_context("run_ctx", "测试上下文", task_dir="/tmp", allowed_tools=["read_file"])
        prompt = _build_subagent_runner_repair_prompt(
            context,
            original_prompt="",
            original_response="",
        )

        assert "Execution Context JSON" in prompt
        assert "测试上下文" in prompt

    # LLM: repair prompts must stay small enough for the model to close the JSON block.
    # 函数用途: 复现真实 E2E 中 repair prompt 太胖导致修复回复再次截断的问题。
    def test_repair_prompt_clips_large_prompt_and_response(self):
        from agent_py_agent.agent.agent_core.runner_prompts import (
            _build_subagent_runner_repair_prompt,
        )

        context = self._make_context("run_clip", task_dir="/tmp")
        prompt = _build_subagent_runner_repair_prompt(
            context,
            original_prompt="P" * 8000 + "PROMPT_TAIL",
            original_response="R" * 14000 + "RESPONSE_TAIL",
            parse_error="缺少结束标记",
        )

        assert "PROMPT_TAIL" in prompt
        assert "RESPONSE_TAIL" in prompt
        assert "[... clipped" in prompt
        assert "evidence_packets" in prompt
        assert "不要复述长报告" in prompt
        assert len(prompt) < 23000


class TestAppendRunnerRepairPrompt:
    """测试 _append_runner_repair_prompt() 函数。"""

    def test_appends_separator(self):
        """验证合并时添加分隔符。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _append_runner_repair_prompt

        result = _append_runner_repair_prompt("原始 prompt", "修复 prompt")

        assert "原始 prompt" in result
        assert "---" in result
        assert "修复 prompt" in result

    def test_includes_repair_header(self):
        """验证包含修复标记。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _append_runner_repair_prompt

        result = _append_runner_repair_prompt("base", "repair")

        assert "Structured Output Repair Prompt" in result


class TestAppendRunnerRepairResponse:
    """测试 _append_runner_repair_response() 函数。"""

    def test_appends_repair_response(self):
        """验证合并修复响应。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _append_runner_repair_response

        result = _append_runner_repair_response("原始响应", "修复响应")

        assert "原始响应" in result
        assert "---" in result
        assert "修复响应" in result

    def test_includes_repair_response_header(self):
        """验证包含修复响应标记。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _append_runner_repair_response

        result = _append_runner_repair_response("original", "repair")

        assert "Structured Output Repair Response" in result


class TestAppendRunnerRepairFailure:
    """测试 _append_runner_repair_failure() 函数。"""

    def test_includes_original_response(self):
        """验证包含原始响应。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _append_runner_repair_failure

        result = _append_runner_repair_failure("原始回复内容", ValueError("测试错误"))

        assert "原始回复内容" in result

    def test_includes_exception_info(self):
        """验证包含异常信息。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _append_runner_repair_failure

        result = _append_runner_repair_failure("response", ValueError("测试错误"))

        assert "ValueError" in result
        assert "测试错误" in result

    def test_includes_failure_header(self):
        """验证包含失败标记。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _append_runner_repair_failure

        result = _append_runner_repair_failure("resp", RuntimeError("运行错误"))

        assert "Structured Output Repair Failure" in result


class TestContextJsonSerialization:
    """测试执行上下文 JSON 序列化。"""

    def _make_context(self, run_id: str, goal: str = "任务", **kwargs):
        """创建 SubAgentExecutionContext 的辅助方法。"""
        from agent_py_agent.agent.subagent import SubAgentExecutionContext

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
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

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
        from agent_py_agent.agent.subagent import SubAgentExecutionContext

        defaults = dict(
            generated_at=1234567890.0,
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
        )
        defaults.update(kwargs)
        return SubAgentExecutionContext(run_id=run_id, goal=goal, **defaults)

    def test_prompt_requires_machine_parseable_result(self):
        """验证 prompt 要求机器可解析结果。"""
        from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt

        context = self._make_context("run_parse", task_dir="/tmp")
        prompt = _build_subagent_runner_prompt(context)

        assert "Required Output" in prompt
        assert "JSON" in prompt

    def test_repair_prompt_forbids_tool_calls(self):
        """验证修复 prompt 要求不调用工具。"""
        from agent_py_agent.agent.agent_core.runner_prompts import (
            _build_subagent_runner_repair_prompt,
        )

        context = self._make_context("run_no_tool", task_dir="/tmp")
        prompt = _build_subagent_runner_repair_prompt(
            context,
            original_prompt="prompt",
            original_response="response",
        )

        # 修复 prompt 应该说明不要调用工具
        assert "不要调用工具" in prompt or "don't call" in prompt.lower() or "no new" in prompt.lower()
