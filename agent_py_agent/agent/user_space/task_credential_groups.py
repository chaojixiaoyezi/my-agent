# LLM: 群共享只读取原私密凭据row；当前typed actor、完整实时成员和显式access_scope共同授权，摘要安全公开authentication与authentication_configured，不创建索引或缓存。
# 模块用途: 按实际填写人优先解析本群凭据并返回含认证状态的安全摘要；本人未撤销的不可用记录不自动降到别人，主动撤销后才允许共享候选。
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict

from ..capability.runtime_config_reload import capability_config_for_agent
from ..common.nofollow_fs import open_directory_beneath, read_text_beneath
from ..conversation.im_tool_approval import approval_recipient
from ..conversation.tool_approval_scope import tool_approval_scope, tool_approval_scope_is_current
from ..path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
from .group_authorization import group_chat_id
from .group_authorization_context import current_group_authority, group_operation_actor
from .owner_resolver import owner_identity_from_config
from .task_credential_schema import credential_ready_expired
from .task_credential_storage import CredentialInputError, credential_stage

_RETIRED = {'revoked', 'cancelled', 'failed'}
_MANAGEMENT = {'configure', 'update', 'revoke', 'share'}


# LLM: 默认选择只在部署能力明确开启的群生效；私聊/旧task不升级，启用群缺实时身份则失败而非降级发卡。
# 函数用途: 在原申请入口决定新凭据采用群共享还是原任务作用域，不写记录或发送卡片。
def new_credential_access_scope(agent, scope) -> str:
    if getattr(agent.home_paths, 'owner_kind', '') != 'group':
        return 'task'
    capability = capability_config_for_agent(agent)
    if getattr(capability, 'im_group_shared_credentials_enabled', False) is not True:
        return 'task'
    _context(agent, scope)
    return 'group'


# LLM: group失败直接抛错；use核own优先，管理限原请求人/填写人，notification必须仍是未被替换的精确旧row而非resolver新ref。
# 函数用途: 给原使用和管理入口复用群授权；返回False只表示旧task记录仍须走原门。
def group_credential_access(agent, row, scope, operation='use') -> bool:
    if _access_scope(row) == 'task':
        return False
    actor, owner, chat = _context(agent, scope, require_enabled=operation not in {'status', 'revoke'})
    _validate_group_row(row, owner, chat)
    if operation in _MANAGEMENT:
        if actor.actor_id not in (row.get('actor_id'), row.get('submitter_actor_id')):
            _denied('只有原请求人或实际填写人可以管理这份群凭据。')
    elif operation == 'use':
        rows = _rows(agent, owner, chat)
        _require_current_row(row, rows)
        active = _current_rows(rows)
        own = _own_rows(active, actor.actor_id, row['origin'])
        if (own and row.get('submitter_actor_id') != actor.actor_id) or row not in active or not _usable(row):
            _denied('自己的同站点凭据优先；不可用时请决定更新或撤销，未自动使用别人的凭据。')
    elif operation == 'notification':
        rows = _rows(agent, owner, chat)
        _require_current_row(row, rows)
        if row not in _current_rows(rows):
            _denied('此凭据已被成功替换，不再发送旧认证拒绝通知。')
    elif operation != 'status':
        _denied('群凭据操作无效。')
    return True


# LLM: 旧group handle仅定位本群准确origin，解析不写别名/复制密文；一次HTTP前选择实际row，失败不在同请求换账号重试。
# 函数用途: 将监控或旧引用解析为本次实际可用凭据，调用方必须以返回row记录认证结果及requested/effective引用。
def resolve_group_credential(agent, row, scope, *, origin) -> dict:
    if _access_scope(row) != 'group':
        _denied('只有明确共享的群凭据可以解析当前可用引用。')
    actor, owner, chat = _context(agent, scope)
    _validate_group_row(row, owner, chat)
    target = _origin(origin)
    if not target or row['origin'] != target:
        _denied('群凭据引用不属于此接口。')
    rows = _rows(agent, owner, chat)
    _require_current_row(row, rows)
    candidates, own_present = _candidates(rows, actor.actor_id, target)
    if not candidates:
        _denied('自己的凭据尚不可用，请决定更新或撤销。' if own_present else '本群没有可用的同站点凭据。')
    if own_present:
        return _stable(candidates)[0]
    exact = next((item for item in candidates if item['credential_ref'] == row['credential_ref']), None)
    same_submitter = [item for item in candidates if item.get('submitter_actor_id') == row.get('submitter_actor_id')]
    return exact or _stable(same_submitter or candidates)[0]


