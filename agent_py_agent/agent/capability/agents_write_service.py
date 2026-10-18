# LLM: 只消费宿主封印的当前用户请求；nonce唯一且消费未知不可重放，确认绑定owner/actor/thread/精确意图。
# 模块用途: TUI/IM共享AGENTS预览→确认→一次提交，身份由宿主绑定。
import json
import time
from dataclasses import dataclass, field
from hashlib import sha256

from ._agents_contract_fields import AgentsContractError
from .agents_control_ledger import control_transaction, read_record, save_record
from .agents_write_contract import (
    AgentsWriteAuthorization,
    AgentsWriteContext,
    validate_write_authorization,
    write_intent_digest,
)

_SEAL = object()
# 常量用途: 用户确认原AGENTS写预览的有效期（秒）；过期只能重新预览，不沿旧预览签权。
PREVIEW_SECONDS = 600.0
USAGE = ('/agents [show|refresh|source local <绝对路径>|source url <https地址>|reset|'
         'write <JSON字符串>|confirm <预览编号>|receipt <预览编号>]；'
         '跨owner使用 /agents owner <规范owner> <上述操作>（管理员且Full Access）')


# LLM: 无JSON解码入口；seal只在可信直接控制组合根设置，不提供模型工具。
# 类用途: 保存宿主选择的owner/actor/请求身份与直接命令原文。
@dataclass(frozen=True)
class AgentsControlRequest:
    owner: str
    actor: str
    thread_id: str
    request_id: str
    raw: str
    actor_is_admin: bool = False
    full_access: bool = False
    admin_target_owner: str = ''
    _seal: object = field(default=None, repr=False, compare=False)


# LLM: 只能由已认证HTTP/direct入口调用；不是反序列化器，禁止向tool catalog注册。
# 函数用途: 从宿主组合根的独立事实创建当前控制请求。
def _direct_request(*, owner, actor, thread_id, request_id, raw,
                    actor_is_admin=False, full_access=False, admin_target_owner=''):
    return AgentsControlRequest(owner, actor, thread_id, request_id, raw,
                                actor_is_admin, full_access, admin_target_owner, _SEAL)


# LLM: 这里只解析显式语法，不从自然语言猜修改授权；write使用JSON字符串保留换行和尾空白。
# 函数用途: 捕获精确动作/值，owner只是显式目标选择，不是操作者身份。
def parse_agents_value(value):
    value = value.strip()
    owner = ''
    if value.startswith('owner '):
        parts = value.split(None, 2)
        if len(parts) != 3:
            raise AgentsContractError('AGENTS_CONTROL_USAGE', 'command')
        _, owner, value = parts
    operation, _, rest = value.partition(' ')
    rest = rest.strip()
    operation = operation or 'show'
    if operation in {'show', 'refresh', 'reset'} and not rest:
        return owner, operation, ''
    if operation in {'confirm', 'receipt'} and rest and ' ' not in rest:
        return owner, operation, rest
    if operation == 'write':
        try:
            text = json.loads(rest)
        except (ValueError, TypeError) as error:
            raise AgentsContractError('AGENTS_CONTROL_USAGE', 'content') from error
        if type(text) is str:
            return owner, operation, text
    if operation == 'source':
        kind, _, locator = rest.partition(' ')
        if kind in {'local', 'url'} and locator.strip():
            return owner, 'set_source', {'kind': 'local_path' if kind == 'local' else 'url',
                                         'locator': locator.strip()}
    raise AgentsContractError('AGENTS_CONTROL_USAGE', 'command')


# LLM: 服务只认native且带私有seal的宿主请求；未知身份、跨owner缺三要素全部拒绝。
# 函数用途: 阻止JSON/学习数据或目标字符串冒充原用户身份。
def _require_request(repo, request):
    if type(request) is not AgentsControlRequest or request._seal is not _SEAL:
        raise AgentsContractError('AGENTS_WRITE_NOT_USER_REQUEST', 'request')
    if request.owner != repo.owner or not all(type(x) is str and x for x in
                                            (request.owner, request.actor, request.thread_id, request.request_id)):
        raise AgentsContractError('AGENTS_WRITE_BINDING_MISMATCH', 'owner')
    if request.owner != request.actor:
        if not (request.actor_is_admin and request.full_access):
            raise AgentsContractError('AGENTS_WRITE_CROSS_OWNER_FORBIDDEN', 'owner')
        if request.admin_target_owner != request.owner:
            raise AgentsContractError('AGENTS_WRITE_ADMIN_TARGET_REQUIRED', 'owner')
    elif request.admin_target_owner:
        raise AgentsContractError('AGENTS_WRITE_ADMIN_TARGET_UNEXPECTED', 'owner')


