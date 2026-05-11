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
