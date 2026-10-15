# LLM: 群能力授权只存原 owner tool_policy.json；当前角色/成员来自 provider，模型、提示词与权限模式不能自行授予。
# 模块用途: 保存人工明确授予、撤销及可信成员事件导致的失效，保持原 owner 策略字段和单一 CAS 权威。
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_json_file_atomic_unlocked,
)
from ..conversation.host_actor import HostChannelActor
from .owner_policy_seed_payloads import default_tool_policy_payload

GROUP_CAPABILITIES = frozenset({'monitors.create', 'assistant.configure'})
GROUP_AUTHORIZATION_FIELD = 'group_authorization'
_SCHEMA = 'group-authorization.v1'
_ERRORS = {
    'GROUP_AUTHORIZATION_REQUIRED': '尚未获得这项群能力授权，请由当前群主或已核验邀请人使用 /group 明确授权。',
    'GROUP_AUTHORIZATION_UNAVAILABLE': '群授权记录不可读取或已损坏，未放宽权限。',
    'GROUP_AUTHORITY_UNAVAILABLE': '无法核验当前群主、机器人及完整成员，请检查飞书权限后重试。',
    'GROUP_AUTHORIZATION_NOT_AUTHORITY': '只有当前群主或已核验且仍在群内的邀请人能授予或撤销群能力。',
    'GROUP_AUTHORIZATION_CONFLICT': '群授权版本已经变化，请重新查看 /group 后重试。',
    'GROUP_AUTHORIZATION_INVALID': '群授权参数或可信发起人范围无效。',
    'GROUP_AUTHORIZATION_MEMBER_REQUIRED': '当前发起人或授权目标不在原群，未执行这项群操作。',
}


# LLM: 拒绝只返回固定码与说明，不携带原策略、成员响应或底层异常正文。
# 类用途: 向工具、人工控制与卡片统一报告可定位的群授权拒绝。
class GroupAuthorizationError(ValueError):
    # LLM: code 只能来自本模块合同，不改变策略或业务状态。
    # 函数用途: 构造固定群授权错误。
    def __init__(self, code: str):
        self.error_code = code
        super().__init__(_ERRORS[code])


# LLM: 对象由宿主 adapter 官方查询生成；成员必须是完整真人 open_id，不能用模型声明或 tenant token 的在群状态代替真人。
# 类用途: 携带本次授权检查的实时 provider 事实，不持久化为另一份成员权限账。
@dataclass(frozen=True)
class GroupAuthoritySnapshot:
    provider: str
    chat_id: str
    app_id: str
    tenant_key: str
    owner_id: str
    member_ids: frozenset[str]

    # LLM: 直接构造同样必须符合准确 provider 字段合同，不能用空角色或非集合成员绕过 from_mapping 校验。
    # 函数用途: 校验当前事实载体，不执行 IO。
    def __post_init__(self):
        if (any(type(getattr(self, key)) is not str or not getattr(self, key) for key in
                ('provider', 'chat_id', 'app_id', 'tenant_key', 'owner_id'))
                or not isinstance(self.member_ids, frozenset)
                or any(type(x) is not str or not x for x in self.member_ids)):
            raise GroupAuthorizationError('GROUP_AUTHORITY_UNAVAILABLE')

    # LLM: 只接受完整当前事实；缺证明、错误类型、分页未完成或机器人不在群均不能形成授权依据。
    # 函数用途: 校验 provider 返回的安全结构，不发网络或读取正文。
    @classmethod
    def from_mapping(cls, value: object):
        if not isinstance(value, dict) or value.get('members_complete') is not True or value.get('bot_is_member') is not True:
            raise GroupAuthorizationError('GROUP_AUTHORITY_UNAVAILABLE')
        fields = ('provider', 'chat_id', 'app_id', 'tenant_key', 'owner_id')
        if any(type(value.get(key)) is not str or not value[key] or value[key] != value[key].strip() for key in fields):
            raise GroupAuthorizationError('GROUP_AUTHORITY_UNAVAILABLE')
        members = value.get('member_ids')
        if not isinstance(members, (list, tuple, frozenset)) or any(type(x) is not str or not x or x != x.strip() for x in members):
            raise GroupAuthorizationError('GROUP_AUTHORITY_UNAVAILABLE')
        return cls(**{key: value[key] for key in fields}, member_ids=frozenset(members))


# LLM: 这只是已认证人工 slash 的结构化意图，构造本对象本身不授予权限，写入口仍核 provider 角色、成员和 CAS。
# 类用途: 传递单次明确的授予或撤销操作，默认目标始终是精确成员。
@dataclass(frozen=True)
class GroupAuthorizationChange:
    operation: str
    expected_revision: int
    capability: str = ''
    subject_id: str = ''
    all_current: bool = False
    grant_id: str = ''


