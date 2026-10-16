# LLM: 原control预览回执是唯一nonce记录，正式内容仍只由CandidateService/PromotionService持有；不新增正式库或权限。
# 模块用途: 绑定候选完整版本和目标CAS，先耐久消费再准确审核提交；候选预览期限独立于规则文件确认，未知永不重放。
import json
import time
from dataclasses import replace
from hashlib import sha256

from ..common.log_redaction import redact_sensitive_text
from ..conversation.channels import project_user_reply, redact_host_absolute_paths
from ..conversation.control_commands import ConversationControlResult
from ..memory_store.promotion import _promotion_target
from .memory_candidate_control import memory_control_failure

# 秒；真人预览的确认窗口，重放原请求不会延期。
_MEMORY_CANDIDATE_PREVIEW_SECONDS = 600
# 字节；超过可完整展示的上限时不提供确认。
_DISPLAY_BYTES = 12_000
_SCHEMA = 'memory_candidate_preview.v1'


# LLM: 只计算完整记录的确定指纹，不把时间相近或同状态当同版本，不持久第二候选正文。
# 函数用途: 将唯一候选的全部结构字段编码成精确版本号。
def candidate_version(candidate):
    return sha256(json.dumps(candidate.to_record(), ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')).hexdigest()


# LLM: 展示包含全部拟提交语义；不能安全完整展示时不给确认，不用被脱敏/截短的内容批准另一份正文。
# 函数用途: 为show/preview生成可核验原文及范围、动作、目标版本和来源摘要。
def candidate_display(candidate):
    target = _promotion_target(candidate)
    if target == 'agents':
        return None, 'MEMORY_CONTROL_USE_AGENTS'
    value = {key: getattr(candidate, key) for key in ('candidate_type', 'content', 'subject_key', 'scope',
        'origin', 'proposed_action', 'target_entry_id', 'target_entry_version', 'valid_from', 'valid_until')}
    value.update(promotion_target=target, source_task_ids=candidate.source_task_ids,
                 source_run_ids=candidate.source_run_ids)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if (len(raw.encode('utf-8')) > _DISPLAY_BYTES or redact_sensitive_text(raw) != raw
            or redact_host_absolute_paths(raw) != raw or project_user_reply(raw).content != raw):
        return None, 'MEMORY_PREVIEW_NOT_FULLY_DISPLAYABLE'
    return value, ''


# LLM: 列表仅投影编号和结构化状态，不展示正文或把候选误报正式记忆；原memory搜索仍保持不变。
# 函数用途: 分页查看本人唯一候选库，并给出可复制的精确预览命令。
def _list_candidates(host, command):
    rows = host.memory_candidates.list(limit=0)
    offset = int(command.target_id or 0)
    items = [{'candidate_id': row.candidate_id, 'status': row.status, 'candidate_type': row.candidate_type,
              'promotion_target': _promotion_target(row)} for row in rows[offset:offset + 20]]
    details = {'state': 'list', 'candidates': items, 'count': len(rows), 'offset': offset}
    if offset + 20 < len(rows):
        details['next_command'] = '/memory candidates list ' + str(offset + 20)
    lines = ['记忆候选（尚未确认的内容不会正式保存）：']
    lines += [row['candidate_id'] + ' ' + row['status'] + ' ' + row['promotion_target']
              + '\n/memory candidates preview ' + row['candidate_id'] for row in items]
    return _result(True, '\n'.join(lines + ([details['next_command']] if 'next_command' in details else [])), details)


# LLM: nonce沿原control操作ID，只引用唯一预览回执；同请求重试由原once返回，不能延长预览有效期。
# 函数用途: 冻结用户实际看到的完整候选版本、内容SHA和目标CAS，不改变候选或正式库。
def _preview(host, paths, command, scope, thread_id):
    from .control_operation_service import gateway_control_operation_identity

    candidate = host.memory_candidates.get(command.target_id)
    value, error = candidate_display(candidate)
    if error:
        message = ('该目标必须使用 /agents 显式预览确认。' if error == 'MEMORY_CONTROL_USE_AGENTS'
                   else '内容无法安全完整展示，未提供确认；请在受控本机候选管理入口完整核验。')
        return _result(False, message, {'state': 'unavailable'}, error)
    if candidate.status not in {'observed', 'pending_review', 'approved'}:
        return memory_control_failure('MEMORY_CANDIDATE_NOT_REVIEWABLE')
    details = {'state': 'show', 'candidate_id': candidate.candidate_id, 'version': candidate_version(candidate),
               'content_sha256': sha256(candidate.content.encode('utf-8')).hexdigest(), 'value': value}
    if command.operation == 'preview':
        nonce = gateway_control_operation_identity(scope, '/memory ' + command.value)[0]
        now = time.time()
        details.update(schema=_SCHEMA, state='preview', nonce=nonce, owner=host.home_paths.owner_id,
            actor=scope.user_id, channel=scope.channel, conversation_id=scope.conversation_id,
            thread_id=thread_id, issued_at=now, expires_at=now + _MEMORY_CANDIDATE_PREVIEW_SECONDS)
        for action in ('confirm', 'reject', 'cancel', 'receipt'):
            details[action + '_command'] = '/memory candidates ' + action + ' ' + nonce
    message = '完整候选；尚未正式保存。\n版本SHA：' + details['version'] + '\n正文SHA：' + details['content_sha256']
    message += '\n' + json.dumps(value, ensure_ascii=False, sort_keys=True)
    if command.operation == 'preview':
        message += '\n' + '\n'.join(details[key + '_command'] for key in ('confirm', 'reject', 'cancel'))
    return _result(True, message, details)


# LLM: 回执只投影原业务事实；unknown运输已受理而业务未确认，绝不触发通用IM重放。
# 函数用途: 构造可持久化的人工控制结果。
def _result(ok, message, details, error=''):
    return ConversationControlResult('memory', ok, message, details=details,
        error_code=error, delivery_status='accepted')


# LLM: 只修改原预览回执的业务投影，不改command摘要/owner/transport状态；fsync失败不能跨入正式提交。
# 函数用途: 让原control记录nonce消费和最终结果，不另建确认账本。
def _save_receipt(paths, receipt, details, *, error=''):
    from .control_operation_service import gateway_control_operation_path
    from .io import write_json_file_atomic

    state = details['state']
    message = {'consumed_unknown': '操作结果未确认，禁止重放；请查询原候选和正式记忆。',
               'completed': '已按预览确认正式保存。', 'rejected': '已拒绝该候选，未提交正式内容。',
               'cancelled': '已取消本次预览，候选保持原状态。',
               'not_committed': '正式内容未提交，请核对原因后重新预览。'}[state]
    result = _result(state != 'consumed_unknown' and not error, message, details, error)
    updated = replace(receipt, result=result.to_dict(), updated_at=time.time())
    write_json_file_atomic(gateway_control_operation_path(paths, receipt.operation_id), updated.to_dict())
    return result


# LLM: 原receipt的身份必须与当前实际owner/actor/channel/conversation/thread全部一致；记录损坏不恢复默认值。
# 函数用途: 拒绝跨用户、跨群、跨owner及非预览控制编号，未知记录不被当作待确认。
def _bound_preview(receipt, host, scope, thread_id):
    if receipt is None or receipt.state != 'completed' or receipt.command_kind != 'memory':
        raise ValueError('preview unavailable')
    from ..conversation.control_commands import parse_conversation_control

    original = parse_conversation_control(receipt.command_text)
    details = receipt.result.get('details', {})
    if (original is None or original.kind != 'memory' or original.operation != 'preview'
            or original.target_id != details.get('candidate_id')
            or (receipt.user_id, receipt.channel, receipt.conversation_id)
            != (scope.user_id, scope.channel, scope.conversation_id)):
        raise ValueError('preview origin mismatch')
    expected = {'schema': _SCHEMA, 'owner': host.home_paths.owner_id, 'actor': scope.user_id,
                'channel': scope.channel, 'conversation_id': scope.conversation_id, 'thread_id': thread_id,
                'nonce': receipt.operation_id}
    if any(details.get(key) != value for key, value in expected.items()):
        raise ValueError('preview binding mismatch')
    return details


# LLM: 授权记录复用原control schema和路径，仅读取方式复用公共严格no-follow；有界实际读且坏文件不成为预览。
# 函数用途: 安全取回唯一原预览回执，不跟随链接、不接受同名路径替身。
def _read_preview(paths, nonce):
    from ..common.nofollow_fs import read_bytes_beneath
    from .control_operation_service import (
        GatewayControlOperationReceipt,
        gateway_control_operation_path,
    )

    path = gateway_control_operation_path(paths, nonce)
    raw = read_bytes_beneath(paths.root, path.relative_to(paths.root).parts,
                             max_bytes=128 * 1024, require_dir_fd=True)
    if raw is None:
        return None
    receipt = GatewayControlOperationReceipt.from_dict(json.loads(raw))
    if receipt.operation_id != nonce:
        raise ValueError('receipt identity mismatch')
    return receipt


# LLM: 当前确认的控制锁之外只尝试原preview锁，永不相互等待；消费先耐久，崩溃或异常后只允许读原回执。
# 函数用途: 对准确预览进行确认、拒绝或取消；所有nonce重放保持原终态。
def _resolve_preview(host, paths, command, scope, thread_id):
    from .control_operation_service import (
        gateway_control_operation_identity,
        try_gateway_control_operation_transition,
    )

    nonce = command.target_id
    current = gateway_control_operation_identity(scope, '/memory ' + command.value)[0]
    if nonce == current:
        return memory_control_failure('MEMORY_PREVIEW_BINDING_MISMATCH')
    with try_gateway_control_operation_transition(paths, nonce) as acquired:
        if not acquired:
            return memory_control_failure('MEMORY_PREVIEW_BUSY')
        receipt = _read_preview(paths, nonce)
        try:
            details = dict(_bound_preview(receipt, host, scope, thread_id))
        except ValueError:
            return memory_control_failure('MEMORY_PREVIEW_BINDING_MISMATCH')
        if details['state'] != 'preview' or command.operation == 'receipt':
            return _result(receipt.result['ok'], receipt.result['message'], details, receipt.result.get('error_code', ''))
        if not details['issued_at'] <= time.time() < details['expires_at']:
            return memory_control_failure('MEMORY_PREVIEW_EXPIRED')
        if command.operation == 'cancel':
            return _save_receipt(paths, receipt, details | {'state': 'cancelled'})
        candidate = host.memory_candidates.get(details['candidate_id'])
        if candidate_version(candidate) != details['version']:
            return memory_control_failure('MEMORY_CANDIDATE_VERSION_CHANGED')
        value, error = candidate_display(candidate)
        if (error or value != details.get('value')
                or sha256(candidate.content.encode('utf-8')).hexdigest() != details.get('content_sha256')):
            return memory_control_failure('MEMORY_PREVIEW_BINDING_MISMATCH')
        details.update(state='consumed_unknown', consumed_request_id=str(scope.metadata['message_id']))
        _save_receipt(paths, receipt, details, error='MEMORY_CONTROL_RESULT_UNKNOWN')
        return _apply_review(host, paths, receipt, details, candidate, command.operation)


# LLM: review与promote复用原唯一候选及目标锁/CAS；精确快照传入两步，模型或来源更新不能替换用户所见内容。
# 函数用途: 按当前人工动作审核并提交，失败保留已消费事实而不自动再执行。
def _apply_review(host, paths, receipt, details, candidate, action):
    try:
        if candidate.status == 'observed':
            candidate = host.memory_candidates.transition(candidate, 'pending_review', reviewer=details['actor'])
        approved = host.memory_promotion.review(candidate, approved=action == 'confirm',
            reviewer=details['actor'], note='当前用户已核验精确预览：' + details['nonce'])
        if action == 'reject':
            return _save_receipt(paths, receipt, details | {'state': 'rejected'})
        result = host.memory_promotion.promote(approved.candidate_id, automatic=False, confirmed=True,
            reviewer=details['actor'], expected_candidate=approved)
        finished = details | {'state': 'completed' if result.promoted else 'not_committed',
            'promotion': {key: getattr(result, key) for key in
                ('candidate_id', 'promoted', 'status', 'reason_code', 'formal_memory_changed')}}
        return _save_receipt(paths, receipt, finished, error='' if result.promoted else result.reason_code)
    except Exception:
        # 任一步可能已提交；保留原耐久unknown，不在错误出口重新读写候选或暴露异常正文。
        return _result(False, '操作结果未确认，禁止重放；请查询原候选和正式记忆。',
                       details, 'MEMORY_CONTROL_RESULT_UNKNOWN')


# LLM: 仅认证直接控制组合根调用；所有读写保持同owner，错误不能把候选改成成功或回退到模型。
# 函数用途: 分派列表、完整预览和单次人工确认。
def run_memory_candidate_control(host, paths, command, scope, thread_id):
    if command.operation == 'list':
        return _list_candidates(host, command)
    if command.operation in {'show', 'preview'}:
        return _preview(host, paths, command, scope, thread_id)
    return _resolve_preview(host, paths, command, scope, thread_id)
