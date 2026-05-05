"""ToolLoopService: tool-calling loop execution."""

from __future__ import annotations

from ..memory_archive import snapshots
from ..tools import ToolExecutionResult
from ._runtime_params import ToolLoopExecuteParams
from .parameters import _one_shot_tool_call_key


class ToolLoopService:
    """Service for executing the main tool-calling loop."""

    def __init__(self, agent):
        self._agent = agent

    def execute(self, params: ToolLoopExecuteParams):
        """Execute the main tool-calling loop."""
        final_prompt = ""
        final_response = None
        tool_rounds = params.tool_rounds

        while True:
            final_prompt = self._agent.prompts.build(
                params.user_prompt,
                params.memories,
                inject=params.runtime_injections,
                prompt_files=params.prompt_files,
                tool_catalog_section=params.tool_catalog_section,
                tool_recommendations_section=params.tool_recommendations_section,
                tool_context=params.tool_context,
            )
            response = self._agent.backend.generate(
                final_prompt, on_chunk=params.effective_on_chunk
            )
            final_response = response

            if not self._agent.config.enable_tools:
                break

            calls = self._agent.tools.parse_tool_calls(response.text)
            if not calls:
                break

            effective_max_tool_rounds = self._agent.config.max_tool_rounds
            attrs_to_check = (
                params.task_attributes
                if params.task_attributes
                else getattr(self._agent, "_current_task_attributes", None)
            )
            if attrs_to_check and "max_tool_rounds" in attrs_to_check:
                effective_max_tool_rounds = int(attrs_to_check["max_tool_rounds"])

            if tool_rounds >= effective_max_tool_rounds:
                params.tool_context.append(
                    "[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。"
                )
                final_prompt = self._agent.prompts.build(
                    params.user_prompt,
                    params.memories,
                    inject=params.runtime_injections,
                    prompt_files=params.prompt_files,
                    tool_catalog_section=params.tool_catalog_section,
                    tool_recommendations_section=params.tool_recommendations_section,
                    tool_context=params.tool_context,
                )
                final_response = self._agent.backend.generate(
                    final_prompt, on_chunk=params.effective_on_chunk
                )
                break

            tool_rounds += 1
            params.tool_context.append(f"[assistant-tool-round-{tool_rounds}]\n{response.text}")
            for idx, payload in enumerate(calls, start=1):
                one_shot_key = _one_shot_tool_call_key(payload)
                if one_shot_key and one_shot_key in params.one_shot_tool_calls:
                    tool_name = str(payload.get("tool") or "unknown")
                    result = ToolExecutionResult(
                        tool_name,
                        False,
                        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
                        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
                    )
                else:
                    result = self._agent.tools.execute_call(
                        payload,
                        allowed_tools=params.allowed_tools,
                        granted_capabilities=params.granted_capabilities,
                        write_boundary=params.write_boundary,
                    )
                    if one_shot_key and result.ok:
                        params.one_shot_tool_calls.add(one_shot_key)
                if result.ok and result.tool not in {"__parse_error__", "unknown"}:
                    params.executed_tools.append(result.tool)
                params.archive_tool_calls.append(
                    {
                        "tool": result.tool,
                        "id": f"{tool_rounds}-{idx}",
                        "ok": result.ok,
                        "parameters": payload,
                    }
                )
                params.tool_context.append(
                    f"[tool-call-{tool_rounds}-{idx}]\n{payload}\n"
                    f"[tool-result-{tool_rounds}-{idx}]\n{result.render_for_prompt()}"
                )

        return final_prompt, final_response, tool_rounds