# LLM: 只能由已认证 provider 事件归一化生成；这些字段不暴露为模型工具参数，也不证明当前成员资格。
# 类用途: 将准确应用、群、事件及相关真实成员送入原策略的授权失效事务。
@dataclass(frozen=True)
class GroupProviderEvent:
    provider: str
    chat_id: str
    app_id: str
    tenant_key: str
    event_id: str
    event_type: str
    created_at: int
    operator_id: str = ''
    member_ids: tuple[str, ...] = ()


# LLM: group owner 的规范身份来自已解析 home；不能从最新会话、显示名或模型参数选另一个群。
# 函数用途: 取得本次策略对应的准确群编号，非群上下文明确拒绝。
def group_chat_id(home) -> str:
    owner = getattr(home, 'owner_id', '')
    provider = getattr(home, 'owner_provider', '')
    prefix = f'providers/{provider}/groups/'
    if getattr(home, 'owner_kind', '') != 'group' or type(owner) is not str or not owner.startswith(prefix):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    chat_id = owner.removeprefix(prefix)
    if not chat_id or '/' in chat_id:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    return chat_id


# LLM: 缺授权块表示初次未授权，不能继承 auto、旧 operation_grants 或本机管理员权限。
# 函数用途: 生成尚未绑定应用的空群授权投影，不写文件。
def _empty_policy():
    return {'schema': _SCHEMA, 'revision': 0, 'app_id': '', 'tenant_key': '', 'group_epoch': 0,
            'inviter': None, 'subject_epochs': {}, 'grants': [], 'last_bot_event_time': 0, 'event_ids': []}


# LLM: 在同一个原 tool_policy.json 读取授权块；坏记录失败关闭，绝不以默认空记录覆盖损坏文件。
# 函数用途: 返回原策略及已验证群授权，供读操作和原锁内写操作共用。
def read_group_authorization(home):
    group_chat_id(home)
    report = read_json_object_report(home.owner_tool_policy_json, context='owner.group_authorization')
    data = report.payload or default_tool_policy_payload()
    if report.load_error or data.get('schema_version') != 'tool-policy.v1':
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_UNAVAILABLE')
    block = data.get(GROUP_AUTHORIZATION_FIELD, _empty_policy())
    if not _valid_policy(block):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_UNAVAILABLE')
    return data, block


# LLM: 版本、成员失效编号及 grant 都是机器合同；未知或损坏结构不能悄悄变为授权。
# 函数用途: 校验原授权块，不打印或修复记录。
def _valid_policy(block):
    if not isinstance(block, dict) or set(block) != set(_empty_policy()) or block.get('schema') != _SCHEMA:
        return False
    if any(type(block[key]) is not int or block[key] < 0 for key in ('revision', 'group_epoch', 'last_bot_event_time')):
        return False
    if any(type(block[key]) is not str for key in ('app_id', 'tenant_key')):
        return False
    epochs = block['subject_epochs']
    if not isinstance(epochs, dict) or any(type(k) is not str or not k or type(v) is not int or v < 0 for k, v in epochs.items()):
        return False
    inviter = block['inviter']
    if inviter is not None and (not isinstance(inviter, dict) or set(inviter) != {'actor_id', 'event_id', 'group_epoch'}
            or any(type(inviter[k]) is not str or not inviter[k] for k in ('actor_id', 'event_id'))
            or type(inviter['group_epoch']) is not int):
        return False
    return (isinstance(block['event_ids'], list) and all(type(x) is str and x for x in block['event_ids'])
        and isinstance(block['grants'], list) and all(_valid_grant(row) for row in block['grants']))


# LLM: 授权必须绑定精确能力、成员失效版本与机器人群代次；展示文字不参与校验。
# 函数用途: 拒绝损坏 grant，防止缺字段被当成广泛授权。
def _valid_grant(row):
    if not isinstance(row, dict) or set(row) != {'grant_id', 'capability', 'subjects', 'group_epoch', 'granted_by', 'authority', 'status', 'created_at'}:
        return False
    subjects = row['subjects']
    return (type(row['grant_id']) is str and bool(row['grant_id'])
        and type(row['capability']) is str and row['capability'] in GROUP_CAPABILITIES
        and type(row['status']) is str and row['status'] in {'active', 'revoked'}
        and type(row['authority']) is str and row['authority'] in {'group_owner', 'bot_inviter'}
        and type(row['granted_by']) is str and bool(row['granted_by']) and type(row['group_epoch']) is int
        and type(row['created_at']) in (int, float) and math.isfinite(row['created_at'])
        and isinstance(subjects, dict) and bool(subjects)
        and all(type(k) is str and k and type(v) is int and v >= 0 for k, v in subjects.items()))


