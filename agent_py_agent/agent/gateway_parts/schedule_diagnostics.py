# LLM: 监控控制诊断只投影结构化异常、errno 和可信 scope；不记录输入正文、参数、文件路径或凭据，不决定重试副作用。
# 模块用途: 解析监控命令参数并提供可追踪的失败回执，IM 用户无需刷新或查看宿主日志。
from __future__ import annotations

import errno
import json
import logging
import time
import uuid

from ..conversation.control_commands import ConversationControlResult
from ..scheduler.repository import SchedulerConflictError, SchedulerNotFoundError
from ..scheduler.schedule import ScheduleValidationError

_LOGGER = logging.getLogger(__name__)
_USAGE = '用法：/schedule [job01]；/schedule history|pause|resume|run|delete|takeover|execution|cancel|recover <job01>；/schedule notify <job01> origin|local|none。'
_MESSAGES = {
    'SCHEDULER_ARGUMENTS_INVALID': _USAGE,
    'SCHEDULER_STORE_BUSY': '监控账本正在频繁更新，本次读取未完成，请稍后再发同一查询。',
    'SCHEDULER_PERMISSION_DENIED': '宿主无法读取或写入监控账本；请管理员检查目录与锁文件权限。',
    'SCHEDULER_READ_ONLY': '监控账本所在文件系统只读，本次操作无法完成；请管理员检查挂载状态。',
    'SCHEDULER_STORE_MISSING': '监控存储路径不可用，请管理员检查安装的数据目录。',
    'SCHEDULER_RESOURCE_EXHAUSTED': '宿主文件资源不足，本次操作无法完成；请管理员检查进程资源。',
    'SCHEDULER_STORE_UNREADABLE': '监控账本不可读取，请管理员根据诊断编号检查存储。',
    'SCHEDULER_STORE_INVALID_CONTENT': '监控账本内容损坏；未修改任务，请管理员核对原账本及备份。',
    'SCHEDULER_STORE_SCHEMA_MISMATCH': '监控账本版本或结构不受支持；未修改任务，请管理员核对运行版本。',
    'SCHEDULER_OWNER_MISMATCH': '监控账本归属校验未通过；未跨用户读取或修改，请管理员核对归属。',
    'SCHEDULER_STORE_OWNER_MISMATCH': '监控账本归属与当前用户不一致；未跨用户读取或修改，请管理员核对归属。',
    'SCHEDULER_CONFLICT': '监控刚被另一操作修改，本次没有覆盖；请重新查询后再控制。',
    'SCHEDULER_NOT_FOUND': '没有找到可管理的本人监控，请先发送 /schedule 查询。',
    'CHANNEL_ACTOR_SCOPE_MISMATCH': '监控创建人或入口归属不一致，已拒绝操作。',
    'CHANNEL_ACTOR_IDENTITY_CONFLICT': '原通知绑定已变化，请重新从管理员私聊接管未来通知。',
    'CHANNEL_ACTOR_IDENTITY_REQUIRED': '当前入口缺少可信发起人，已拒绝操作。',
    'CHANNEL_ACTOR_IDENTITY_INVALID': '原发起人记录不可用，已拒绝操作。',
    'SCHEDULER_CONTROL_FAILED': '监控控制出现内部错误，请管理员按诊断编号核查。',
}


# LLM: 参数错误是明确协议错误，不按错误正文裁决；不会读写任务或解析自然语言控制。
# 类用途: 标记监控命令形状不合法，统一给出可执行的用法。
class ScheduleArgumentsError(ScheduleValidationError):
    pass


# LLM: 身份变化固定错误码，不能通过重试改 owner 或借原查询授权。
# 类用途: 标记冷查询仓库与实际控制仓库的归属不一致。
class ScheduleOwnerChanged(ValueError):
    error_code = 'SCHEDULER_OWNER_MISMATCH'