# LLM: 控制确认本身就是本次直接请求；预览不产生可写授权，只冻结旧版本与实际新值。
# 类用途: 组合独立原请求账本和A唯一CAS/权限/安全写入口。
class AgentsWriteService:
    # LLM: repository由宿主组合根绑定；clock仅供可重复测试，不从命令取时钟。
    # 函数用途: 绑定当前owner仓库与可信时间源。
    def __init__(self, repository, *, clock=time.time):
        self.repository = repository
        self.clock = clock

    # LLM: 仅当前宿主请求可进入预览确认；目录与nonce都沿唯一owner账，refresh不提前改变冻结前缀。
    # 函数用途: 执行查看、刷新、预览、确认和只读结果查询。
    def execute(self, request):
        repo = self.repository
        _require_request(repo, request)
        target, operation, value = parse_agents_value(request.raw)
        if target and target != repo.owner:
            raise AgentsContractError('AGENTS_WRITE_BINDING_MISMATCH', 'owner')
        if operation in {'show', 'refresh'}:
            loaded = repo.load()
            return dict(ok=loaded.content is not None, state='loaded',
                        snapshot=loaded.snapshot.to_dict(), content=loaded.content,
                        activation='pending_until_existing_refresh_point')
        with control_transaction(repo.owner_home, file_commit=repo._file_commit):
            if operation in {'confirm', 'receipt'}:
                record = read_record(repo.owner_home, value)
                self._require_record(record, request)
                if operation == 'receipt':
                    return self._receipt(record)
                return self._confirm(record, request)
            if operation == 'reset':
                operation, value = 'set_source', dict(kind='owner_default', locator=str(repo.default_path))
            return self._preview(operation, value, request)

    # LLM: nonce由原owner/actor/thread/request决定；原file commit保存preview，相同请求不续期/不覆盖已消费。
    # 函数用途: 持久保存当前旧CAS/SHA和精确新值以及原直接命令证据。
    def _preview(self, action, value, request):
        repo = self.repository
        nonce = sha256(json.dumps([request.owner, request.actor, request.thread_id,
                                  request.request_id], ensure_ascii=False).encode()).hexdigest()
        existing = read_record(repo.owner_home, nonce)
        if existing is not None:
            self._require_record(existing, request)
            if existing['preview_request'] != request.raw:
                raise AgentsContractError('AGENTS_WRITE_BINDING_MISMATCH', 'request_id')
            return self._receipt(existing)
        intent = write_intent_digest(action, value)
        loaded = repo.load()
        snapshot = loaded.snapshot if loaded.content is not None else repo.configuration()
        if action == 'write' and (loaded.content is None or snapshot.kind == 'url'):
            raise AgentsContractError('AGENTS_WRITE_SOURCE_READ_ONLY' if snapshot.kind == 'url'
                                      else 'AGENTS_SOURCE_UNAVAILABLE', 'content')
        now = float(self.clock())
        record = dict(schema_version=1, nonce=nonce, owner=request.owner, actor=request.actor,
                      thread_id=request.thread_id, preview_request_id=request.request_id,
                      preview_request=request.raw, action=action, value=value,
                      old_cas=list(snapshot.configuration_cas),
                      old_sha=snapshot.content_sha256 if loaded.content is not None else '',
                      intent_sha256=intent, issued_at=now, expires_at=now + PREVIEW_SECONDS,
                      admin_target_owner=request.admin_target_owner, state='preview')
        repo.commit_file(lambda: save_record(repo.owner_home, record))
        return self._receipt(record)

    # LLM: 从独立账本读取的owner/actor/thread必须逐项一致，不能由确认编号换目标。
    # 函数用途: 约束原预览只可由同一用户同一会话确认或查询。
    def _require_record(self, record, request):
        if record is None:
            raise AgentsContractError('AGENTS_WRITE_NOT_HOST_ISSUED', 'authorization_id')
        if any(record[key] != getattr(request, key) for key in
               ('owner', 'actor', 'thread_id', 'admin_target_owner')):
            raise AgentsContractError('AGENTS_WRITE_BINDING_MISMATCH', 'owner')

    # LLM: 先经原file commit耐久消费未知再调用A；原执行拒绝/运输未结同对象外抛，普通写失败沿原消费未知，不恢复preview或重放。
    # 函数用途: 为当前直接确认发行精确授权，执行最多一次并耐久记录结果。
    def _confirm(self, record, request):
        if record['state'] != 'preview':
            raise AgentsContractError('AGENTS_WRITE_REPLAYED', 'authorization_id')
        now = float(self.clock())
        if not record['issued_at'] <= now < record['expires_at']:
            raise AgentsContractError('AGENTS_WRITE_EXPIRED', 'expires_at')
        value = record['value']
        intent = write_intent_digest(record['action'], value)
        if intent != record['intent_sha256']:
            raise AgentsContractError('AGENTS_WRITE_BINDING_MISMATCH', 'intent_sha256')
        auth = AgentsWriteAuthorization(
            1, record['nonce'], request.owner, request.actor, request.thread_id,
            request.request_id, request.request_id, record['action'], tuple(record['old_cas']),
            record['old_sha'], now, record['expires_at'],
            'agents-control:' + record['nonce'], request.admin_target_owner, intent)
        context = AgentsWriteContext(
            1, request.owner, request.actor, request.thread_id, request.request_id,
            request.request_id, record['action'], tuple(record['old_cas']), record['old_sha'],
            now, 'user_dialogue', auth, (), request.actor_is_admin, request.full_access,
            request.admin_target_owner, intent)
        validate_write_authorization(auth, context)
        record.update(state='consumed_unknown', authorization=auth.to_dict(),
                      confirmation_request=request.raw, confirmation_request_id=request.request_id)
        self.repository.commit_file(lambda: save_record(self.repository.owner_home, record))
        # 消费已耐久；即使正文成功而回执fsync失败，下次也只允许receipt查询。
        try:
            if record['action'] == 'write':
                result = self.repository.write_text(value, authorization=auth, context=context).snapshot
            else:
                result = self.repository.set_source(value['kind'], value['locator'],
                                                    authorization=auth, context=context)
            record.update(state='completed', result=result.to_dict())
            self.repository.commit_file(lambda: save_record(self.repository.owner_home, record))
        except Exception as error:
            _raise_agents_runtime_error(error)
            # 不假定A抛错前没有副作用；保留消费未知，诊断保存失败也不能成功。
            record.update(state='consumed_unknown', error_code=getattr(error, 'code', 'AGENTS_WRITE_RESULT_UNKNOWN'))
            try:
                self.repository.commit_file(lambda: save_record(self.repository.owner_home, record))
            except Exception as diagnostic_error:
                _raise_agents_runtime_error(diagnostic_error)
            return self._receipt(record)
        return self._receipt(record)

    # LLM: 完成仅来自完整耐久回执；未知返回ok=false，预览明确不等于已写入。
    # 函数用途: 投影可展示的预览/结果与可复制确认命令。
    def _receipt(self, record):
        state = record['state']
        prefix = '/agents ' + ('owner ' + record['owner'] + ' ' if record['admin_target_owner'] else '')
        result = dict(ok=state in {'preview', 'completed'}, state=state, nonce=record['nonce'],
                      action=record['action'], old_cas=record['old_cas'], old_sha=record['old_sha'],
                      intent_sha256=record['intent_sha256'], expires_at=record['expires_at'],
                      receipt_command=prefix + 'receipt ' + record['nonce'])
        if state == 'preview':
            result.update(value=record['value'], confirm_command=prefix + 'confirm ' + record['nonce'])
        if 'result' in record and state == 'completed':
            result['snapshot'] = record['result']
        if state == 'consumed_unknown':
            result['error_code'] = record.get('error_code', 'AGENTS_WRITE_RESULT_UNKNOWN')
        return result


# LLM: 业务未知回执不得吞掉原执行源拒绝或运输未结；只依据原异常类型同对象抛出，不解析文案或伪造成功/恢复。
# 函数用途: 让底层无法确认的执行状态沿原请求出口处理，普通文件失败仍保留原消费未知策略。
def _raise_agents_runtime_error(error):
    from ..backends.bounded_call import BoundedCallExitUnconfirmedError
    from ..common.global_write_commit import GlobalWriteCommitUnavailable
    from ..common.global_write_guard import GlobalWriteInWorkerError
    from ..common.request_execution_ports import raise_if_transport_uncertain

    raise_if_transport_uncertain(error)
    if isinstance(error, (BoundedCallExitUnconfirmedError, GlobalWriteCommitUnavailable, GlobalWriteInWorkerError)):
        raise error