# LLM: 只比较本群、本应用当前事实与原授权；成员事件编号只使旧授权失效，不替代官方当前成员查询。
# 函数用途: 在原锁内判断这位真实发起人是否有这项能力。
def member_has_group_capability(block, snapshot, actor, capability):
    return (_same_application(block, snapshot) and actor.actor_id in snapshot.member_ids
        and any(row['status'] == 'active' and row['capability'] == capability
            and row['group_epoch'] == block['group_epoch']
            and row['subjects'].get(actor.actor_id) == block['subject_epochs'].get(actor.actor_id, 0)
            for row in block['grants']))


# LLM: open_id 仅在原应用内匹配；换应用或租户不能继承旧 grant 或邀请人。
# 函数用途: 核对授权与当前 provider 应用身份。
def _same_application(block, snapshot):
    return (block['app_id'], block['tenant_key']) == (snapshot.app_id, snapshot.tenant_key)


# LLM: 人工管理者必须仍在群内；历史邀请人仅来自本应用真实 bot-added 事件，老群不猜第一个发言人。
# 函数用途: 为一次明确人工授权核验当前群主或有效邀请人身份。
def _grantor_authority(home, block, snapshot, actor):
    if (not isinstance(actor, HostChannelActor) or actor.owner_id != home.owner_id
            or snapshot.provider != home.owner_provider or actor.channel != snapshot.provider or actor.conversation_id != snapshot.chat_id
            or snapshot.chat_id != group_chat_id(home)):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    if actor.actor_id not in snapshot.member_ids:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_MEMBER_REQUIRED')
    if actor.actor_id == snapshot.owner_id:
        return 'group_owner'
    inviter = block['inviter']
    if (_same_application(block, snapshot) and inviter and inviter['actor_id'] == actor.actor_id
            and inviter['group_epoch'] == block['group_epoch']):
        return 'bot_inviter'
    raise GroupAuthorizationError('GROUP_AUTHORIZATION_NOT_AUTHORITY')


# LLM: 仅认证人工控制调用此写入口；模型工具不暴露它。锁内复核角色、版本后修改原策略，保留其它 owner 字段。
# 函数用途: 明确授予精确成员或当时全体真人快照，或按准确 grant_id 撤销，不修改任何业务任务。
def change_group_authorization(home, snapshot, actor, request: GroupAuthorizationChange):
    if not isinstance(request, GroupAuthorizationChange) or not isinstance(snapshot, GroupAuthoritySnapshot):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    with locked_json_path(home.owner_tool_policy_json):
        data, block = read_group_authorization(home)
        authority = _grantor_authority(home, block, snapshot, actor)
        if type(request.expected_revision) is not int or request.expected_revision != block['revision']:
            raise GroupAuthorizationError('GROUP_AUTHORIZATION_CONFLICT')
        updated = {**block, 'grants': [dict(row) for row in block['grants']]}
        if request.operation == 'allow':
            _allow_group_capability(updated, snapshot, actor, request, authority)
        elif request.operation == 'revoke' and request.grant_id:
            matches = [row for row in updated['grants'] if row['grant_id'] == request.grant_id]
            if len(matches) != 1:
                raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
            matches[0]['status'] = 'revoked'
        else:
            raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
        updated['revision'] += 1
        write_private_json_file_atomic_unlocked(home.owner_tool_policy_json, {**data, GROUP_AUTHORIZATION_FIELD: updated})
        return updated


# LLM: all_current 是授予时完整真人成员快照，后加入者不会自动获得；同编号离群/重入事件使旧 subject epoch 失效。
# 函数用途: 在已授权人工事务内创建一条 grant，应用变化时建立新群主确认基线。
def _allow_group_capability(block, snapshot, actor, request, authority):
    if request.capability not in GROUP_CAPABILITIES or type(request.all_current) is not bool:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    subjects = snapshot.member_ids if request.all_current else frozenset({request.subject_id})
    if (request.all_current and request.subject_id) or not subjects or not subjects <= snapshot.member_ids:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_MEMBER_REQUIRED')
    if not _same_application(block, snapshot):
        block.update(app_id=snapshot.app_id, tenant_key=snapshot.tenant_key,
                     group_epoch=block['group_epoch'] + 1, inviter=None)
    block['grants'].append({'grant_id': str(uuid4()), 'capability': request.capability,
        'subjects': {key: block['subject_epochs'].get(key, 0) for key in sorted(subjects)},
        'group_epoch': block['group_epoch'], 'granted_by': actor.actor_id, 'authority': authority,
        'status': 'active', 'created_at': time.time()})


