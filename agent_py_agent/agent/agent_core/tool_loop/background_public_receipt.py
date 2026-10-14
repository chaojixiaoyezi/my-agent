# LLM: 此合同只在冻结的 explicit+origin 后台回合生效；完整 JSON 可带单一 json 围栏，语义由模型判断，宿主只守形态、来源和已有回复义务。
# 模块用途: 分离后台内部执行结果与公开通知，允许无公开内容的成功运行，并复用原零工具表达修复。
from __future__ import annotations

from dataclasses import replace

from ...common.structured_output import json_value_from_complete_response
from ...contracts.background_public_receipt import CONTRACT_KEY, RESULT_SCHEMA
from ...conversation.user_visible_text import sanitize_user_visible_text
from ..model.response_provenance import response_provenance_error, response_provenance_projection
from ..runtime.guidance import active_turn_user_reply_required, satisfy_active_turn_user_reply

_RESULT_KEYS = {'schema', 'internal_result', 'notification_intent', 'public_reply'}


# LLM: 仅认宿主装配的已知完成合同，不读取模型 attrs、原始正文或默认猜测。
# 函数用途: 判断当前循环是否使用明确开启的后台公开回执协议。
def explicit_public_receipt_active(params: object) -> bool:
    contract = getattr(params, 'delivery_contract', None)
    fact = contract.get(CONTRACT_KEY) if isinstance(contract, dict) else None
    return isinstance(fact, dict) and fact.get('schema') == RESULT_SCHEMA and fact.get('reply_policy') == 'explicit'


# LLM: 协议说明只影响明确启用的后台最终输出，工具阶段仍使用原协议；internal_result 不是宿主已核验事实。
# 函数用途: 让主模型在原业务轮的最终响应中显式分开内部结果与公开内容，正常成功不增调用。
def public_receipt_contract_prompt(params: object) -> str:
    if not explicit_public_receipt_active(params):
        return ''
    return (
        '[background-result-contract]\n'
        '本轮后台最终完成响应必须是一个完整 JSON 对象，只有 schema、internal_result、notification_intent、public_reply 四个字段。'
        'schema 固定为 background-result.v1；internal_result 是非空的内部执行总结，它不是已核验事实声明；'
        'notification_intent 只能是 publish 或 internal_only。需要通知时用 publish，把直接给用户的内容放 public_reply 字符串；'
        '本轮没有对外事项时用 internal_only，public_reply 必须为空字符串，仍在 internal_result 保留执行结论。'
        '不要用占位文字表示静默，也不要把内部判断 JSON 当作公开回执；用户需要合法 JSON 时，完整放入 public_reply 字符串。'
        '公开内容须围绕当前真实任务和已发生工具结果，无法确认的事实如实说明，不能捏造告警或成功。'
        '实际待交付附件或已接收用户插话的回复义务不能被 internal_only 取消。'
        '还要继续执行时照常调用原工具，只有最终零工具响应采用本协议。'
    )


# LLM: 只解码整个受管响应的一份 JSON 或其单一 json 围栏，再校验显式 schema；不得搜索说明文字中的对象或改变普通正文路径。
# 函数用途: 校验完整后台完成信封，兼容无歧义的模型展示包装，返回结构化声明或明确的协议错误。
def parse_background_result(text: str) -> tuple[dict[str, str] | None, str]:
    try:
        value = json_value_from_complete_response(text)
    except (ValueError, TypeError):
        return None, 'BACKGROUND_RECEIPT_CONTRACT_INVALID'
    if not isinstance(value, dict) or set(value) != _RESULT_KEYS:
        return None, 'BACKGROUND_RECEIPT_CONTRACT_INVALID'
    if any(not isinstance(value[key], str) for key in _RESULT_KEYS):
        return None, 'BACKGROUND_RECEIPT_CONTRACT_INVALID'
    if value['schema'] != RESULT_SCHEMA or not value['internal_result'].strip():
        return None, 'BACKGROUND_RECEIPT_CONTRACT_INVALID'
    if value['notification_intent'] not in {'publish', 'internal_only'}:
        return None, 'BACKGROUND_RECEIPT_CONTRACT_INVALID'
    if value['notification_intent'] == 'internal_only' and value['public_reply'].strip():
        return None, 'BACKGROUND_RECEIPT_CONTRACT_INVALID'
    return value, ''


# LLM: 待投附件来自已声明目标和本 run 原工具产物引用；只复用finalization提取器，不解析正文路径或扫描目录。
# 函数用途: 在静默收口前核对是否仍有实际附件必须交付。
def _has_pending_delivery_artifacts(params: object) -> bool:
    from types import SimpleNamespace

    from .._finalization_service import _structured_delivery_artifacts

    context = SimpleNamespace(task_attributes=getattr(params, 'task_attributes', None),
                              archive_tool_calls=getattr(params, 'archive_tool_calls', None))
    return bool(_structured_delivery_artifacts(context))


# LLM: 合同错误只拒绝错误协议或来源，不裁决报告深度；不能把错误正文或空供应商响应降级成成功静默。
# 函数用途: 为不可公开的后台完成响应给出无正文的明确失败状态。
def _invalid_receipt(response: object, code: str) -> object:
    return replace(response, text='', runtime_status='failed', runtime_reason=code,
                   runtime_source='background_public_receipt', public_receipt=None)


