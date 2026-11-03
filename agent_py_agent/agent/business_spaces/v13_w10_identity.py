"""身份、偏好和角色投影；角色目录不能提升网关的原生管理员权限。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from ..plugin_host.errors import HostAPIError
from ..user_space.local_user_store import LocalUserStore


# LLM: 根目录仅来自本次可信宿主，不接受参数指定路径；不产生IO。
# 函数用途: 为原生身份服务取得宿主根。
def root(call):
    return Path(call.context.host.home_paths.root)


# LLM: local身份以当前owner为准，主体展示值不能选择另一个账号。
# 函数用途: 取得当前身份对应的普通账号编号。
def uid(call):
    return call.owner_id.removeprefix('local/') if call.owner_id.startswith('local/') else None


# LLM: 始终重查原生管理员门，不读rbac角色；只把明确权限拒绝转成False。
# 函数用途: 查询当前调用者是否拥有原生管理员身份。
def admin(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    try:
        v13_require_admin(call)
        return True
    except HostAPIError as error:
        if error.code == 'BUSINESS_ACCESS_DENIED':
            return False
        raise


# LLM: main与owner范围由W0绑定并校验；不自行拼接跨用户目录。
# 函数用途: 取得本次调用的规范业务存储。
def store(call, scope='owner'):
    from ..gateway_parts.business_api_v13 import v13_action_spec, v13_require_admin
    from .v13_store import v13_store_for
    backend = v13_store_for(call, scope)
    access_check = backend.authorize

    # LLM: W0在锁内、回执重放及持久提交前调用；管理员动作不能仅在入口核一次。
    # 函数用途: 给当前事务绑定实时访问与管理员末核。
    def authorize():
        access_check()
        if v13_action_spec(call.action).definition.admin:
            v13_require_admin(call)
        return True

    backend.authorize = authorize
    return backend


# LLM: 只读取已授权集合，返回隔离副本；存储故障不伪装成空列表。
# 函数用途: 列出集合记录。
def rows(call, collection, scope='owner'):
    return store(call, scope).list(collection)


# LLM: 缺失记录由调用者按动作语义处理，不自动创建。
# 函数用途: 读取集合内指定编号。
def one(call, collection, record_id, scope='owner'):
    return store(call, scope).get(collection, record_id)


# LLM: 所有摘要都只保存散列；键绑定动作、owner和subject，避免跨动作或跨主体重放。
# 函数用途: 在W0短事务内复核实时身份并提交记录和脱敏回执。
def write(call, collection, mutate, *, scope='owner', receipt_view=None):
    digest = hashlib.sha256(json.dumps([call.action, call.owner_id, call.subject_id, call.params,
        call.expected_revision], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    key = hashlib.sha256(json.dumps([call.action, call.owner_id, call.subject_id,
        call.idempotency_key], separators=(',', ':')).encode()).hexdigest()

    # LLM: 存储锁内再核live身份；原生副作用也必须置于该检查之后。
    # 函数用途: 把业务记录字典交给动作的短提交函数。
    def change(state):
        call.context.check(call.action, {'owner_id': call.owner_id})
        return mutate(state)

    return store(call, scope).transaction(collection, change, idempotency_key=key,
        params_digest=digest, receipt_view=receipt_view)


# LLM: 多集合沿同一W0锁与提交日志，禁止在单集合回调中嵌套事务。
# 函数用途: 原子提交同范围的多集合与一份脱敏回执。
def write_many(call, collections, mutate, *, scope='owner', receipt_view=None):
    digest = hashlib.sha256(json.dumps([call.action, call.owner_id, call.subject_id, call.params,
        call.expected_revision], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    key = hashlib.sha256(json.dumps([call.action, call.owner_id, call.subject_id,
        call.idempotency_key], separators=(',', ':')).encode()).hexdigest()

    # LLM: 回调拿到每个集合的记录字典，提交前复核原上下文。
    # 函数用途: 执行多集合的业务变更。
    def change(records):
        call.context.check(call.action, {'owner_id': call.owner_id})
        return mutate(records)

    return store(call, scope).transaction_many(collections, change, idempotency_key=key,
        params_digest=digest, receipt_view=receipt_view)


# LLM: 使用宿主注入时钟；不接受客户时间作为过期或审查依据。
# 函数用途: 返回UTC时间戳。
def now(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat().replace('+00:00', 'Z')


# LLM: CAS针对业务记录而非整集合，创建前态为0；严格拒绝bool伪装int。
# 函数用途: 检查本次请求的记录修订号。
def cas(call, record):
    revision = record['revision'] if record is not None else 0
    if type(call.expected_revision) is not int or call.expected_revision != revision:
        raise HostAPIError('REVISION_CONFLICT', 409)


# LLM: 复用原HMAC游标，先拒绝非规范Base64和递归解析异常；签名、主体和快照仍由原服务核验，读取零落盘。
# 函数用途: 为原生投影列表提供不可篡改分页。
def paged(call, items, label):
    import base64
    cursor = call.params.get('cursor')
    if cursor is not None:
        try:
            decoded = base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True)
            if base64.urlsafe_b64encode(decoded).decode('ascii') != cursor:
                raise ValueError('cursor')
            json.loads(decoded)
        except (ValueError, UnicodeError, RecursionError):
            raise HostAPIError('CURSOR_EXPIRED', 503) from None
    return call.services.spaces.page(label, call.params, items, {'revision': 0},
        actor=SimpleNamespace(subject_id=call.subject_id))


# LLM: 仅生成记录元数据，不提交；main集合明确标记其原生owner。
# 函数用途: 为新记录添加时间和修订。
def metadata(call, *, scope='owner'):
    return {'owner_id': 'local/main' if scope == 'main' else call.owner_id,
        'revision': 1, 'created_at': now(call), 'updated_at': now(call)}


# LLM: 枚举冻结与扩展动作事实，不从自然语言推导权限。
# 函数用途: 返回当前可描述的权限目录。
def permission_catalog(call):
    from ..gateway_parts.business_api_v13 import v13_action_specs
    from ..plugin_host.business_action_specs import business_action_specs
    catalog = {}
    for source, specs in (('frozen', business_action_specs()), ('v13', v13_action_specs())):
        for spec in specs.values():
            name = spec.action
            catalog[name] = {'permission': name, 'effect': spec.metadata['effect'],
                'domain': name.split('.')[0], 'source': source}
    return [catalog[name] for name in sorted(catalog)]


# LLM: 权限字符串只允许当前目录内精确动作名；不支持通配符或角色提升。
# 函数用途: 校验角色或API key申请的权限集合。
def validate_permissions(call, permissions):
    known = {entry['permission'] for entry in permission_catalog(call)}
    if any(permission not in known for permission in permissions):
        raise HostAPIError('INVALID_SCHEMA', 400)
    return sorted(set(permissions))


# LLM: admin行仅只读描述原生权限，永远不能经角色写入分配。
# 函数用途: 生成内置管理员角色。
def builtin_role(call):
    return {'role_id': 'admin', 'name': 'admin', 'description': '原生宿主管理员（不可分配）',
        'permissions': [entry['permission'] for entry in permission_catalog(call)][:1000],
        'builtin': True, 'member_count': 1, 'revision': 0, 'created_at': None, 'updated_at': None}


# LLM: 主集合只读投影剔除内部owner字段；成员数来自当前实际分配而非旧缓存。
# 函数用途: 输出一个角色的公开字段。
def role_view(call, record):
    members = rows(call, 'rbac_assignments', 'main')
    result = {key: record[key] for key in ('role_id', 'name', 'description', 'permissions',
        'builtin', 'revision', 'created_at', 'updated_at')}
    result['member_count'] = sum(record['role_id'] in member['roles'] for member in members)
    return result


# LLM: 当前用户角色仅来自主集合分配；内置admin由调用者明确的原生决策追加。
# 函数用途: 将角色编号解析成公开名称。
def assigned_roles(call, user_id, *, native_admin=False):
    assignment = one(call, 'rbac_assignments', user_id, 'main')
    ids = set(assignment['roles']) if assignment else set()
    names = {role['name'] for role in rows(call, 'rbac_roles', 'main') if role['role_id'] in ids}
    if native_admin:
        names.add('admin')
    return sorted(names)


# LLM: 只消费本地账号公开字段与原生MFA状态，password_hash和种子从不输出。
# 函数用途: 生成管理员用户列表的封闭投影。
def user_view(call, user):
    from ..user_space.totp_sso_auth import _read
    view = user.public_view()
    return {'user_id': user.user_id, 'display_name': user.display_name or user.user_id,
        'status': user.status, 'roles': assigned_roles(call, user.user_id), 'is_admin': False,
        'has_password': view['has_password'], 'mfa_enabled': bool(_read(root(call), user.user_id + '.json').get('enabled')),
        'in_audit_group': user.in_audit_group, 'revision': user.revision,
        'created_at': user.created_at, 'updated_at': user.updated_at}


# LLM: 偏好未设置时使用协议默认值，读取不会物化记录。
# 函数用途: 查询用户UI偏好。
def get_preferences(call):
    record = one(call, 'user_preferences', call.subject_id)
    return {'preferences': {key: record[key] for key in ('theme', 'default_time_window', 'locale', 'updated_at')}
        if record else {'theme': 'system', 'default_time_window': '24h', 'locale': 'zh-CN', 'updated_at': None}}


# LLM: 用户偏好last-write-wins，事务合并而不是锁外整条覆盖；locale额外遵守产品格式。
# 函数用途: 更新当前主体的UI偏好。
def update_preferences(call):
    if 'locale' in call.params and not re.fullmatch(r'[a-z]{2}(?:-[A-Z]{2})?', call.params['locale']):
        raise HostAPIError('INVALID_SCHEMA', 400)

    # LLM: 记录合并在存储锁内进行，严格绑定当前subject和owner。
    # 函数用途: 合并本次偏好补丁。
    def change(records):
        old = records.get(call.subject_id)
        if old is not None and old['owner_id'] != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        value = old or {**metadata(call), 'subject_id': call.subject_id, 'theme': 'system',
            'default_time_window': '24h', 'locale': 'zh-CN'}
        value = {**value, **call.params, 'revision': value['revision'] + bool(old), 'updated_at': now(call)}
        records[call.subject_id] = value
        return {'preferences': {key: value[key] for key in ('theme', 'default_time_window', 'locale', 'updated_at')}}

    return write(call, 'user_preferences', change)


# LLM: 仅读取原生SSO配置与真实验签器绑定，不把启用配置冒充可登录。
# 函数用途: 返回SSO状态的安全投影。
def sso_status(call):
    from ..user_space.totp_sso_auth import TotpSSOAuthService
    service = TotpSSOAuthService(root(call))
    config = service.get_sso_config()
    verifier = getattr(call.context.host, 'sso_assertion_verifier', None)
    return {'enabled': bool(config['enabled']), 'provider': config.get('protocol'),
        'issuer_host': urlsplit(config.get('issuer_url', '')).hostname,
        'client_id_masked': '***' if config.get('client_id') else None,
        'login_supported': bool(config['enabled']) and callable(verifier), 'revision': config.get('revision')}


# LLM: 本次认证主体、原生管理员与主集合MFA策略合并；没有email字段就返回null。
# 函数用途: 返回当前用户身份和偏好。
def session_me(call):
    from ..user_space.totp_sso_auth import _read
    local_id = uid(call)
    user_id = local_id or call.subject_id
    user = LocalUserStore(root(call)).read_user(local_id) if local_id not in (None, 'main') else None
    native_admin = admin(call)
    policy = one(call, 'mfa_policy', 'tenant', 'main') or {'required': False, 'enforce_for': 'all'}
    return {'me': {'user_id': user_id, 'owner_id': call.owner_id,
        'display_name': (user.display_name or user_id) if user else call.subject_id, 'email': None,
        'roles': assigned_roles(call, local_id, native_admin=native_admin) if local_id is not None
            else (['admin'] if native_admin else []), 'is_admin': native_admin,
        'preferences': get_preferences(call)['preferences'],
        'mfa': {'enabled': bool(_read(root(call), user_id + '.json').get('enabled')) if user else False,
            'required_by_policy': bool(policy['required']) and (policy['enforce_for'] == 'all' or native_admin)},
        'auth_method': 'gateway_token'}}


# LLM: 只走原生authenticate_user；秘密只在首次响应内，回执不保存口令、动态码或token。
# 函数用途: 为当前主体完成密码与第二因素登录握手。
def login(call):
    from ..user_space.user_auth import authenticate_user
    if uid(call) in (None, 'main'):
        raise HostAPIError('BUSINESS_ACCESS_DENIED', 403)

    # LLM: 副作用前再次检查调用身份；原生认证会复核账号有效性。
    # 函数用途: 签发一次当前owner的登录会话。
    def change(records):
        try:
            result = authenticate_user(root(call), account_source='local', user_id=uid(call),
                password=call.params['password'], ttl_seconds=call.params.get('ttl_seconds', 86400),
                totp_code=call.params.get('totp_code')).public_view()
        except HostAPIError as error:
            if error.code in {'INVALID_CREDENTIALS', 'TOTP_REQUIRED', 'TOTP_INVALID'}:
                raise HostAPIError('VERIFICATION_FAILED', 403) from None
            if error.code == 'TOTP_RATE_LIMITED':
                raise HostAPIError('RATE_LIMITED', 429) from None
            raise
        if result['owner_id'] != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return result

    return write(call, 'user_preferences', change,
        receipt_view=lambda result: {**result, 'session_token': '<redacted>'})


# LLM: 原生管理员门每次复核，内置角色只作为读取投影。
# 函数用途: 列出角色与当前成员数。
def list_roles(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    return {'roles': [builtin_role(call)] + [role_view(call, row) for row in rows(call, 'rbac_roles', 'main')]}


# LLM: 不接受未知权限或保留admin名称，重复名称不生成第二个角色。
# 函数用途: 创建自定义角色。
def create_role(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    permissions = validate_permissions(call, call.params['permissions'])

    # LLM: 名称唯一约束与写入处于同一角色事务。
    # 函数用途: 提交新角色。
    def change(records):
        if len(records) >= 199:
            raise HostAPIError('LIMIT_EXCEEDED', 400)
        if call.params['name'] == 'admin' or any(row['name'] == call.params['name'] for row in records.values()):
            raise HostAPIError('STATE_CONFLICT', 409)
        key = uuid4().hex
        row = {**metadata(call, scope='main'), 'role_id': key, 'name': call.params['name'],
            'description': call.params.get('description', ''), 'permissions': permissions,
            'builtin': False, 'member_count': 0}
        records[key] = row
        return {'role': role_view(call, row)}

    return write(call, 'rbac_roles', change, scope='main')


# LLM: 内置角色和同名冲突不可修改；修订检查位于提交锁内。
# 函数用途: 更新一个自定义角色。
def update_role(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    permissions = validate_permissions(call, call.params['permissions']) if 'permissions' in call.params else None

    # LLM: 使用锁内当前行而不是锁外快照。
    # 函数用途: 提交角色补丁。
    def change(records):
        key = call.params['role_id']
        if key == 'admin':
            raise HostAPIError('STATE_CONFLICT', 409)
        row = records.get(key)
        if row is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        if row['builtin']:
            raise HostAPIError('STATE_CONFLICT', 409)
        cas(call, row)
        name = call.params.get('name', row['name'])
        if name == 'admin' or any(other['name'] == name and other['role_id'] != key for other in records.values()):
            raise HostAPIError('STATE_CONFLICT', 409)
        row.update(name=name, description=call.params.get('description', row['description']),
            permissions=permissions if permissions is not None else row['permissions'],
            revision=row['revision'] + 1, updated_at=now(call))
        return {'role': role_view(call, row)}

    return write(call, 'rbac_roles', change, scope='main')


# LLM: 删除前检查分配引用，不能留下孤立角色；内置角色始终只读。
# 函数用途: 删除没有成员的自定义角色。
def delete_role(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)

    # LLM: 本回调持角色短锁，在实际删除前查引用和CAS。
    # 函数用途: 删除一个未分配的角色记录。
    def change(records):
        key = call.params['role_id']
        if key == 'admin':
            raise HostAPIError('STATE_CONFLICT', 409)
        row = records.get(key)
        if row is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        if row['builtin'] or any(key in entry['roles'] for entry in rows(call, 'rbac_assignments', 'main')):
            raise HostAPIError('STATE_CONFLICT', 409)
        cas(call, row)
        del records[key]
        return {'role_id': key, 'deleted': True}

    return write(call, 'rbac_roles', change, scope='main')


# LLM: 每个默认名最多生成一次，已存在角色不被覆盖或扩权。
# 函数用途: 初始化三类SOC默认角色。
def seed_roles(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    catalog = permission_catalog(call)

    # LLM: 默认权限只取结构化effect和精确域前缀，不分析描述文案。
    # 函数用途: 在一次事务中补齐缺失的默认角色。
    def change(records):
        created = []
        for name in ('soc_admin', 'soc_analyst', 'soc_viewer'):
            if any(row['name'] == name for row in records.values()):
                continue
            if len(records) >= 199:
                raise HostAPIError('LIMIT_EXCEEDED', 400)
            permissions = [entry['permission'] for entry in catalog if name == 'soc_admin' or entry['effect'] == 'read'
                or name == 'soc_analyst' and entry['domain'] in {'soc', 'cases', 'feedback', 'investigations'}]
            key = uuid4().hex
            records[key] = {**metadata(call, scope='main'), 'role_id': key, 'name': name,
                'description': '默认SOC角色', 'permissions': permissions, 'builtin': False, 'member_count': 0}
            created.append(key)
        return {'roles': [builtin_role(call)] + [role_view(call, row) for row in records.values()], 'created': created}

    return write(call, 'rbac_roles', change, scope='main')


# LLM: revision由排序后的精确动作名称计算，不受描述或排列变化影响。
# 函数用途: 列出权限矩阵及目录指纹。
def list_permissions(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    catalog = permission_catalog(call)
    revision = hashlib.sha256(json.dumps([entry['permission'] for entry in catalog], separators=(',', ':')).encode()).hexdigest()
    return {'permissions': catalog, 'catalog_revision': revision}


# LLM: 原生账号public_view是唯一来源，坏记录不跳过；分页绑定实际结果。
# 函数用途: 管理员查询账号列表。
def list_users(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    users = LocalUserStore(root(call)).list_users()
    result = [user_view(call, user) for user in users if user.status in {'enabled', 'disabled'}
        and ('status' not in call.params or user.status == call.params['status'])
        and ('query' not in call.params or call.params['query'].casefold() in (user.user_id + ' ' + user.display_name).casefold())]
    return paged(call, result, 'admin.users.list')


# LLM: 输入编号经过原生store验证，缺失不能返回空成功。
# 函数用途: 管理员查询一个账号。
def get_user(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    user = LocalUserStore(root(call)).read_user(call.params['user_id'])
    if user is None or user.status not in {'enabled', 'disabled'}:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return {'user': user_view(call, user)}


# LLM: 用户角色是展示与计算数据，不改变原生admin；只允许既有自定义role_id。
# 函数用途: 替换、添加或移除账号角色。
def set_user_roles(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    user_id = call.params['user_id']
    if LocalUserStore(root(call)).read_user(user_id) is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)

    # LLM: CAS和合并在同一赋值事务中，禁止保留的admin编号。
    # 函数用途: 按模式提交角色分配。
    def change(records):
        catalog = {row['role_id']: row for row in rows(call, 'rbac_roles', 'main')}
        requested = set(call.params['roles'])
        if 'admin' in requested or any(catalog.get(role, {}).get('builtin') for role in requested):
            raise HostAPIError('STATE_CONFLICT', 409)
        if requested - set(catalog):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        old = records.get(user_id)
        cas(call, old)
        previous = set(old['roles']) if old else set()
        mode = call.params.get('mode', 'replace')
        selected = requested if mode == 'replace' else previous | requested if mode == 'add' else previous - requested
        if len(selected) > 50:
            raise HostAPIError('LIMIT_EXCEEDED', 400)
        record = old or {**metadata(call, scope='main'), 'user_id': user_id}
        record.update(roles=sorted(selected), revision=old['revision'] + 1 if old else 1, updated_at=now(call))
        records[user_id] = record
        return {'user_id': user_id, 'roles': sorted(catalog[role]['name'] for role in selected), 'revision': record['revision']}

    return write(call, 'rbac_assignments', change, scope='main')


# LLM: 不复制账号状态机；调用原生启停、revision与资源收口门，DELETE只映射disabled。
# 函数用途: 管理员启用或停用一个普通账号。
def set_user_status(call):
    from ..common.global_write_guard import require_global_writer
    from ..conversation.business_access import BusinessError
    from ..gateway_parts.business_api_v13 import v13_require_admin
    from ..user_space.local_user_lifecycle import LocalUserLifecycle
    from ..user_space.owner_resolver import OwnerIdentity
    v13_require_admin(call)
    require_global_writer('local_users')
    if len(call.params.get('reason', '')) > 256:
        raise HostAPIError('INVALID_SCHEMA', 400)

    # LLM: 原生生命周期每个发布点重新确认管理员和全局writer；角色不参与判断。
    # 函数用途: 在提交过程中复核原生账号治理资格。
    def revalidate():
        call.context.check(call.action, {'owner_id': call.owner_id})
        v13_require_admin(call)
        require_global_writer('local_users')

    lifecycle = LocalUserLifecycle(LocalUserStore(root(call)), revalidate=revalidate)
    method = lifecycle.disable if call.params['status'] == 'disabled' else lifecycle.enable
    native_key = hashlib.sha256(json.dumps([call.action, call.owner_id, call.subject_id,
        call.idempotency_key], separators=(',', ':')).encode()).hexdigest()
    committed = [False]

    # LLM: 原生操作有自己的耐久幂等回执，v13只保存安全公开投影。
    # 函数用途: 在业务回执事务内执行原生状态转换。
    def change(records):
        original = LocalUserStore(root(call)).read_user(call.params['user_id'])
        if original is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        # 易失败的MFA/角色读取先于原生副作用；提交后只投影已返回的原生账号。
        projection = user_view(call, original)
        try:
            if call.params['status'] == 'disabled':
                result = method(call.params['user_id'], call.expected_revision,
                    reason=call.params.get('reason', ''), idempotency_key=native_key, actor_owner=OwnerIdentity.local_main())
            else:
                result = method(call.params['user_id'], call.expected_revision,
                    idempotency_key=native_key, actor_owner=OwnerIdentity.local_main())
        except HostAPIError as error:
            mapping = {'USER_NOT_FOUND': ('RESOURCE_NOT_FOUND', 404), 'USER_REVISION_CHANGED': ('REVISION_CONFLICT', 409),
                'USER_STATE_CONFLICT': ('STATE_CONFLICT', 409)}
            if error.code in mapping:
                raise HostAPIError(*mapping[error.code], outcome=error.outcome) from None
            raise
        committed[0] = True
        projection.update(status=result.user.status, revision=result.user.revision,
            updated_at=result.user.updated_at)
        return {'user': projection}

    try:
        return write(call, 'rbac_assignments', change, scope='main')
    except (HostAPIError, BusinessError) as error:
        if committed[0]:
            raise HostAPIError(error.code, getattr(error, 'status', 503), outcome='committed') from None
        raise


# LLM: 查询别人必须通过原生admin；角色权限只做聚合而不直接授予网关控制。
# 函数用途: 聚合本地主体的有效权限与身份来源。
def effective_permissions(call):
    local_id = uid(call)
    user_id = call.params.get('user_id', local_id or call.subject_id)
    native_admin = admin(call)
    if user_id != (local_id or call.subject_id) and not native_admin:
        raise HostAPIError('BUSINESS_ACCESS_DENIED', 403)
    if local_id is None and user_id == call.subject_id:
        # 外部主体不按同名猜本地账号；原生关联只作来源说明，不自动继承本地角色。
        provider = call.context.host.home_paths.owner_provider
        selected = call.params.get('provider', 'all')
        links = [{'provider': provider, 'linked': True, 'subject': call.subject_id}]
        links = links if provider in ('local', 'feishu') and selected in ('all', provider) else []
        return {'user_id': user_id, 'providers': links, 'is_admin': native_admin,
            'roles': ['admin'] if native_admin else [], 'permissions': [
                {'permission': entry['permission'], 'granted_by': ['native_admin']}
                for entry in permission_catalog(call)] if native_admin else [], 'coverage': 'partial'}
    user = LocalUserStore(root(call)).read_user(user_id) if user_id != 'main' else None
    if user_id != 'main' and user is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    target_admin = native_admin if user_id == local_id else user_id == 'main'
    assignment = one(call, 'rbac_assignments', user_id, 'main')
    role_ids = set(assignment['roles']) if assignment else set()
    granted = {}
    for role in rows(call, 'rbac_roles', 'main'):
        if role['role_id'] in role_ids:
            for permission in role['permissions']:
                granted.setdefault(permission, []).append('role:' + role['role_id'])
    if target_admin:
        for entry in permission_catalog(call):
            granted.setdefault(entry['permission'], []).append('native_admin')
    links, coverage = provider_links(call, user_id)
    selected_provider = call.params.get('provider', 'all')
    if selected_provider != 'all':
        links = [link for link in links if link['provider'] == selected_provider]
    return {'user_id': user_id, 'providers': links, 'is_admin': target_admin,
        'roles': assigned_roles(call, user_id, native_admin=target_admin),
        'permissions': [{'permission': permission, 'granted_by': sorted(sources)} for permission, sources in sorted(granted.items())],
        'coverage': coverage}


# LLM: 只读原身份关联账，按明确owner字段关联，不凭姓名或展示文本合并身份。
# 函数用途: 列出指定本地主体的本地和飞书绑定。
def provider_links(call, user_id):
    from ..common.json_io import read_jsonl_objects_report
    from ..user_space.identity_store import _provider_index_path
    home = call.context.host.home_paths
    reports = [read_jsonl_objects_report(_provider_index_path(home, 'feishu')),
        read_jsonl_objects_report(home.linked_identities_jsonl)]
    links = [{'provider': 'local', 'linked': True, 'subject': user_id}]
    latest, partial = {}, False
    for report in reports:
        for row in report.records:
            provider, subject = row.get('provider'), row.get('provider_subject_id')
            if provider not in ('local', 'feishu') or type(subject) is not str or not 1 <= len(subject) <= 256:
                partial = True
                continue
            # 先解析最新关联再筛owner，防止已迁走/撤销的旧行被复活；canonical账优先。
            latest[(provider, subject)] = row
    for (provider, subject), row in sorted(latest.items()):
        owner_matches = row.get('owner_id') == 'local/' + user_id or (
            provider == 'local' and row.get('owner_id') == user_id)
        if owner_matches and row.get('status', 'active') == 'active':
            link = {'provider': provider, 'linked': True, 'subject': subject}
            if link not in links:
                links.append(link)
    if len(links) > 10:
        return links[:10], 'partial'
    return links, 'partial' if partial or any(report.load_errors for report in reports) else 'complete'
