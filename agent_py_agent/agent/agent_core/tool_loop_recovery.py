# LLM: Tool loop recovery helpers keep prompt-safety and scope helpers outside the main service.
# 模块用途: 承接工具轮数收口、read_artifact 作用域注入和长内容恢复上下文。

from __future__ import annotations

from ..backends import ModelResponse
from ..tooling.content_recovery_mode import (
    LongContentRecoveryRequest,
    long_content_recovery_context,
)
from ._runtime_params import ToolLoopExecuteParams
from .tool_round_execution import ToolCallRecordParams


# LLM: without_tool_call_after_limit enforces max-tool-round boundaries even if the model ignores the stop hint.
# 函数用途: 工具轮数已到顶后，如果模型仍输出工具调用，改成确定性停止说明，避免上层把新工具请求当最终答复。
def without_tool_call_after_limit(agent, response: ModelResponse) -> ModelResponse:
    if not agent.tools.parse_tool_calls(response.text):
        return response
    return ModelResponse(
        text=(
            "已达到最大工具轮数限制，系统已经停止执行新的工具调用。"
            "模型在收口阶段仍输出工具调用请求，后续工具请求不会被执行；"
            "请只基于已有工具结果总结，若已有证据足够则进入等待验收。"
        ),
        backend=response.backend,
    )


# LLM: payload_with_runtime_scope injects current runner ids into scoped tools without model involvement.
# 函数用途: 让 read_artifact 短引用按当前 run/task/request 解析，避免同号 artifact 跨 run 串线。
def payload_with_runtime_scope(agent, params: ToolLoopExecuteParams, payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    tool = str(payload.get("tool") or "")
    if tool == "read_artifact":
        scoped = dict(payload)
        scoped.setdefault("run_id", runtime_run_id(agent, params))
        scoped.setdefault("task_id", params.task_id)
        scoped.setdefault("request_id", params.request_id)
        return scoped
    if tool == "file_write_session":
        scoped = dict(payload)
        scoped["_runtime_scope"] = {
            "request_id": params.request_id,
            "run_id": runtime_run_id(agent, params),
            "task_id": params.task_id,
        }
        return scoped
    return payload


# LLM: runtime_run_id falls back to active subagent context for nested runner tool records.
# 函数用途: 子代理 runner 调用 agent.run(save=False) 时通常不显式传 run_id，这里补当前 runner id。
def runtime_run_id(agent, params: ToolLoopExecuteParams) -> str:
    return str(params.run_id or getattr(agent, "_current_subagent_run_id", "") or "")


# LLM: append_long_content_recovery_context makes large-write recovery a live prompt policy.
# 函数用途: 工具调用因长正文截断/拒绝失败时，给下一轮模型追加分块恢复规则。
def append_long_content_recovery_context(record: ToolCallRecordParams) -> None:
    context = long_content_recovery_context(
        LongContentRecoveryRequest(
            payload=record.payload,
            result_tool=record.result.tool,
            result_ok=record.result.ok,
            output=record.result.output,
        )
    )
    if context:
        record.params.tool_context.append(context)