# LLM: 回执修复的执行证据只来自本run原工具账，投影有界，不把模型内部总结升级为宿主证明。
# 函数用途: 提供最近实际工具结果的状态、效果与产物引用，让一次表达修复能围绕已有事实说明。
def _tool_receipt_facts(params: object) -> list[dict[str, object]]:
    records = [item for item in (getattr(params, 'archive_tool_calls', None) or []) if isinstance(item, dict)]
    facts = []
    for record in records[-12:]:
        envelope = record.get('tool_result_envelope')
        envelope = envelope if isinstance(envelope, dict) else {}
        facts.append({'call_id': record.get('call_id', ''), 'tool': record.get('tool', ''),
            'ok': record.get('ok') is True, 'status': envelope.get('status', ''),
            'effect_outcome': envelope.get('effect_outcome', ''), 'error_code': envelope.get('error_code', ''),
            'artifact_refs': record.get('artifact_registry_refs') or []})
    return facts


# LLM: 公开意图只由本次主调用的合法 schema 声明，宿主来源覆盖模型字段；用户插话和附件义务不允许被静默声明取消。
# 函数用途: 消费主轮完成信封，返回可公开内容或登记至多一次现有零工具表达修复。
def prepare_background_public_receipt(params: object, response: object) -> tuple[str, object]:
    if not explicit_public_receipt_active(params):
        return 'normal', response
    if str(getattr(response, 'runtime_status', 'ok') or 'ok') != 'ok' or getattr(response, 'truncated', False):
        return 'normal', response
    # 物理空响应继续原空输出恢复，不能在这里捏造 internal_only。
    if not str(getattr(response, 'text', '') or '').strip():
        return 'normal', response
    if error := response_provenance_error(params, response):
        return 'finish', _invalid_receipt(response, error)
    payload, error = parse_background_result(response.text)
    if error:
        return 'finish', _invalid_receipt(response, error)
    reply_required = active_turn_user_reply_required(params)
    attachments_required = _has_pending_delivery_artifacts(params)
    source = response_provenance_projection(response)
    receipt = {'schema': RESULT_SCHEMA, 'notification_intent': payload['notification_intent'],
               'execution_source': source, 'public_source': source}
    prepared = replace(response, text=sanitize_user_visible_text(payload['public_reply']).content,
                       internal_response=payload['internal_result'], public_receipt=receipt)
    if payload['notification_intent'] == 'internal_only' and attachments_required:
        return 'finish', _invalid_receipt(prepared, 'BACKGROUND_RECEIPT_PUBLIC_OBLIGATION')
    if (reply_required and payload['notification_intent'] == 'internal_only') or (
        payload['notification_intent'] == 'publish' and not prepared.text.strip() and not attachments_required
    ):
        from .natural_user_reply import queue_natural_user_reply

        queue_natural_user_reply(params, kind='background_final_receipt', facts={
            'current_user_request': str(getattr(params, 'user_prompt', '') or ''),
            'internal_execution_result': payload['internal_result'],
            'internal_result_is_model_authored': True,
            'execution_source': source,
            'tool_execution_count': len(list(getattr(params, 'archive_tool_calls', None) or [])),
            'tool_result_facts': _tool_receipt_facts(params),
            'reply_is_interim': False, 'task_continues_without_more_user_input': False,
            'public_reply_required': True,
        })
        state = getattr(params, 'live_archive_state', None)
        if isinstance(state, dict):
            state['_background_receipt_execution_response'] = {
                'internal_response': prepared.internal_response, 'public_receipt': dict(receipt),
            }
        return 'repair', prepared
    if prepared.text.strip() and reply_required:
        satisfy_active_turn_user_reply(params)
    return 'finish', prepared


# LLM: 修复只消费原 pending 槽且只给一次机会，不执行工具；公开来源须由同 run 的 repair 调用签发。
# 函数用途: 将一次公开表达合回原内部结果，失败时保留内部产出并明确不冒充送达。
def finish_background_receipt_repair(params: object, response: object, *, accepted: bool) -> object:
    state = getattr(params, 'live_archive_state', None)
    original = state.pop('_background_receipt_execution_response', None) if isinstance(state, dict) else None
    if not isinstance(original, dict):
        return _invalid_receipt(response, 'BACKGROUND_RECEIPT_CONTRACT_INVALID')
    error = response_provenance_error(params, response, purposes=('background_receipt_repair',))
    if error or not accepted:
        failed = _invalid_receipt(response, error or 'BACKGROUND_RECEIPT_PUBLIC_UNAVAILABLE')
        return replace(failed, internal_response=str(original.get('internal_response') or ''))
    receipt = dict(original.get('public_receipt') or {})
    receipt.update(notification_intent='publish', public_source=response_provenance_projection(response))
    satisfy_active_turn_user_reply(params)
    return replace(response, text=sanitize_user_visible_text(response.text).content,
                   internal_response=str(original.get('internal_response') or ''), public_receipt=receipt)