# LLM: 摘要只白名单标量，不复制headers/query/note/用户名/密文/任意metadata；name筛选不能把本人不可用误判成没有本人凭据。
# 函数用途: 让原凭据工具列出本群安全摘要或推荐同origin引用；不执行认证、不发送卡片。
def discover_group_credentials(agent, *, url='', name='', operation='list') -> dict:
    if operation not in {'list', 'find'} or not isinstance(name, str) or len(name) > 128:
        _denied('群凭据发现参数无效。')
    actor, owner, chat = _context(agent, require_enabled=operation != 'list')
    origin = _origin(url) if url else ''
    if operation == 'find' and not origin:
        _denied('查找凭据需要准确的接口地址。')
    all_rows = _rows(agent, owner, chat, include_task=True)
    rows = [row for row in all_rows if _access_scope(row) == 'group']
    active = _current_rows(rows)
    enabled = getattr(capability_config_for_agent(agent), 'im_group_shared_credentials_enabled', False) is True
    candidates, own_present = _candidates(rows, actor.actor_id, origin) if origin else (
        [], bool(_own_rows(active, actor.actor_id, None)))
    visible = [row for row in rows if (not origin or row['origin'] == origin)
        and (not name or row.get('credential_name') == name)]
    candidates = [row for row in candidates if enabled and (not name or row.get('credential_name') == name)]
    selected = _stable(candidates)[0]['credential_ref'] if candidates else ''
    reason = ('own_credential' if own_present else 'group_credential') if selected else (
        'group_sharing_disabled' if not enabled else 'list_only' if not origin else
        'own_credential_unavailable' if own_present else 'no_usable_credential')
    active_refs = {row['credential_ref'] for row in active}
    return {'credentials': [_summary(row, actor.actor_id, superseded=row['credential_ref'] not in active_refs)
                            for row in _stable(visible)],
        'shareable_credentials': [_summary(row, actor.actor_id) for row in _stable(all_rows)
            if enabled and _access_scope(row) == 'task' and (not origin or row['origin'] == origin)
            and (not name or row.get('credential_name') == name) and _shareable(agent, row, actor.actor_id)],
        'selected_credential_ref': selected, 'selection_reason': reason, 'own_credential_present': own_present,
        'group_sharing_enabled': enabled}


# LLM: 调用方须在凭据写锁外执行；不缓存成员，只核当前原claim/child attempt及原HostActor，status/revoke可不要求开关。
# 函数用途: 为一次发现/使用/管理读取准确群身份；未知、退群或分页不完整直接安全拒绝，不挂起等待。
def _context(agent, scope=None, *, require_enabled=True):
    try:
        home = agent.home_paths
        capability = capability_config_for_agent(agent)
        if (home.owner_kind != 'group' or require_enabled
                and getattr(capability, 'im_group_shared_credentials_enabled', False) is not True):
            _denied('当前来源未启用群共享凭据。')
        current = getattr(agent, '_current_run_params', None)
        if scope is None:
            scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
        if not _scope_is_current(agent, scope):
            _denied('当前凭据调用已失效。')
        channel, actor_id = approval_recipient(agent, scope)
        link = agent.conversation_store.tasks.load(scope.root_task_id)
        if link is None:
            _denied('当前任务缺少原群来源。')
        actor = group_operation_actor(agent, link.thread_id)
        identity = owner_identity_from_config(agent.config)
        chat = group_chat_id(home)
        if (actor.channel, actor.actor_id) != (channel, actor_id) or (
                identity.provider, identity.owner_kind, identity.owner_id) != (home.owner_provider, 'group', chat):
            _denied('当前群凭据身份无法核验。')
        snapshot = current_group_authority(agent)
        if not _scope_is_current(agent, scope):
            _denied('群成员核验期间原执行已失效，未使用凭据。')
        if actor.actor_id not in snapshot.member_ids:
            _denied('当前发起人已不在本群，未使用群凭据。')
        return actor, asdict(identity), chat
    except CredentialInputError:
        raise
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        _denied('无法核验当前群身份与完整成员，请稍后重试；不要在聊天中发送密钥。')


