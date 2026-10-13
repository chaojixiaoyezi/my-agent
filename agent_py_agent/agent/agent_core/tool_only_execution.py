# LLM: 宿主显式只读工具沿主代理同一身份、权限、执行、归档与事件账；不会构造模型请求，不创建第二棵任务树。
# 模块用途: 为宿主只读操作提供工具工作片与完整输出，当前 attempt 在成功、失败和中断时沿原路径收尾。
from __future__ import annotations

from .runtime.loop_models import RunParams


# LLM: params 来自准确 task/thread/claim，工具参数不提供身份；调用方持原执行租约和中断登记，停止不降为工具失败。
# 函数用途: 运行并归档一项显式只读工具，返回原执行回执和完整输出；不把有界展示正文当作检查事实。
def execute_read_only_tool(agent, params: RunParams, *, tool_name: str, arguments: dict):
    return execute_tool_work(agent, params, tool_name=tool_name, arguments=arguments, read_only=True)


# LLM: 显式 scheduler 动作仍通过真实 ToolExecutor 权限、审批、取消与原 attempt 账；read_only=False 不代表授权绕过。
# 函数用途: 执行已登记的一项工具工作，不调用模型；预检继续要求只读，NO 规则使用正常工具策略。
def execute_tool_work(agent, params: RunParams, *, tool_name: str, arguments: dict, read_only: bool = False):
    from ..common.cancellation import CancellationToken
    from ..concurrency.interrupt import is_interrupted, register_interrupt_callback
    from ..tooling.models import tool_effect_for_runtime_policy
    from ..tooling.runtime_contracts import ToolCall
    from ._tool_loop_service import _record_tool_call
    from .runtime_mixin import (
        RunCloseoutFacts,
        _bind_main_agent_turn_params,
        _settle_main_agent_run_status,
        current_prompt_scope,
    )
    from .tool_call_runtime import ToolCallRuntimeRequest, execute_traced_tool_call
    from .tool_loop.round_execution import ToolCallExecuteParams, ToolCallRecordParams

    bound = _bind_main_agent_turn_params(agent, params.root_user_prompt, params)
    status = "failed"
    cancellation = CancellationToken(_external_check=is_interrupted)
    try:
        with current_prompt_scope(agent, params.root_user_prompt, bound), register_interrupt_callback(cancellation.cancel):
            if is_interrupted():
                raise InterruptedError("宿主工具执行已中断")
            snapshot = agent.tools.runtime_snapshot(allowed_tools=[tool_name], run_id=bound.run_id)
            runtime = snapshot.runtime(tool_name)
            if runtime is None or (read_only and tool_effect_for_runtime_policy(runtime.runtime_policy, arguments) != "read_only"):
                raise ValueError("宿主只读工具不可用或具有写入副作用")
            call = ToolCall(call_id=f"host-tool-{params.request_id}", tool_name=tool_name, arguments=arguments,
                source_protocol="text", schema_hash=runtime.model_spec.schema_hash,
                run_id=bound.run_id, turn_id=bound.request_id, attempt_id=bound.attempt_id)
            loop = _tool_only_params(bound, snapshot, cancellation, tool_name)
            outputs = []
            result = execute_traced_tool_call(ToolCallRuntimeRequest(agent,
                ToolCallExecuteParams(loop, 1, 1, call, actor="host"), call, outputs.append))
            _record_tool_call(agent, ToolCallRecordParams(loop, 1, 1, result.call, result.result,
                execution_states=result.states, actor="host"))
            if is_interrupted():
                raise InterruptedError("宿主工具执行已中断")
            status = "ok" if result.result.ok else "failed"
            return result, outputs[-1] if outputs else ""
    except (InterruptedError, KeyboardInterrupt):
        status = "cancelled"
        raise
    finally:
        _settle_main_agent_run_status(agent, run_id=bound.run_id, attempt_id=bound.attempt_id,
            facts=RunCloseoutFacts(runtime_status=status, runtime_reason="host_tool_work", tool_rounds=1))


# LLM: 只装配已有 ToolLoopExecuteParams，不启动模型或消费输入；原 run/attempt、工作区、取消及归档配置沿用宿主绑定。
# 函数用途: 将只读工作片接入共享工具链，模型上下文容器只在本次调用内使用。
def _tool_only_params(bound, snapshot, cancellation, tool_name):
    from ._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(user_prompt=bound.root_user_prompt, root_user_prompt=bound.root_user_prompt,
        memories=[], runtime_injections=[], prompt_files=[], tool_catalog_section="", tool_recommendations_section="",
        tool_context=[], effective_on_chunk=bound.on_chunk, allowed_tools=[tool_name], write_boundary=bound.write_boundary,
        task_attributes=bound.task_attributes, request_id=bound.request_id, run_id=bound.run_id, task_id=bound.task_id,
        attempt_id=bound.attempt_id, one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        tool_runtime_snapshot=snapshot, cancellation_token=cancellation, source=bound.source, save=bound.save,
        conversation_turn_id=bound.conversation_turn_id, consume_pending_turn_input=False)