# LLM: 只解析原 slash 查询、删除、当前取消、恢复和未来通知接管参数；控制必须先通过形状检查，避免无效输入触发 owner 初始化或读取。
# 函数用途: 给监控列表、详情与准确生命周期控制返回一致的 operation、编号和参数，不请求模型。
def schedule_arguments(value: str) -> tuple[str, str, list[str]]:
    parts = value.split()
    if not parts:
        return 'list', '', parts
    operations = {'history', 'pause', 'resume', 'run', 'notify', 'delete', 'takeover', 'execution', 'cancel', 'recover'}
    operation, identifier = (parts[0], parts[1] if len(parts) > 1 else '') if parts[0] in operations else ('get', parts[0])
    limits = {'get': {1}, 'history': {2}, 'pause': {2, 3}, 'resume': {2, 3}, 'run': {2, 3}, 'notify': {3}, 'delete': {2}, 'takeover': {2}, 'execution': {2}, 'cancel': {2}, 'recover': {2}}
    if len(parts) not in limits[operation] or (operation == 'notify' and parts[2] not in {'origin', 'local', 'none'}):
        raise ScheduleArgumentsError(_USAGE)
    return operation, identifier, parts


# LLM: 只按结构化类型、errno 和仓库自身错误码分类，不解析异常文案；原异常文本不能进入用户或日志。
# 函数用途: 区分无匹配、版本竞争、存储权限和内部失败。
def schedule_error_code(exc: Exception) -> str:
    code = str(getattr(exc, 'error_code', '') or '')
    if isinstance(exc, ScheduleArgumentsError):
        return 'SCHEDULER_ARGUMENTS_INVALID'
    if isinstance(exc, SchedulerConflictError):
        return 'SCHEDULER_CONFLICT'
    if isinstance(exc, SchedulerNotFoundError):
        return 'SCHEDULER_NOT_FOUND'
    number = getattr(exc, 'errno', None)
    storage_code = {errno.EACCES: 'SCHEDULER_PERMISSION_DENIED', errno.EPERM: 'SCHEDULER_PERMISSION_DENIED',
            errno.EROFS: 'SCHEDULER_READ_ONLY', errno.ENOENT: 'SCHEDULER_STORE_MISSING',
            errno.EMFILE: 'SCHEDULER_RESOURCE_EXHAUSTED', errno.ENFILE: 'SCHEDULER_RESOURCE_EXHAUSTED',
            errno.EAGAIN: 'SCHEDULER_STORE_BUSY'}.get(number)
    return storage_code or (code if code in _MESSAGES else 'SCHEDULER_CONTROL_FAILED')


# LLM: 诊断编号只关联本次错误；日志仅包含可信身份和数值，不含用户参数，写入不改变监控任务状态。
# 函数用途: 保存可检索的结构化失败信息并返回无需刷新的中文回执。
def schedule_failure(exc: Exception, scope: object, *, operation: str, stage: str, started: float) -> ConversationControlResult:
    code = schedule_error_code(exc)
    diagnostic_id = uuid.uuid4().hex[:16]
    owner = getattr(scope, 'resolved_owner', None)
    details = {'diagnostic_id': diagnostic_id, 'error_code': code, 'exception_type': type(exc).__name__,
               'errno': getattr(exc, 'errno', None), 'operation': operation, 'stage': stage,
               'elapsed_ms': max(0, int((time.monotonic() - started) * 1000))}
    source = getattr(exc, 'details', {})
    if isinstance(source, dict):
        details.update({key: value for key in ('line_no', 'column')
                        if type(value := source.get(key)) is int and value > 0})
    _LOGGER.warning('scheduler_control_failed %s', json.dumps({**details,
        'owner_provider': str(getattr(owner, 'provider', '') or ''),
        'owner_kind': str(getattr(owner, 'owner_kind', '') or ''),
        'owner_id': str(getattr(owner, 'owner_id', '') or '')}, ensure_ascii=False))
    message = _MESSAGES[code]
    if code != 'SCHEDULER_ARGUMENTS_INVALID':
        message += f'\n诊断：{code} / {diagnostic_id}'
    return ConversationControlResult('schedule', False, message, details=details, error_code=code)
