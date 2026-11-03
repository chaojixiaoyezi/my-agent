"""租户身份、原生账号开通、候补审批及严格只读的跨租户安全视图。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from ..common.global_write_guard import require_global_writer
from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from ..user_space.local_user_lifecycle import LocalUserLifecycle, LocalUserOperation
from ..user_space.local_user_store import LocalUserStore
from ..user_space.owner_quota import (
    LocalUserQuotaLimits,
    OwnerQuotaUnavailable,
    owner_logical_usage_bytes,
    read_local_user_quota,
)


# LLM: 原生管理员判断独立于自定义角色，入口再次核验实时身份，不扩大跨owner权限。
# 函数用途: 为管理员租户和候补操作守住原生权限门。
def _require_admin(call):
    from .v13_w10_identity import admin
    call.context.check(call.action, {'owner_id': call.owner_id})
    if not admin(call):
        raise HostAPIError('BUSINESS_ACCESS_DENIED', 403)


# LLM: 只读原配额和逻辑用量，失败返回未知而非零；不会创建配额、锁或回执。
# 函数用途: 投影租户磁盘配额。
def _quota(root, home, *, local_user=False):
    used = None
    limit = None
    try:
        if local_user:
            limits, _revision = read_local_user_quota(root, home)
            limit = limits.max_disk_mb * 1024 * 1024
        used = owner_logical_usage_bytes(home)
    except (OwnerQuotaUnavailable, OSError, ValueError):
        pass
    return {'used_bytes': used, 'limit_bytes': limit}


# LLM: 普通用户身份从原生注册表得到，物化中或失败状态不会冒充启用；不暴露内部路径。
# 函数用途: 将原生本地账号转换为封闭租户视图。
def _tenant(call, user):
    from .v13_w10_identity import root
    registry = LocalUserStore(root(call))
    owner = registry.owner_home(user.user_id)
    return {'tenant_id': owner.owner_id, 'owner_id': owner.owner_id, 'kind': 'local_user',
        'display_name': user.display_name or user.user_id,
        'status': user.status if user.status in {'enabled', 'disabled'} else 'unknown',
        'is_admin': False, 'quota': _quota(root(call), owner.home_dir, local_user=True),
        'created_at': user.created_at}


# LLM: 本次可信owner与subject是唯一身份来源，参数不能选择其它租户；读取不物化目录。
# 函数用途: 查询当前租户和通道身份。
def me(call):
    from .v13_w10_identity import admin, root, uid
    call.context.check(call.action, {'owner_id': call.owner_id})
    paths = call.context.host.home_paths
    if call.owner_id.startswith('local/') and call.owner_id != 'local/main':
        user = LocalUserStore(root(call)).read_user(uid(call))
        if user is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        tenant = _tenant(call, user)
    else:
        kind = 'main' if call.owner_id == 'local/main' else (
            'provider_group' if getattr(paths, 'owner_kind', None) == 'group' else 'provider_user')
        tenant = {'tenant_id': call.owner_id, 'owner_id': call.owner_id, 'kind': kind,
            'display_name': call.subject_id, 'status': 'active', 'is_admin': admin(call),
            'quota': _quota(root(call), call.owner_home), 'created_at': None}
    actor = call.context.actor
    channel = getattr(actor, 'channel', None) or 'web'
    return {'tenant': tenant, 'identity': {'subject_id': call.subject_id, 'channel': channel}}


# LLM: 管理员枚举原生账号，普通本地租户仅见自身；provider租户不映射到任意local账号。
# 函数用途: 分页返回当前租户可见的账号。
def my_users(call):
    from .v13_w10_identity import admin, paged, root, uid, user_view
    call.context.check(call.action, {'owner_id': call.owner_id})
    registry = LocalUserStore(root(call))
    if admin(call):
        users = registry.list_users()
    elif call.owner_id.startswith('local/') and call.owner_id != 'local/main':
        user = registry.read_user(uid(call))
        if user is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        users = [user]
    else:
        users = []
    return paged(call, [user_view(call, user) for user in users], 'tenant-users')


# LLM: 列表只消费权威账号和配额，不调用开通接口；状态筛选在分页前完成。
# 函数用途: 管理员分页查询本地主机租户。
def admin_list(call):
    from .v13_w10_identity import paged, root
    _require_admin(call)
    items = [_tenant(call, user) for user in LocalUserStore(root(call)).list_users()]
    if 'status' in call.params:
        items = [item for item in items if item['status'] == call.params['status']]
    return paged(call, items, 'admin-tenants')


# LLM: 原生配额以整数MiB存储，拒绝不能精确表示的字节数而不静默提高或降低配额。
# 函数用途: 校验开通参数并生成原生有限配额。
def _provision_quota(params):
    quota = LocalUserQuotaLimits().to_dict()
    if len(params.get('display_name', '')) > 80:
        raise BusinessError('INVALID_SCHEMA', details={'path': 'params.display_name'})
    if 'quota_bytes' in params:
        value = params['quota_bytes']
        if type(value) is not int or value < 1024 * 1024 or value % (1024 * 1024):
            raise BusinessError('INVALID_SCHEMA', details={'path': 'params.quota_bytes'})
        quota['max_disk_mb'] = value // (1024 * 1024)
    try:
        LocalUserQuotaLimits.parse(quota)
    except ValueError:
        raise BusinessError('INVALID_SCHEMA', details={'path': 'params.quota_bytes'}) from None
    return quota


# LLM: 候补记录只属于main集合，跨归属和缺失都返回404；公开视图不暴露内部owner字段。
# 函数用途: 读取锁内候补记录并校验归属。
def _entry(records, entry_id):
    record = records.get(entry_id)
    if record is None or record.get('owner_id') != 'local/main':
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return record


# LLM: 输出字段严格匹配WaitlistEntry，不让持久化内部字段泄漏到响应。
# 函数用途: 转换候补公开视图。
def _entry_view(record):
    return {key: record[key] for key in ('entry_id', 'email', 'organization', 'note', 'status',
        'decided_by', 'tenant_id', 'revision', 'created_at', 'updated_at')}


# LLM: 原生生命周期负责物化、策略与配额；实时复查和global writer在原生每个发布点生效。
# 函数用途: 在一次main短事务内开通账号并推进已批准候补。
def provision(call):
    from .v13_w10_identity import now, root, write
    _require_admin(call)
    quota = _provision_quota(call.params)
    require_global_writer('local_users')

    # LLM: 原生服务会在每个写入前调用本回调，撤权或受限执行器均不能继续发布。
    # 函数用途: 重查开通操作的实时管理员与全局写资格。
    def revalidate():
        _require_admin(call)
        require_global_writer('local_users')

    # LLM: 候补前态在同一锁内验证和更新；原生幂等可恢复账号已发表但外层回执未提交的重试。
    # 函数用途: 执行一次原生开通并提交候补终态。
    def change(records):
        entry_id = call.params.get('waitlist_entry_id')
        entry = _entry(records, entry_id) if entry_id else None
        if entry is not None and entry['status'] != 'approved':
            raise HostAPIError('STATE_CONFLICT', 409)
        lifecycle = LocalUserLifecycle(LocalUserStore(root(call)), revalidate=revalidate)
        # 原生索引固定动作、owner、subject及原键；activation绑定完整v13输入，包含候补ID和修订。
        native_key = hashlib.sha256(json.dumps([call.action, call.owner_id, call.subject_id,
            call.idempotency_key], separators=(',', ':')).encode()).hexdigest()
        intent_digest = hashlib.sha256(json.dumps([call.action, call.owner_id, call.subject_id,
            call.params, call.expected_revision], ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        operation = LocalUserOperation('v13-w10.tenants.admin.provision', intent_digest, native_key)
        try:
            result = lifecycle.create(call.params['user_id'], operation,
                display_name=call.params.get('display_name', ''), quota=quota,
                actor_owner='local/main')
        except HostAPIError as error:
            # 本adapter的activation是完整输入摘要，原生ACTIVATION_REVOKED在此准确表示同键换参。
            if error.code == 'ACTIVATION_REVOKED':
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409) from None
            if error.code in {'USER_EXISTS', 'USER_UNMANAGED', 'USER_STATE_CONFLICT'}:
                raise HostAPIError('STATE_CONFLICT', 409) from None
            raise
        tenant = _tenant(call, result.user)
        if entry is not None:
            entry = {**entry, 'status': 'provisioned', 'tenant_id': tenant['tenant_id'],
                'revision': entry['revision'] + 1, 'updated_at': now(call)}
            records[entry_id] = entry
        return {'tenant': tenant, 'waitlist_entry': _entry_view(entry) if entry else None}

    return write(call, 'waitlist_entries', change, scope='main')


# LLM: 创建是候补名单唯一生产入口；email仅做合同规定的格式校验，不从文本推断状态。
# 函数用途: 让已认证主体在主租户候补集合提交申请。
def waitlist_create(call):
    from .v13_w10_identity import now, write
    email = call.params['email']
    parts = email.split('@')
    if len(parts) != 2 or not all(parts) or any(char.isspace() for char in email):
        raise BusinessError('INVALID_SCHEMA', details={'path': 'params.email'})
    require_global_writer('waitlist_entries')

    # LLM: 生成编号和初始状态在幂等事务内，回放不再创建第二条申请。
    # 函数用途: 提交待审批候补记录。
    def change(records):
        require_global_writer('waitlist_entries')
        stamp = now(call)
        record = {'entry_id': 'wait_' + uuid4().hex, 'email': email,
            'organization': call.params.get('organization'), 'note': call.params.get('note'),
            'status': 'pending', 'decided_by': None, 'tenant_id': None, 'revision': 1,
            'created_at': stamp, 'updated_at': stamp, 'owner_id': 'local/main'}
        records[record['entry_id']] = record
        return {'entry': _entry_view(record)}

    return write(call, 'waitlist_entries', change, scope='main')


# LLM: 管理员查询main候补集合，读操作无状态修补或回执写入。
# 函数用途: 按状态分页查询候补名单。
def waitlist_list(call):
    from .v13_w10_identity import paged, rows
    _require_admin(call)
    items = [_entry_view(row) for row in rows(call, 'waitlist_entries', 'main')
        if row.get('owner_id') == 'local/main' and (
            'status' not in call.params or row['status'] == call.params['status'])]
    items.sort(key=lambda row: (row['created_at'] or '', row['entry_id']), reverse=True)
    return paged(call, items, 'waitlist-entries')


# LLM: CAS与前态校验均在存储锁内；rejected/provisioned不可逆，approved可明确转为rejected。
# 函数用途: 管理员审批候补并保存审计主体。
def waitlist_update(call):
    from .v13_w10_identity import cas, now, write
    _require_admin(call)
    require_global_writer('waitlist_entries')

    # LLM: 不读取锁外旧快照覆盖记录，拒绝终态或非法转换，幂等重放由存储先处理。
    # 函数用途: 在当前修订上提交审批结果。
    def change(records):
        _require_admin(call)
        require_global_writer('waitlist_entries')
        record = _entry(records, call.params['entry_id'])
        cas(call, record)
        if record['status'] not in {'pending', 'approved'}:
            raise HostAPIError('STATE_CONFLICT', 409)
        if call.params['status'] not in {'approved', 'rejected'}:
            raise HostAPIError('INVALID_SCHEMA', 400)
        record = {**record, 'status': call.params['status'], 'decided_by': call.subject_id,
            'note': call.params.get('note', record['note']), 'revision': record['revision'] + 1,
            'updated_at': now(call)}
        records[record['entry_id']] = record
        return {'entry': _entry_view(record)}

    return write(call, 'waitlist_entries', change, scope='main')


# LLM: 缺字段、重复编号或非法枚举都单列为覆盖不完整，不当作零。
# 函数用途: 校验跨租户读口返回的单条安全记录。
def _valid_security_record(record, collection, owner_id):
    if not isinstance(record, dict) or record.get('owner_id') != owner_id:
        return False
    id_key = 'alert_id' if collection == 'soc_alerts' else 'case_id'
    if not isinstance(record.get(id_key), str) or not record[id_key]:
        return False
    statuses = {'new', 'acknowledged', 'in_progress', 'resolved', 'closed'} if collection == 'soc_alerts' else {
        'open', 'investigating', 'contained', 'resolved', 'closed'}
    if record.get('status') not in statuses:
        return False
    if collection == 'soc_alerts':
        if record.get('severity') not in {'info', 'low', 'medium', 'high', 'critical'}:
            return False
        if not isinstance(record.get('title'), str) or not 1 <= len(record['title']) <= 300:
            return False
        if 'created_at' not in record:
            return False
        stamp = record['created_at']
        if stamp is not None:
            try:
                if not isinstance(stamp, str) or datetime.fromisoformat(stamp.replace('Z', '+00:00')).tzinfo is None:
                    return False
            except ValueError:
                return False
    return True


# LLM: 必须调用F2 read_only，禁止可写store、配额准入或回执；坏源不能隐藏成空成功。
# 函数用途: 读取一个子租户集合并排除结构异常和重复记录。
def _security_rows(home, owner_id, collection):
    from .v13_store import V13Store
    try:
        records = V13Store.read_only(home).list(collection)
        if not isinstance(records, list):
            return [], False
        result = []
        seen = set()
        complete = True
        id_key = 'alert_id' if collection == 'soc_alerts' else 'case_id'
        for record in records:
            if not _valid_security_record(record, collection, owner_id):
                complete = False
                continue
            identifier = record[id_key]
            if identifier in seen:
                complete = False
                continue
            seen.add(identifier)
            result.append(record)
        return result, complete
    except (HostAPIError, BusinessError, OSError, ValueError, TypeError):
        return [], False


# LLM: 只按原生账号ID枚举受管子租户；坏账号保留未知行，坏集合计数为null而非部分求和。
# 函数用途: 生成MSSP子租户和告警的只读快照。
def _portfolio_snapshot(call, *, include_cases=True):
    from .v13_w10_identity import root
    _require_admin(call)
    registry = LocalUserStore(root(call))
    children = []
    all_alerts = []
    complete = True
    alerts_complete = True
    cases_complete = True
    for user_id in registry.list_user_ids():
        home = registry.owner_home(user_id)
        if call.params.get('tenant_id') not in (None, home.owner_id):
            continue
        try:
            user = registry.read_user(user_id)
            if user is None:
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            summary = {'tenant_id': home.owner_id, 'display_name': user.display_name or user_id,
                'status': user.status, 'alerts_open': None, 'alerts_critical_open': None,
                'cases_open': None, 'last_alert_at': None}
        except (HostAPIError, OSError, ValueError, TypeError):
            children.append({'tenant_id': home.owner_id, 'display_name': user_id, 'status': 'unknown',
                'alerts_open': None, 'alerts_critical_open': None, 'cases_open': None, 'last_alert_at': None})
            complete = alerts_complete = cases_complete = False
            continue
        alerts, alerts_ok = _security_rows(home.home_dir, home.owner_id, 'soc_alerts')
        cases, cases_ok = _security_rows(home.home_dir, home.owner_id, 'soc_cases') if include_cases else ([], True)
        if alerts_ok:
            opened = [alert for alert in alerts if alert['status'] not in {'resolved', 'closed'}]
            summary['alerts_open'] = len(opened)
            summary['alerts_critical_open'] = sum(alert['severity'] == 'critical' for alert in opened)
            stamps = [alert['created_at'] for alert in alerts if alert['created_at'] is not None]
            summary['last_alert_at'] = max(stamps, key=lambda stamp: datetime.fromisoformat(
                stamp.replace('Z', '+00:00'))) if stamps else None
        if include_cases and cases_ok:
            summary['cases_open'] = sum(case['status'] not in {'resolved', 'closed'} for case in cases)
        children.append(summary)
        all_alerts.extend({'tenant_id': home.owner_id, **{key: alert[key] for key in (
            'alert_id', 'title', 'severity', 'status', 'created_at')}} for alert in alerts)
        complete = complete and alerts_ok and cases_ok
        alerts_complete = alerts_complete and alerts_ok
        cases_complete = cases_complete and cases_ok
    return children, all_alerts, complete, alerts_complete, cases_complete


# LLM: 部分覆盖不能被分页成功覆盖；分页仍由原HMAC游标绑定筛选和快照。
# 函数用途: 为MSSP不完整快照附加真实覆盖状态。
def _portfolio_page(call, items, label, complete):
    from .v13_w10_identity import paged
    result = paged(call, items, label)
    if not complete:
        result['coverage'] = 'partial'
    return result


# LLM: 跨owner只读扫描后返回公开计数，不触发子租户初始化或安全记录更新。
# 函数用途: 分页查看MSSP受管子租户。
def children(call):
    items, _alerts, complete, _alerts_ok, _cases_ok = _portfolio_snapshot(call)
    return _portfolio_page(call, items, 'mssp-children', complete)


# LLM: 任一告警源未知则总告警及各severity计数为null，不把已读子集伪装成完整总量。
# 函数用途: 聚合受管子租户的真实告警与事件统计。
def portfolio(call):
    from .v13_w10_identity import now
    items, alerts, complete, alerts_ok, cases_ok = _portfolio_snapshot(call)
    severities = dict.fromkeys(('info', 'low', 'medium', 'high', 'critical'), 0 if alerts_ok else None)
    if alerts_ok:
        for alert in alerts:
            if alert['status'] not in {'resolved', 'closed'}:
                severities[alert['severity']] += 1
    ranked = sorted(items, key=lambda item: (item['alerts_open'] is None,
        -(item['alerts_open'] or 0), item['tenant_id']))
    return {'tenants': len(items), 'alerts_open': sum(item['alerts_open'] for item in items) if alerts_ok else None,
        'alerts_by_severity': severities,
        'cases_open': sum(item['cases_open'] for item in items) if cases_ok else None,
        'top_tenants': ranked[:20], 'generated_at': now(call), 'coverage': 'complete' if complete else 'partial'}


# LLM: 筛选只使用结构化字段，分页前做跨owner投影；该读入口绝不回写访问状态。
# 函数用途: 分页查询MSSP跨租户告警列表。
def portfolio_alerts(call):
    _items, alerts, complete, _alerts_ok, _cases_ok = _portfolio_snapshot(call, include_cases=False)
    selected = [alert for alert in alerts if all(
        key not in call.params or alert[key] == call.params[key] for key in ('tenant_id', 'severity', 'status'))]
    selected.sort(key=lambda alert: (alert['created_at'] or '', alert['tenant_id'], alert['alert_id']), reverse=True)
    return _portfolio_page(call, selected, 'mssp-portfolio-alerts', complete)
