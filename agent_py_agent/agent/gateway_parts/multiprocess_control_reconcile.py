# LLM: 原W对账仅读原guidance规范终态，复用原transition锁且可产生锁侧文件；不修改正文/投影，不认领、不续租，不创建owner或第二账本。
# 模块用途: 在原锁内观察插话已确认的消费或拒绝事实，不能把未知写成已交付。
from .control_service import (
    _control_result_from_steer_receipt,
    _SteerReceiptState,
    active_turn_guidance_dedupe_key,
)
from .execution_target import GatewayExecutionTarget
from .io import locked_file_transition


# LLM: 调用方已核原W出生/登记并固定owner池；原transition锁可建锁侧文件，但只读消息正文与冻结目标，不选当前活动任务或更新guidance。
# 函数用途: 沿原锁读取已存在的终态插话，身份或目标不匹配就保持未知。
def read_original_steer_delivery(agent, receipt, scope):
    key = receipt.result.get('guidance_dedupe_key')
    target = GatewayExecutionTarget.from_dict(receipt.dispatch['execution'])
    if not key or key != active_turn_guidance_dedupe_key(scope) or target is None:
        return None
    guidance = agent.conversation_store.guidance
    path = guidance.storage.guidance_dedupe_path(key)
    with locked_file_transition(path.with_name(f'.{path.name}.transition')):
        row = _read_guidance_observation(path)
    if row is None or row.status not in {'consumed', 'rejected'} or row.dedupe_key != key:
        return None
    from ..conversation.control_commands import parse_conversation_control

    command = parse_conversation_control(receipt.command_text, reject_unknown_slash=True)
    if command is None or not command.valid or command.kind != 'steer':
        return None
    entry, metadata = row.entry, row.entry.metadata
    target_id = target.task_id if target.kind == 'task' else target.turn_id
    matches = isinstance(metadata, dict) and all((
        entry.message == command.value.strip(), entry.target_type == target.kind,
        entry.target_id == target_id, metadata.get('thread_id') == target.thread_id,
        metadata.get('channel_message_id') == receipt.client_message_id,
        metadata.get('expected_turn_id') == target.turn_id,
        receipt.conversation_thread_id == target.thread_id,
        entry.sender == scope.user_id, entry.priority == 'high',
        entry.delivery == ('current_task' if target.kind == 'task' else 'current_request'),
        metadata.get('channel') == scope.channel,
        metadata.get('conversation_id') == scope.conversation_id,
        metadata.get('kind') == 'active_turn_user_input',
        metadata.get('record_in_transcript') is True,
        metadata.get('gateway_input_request_id') == str(scope.metadata.get('gateway_input_request_id') or '').strip(),
    ))
    if not matches:
        return None
    return _control_result_from_steer_receipt(_SteerReceiptState(
        store=agent.conversation_store, dedupe_key=key, status=row.status,
        entry=entry, input_matches=True))


# LLM: 共享唯一原v4纯codec，保原control默认读取/旧schema未知；不调用会迁移或修投影的GuidanceStore API。
# 函数用途: 在原回执锁内观察guidance，不认领、不更新正文。
def _read_guidance_observation(path):
    from .input_guidance_observation import read_guidance_observation

    return read_guidance_observation(path)
