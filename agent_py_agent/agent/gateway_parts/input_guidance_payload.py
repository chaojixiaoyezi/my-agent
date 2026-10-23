# LLM: 输入只消费自己完整prepared与冻结运输，复用原control builder；无NL审批/owner推导，正文和摘要匹配必须在原GuidanceStore codec验证。
# 模块用途: 将唯一input receipt投影到原guidance协议，不创建另一份消息账本。
from dataclasses import replace

from .control_guidance_payload import control_guidance_payload
from .control_service import GatewayControlScope
from .input_transport import validated_input_transport
from .request_owner_binding import frozen_request_owner


# LLM: metadata/client message来自原prepared，不从新header或当前owner重签；所有运输目标取已冻结同一份v3投影。
# LLM: 媒体作为原输入的一部分参与同一 guidance 摘要；文件/执行选项仍不得借媒体夹带，实际写入先核 owner。
# 函数用途: 构造与原 steer 完全同口径的带图或纯文字追加参数。
def input_guidance_payload(receipt):
    from ..conversation.control_commands import ConversationControlCommand

    transport = validated_input_transport(receipt.transport)
    target = transport['target']
    scope = input_scope(receipt)
    payload = control_guidance_payload(target['kind'], target.get('task_id') if target['kind'] == 'task' else target['turn_id'],
        ConversationControlCommand('steer', receipt.prepared_request['goal']), scope, target['thread_id'], target['turn_id'])
    payload['metadata']['gateway_input_transport'] = transport
    return payload


# LLM: 只从同一完整prepared恢复原peer和typed owner，metadata不能自证当前权限；用于同口径纯比较，不创建线程或Agent。
# 函数用途: 给固定guidance与目标核验共用一份输入scope。
def input_scope(receipt):
    prepared, metadata = receipt.prepared_request, dict(receipt.prepared_request.get('metadata') or {})
    conversation = prepared.get('conversation') or {}
    metadata.update(message_id=receipt.client_message_id, gateway_input_request_id=receipt.request_id)
    scope = GatewayControlScope(user_id=prepared['user_id'], channel=metadata['channel'],
        conversation_id=str(conversation.get('channel_conversation_id') or conversation.get('id') or ''),
        metadata=metadata, input_media=tuple(prepared.get('input_media') or ()))
    scope = replace(scope, resolved_owner=frozen_request_owner(prepared))
    return scope


# LLM: 原v4完整输入摘要/规范entry与原input逐字段一致才可绑定；不是只比较消息id或外部verified标记。
# LLM: 同deadline贯穿纯reader；v2始终比原冻结target/metadata，不因Compact当前attempt换代重签旧guidance。
# 函数用途: 验证同一份已持久guidance，返回其存在事实而不认领或推断ACK。
def original_input_guidance(store, receipt, *, deadline=None):
    from ..conversation.store_guidance_records import guidance_receipt_input_matches
    from ..runtime_errors import DataCorruptionError
    from . import input_delivery_service as inputs
    from .input_guidance_observation import read_guidance_observation

    row = read_guidance_observation(store.storage.guidance_dedupe_path(receipt.guidance_dedupe_key),
                                    blocking=False, strict=True, deadline=deadline)
    if row is None:
        return None
    expected = input_guidance_payload(receipt)
    entry = row.entry
    scalar_matches = all(getattr(entry, key) == str(expected[key]).strip()
                         for key in ('target_type', 'target_id', 'message', 'sender', 'priority', 'delivery'))
    metadata = dict(entry.metadata)
    metadata.pop('dedupe_key', None)
    if (row.dedupe_key != receipt.guidance_dedupe_key or not guidance_receipt_input_matches(row, expected)
            or not scalar_matches or metadata != expected['metadata']):
        raise DataCorruptionError('gateway input guidance identity conflicts')
    inputs._validate_guidance_binding(receipt, row, receipt.transport['target']['turn_id'])
    return row