# LLM: 慢成员REST前后都只复读原本地身份；子代还比对当前RunParams与canonical attempt，不用旧上下文借新代资格。
# 函数用途: 拒绝查询期间已取消、切换根任务或过期的执行，不再发成员请求。
def _scope_is_current(agent, scope):
    current = getattr(agent, '_current_run_params', None)
    return bool(current is not None and scope.active and scope.thread_id
        and str(current.task_id or '') == scope.root_task_id
        and (scope.agent_kind != 'subagent' or (str(current.run_id or '') == scope.run_id
            and scope.execution_attempt_id and str(current.attempt_id or '') == scope.execution_attempt_id))
        and tool_approval_scope_is_current(agent, scope))


# LLM: access_scope是宿主有限协议，不接受多语言别名；缺字段只保留旧task，未知值不能猜共享或降级。
# 函数用途: 解析原记录的共享范围，不从群路径或模型正文推断。
def _access_scope(row):
    if not isinstance(row, dict):
        _denied('凭据记录格式无效。')
    value = row.get('access_scope', 'task')
    if value not in ('task', 'group'):
        _denied('凭据共享范围无法识别。')
    return value


# LLM: group row仍属于准确owner/channel/basegroup；共享仅解除actor/root/job隔离，不改变origin、网络权限或私密存储路径。
# 函数用途: 校验可共享的原记录出处，拒绝私聊、跨群和损坏结构。
def _validate_group_row(row, owner, chat):
    presentation = row.get('presentation')
    if (row.get('schema_version') != 'task_credential.v1' or row.get('kind') != 'task_credential'
            or row.get('owner') != owner or row.get('channel') != owner['provider']
            or not isinstance(presentation, dict) or presentation.get('kind') != 'group'
            or presentation.get('chat_id') != chat or not isinstance(row.get('actor_id'), str) or not row['actor_id']
            or not isinstance(row.get('status'), str)
            or row.get('submitter_actor_id') is not None and not isinstance(row['submitter_actor_id'], str)
            or not isinstance(row.get('origin'), str) or row['origin'] and _origin(row['origin']) != row['origin']):
        _denied('凭据不属于当前群或接口。')
    ref = row.get('credential_ref')
    if not isinstance(ref, str) or re.fullmatch(r'cred_[0-9a-f]{32}', ref) is None:
        _denied('凭据引用格式无效。')


# LLM: 只枚举原owner受保护目录的cred文件并沿原no-follow原语读取；损坏/未知scope不可被跳过后触发更高权限fallback。
# 函数用途: 在本次调用内读取原row集合，拒绝符号链接，不建索引、不解密、不改状态。
def _rows(agent, owner, chat, *, include_task=False):
    home = agent.home_paths.owner_home_dir
    with credential_stage('group_discovery'):
        try:
            descriptor = open_directory_beneath(home, PRIVATE_INTERACTIONS_OWNER_PARTS)
        except FileNotFoundError:
            return []
        try:
            names = sorted(os.listdir(descriptor))
        finally:
            os.close(descriptor)
        rows = []
        for name in names:
            if re.fullmatch(r'cred_[0-9a-f]{32}\.json', name) is None:
                continue
            try:
                text = read_text_beneath(home, (*PRIVATE_INTERACTIONS_OWNER_PARTS, name))
            except UnicodeError:
                _denied('凭据记录编码损坏，未自动改用其它人的凭据。')
            if text is None:
                _denied('凭据记录在读取期间变化，请重试。')
            try:
                row = json.loads(text)
            except (UnicodeError, ValueError):
                _denied('凭据记录损坏，未自动改用其它人的凭据。')
            if _access_scope(row) == 'task' and (not include_task or not isinstance(row.get('presentation'), dict)
                    or row['presentation'].get('kind') != 'group'):
                continue
            _validate_group_row(row, owner, chat)
            if row['credential_ref'] + '.json' != name:
                _denied('凭据记录与引用冲突。')
            rows.append(row)
        return rows


