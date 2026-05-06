
from __future__ import annotations

from dataclasses import dataclass

from ..memory_archive import snapshots
from ..prompting_parts.builder import ToolSections
from ..tools import ToolExecutionResult
from ._runtime_params import ToolLoopExecuteParams
from .parameters import _one_shot_tool_call_key


@dataclass(frozen=True)
class ToolCallRecordParams:
    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object
    result: ToolExecutionResult


def _build_prompt(agent, params: ToolLoopExecuteParams) -> str:
    return agent.prompts.build(
        params.user_prompt,
        params.memories,
        inject=params.runtime_injections,
        prompt_files=params.prompt_files,
        tools=ToolSections(
            tool_catalog_section=params.tool_catalog_section,
            tool_recommendations_section=params.tool_recommendations_section,
            tool_context=params.tool_context,
        ),
    )


def _effective_max_tool_rounds(agent, params: ToolLoopExecuteParams) -> int:
    effective = agent.config.max_tool_rounds
    attrs_to_check = params.task_attributes or getattr(agent, "_current_task_attributes", None)
    if attrs_to_check and "max_tool_rounds" in attrs_to_check:
        return int(attrs_to_check["max_tool_rounds"])
    return effective


def _duplicate_one_shot_result(payload: dict[str, object]) -> ToolExecutionResult:
    tool_name = str(payload.get("tool") or "unknown")
    return ToolExecutionResult(
        tool_name,
        False,
        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
    )


class ToolLoopService:

    def __init__(self, agent):
        self._agent = agent

    def execute(self, params: ToolLoopExecuteParams):
        final_prompt = ""
        final_response = None
        tool_rounds = params.tool_rounds

        while True:
            final_prompt = _build_prompt(self._agent, params)
            response = self._agent.backend.generate(
                final_prompt, on_chunk=params.effective_on_chunk
            )
            final_response = response

            if not self._agent.config.enable_tools:
                break

            calls = self._agent.tools.parse_tool_calls(response.text)
            if not calls:
                break

            if self._tool_round_limit_reached(params, tool_rounds):
                final_prompt, final_response = self._final_response_after_tool_limit(params)
                break

            tool_rounds += 1
            params.tool_context.append(f"[assistant-tool-round-{tool_rounds}]\n{response.text}")
            for idx, payload in enumerate(calls, start=1):
                result = self._execute_one_tool_call(params, payload)
                self._record_tool_call(
                    ToolCallRecordParams(params, tool_rounds, idx, payload, result)
                )

        return final_prompt, final_response, tool_rounds

    def _tool_round_limit_reached(self, params: ToolLoopExecuteParams, tool_rounds: int) -> bool:
        return tool_rounds >= _effective_max_tool_rounds(self._agent, params)

    def _final_response_after_tool_limit(self, params: ToolLoopExecuteParams):
        params.tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
        final_prompt = _build_prompt(self._agent, params)
        final_response = self._agent.backend.generate(
            final_prompt, on_chunk=params.effective_on_chunk
        )
        return final_prompt, final_response

    def _execute_one_tool_call(self, params: ToolLoopExecuteParams, payload):
        one_shot_key = _one_shot_tool_call_key(payload)
        if one_shot_key and one_shot_key in params.one_shot_tool_calls:
            return _duplicate_one_shot_result(payload)
        result = self._agent.tools.execute_call(
            payload,
            allowed_tools=params.allowed_tools,
            granted_capabilities=params.granted_capabilities,
            write_boundary=params.write_boundary,
        )
        if one_shot_key and result.ok:
            params.one_shot_tool_calls.add(one_shot_key)
        return result

    def _record_tool_call(self, record: ToolCallRecordParams) -> None:
        if record.result.ok and record.result.tool not in {"__parse_error__", "unknown"}:
            record.params.executed_tools.append(record.result.tool)
        record.params.archive_tool_calls.append(
            {
                "tool": record.result.tool,
                "id": f"{record.tool_rounds}-{record.idx}",
                "ok": record.result.ok,
                "parameters": record.payload,
            }
        )
        record.params.tool_context.append(
            f"[tool-call-{record.tool_rounds}-{record.idx}]\n{record.payload}\n"
            f"[tool-result-{record.tool_rounds}-{record.idx}]\n"
            f"{record.result.render_for_prompt()}"
        )