# LLM: 只更新同一 tool_policy 的授权失效编号与 bot-added 证明，不保存第二成员名单，不改业务任务或赋予能力。
# 函数用途: 处理可信加入/移除事件；机器人重入换群代次，成员变化使该成员旧授权失效。
def record_group_provider_event(home, event: GroupProviderEvent):
    if not isinstance(event, GroupProviderEvent):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    home = _provider_event_home(home, event)
    names = {'im.chat.member.bot.added_v1', 'im.chat.member.bot.deleted_v1',
             'im.chat.member.user.added_v1', 'im.chat.member.user.deleted_v1'}
    if (not isinstance(event, GroupProviderEvent) or event.event_type not in names
            or event.provider != home.owner_provider or event.chat_id != group_chat_id(home)
            or any(type(getattr(event, key)) is not str or not getattr(event, key)
                   for key in ('app_id', 'tenant_key', 'event_id'))
            or type(event.created_at) is not int or event.created_at < 0
            or type(event.operator_id) is not str or not isinstance(event.member_ids, tuple)
            or any(type(x) is not str or not x for x in event.member_ids)):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    with locked_json_path(home.owner_tool_policy_json):
        data, block = read_group_authorization(home)
        if event.event_id in block['event_ids']:
            return block
        updated = {**block, 'subject_epochs': dict(block['subject_epochs'])}
        if not _apply_provider_event(updated, event):
            return block
        updated['event_ids'] = [*block['event_ids'], event.event_id][-128:]
        updated['revision'] += 1
        write_private_json_file_atomic_unlocked(home.owner_tool_policy_json, {**data, GROUP_AUTHORIZATION_FIELD: updated})
        return updated


# LLM: adapter 只提供已配置的绝对宿主根；群路径沿原 owner resolver 解析，不从事件正文或环境猜根，不初始化 Agent。
# 函数用途: 将可信事件写到准确群策略，允许原 SDK hook 只携带宿主根目录。
def _provider_event_home(home, event):
    if not isinstance(home, (str, Path)):
        return home
    from .home_layout import home_paths
    from .owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home
    root = Path(home)
    if not root.is_absolute():
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    identity = OwnerIdentity.provider_group(event.provider, event.chat_id)
    return home_paths_with_owner(home_paths(root), resolve_owner_home(root, identity))


# LLM: 只允许可信删除事件沿原应用/群条目纯失效；GET chat 已失权时可读原 tenant 绑定，绝不能借此新增邀请人或 grant。
# 函数用途: 机器人被移除后仍能撤销旧权限，避免要求已经不可访问的群元数据才能止损。
def record_group_provider_invalidation(home, event: GroupProviderEvent):
    if (not isinstance(event, GroupProviderEvent)
            or event.event_type not in {'im.chat.member.bot.deleted_v1', 'im.chat.member.user.deleted_v1'}):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    home = _provider_event_home(home, event)
    if event.provider != home.owner_provider or event.chat_id != group_chat_id(home):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    _data, block = read_group_authorization(home)
    if not block['app_id'] or block['app_id'] != event.app_id:
        return block
    if event.tenant_key and event.tenant_key != block['tenant_key']:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    return record_group_provider_event(home, replace(event, tenant_key=block['tenant_key']))


# LLM: 旧 bot 事件不能恢复旧邀请人；成员事件只撤旧授权，不猜连续任期，缺历史的老群仍由当前群主明确重授权。
# 函数用途: 更新授权证明的最小失效信息，不把事件副本当实时成员事实。
def _apply_provider_event(block, event):
    same = (block['app_id'], block['tenant_key']) == (event.app_id, event.tenant_key)
    if '.bot.' in event.event_type:
        if event.created_at < block['last_bot_event_time']:
            return False
        if event.event_type.endswith('deleted_v1') and not same:
            return False
        simultaneous = event.created_at == block['last_bot_event_time']
        block.update(app_id=event.app_id, tenant_key=event.tenant_key,
                     group_epoch=block['group_epoch'] + 1, last_bot_event_time=event.created_at, inviter=None)
        if not simultaneous and event.event_type.endswith('added_v1') and event.operator_id:
            block['inviter'] = {'actor_id': event.operator_id, 'event_id': event.event_id, 'group_epoch': block['group_epoch']}
        return True
    if not same:
        return False
    for member in event.member_ids:
        block['subject_epochs'][member] = block['subject_epochs'].get(member, 0) + 1
        if block['inviter'] and block['inviter']['actor_id'] == member:
            block['inviter'] = None
    return True