# LLM: 请求row必须仍等于原记录；旧handle可已revoked但不能伪造scope/代次来跨群定位，也不复活旧密文。
# 函数用途: 在解析前拒绝已更换、损坏或并发改变的调用投影。
def _require_current_row(row, rows):
    if not any(item == row for item in rows):
        _denied('凭据记录已经变化，请重新读取原引用。')


# LLM: own只取实际填写人；主动撤销及未成功输入不占own池，未撤销拒绝/过期/待配置仍阻止换人。
# 函数用途: 取得当前发起人的同origin原记录集合，名称和请求者不影响归属。
def _own_rows(rows, actor_id, origin):
    return [row for row in rows if (origin is None or row['origin'] == origin) and row.get('submitter_actor_id') == actor_id
        and row.get('status') != 'pending'
        and row.get('status') not in _RETIRED]


# LLM: 可用性只读原结构事实；已保存不等于已配置或验证，未知认证/坏deadline/拒绝记录不进入自动候选。
# 函数用途: 判断原共享row当前是否可交给既有HTTP认证器，不发网络验证。
def _usable(row):
    return (row.get('status') == 'ready' and row.get('verification_status') in ('unverified', 'verified')
        and row.get('authentication') in ('basic', 'bearer', 'header', 'signature', 'login')
        and isinstance(row.get('submitter_actor_id'), str) and bool(row['submitter_actor_id'])
        and isinstance(row.get('ciphertext'), str) and bool(row['ciphertext'])
        and not credential_ready_expired(row, now=time.time()))


# LLM: 使用门和发现共用own池裁决，不能先过滤失效/名称后把本人记录变成不存在。
# 函数用途: 返回可推荐集合及本人存在事实，不选更高权限替代账号。
def _candidates(rows, actor_id, origin):
    active = _current_rows(rows)
    own = _own_rows(active, actor_id, origin)
    return [row for row in (own or active) if row['origin'] == origin and _usable(row)], bool(own)


# LLM: superseded仅是原replacement事实的本次只读投影；真实提交+同群同origin+原代次+合法请求者缺一不可，取消未填不能退休旧key。
# 函数用途: 排除已被成功替换的旧row，避免用户撤销新key后旧key复活；不修改任何记录或建立引用别名。
def _current_rows(rows):
    indexed = {row['credential_ref']: row for row in rows}
    retired, edges = set(), {}
    for row in rows:
        old_ref = row.get('replaces_ref')
        old = indexed.get(old_ref) if isinstance(old_ref, str) else None
        if (old is None or row.get('status') not in ('ready', 'expired', 'revoked')
                or not _submitted(row) or row['origin'] != old['origin']
                or type(row.get('replaces_generation')) is not int or type(old.get('generation')) is not int
                or row['replaces_generation'] != old['generation'] or old['generation'] < 1
                or row.get('actor_id') not in (old.get('actor_id'), old.get('submitter_actor_id'))):
            continue
        if old_ref == row['credential_ref']:
            _denied('凭据替换关系损坏，未自动切换凭据。')
        edges[row['credential_ref']] = old_ref
        retired.add(old_ref)
    checked = set()
    for reference in edges:
        seen = set()
        while reference in edges and reference not in checked:
            if reference in seen:
                _denied('凭据替换关系损坏，未自动切换凭据。')
            seen.add(reference)
            reference = edges[reference]
        checked.update(seen)
    return [row for row in rows if row['credential_ref'] not in retired]


