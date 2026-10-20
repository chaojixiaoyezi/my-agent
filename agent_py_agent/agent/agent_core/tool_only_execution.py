# LLM: 宿主工具及必要通知表达共用原身份/attempt/取消/用量链；工具工作不请求模型，表达工作零工具，不创建第二棵任务树。
# 模块用途: 执行显式工具或已有事实的有界自然表达，各自在原attempt内结算，不重执业务。
from __future__ import annotations

from .host_turn_scope import HostTurn, host_turn_scope
from .runtime.loop_models import RunParams


# LLM: params 来自准确 task/thread/claim，工具参数不提供身份；调用方持原执行租约和中断登记，停止不降为工具失败。
# 函数用途: 运行并归档一项显式只读工具，返回原执行回执和完整输出；不把有界展示正文当作检查事实。
def execute_read_only_tool(agent, params: RunParams, *, tool_name: str, arguments: dict):
    return execute_tool_work(agent, params, tool_name=tool_name, arguments=arguments, read_only=True)


# LLM: 显式scheduler沿原权限/审批/取消；共享host轮统一收口，actual未知不伪结，read_only=False不代表授权绕过。
# 函数用途: 执行已登记的一项工具工作，不调用模型；预检继续要求只读，NO 规则使用正常工具策略。
def execute_tool_work(agent, params: RunParams, *, tool_name: str, arguments: dict, read_only: bool = False):
    from ..common.cancellation import CancellationToken
    from ..concurrency.interrupt import is_interrupted, register_interrupt_callback
    from ..tooling.models import tool_effect_for_runtime_policy
    from ..tooling.runtime_contracts import ToolCall
    from ._tool_loop_service import _record_tool_call
    from .tool_call_runtime import ToolCallRuntimeRequest, execute_traced_tool_call
    from .tool_loop.round_execution import ToolCallExecuteParams, ToolCallRecordParams

    cancellation = CancellationToken(_external_check=is_interrupted)
    with host_turn_scope(agent, params.root_user_prompt, params, reason="host_tool_work", tool_rounds=1) as turn:
        bound = turn.params
        with register_interrupt_callback(cancellation.cancel):
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
            turn.runtime_status = "ok" if result.result.ok else "failed"
            return result, outputs[-1] if outputs else ""


# LLM: 只装配已有 ToolLoopExecuteParams，不启动模型或消费输入；原 run/attempt、工作区、取消及归档配置沿用宿主绑定。
# 函数用途: 将只读工作片接入共享工具链，模型上下文容器只在本次调用内使用。
def _tool_only_params(bound, snapshot, cancellation, tool_name):
    from ._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(user_prompt=bound.root_user_prompt, root_user_prompt=bound.root_user_prompt,
        memories=[], runtime_injections=[], prompt_files=[], tool_catalog_section="", tool_recommendations_section="",
        tool_context=[], effective_on_chunk=bound.on_chunk, allowed_tools=[tool_name] if tool_name else [], write_boundary=bound.write_boundary,
        task_attributes=bound.task_attributes, request_id=bound.request_id, run_id=bound.run_id, task_id=bound.task_id,
        attempt_id=bound.attempt_id, one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        tool_runtime_snapshot=snapshot, cancellation_token=cancellation, source=bound.source, save=bound.save,
        conversation_turn_id=bound.conversation_turn_id, consume_pending_turn_input=False)


# LLM: 原通知阶段传入同一个HostTurn则不二次绑定/结算；独立旧入口仍自行创建原core轮，工具始终为空。
# 函数用途: 对尚无公开回复的宿主事实调用有界自然表达，保持身份开放直到外层末次投递完成。
def express_required_user_action(agent, params: RunParams, facts: dict, *, execution: HostTurn | None = None):
    from dataclasses import replace

    if execution is not None:
        execution.require(agent, params)
        return _express_required_user_action(agent, execution, facts)
    with host_turn_scope(agent, '', replace(params, source='background_user_action_notice', allowed_tools=[]),
            reason='required_user_action_notice', tool_rounds=0) as turn:
        return _express_required_user_action(agent, turn, facts)


# LLM: 输入已由外层core绑定；这里只采样原natural-user-reply，实际unknown/中断交外层，不提前结束宿主attempt。
# 函数用途: 在当前真实宿主轮生成一次合法零工具表达并保留原有拒绝/修复预算。
def _express_required_user_action(agent, turn: HostTurn, facts: dict):
    from dataclasses import replace

    from ..common.cancellation import CancellationToken
    from ..concurrency.interrupt import is_interrupted
    from ..contracts.background_public_receipt import BackgroundPublicReceiptError
    from ._tool_loop_service import _request_tool_loop_model_response
    from .model.response_provenance import response_provenance_error
    from .native_tool_protocol import select_tool_protocol
    from .tool_loop.natural_user_reply import (
        discard_pending_natural_user_reply,
        finish_natural_user_reply,
        natural_user_reply_model_params,
        natural_user_reply_rejection_reason,
        queue_natural_user_reply,
        retry_natural_user_reply,
    )

    bound = turn.params
    turn.require(agent, bound)
    turn.runtime_status = 'failed'
    loop = _tool_only_params(bound, agent.tools.runtime_snapshot(allowed_tools=[], run_id=bound.run_id),
        CancellationToken(_external_check=is_interrupted), '')
    loop = replace(loop, tool_protocol_snapshot=select_tool_protocol(agent, run_id=bound.run_id, request_id=bound.request_id))
    queue_natural_user_reply(loop, kind='background_user_action_required', facts=facts)
    while True:
        if is_interrupted():
            raise InterruptedError('后台通知已停止')
        _, response = _request_tool_loop_model_response(agent, natural_user_reply_model_params(loop), 0)
        if response.runtime_status in {'cancelled', 'aborted'}:
            turn.runtime_status = 'cancelled'
            discard_pending_natural_user_reply(loop)
            return response
        error = response_provenance_error(loop, response, purposes=('natural_user_reply',))
        reason = error or natural_user_reply_rejection_reason(response)
        if not reason:
            turn.runtime_status = 'ok'
            return finish_natural_user_reply(loop, response, accepted=True)
        if error or not retry_natural_user_reply(loop, rejection_reason=reason, response=response):
            raise BackgroundPublicReceiptError(error or 'BACKGROUND_RECEIPT_PUBLIC_UNAVAILABLE')
