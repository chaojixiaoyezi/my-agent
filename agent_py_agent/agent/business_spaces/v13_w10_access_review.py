"""权限审查只消费原生事实；发现与回执入主存储，不直接撤销任何权限。"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from ..user_space.local_user_store import LocalUserStore
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from . import v13_w10_identity as identity


# LLM: 可编辑角色不是授权依据，所有公开动作再次检查原生管理员。
# 函数用途: 复核本次调用的管理员权限。
def _require_admin(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)


# LLM: 封闭投影避免owner及私有源字段进入HTTP响应。
# 函数用途: 返回发现的公开字段。
def _view(record):
    return {key: record[key] for key in ('finding_id', 'kind', 'provider', 'subject', 'severity',
        'status', 'evidence', 'first_seen_at', 'last_seen_at', 'acknowledged_by', 'note', 'revision')}


# LLM: 未知、非有限数和非法字符串不当作零时间。
# 函数用途: 规范化原生epoch或带时区时间戳。
def _epoch(value):
    if type(value) in (float, int):
        try:
            return float(value) if math.isfinite(value) and value > 0 else None
        except OverflowError:
            return None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return None
            stamp = parsed.timestamp()
            return stamp if math.isfinite(stamp) and stamp > 0 else None
        except (ValueError, OverflowError):
            return None
    return None


# LLM: 飞书源没有原生活动扫描器时不宣称已覆盖。
# 函数用途: 返回权限审查分类目录。
def kinds(call):
    _require_admin(call)
    return {'kinds': [
        {'kind': 'dormant_account', 'description': '启用账号已知最后活动距今至少90天', 'providers': ['local']},
        {'kind': 'stale_token', 'description': '启用令牌90天未用或已过期',
         'providers': ['api_keys', 'scim', 'inbox_tokens']},
        {'kind': 'privilege_drift', 'description': '相对上次基线的角色、权限或审计组变化', 'providers': ['local']}]}


# LLM: 不拿其他provider扫描冒充当前provider；all记录保存逐源覆盖。
# 函数用途: 找到覆盖指定提供商的最近运行记录。
def _latest(records, provider):
    eligible = [row for row in records if row.get('state') == 'completed'
        and (row.get('provider') in ('all', provider) if provider != 'all' else True)]
    return max(eligible, key=lambda row: (row.get('updated_at', ''), row.get('revision', 0), row['run_id']), default=None)


# LLM: 尚未扫描不能报告complete；确认或回收标记仍计入未解决项。
# 函数用途: 按提供商统计仍未解决的发现。
def summary(call):
    _require_admin(call)
    provider = call.params.get('provider', 'all')
    records = identity.rows(call, 'access_findings', 'main')
    latest = _latest(identity.rows(call, 'access_review_runs', 'main'), provider)
    opened = [row for row in records if row.get('status') != 'resolved'
        and (provider == 'all' or row.get('provider') == provider)]
    by_kind, by_severity = {}, {}
    for row in opened:
        by_kind[row['kind']] = by_kind.get(row['kind'], 0) + 1
        by_severity[row['severity']] = by_severity.get(row['severity'], 0) + 1
    coverage = latest['baseline'].get('coverage', {}).get(provider, 'partial') if latest else 'unknown'
    return {'provider': provider, 'last_run_at': latest.get('updated_at') if latest else None,
        'open': len(opened), 'by_kind': by_kind, 'by_severity': by_severity, 'coverage': coverage}


# LLM: 共用HMAC分页绑定动作、筛选和结果快照，不能接受自行拼的offset。
# 函数用途: 列出匹配筛选条件的发现。
def findings(call):
    _require_admin(call)
    records = identity.rows(call, 'access_findings', 'main')
    selected = [row for row in records if all(call.params.get(key) in (None, 'all', row[key])
        for key in ('provider', 'kind', 'status'))]
    selected.sort(key=lambda row: (row['last_seen_at'] or '', row['finding_id']), reverse=True)
    return identity.paged(call, [_view(row) for row in selected], 'access_review.findings')


# LLM: 只以read_only打开；SQL空值为无记录，非空坏时间必须使覆盖不完整。
# 函数用途: 查询该owner的最近任务或代理运行时间。
def _runtime_activity(home, owner_id):
    from ..runtime_db.repository import RuntimeRepository
    from ..runtime_db.schema import runtime_db_path
    path = runtime_db_path(home)
    if path.resolve() != path.absolute():
        raise ValueError('invalid runtime source')
    repository = RuntimeRepository(path, read_only=True)
    with repository.transaction() as connection:
        row = connection.execute(
            'SELECT MAX(stamp) FROM ('
            'SELECT tr.created_at AS stamp FROM task_runs tr JOIN tasks t ON t.task_id=tr.task_id '
            'WHERE t.owner_id=? UNION ALL '
            'SELECT ar.created_at AS stamp FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id '
            'JOIN tasks t ON t.task_id=tr.task_id WHERE t.owner_id=?)', (owner_id, owner_id)).fetchone()
    stamp = row[0]
    parsed = _epoch(stamp)
    return parsed, stamp is None or parsed is not None


# LLM: SessionListIndex.page会写回填；跨owner审查只读既有投影并复核canonical会话。
# 函数用途: 有界只读会话索引与原会话，返回活动和真实覆盖状态。
def _session_activity(home):
    from ..conversation.session_list_index import _stamp
    from ..session.manager import _read_session_report
    root = Path(home) / 'sessions'
    index = root.parent / f'.{root.name}-session-list.sqlite3'
    if index.resolve() != index.absolute() or root.resolve() != root.absolute():
        raise ValueError('invalid session source')
    connection = sqlite3.connect(index.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        meta = dict(connection.execute('SELECT key,value FROM meta'))
        entries = connection.execute('SELECT path FROM entries ORDER BY sort_at DESC,path DESC LIMIT 10001').fetchall()
    finally:
        connection.close()
    complete = meta.get('complete') == '1' and meta.get('directory') == _stamp(root) and len(entries) <= 10000
    latest = None
    for (relative,) in entries[:10000]:
        path = Path(relative)
        if path.is_absolute() or len(path.parts) != 2 or path.parts[-1] != 'session.json' or '..' in path.parts:
            raise ValueError('invalid session reference')
        session, error = _read_session_report(root / path, path.parts[0])
        stamp = _epoch(session.updated_at) if session is not None and error is None else None
        if stamp is None:
            complete = False
        elif latest is None or stamp > latest:
            latest = stamp
    return latest, complete


# LLM: 任意已知近期活动足以排除休眠，旧活动必须全部源完整；不回退created_at。
# 函数用途: 聚合原生运行与会话活动。
def _activity(home, owner_id, current):
    stamps, complete = [], True
    for reader in (_runtime_activity, _session_activity):
        try:
            stamp, covered = reader(home, owner_id) if reader is _runtime_activity else reader(home)
            complete = complete and covered
            if stamp is not None and stamp <= current:
                stamps.append(stamp)
            elif stamp is not None:
                complete = False
        except (OSError, ValueError, sqlite3.Error, HostAPIError):
            complete = False
    return max(stamps, default=None), complete


# LLM: 缺源与损坏均保留partial，绝不物化子账号目录。
# 函数用途: 通过W0只读口读取令牌扫描的原生业务集合。
def _source_rows(home, collection):
    from .v13_store import V13Store
    path = Path(home) / 'data' / 'business_v13' / f'{collection}.json'
    if not path.exists() or path.is_symlink():
        raise ValueError('missing token source')
    result = V13Store.read_only(home).list(collection)
    if len(result) > 10000:
        raise ValueError('token source limit')
    return result


# LLM: 沿W9的原remote_push/tokens.json读取，只消费时间和启用字段。
# 函数用途: 读取原生推送令牌元数据。
def _inbox_rows(call):
    from ..gateway_parts.remote_push_service import push_json
    raw = read_bytes_beneath(identity.root(call), ('remote_push', 'tokens.json'), max_bytes=16777216)
    if raw is None:
        raise ValueError('missing inbox source')
    rows = push_json(raw).get('tokens')
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError('invalid inbox source')
    return rows


# LLM: 散列和密文不复制到审查结果，只保存被规则需要的非秘密证据。
# 函数用途: 构造一条候选发现。
def _candidate(kind, provider, subject, evidence):
    return {'kind': kind, 'provider': provider, 'subject': subject,
        'severity': 'high' if kind == 'privilege_drift' else 'medium', 'evidence': evidence}


# LLM: 仅已追踪使用的源可从created_at推断从未使用；inbox缺失使用时间必须未知。
# 函数用途: 判断启用令牌是否过期或90天未使用。
def _token_check(row, current, *, tracks_usage=True):
    if type(row.get('enabled')) is not bool:
        return None, False
    if not row['enabled']:
        return None, True
    expiry = _epoch(row.get('expires_at'))
    if row.get('expires_at') is not None and expiry is None:
        return None, False
    if expiry is not None and expiry <= current:
        return {'reason': 'expired_enabled', 'expires_at': row['expires_at']}, True
    used = _epoch(row.get('last_used_at'))
    if row.get('last_used_at') is not None and used is None:
        return None, False
    if used is None and not tracks_usage:
        return None, False
    stamp = used if used is not None else _epoch(row.get('created_at'))
    if stamp is None or stamp > current:
        return None, False
    if current - stamp >= 90 * 86400:
        return {'reason': 'unused_90_days', 'last_used_at': row.get('last_used_at'),
            'created_at': row.get('created_at')}, True
    return None, True


# LLM: 每个集合先检查重复编号和owner错配，脏记录不参与统计、不触发旧发现自动解决。
# 函数用途: 扫描某提供商的一批真实令牌。
def _scan_tokens(provider, batches, current):
    candidates, complete, counts = [], True, {}
    key = 'key_id' if provider == 'api_keys' else 'token_id'
    for owner_id, records in batches:
        for row in records:
            if isinstance(row, dict) and isinstance(row.get(key), str):
                subject = f'{owner_id}:{row[key]}'
                counts[subject] = counts.get(subject, 0) + 1
    for owner_id, records in batches:
        for row in records:
            token_id = row.get(key) if isinstance(row, dict) else None
            actual_owner = row.get('owner_id', owner_id) if isinstance(row, dict) else None
            if not isinstance(token_id, str) or not token_id or actual_owner != owner_id:
                complete = False
                continue
            subject = f'{owner_id}:{token_id}'
            if len(subject) > 256 or counts.get(subject) != 1:
                complete = False
                continue
            evidence, known = _token_check(row, current, tracks_usage=provider != 'inbox_tokens')
            complete = complete and known
            if evidence is not None:
                candidates.append(_candidate('stale_token', provider, subject, evidence))
    return candidates, complete


# LLM: 基线只包含权限事实，不包含用户创建时间或密码信息；坏引用不构造不实权限。
# 函数用途: 收集角色权限和原生审计组快照。
def _permission_baseline(call, users):
    roles = identity.rows(call, 'rbac_roles', 'main')
    assignments = identity.rows(call, 'rbac_assignments', 'main')
    role_map = {row['role_id']: row for row in roles}
    if len(role_map) != len(roles):
        raise ValueError('duplicate role')
    result = {user.user_id: {'roles': [], 'permissions': [], 'in_audit_group': user.in_audit_group}
        for user in users}
    seen = set()
    for assignment in assignments:
        user_id = assignment['user_id']
        if user_id in seen or user_id not in result or any(role not in role_map for role in assignment['roles']):
            raise ValueError('invalid assignment')
        seen.add(user_id)
        result[user_id]['roles'] = sorted(set(assignment['roles']))
        result[user_id]['permissions'] = sorted({permission for role in assignment['roles']
            for permission in role_map[role]['permissions']})
    return result


# LLM: 扫描是有界本地读，按规则记录覆盖；未提供飞书事实源不伪造空成功。
# 函数用途: 收集本次候选发现、可解决范围及基线。
def _scan(call, previous):
    provider, current = call.params.get('provider', 'all'), call.clock()
    providers = ['local', 'feishu', 'api_keys', 'scim', 'inbox_tokens'] if provider == 'all' else [provider]
    coverage = dict.fromkeys(providers, 'complete')
    candidates, covered, blocked, snapshots, unavailable = [], set(), set(), {}, []
    users, user_source_ok = [], True
    native = LocalUserStore(identity.root(call))
    if any(name in providers for name in ('local', 'api_keys')):
        try:
            users = native.list_users()
            if len(users) > 10000:
                raise ValueError('user source limit')
        except (OSError, ValueError, HostAPIError):
            user_source_ok = False
            unavailable.append('local_users')
    if 'local' in providers:
        if user_source_ok:
            covered.add(('dormant_account', 'local'))
            for user in users:
                if user.status != 'enabled':
                    continue
                last, complete = _activity(native.owner_home(user.user_id).home_dir, f'local/{user.user_id}', current)
                if not complete or last is None:
                    coverage['local'] = 'partial'
                    unavailable.append(f'activity:{user.user_id}')
                if last is None or (not complete and current - last >= 90 * 86400):
                    blocked.add(('dormant_account', 'local', user.user_id))
                elif complete and current - last >= 90 * 86400:
                    candidates.append(_candidate('dormant_account', 'local', user.user_id,
                        {'last_activity_at': last, 'inactive_days': int((current - last) / 86400)}))
            try:
                snapshots['local'] = _permission_baseline(call, users)
                covered.add(('privilege_drift', 'local'))
                before = previous.get('local', {})
                if 'local' in previous:
                    for user_id in sorted(set(before) | set(snapshots['local'])):
                        old, new = before.get(user_id), snapshots['local'].get(user_id)
                        if old != new:
                            candidates.append(_candidate('privilege_drift', 'local', user_id,
                                {'before': old, 'after': new}))
            except (OSError, ValueError, KeyError, TypeError, HostAPIError, BusinessError):
                coverage['local'] = 'partial'
                unavailable.append('rbac_baseline')
        else:
            coverage['local'] = 'partial'
    if 'feishu' in providers:
        coverage['feishu'] = 'partial'
        unavailable.append('feishu_activity')
    for name in ('api_keys', 'scim', 'inbox_tokens'):
        if name not in providers:
            continue
        batches, complete = [], True
        if name == 'inbox_tokens':
            try:
                records = _inbox_rows(call)
                batches = [(row.get('owner_id', 'local/main'), [row]) for row in records if isinstance(row, dict)]
                complete = len(batches) == len(records)
            except (OSError, ValueError, HostAPIError):
                complete = False
        else:
            homes = [('local/main', resolve_owner_home(native.root, OwnerIdentity.local_main()).home_dir)]
            if name == 'api_keys':
                complete = user_source_ok
                homes += [(f'local/{user.user_id}', native.owner_home(user.user_id).home_dir) for user in users]
            for owner_id, home in homes:
                try:
                    if name == 'scim':
                        if not (home / 'data' / 'business_v13' / 'scim_tokens.json').exists():
                            raise ValueError('missing scim source')
                        batches.append((owner_id, identity.rows(call, 'scim_tokens', 'main')))
                    else:
                        batches.append((owner_id, _source_rows(home, 'api_keys')))
                except (OSError, ValueError, HostAPIError, BusinessError):
                    complete = False
        found, checked = _scan_tokens(name, batches, current)
        candidates.extend(found)
        if complete and checked:
            covered.add(('stale_token', name))
        else:
            coverage[name] = 'partial'
            unavailable.append(name)
    coverage[provider] = 'complete' if all(value == 'complete' for value in coverage.values()) else 'partial'
    return candidates, covered, blocked, {'snapshots': snapshots, 'coverage': coverage,
        'unavailable': sorted(set(unavailable))}


# LLM: 编号只绑定发现事实，不绑定扫描次数或展示顺序。
# 函数用途: 为候选生成稳定的发现编号。
def _finding_id(candidate):
    triple = (candidate['kind'], candidate['provider'], candidate['subject'])
    return 'finding-' + hashlib.sha256('\0'.join(triple).encode()).hexdigest()[:40]


# LLM: 三元组稳定编号避免重复扫描增殖；确认状态保留，重新出现才从resolved重开。
# 函数用途: 在发现集合短事务内更新候选并解决已确证消失的发现。
def _upsert(call, records, candidates, covered, blocked, stamp):
    observed, created, resolved = set(), 0, 0
    for candidate in candidates:
        triple = (candidate['kind'], candidate['provider'], candidate['subject'])
        observed.add(triple)
        finding_id = _finding_id(candidate)
        row = records.get(finding_id)
        if row is None:
            row = {**candidate, 'finding_id': finding_id, 'status': 'open', 'first_seen_at': stamp,
                'acknowledged_by': None, 'note': None, 'revision': 0, 'created_at': stamp,
                'owner_id': 'local/main'}
            records[finding_id] = row
            created += 1
        elif row['status'] == 'resolved':
            row.update(status='open', acknowledged_by=None, note=None)
        row.update(candidate, last_seen_at=stamp, updated_at=stamp, revision=row['revision'] + 1)
    for row in records.values():
        triple = (row['kind'], row['provider'], row['subject'])
        if triple[:2] in covered and triple not in observed and triple not in blocked and row['status'] != 'resolved':
            row.update(status='resolved', updated_at=stamp, revision=row['revision'] + 1)
            resolved += 1
    return {'findings_created': created, 'findings_resolved': resolved}


# LLM: 扫描意图与待应用事实同笔保存；小扫描仍只提交一笔，不能先发布空running记录。
# 函数用途: 在存储锁内建立扫描快照并选择原子或分批路径。
def _prepare_run(call, collections, run_id, intent, stamp):
    provider = call.params.get('provider', 'all')
    records = collections['access_review_runs']
    if any(row.get('state') == 'running' for row in records.values()):
        raise HostAPIError('STATE_CONFLICT', 409)
    latest = _latest(list(records.values()), 'local')
    previous = latest['baseline'].get('snapshots', {}) if latest else {}
    candidates, covered, blocked, baseline = _scan(call, previous)
    for name, snapshot in previous.items():
        baseline['snapshots'].setdefault(name, snapshot)
    findings = collections['access_findings']
    proposed = deepcopy(findings)
    counts = _upsert(call, proposed, candidates, covered, blocked, stamp)
    changed = [key for key in proposed if proposed[key] != findings.get(key)]
    row = {'run_id': run_id, 'owner_id': 'local/main', 'provider': provider,
        'state': 'running', 'baseline': baseline, 'findings_created': 0, 'findings_resolved': 0,
        'revision': max((record.get('revision', 0) for record in records.values()), default=0),
        'created_at': stamp, 'updated_at': stamp}
    records[run_id] = row
    progress = {'intent': intent, 'cursor': 0, 'last_step': 0}
    baseline['progress'] = progress
    if len(changed) <= 100:
        findings.update(proposed)
        row.update(counts)
        progress['operations'] = []
        return row
    # LLM: 候选先于解决项，且恢复只应用冻结事实；不复制整行覆盖期间的确认状态。
    # 函数用途: 保存分批操作和已完成位置，保留尚未处理发现的原状态。
    progress['operations'] = [{'candidate': candidate} for candidate in candidates]
    progress['operations'].extend({'resolve': key} for key in changed
        if proposed[key]['status'] == 'resolved')
    return row


# LLM: 每笔至多100条发现加一条run；游标与计数同笔推进，不会重算已提交批次。
# 函数用途: 应用一个耐久批次并仅在最后一笔生成完成回执。
def _advance_run(call, collections, row):
    progress = row['baseline']['progress']
    start = progress['cursor']
    operations = progress['operations']
    batch = operations[start:start + 100]
    records = collections['access_findings']
    candidates = [operation['candidate'] for operation in batch if 'candidate' in operation]
    selected = {_finding_id(candidate): records[_finding_id(candidate)] for candidate in candidates
        if _finding_id(candidate) in records}
    counts = _upsert(call, selected, candidates, set(), set(), row['created_at']) if batch else {
        'findings_created': 0, 'findings_resolved': 0}
    records.update(selected)
    for operation in batch:
        if 'resolve' in operation:
            finding = records.get(operation['resolve'])
            if finding is not None and finding['status'] != 'resolved':
                finding.update(status='resolved', updated_at=row['created_at'], revision=finding['revision'] + 1)
                counts['findings_resolved'] += 1
    for name, count in counts.items():
        row[name] += count
    progress.update(cursor=start + len(batch), last_step=start)
    row.update(revision=row['revision'] + 1, updated_at=identity.now(call))
    if progress['cursor'] < len(operations):
        return {'pending': row['run_id']}
    row['state'] = 'completed'
    progress.pop('operations')
    result = {'run': {'run_id': row['run_id'], 'state': 'completed', 'provider': row['provider'],
        'started_at': row['created_at'], 'finished_at': row['updated_at'],
        'findings_created': row['findings_created'], 'findings_resolved': row['findings_resolved'],
        'coverage': row['baseline']['coverage'][row['provider']]}}
    progress['result'] = result
    return result


# LLM: 两集合与回执沿W0同一quota、全局锁和提交日志；每次释放锁后按耐久游标继续。
# 函数用途: 同步执行审查，小扫描原子提交，大扫描可中断恢复。
def run(call):
    _require_admin(call)
    run_id = 'review-' + hashlib.sha256((call.owner_id + '\0' + call.subject_id + '\0'
        + call.idempotency_key).encode()).hexdigest()[:40]
    intent = hashlib.sha256(json.dumps([call.action, call.owner_id, call.subject_id, call.params,
        call.expected_revision], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    while True:
        existing = identity.one(call, 'access_review_runs', run_id, 'main')
        progress = existing['baseline'].get('progress', {}) if existing else {}
        if progress and progress['intent'] != intent:
            raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
        step = progress.get('last_step', 0) if existing and existing['state'] == 'completed' else progress.get('cursor', 0)
        # 旧版完成记录没有progress，仍从原W0回执重放，不迁移或重算过去的发现。
        step_call = call if existing and not progress else replace(call, idempotency_key=hashlib.sha256(json.dumps(
            ['review-step', run_id, call.idempotency_key, step], separators=(',', ':')).encode()).hexdigest())

        # LLM: 最新基线、计划检查和发现变更在同一锁内，不嵌套另一个写事务。
        # 函数用途: 原子应用本批事实或重放完成结果。
        def change(collections):
            row = collections['access_review_runs'].get(run_id)
            if row is None:
                row = _prepare_run(call, collections, run_id, intent, identity.now(call))
            if 'progress' not in row['baseline']:
                raise HostAPIError('STATE_CONFLICT', 409)
            if row['baseline']['progress']['intent'] != intent:
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            if row['state'] == 'completed':
                return row['baseline']['progress']['result']
            return _advance_run(call, collections, row)

        result = identity.write_many(step_call, ('access_findings', 'access_review_runs'), change, scope='main')
        if 'run' in result:
            return result


# LLM: 标记不触碰源账号或令牌，实时身份及CAS都在共用事务内复核。
# 函数用途: 确认发现或标记需要回收的权限。
def acknowledge(call):
    _require_admin(call)

    # LLM: owner错配与不存在统一404，避免通过编号跨范围探测。
    # 函数用途: 校验修订并只更新发现本身。
    def change(records):
        row = records.get(call.params['finding_id'])
        if row is None or row.get('owner_id') != 'local/main':
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        identity.cas(call, row)
        if row['status'] != 'resolved':
            row['status'] = 'revocation_marked' if call.params.get('decision') == 'mark_revocation' else 'acknowledged'
        row.update(acknowledged_by=call.subject_id, note=call.params.get('note'),
            revision=row['revision'] + 1, updated_at=identity.now(call))
        return {'finding': _view(row)}

    return identity.write(call, 'access_findings', change, scope='main')
