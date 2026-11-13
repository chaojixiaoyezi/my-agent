"""原工具仍在原worker执行；主runner仅在等候安全点答复已认证的新输入。"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import Future, TimeoutError
from dataclasses import is_dataclass, replace

_LOG = logging.getLogger(__name__)
# 双0保持无新增配额；仅给每个已获主回合准入的活跃工具一条同进程线程，退出显式join。
def submit_wait_item(item):
    future = Future()

    def run():
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = item.captured_context.run(item.run)
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)

    worker = threading.Thread(target=run, name='my-agent-tool-wait', daemon=True)
    future._tool_wait_worker = worker
    try:
        worker.start()
    except BaseException as error:
        future._tool_wait_worker = None
        future.set_exception(error)
    return future


def enabled(request):
    return (getattr(getattr(request.agent, 'config', None), 'tool_wait_input_reply_enabled', True) is True
            and getattr(request.params, 'consume_pending_turn_input', True) is not False)


def await_tool_futures(request, futures, *, input_reply=None, before_input_error=None):
    """不取消/替换Future，不造ToolResult；输入处理失败也等原物理工作完成并保留结果。"""
    reply = input_reply or reply_to_pending_input
    failed = False
    input_error = None
    next_input_probe = 0.0
    try:
        for future in futures:
            while not future.done():
                try:
                    future.result(timeout=0.1)
                except TimeoutError:
                    token = getattr(request.params, 'cancellation_token', None)
                    if (enabled(request) and not failed and not getattr(token, 'cancelled', False)
                            and time.monotonic() >= next_input_probe):
                        next_input_probe = time.monotonic() + 0.5
                        try:
                            reply(request)
                        except Exception as error:
                            from .round_execution import _must_propagate_round_exception
                            if _must_propagate_round_exception(error):
                                input_error = error
                            _LOG.warning('TOOL_WAIT_INPUT_REPLY_FAILED effect=tool_still_running error_type=%s error=%s',
                                         type(error).__name__, str(error)[:240])
                            failed = True
                # 工具自身异常不在这里消费；最终仍由原收集顺序传播。
                except Exception:
                    break
        results = [future.result() for future in futures]
        if input_error is not None:
            # 已启动工具的真实结果先交原轮次按序记账；输入失源不能把物理成功变成未执行或丢掉回执。
            if before_input_error is not None:
                try:
                    before_input_error(results, input_error)
                except BaseException as recording_error:
                    input_error.add_note('completed tool receipt: ' + type(recording_error).__name__)
            raise input_error
        return results
    except BaseException as error:
        if isinstance(error, (InterruptedError, KeyboardInterrupt)):
            cancel = getattr(getattr(request.params, 'cancellation_token', None), 'cancel', None)
            if callable(cancel):
                cancel('interrupted')
        raise
    finally:
        for future in futures:
            try:
                future.result()
            except BaseException:
                pass  # 原结果/异常已按原顺序处理；这里只等物理退出，不能替换首因。
            worker = getattr(future, '_tool_wait_worker', None)
            if worker is not None:
                worker.join()


def _reply_params(params):
    fields = dict(live_archive_state={}, tool_ir_history=[], tool_context=[],
                  runtime_injections=[], active_turn_user_inputs=[])
    if is_dataclass(params):
        return replace(params, **fields)
    value = copy.copy(params)
    for key, item in fields.items():
        setattr(value, key, item)
    return value


def _carry_reserved_input(original, params, packets):
    from ...conversation.active_turn_input import merge_active_turn_user_inputs

    original.active_turn_user_inputs[:] = merge_active_turn_user_inputs(
        list(getattr(original, 'active_turn_user_inputs', ()) or ()), packets)
    for key in ('_guidance_ack_ids', '_guidance_ack_entries'):
        value = params.live_archive_state.get(key)
        if isinstance(value, set):
            original.live_archive_state.setdefault(key, set()).update(value)
        elif isinstance(value, dict):
            original.live_archive_state.setdefault(key, {}).update(value)
    original.tool_context.extend(params.tool_context)
    _queue_resume_inputs(original, packets)


def _queue_resume_inputs(params, packets):
    queue = params.live_archive_state.setdefault('_tool_wait_resume_inputs', [])
    known = {tuple(packet['input_ids']) for packet in queue}
    queue.extend(dict(packet) for packet in packets if tuple(packet['input_ids']) not in known)


def flush_resume_inputs(params):
    """原工具轮全部配对后再追加UserTurn，保留媒体给下一次主模型出站。"""
    from ..native_tool_protocol import native_tool_use_active
    from ..tool_ir_history import record_user_turn_ir

    state = getattr(params, 'live_archive_state', None)
    if not isinstance(state, dict):
        return
    queue = state.get('_tool_wait_resume_inputs', [])
    while queue:
        packet = queue[0]
        if native_tool_use_active(params):
            record_user_turn_ir(params, packet['text'], input_ids=tuple(packet['input_ids']),
                                media=tuple(packet.get('input_media', ())))
        queue.pop(0)


def reply_to_pending_input(request):
    from ...conversation.active_turn_input import merge_active_turn_user_inputs
    from ...conversation.auxiliary_model_call import (
        AuxiliaryModelCallRequest,
        generate_auxiliary_model_response,
    )
    from ...conversation.guidance_reply_obligations import pending, settle
    from ...conversation.user_visible_text import sanitize_user_visible_text
    from ..model.call_runtime import model_call_ledger
    from ..model.response_provenance import stamp_response_provenance
    from ..model.visible_response import commit_visible_response
    from ..runtime.guidance import (
        acknowledge_injected_turn_input,
        has_pending_turn_input,
        inject_pending_turn_input,
        mark_injected_turn_input_submitted,
        restore_turn_input_after_failed_provider_call,
    )

    agent, original = request.agent, request.params
    if not has_pending_turn_input(agent, original, user_input_only=True):
        return False
    store = getattr(agent, 'conversation_store', None)
    attributes = getattr(original, 'task_attributes', None) or {}
    thread_id = str(attributes.get('conversation_thread_id') or '')
    if store is None or not thread_id:
        return False
    sink = getattr(original, 'effective_on_chunk', None) or getattr(original, 'on_chunk', None)
    if not callable(getattr(sink, 'commit_model_response', None)):
        return False
    params = _reply_params(original)
    try:
        inject_pending_turn_input(agent, params, user_input_only=True)
    except BaseException:
        _carry_reserved_input(original, params, list(params.active_turn_user_inputs))
        raise
    packets = list(params.active_turn_user_inputs)
    if not packets:
        return False  # 子代理/运行事件不冒充本人提问。
    _carry_reserved_input(original, params, packets)
    if any(packet.get('input_media') for packet in packets):
        return False  # 媒体未在薄调用编码，不提交/ACK；留原主请求完整消费。
    input_ids = tuple(identity for packet in packets for identity in packet['input_ids'])
    question = '\n'.join(packet['text'] for packet in packets)
    entries, errors = store.messages.recent_report(thread_id, limit=20)
    if errors:
        raise ValueError('TOOL_WAIT_INPUT_CONTEXT_UNAVAILABLE')
    messages = [{'role': row.role, 'content': row.content[-2000:]} for row in entries
                if row.role in {'user', 'assistant'}][-20:]
    calls = []
    source_entries = list(params.live_archive_state.get('_guidance_ack_entries', {}).values())
    source_channels = {str((entry.metadata or {}).get('channel') or '') for entry in source_entries}

    def submitted(call_id):
        calls.append(call_id)
        mark_injected_turn_input_submitted(agent, params, provider_call_id=call_id)

    try:
        response = generate_auxiliary_model_response(AuxiliaryModelCallRequest(agent=agent,
            prompt=question, messages=messages, tools=[],
            system_instruction=('回答当前用户的新输入。原工具尚未返回最终结果，本轮仅答复和确认新要求，不执行工具。'
                '进度只依据所给事实；不能声称原命令或新任务已经完成。后续主执行会收到原始新要求。'
                '\n运行事实：'+json.dumps({'state': 'waiting_for_tool', 'task_id': original.task_id,
                    'run_id': original.run_id, 'command_completed': False}, ensure_ascii=False)),
            request_id=original.request_id, run_id=original.run_id, task_id=original.task_id,
            attempt_id=str(getattr(original, 'attempt_id', '') or ''), thread_id=thread_id,
            purpose='active_turn_input_reply', on_submitted=submitted))
        if str(getattr(response, 'runtime_status', 'ok') or 'ok') != 'ok' or not str(getattr(response, 'text', '')).strip():
            raise ValueError('TOOL_WAIT_INPUT_REPLY_EMPTY_OR_REJECTED')
        acknowledge_injected_turn_input(agent, params)
    except BaseException:
        if calls:
            restore_turn_input_after_failed_provider_call(agent, params, provider_call_id=calls[-1])
        raise
    for key in ('_guidance_ack_ids', '_guidance_ack_entries'):
        value = original.live_archive_state.get(key)
        if isinstance(value, set):
            value.difference_update(input_ids)
        elif isinstance(value, dict):
            for identity in input_ids:
                value.pop(identity, None)
    original.active_turn_user_inputs[:] = merge_active_turn_user_inputs(
        original.active_turn_user_inputs, packets)
    reply_task_id = str(attributes.get('conversation_task_id') or original.task_id)
    accepted = set(pending(store.storage, thread_id, reply_task_id))
    reply_ids = tuple(identity for identity in input_ids if identity in accepted)
    if not reply_ids:
        return False
    response.text = sanitize_user_visible_text(response.text).content
    key = hashlib.sha256(json.dumps([agent.home_paths.owner_id, thread_id, reply_task_id, reply_ids]).encode()).hexdigest()
    message = store.messages.append_once({'thread_id': thread_id, 'role': 'assistant', 'content': response.text,
        'metadata': {'reply_is_interim': True, 'user_reply_input_ids': list(reply_ids),
                     'task_id': original.task_id, 'run_id': original.run_id, 'model_call_id': calls[-1]}},
        dedupe_key='active-tool-input-reply:' + key)
    stamp_response_provenance(params, model_call_ledger(agent), calls[-1], response)
    delivered = commit_visible_response(params, response, boundary='tools', canonical_message_id=message.message_id)
    thread = store.threads.require(thread_id)
    bindings = {binding.channel for binding in thread.channel_bindings}
    local_transport = source_channels == {'chat'} or (source_channels == {''} and bindings == {'chat'})
    local_delivery = delivered and local_transport
    from ..native_tool_protocol import native_tool_use_active

    # native注入会投影成IR尾部事实；文本路径的注入区在旧工具历史之前，必须改沿原tool_context尾部追加。
    context = original.runtime_injections if native_tool_use_active(original) else original.tool_context
    context.append(('[工具等待时已向用户回复]' if local_delivery
        else '[临时回复已生成，原通道交付仍待确认]') + '\n' + message.content)
    if local_delivery:
        settle(store.storage, thread_id, reply_task_id, reply_ids)
    return True