# LLM: replacement完成事实只取原提交时间/ready期限；坏期限不能使旧本人记录消失并触发借他人key。
# 函数用途: 验证原row曾成功提交，主动撤销后无需仍保存密文。
def _submitted(row):
    submitted, deadline = row.get('submitted_at'), row.get('ready_expires_at')
    return (_finite(submitted) and submitted > 0 and _finite(deadline) and deadline >= 0
        and (deadline > 0 or type(row.get('ttl_seconds')) is int and row['ttl_seconds'] == 0))


# LLM: 时间元数据允许有限int/float，拒绝bool/NaN/无限及整数溢出；坏值不能参与授权投影。
# 函数用途: 安全判断原记录时间是否可比较。
def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


# LLM: 时间只用于相同权限集合内的稳定推荐，不产生授权；缺失/畸形时间取0并以opaque ref破同分。
# 函数用途: 优先最近提交的有效凭据，重复发现不会随机换账号。
def _stable(rows):
    # LLM: 只排序本次已经核验的同权限集合，未知时间不得参与授权。
    # 函数用途: 生成有限数值时间与引用组成的稳定排序键。
    def key(row):
        value = row.get('submitted_at', 0)
        return (-(value if _finite(value) else 0), row['credential_ref'])
    return sorted(rows, key=key)


# LLM: origin只通过原结构URL规范器，无query/用户名输出；不扩大原HTTP网络门。
# 函数用途: 将调用目标归一到准确scheme/host/port，拒绝伪造站点。
def _origin(url):
    from .task_credentials import credential_origin

    if not isinstance(url, str):
        _denied('接口地址格式无效。')
    try:
        return credential_origin(url) if url else ''
    except ValueError:
        _denied('接口地址格式无效。')


# LLM: 只投影明确公开的标量；名称属于用户非秘密标签，其余任意metadata不透传；认证方式仅放行已知枚举，未知记录标unknown。
# 函数用途: 为群成员展示引用、origin、可用状态、认证方式与本人/管理资格，不暴露账号或密文。
def _summary(row, actor_id, *, superseded=False):
    name = row.get('credential_name', '')
    return {'credential_ref': row['credential_ref'], 'access_scope': _access_scope(row), 'origin': row['origin'],
        'name': name[:128] if isinstance(name, str) else '',
        'status': row.get('status') if row.get('status') in ('pending', 'ready', 'expired', 'revoked', 'cancelled', 'failed') else 'unknown',
        'verification_status': row.get('verification_status') if row.get('verification_status') in ('unverified', 'verified', 'rejected') else 'unknown',
        'authentication': row.get('authentication') if row.get('authentication') in ('collected', 'basic', 'bearer', 'header', 'signature', 'login') else 'unknown',
        'authentication_configured': row.get('authentication') in ('basic', 'bearer', 'header', 'signature', 'login'),
        'usable': _access_scope(row) == 'group' and not superseded and _usable(row), 'own': row.get('submitter_actor_id') == actor_id,
        'superseded': superseded, 'can_manage': actor_id in (row.get('actor_id'), row.get('submitter_actor_id'))}


# LLM: 旧task仅向其原请求人/填写人展示可share摘要，原卡MAC由同一宿主校验器负责；不自动公开或改写。
# 函数用途: 判断本人可无须重填升级的旧群卡，坏签名只是不列出可分享引用。
def _shareable(agent, row, actor_id):
    from .task_credential_forms import assert_credential_record_signature

    if row.get('status') != 'ready' or actor_id not in (row.get('actor_id'), row.get('submitter_actor_id')):
        return False
    try:
        assert_credential_record_signature(agent, row)
        return True
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False


# LLM: 错误只携固定宿主说明与码，不携路径、平台响应、秘密或模型正文，不转换成pending等待。
# 函数用途: 统一拒绝群凭据越界或缺事实，保留原凭据工具的安全错误协议。
def _denied(message):
    raise CredentialInputError('CREDENTIAL_NOT_AUTHORIZED', message, stage='group_authorization', effect_outcome='not_started')
