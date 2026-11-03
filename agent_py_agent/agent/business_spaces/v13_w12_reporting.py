"""W12 周报、共享审计投影和保存视图；复用原身份、事务与产物登记。

周报只读指标与用量；审计只经 F8 读取，导出不新建审计或幂等账本。
保存视图以 owner 为租户、created_by 为写权限边界，CAS 使用 F2 集合修订。
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import html
import json
import re
import secrets
import unicodedata
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import BytesIO, StringIO
from typing import TYPE_CHECKING

from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json
from .v13_audit import audit_records
from .v13_store import v13_store_for

if TYPE_CHECKING:
    from ..gateway_parts.business_api_v13 import V13Call


# LLM: 时间只来自本次注入时钟，所有可见时间统一为 UTC。
# 函数用途: 格式化 epoch 时间。
def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


# LLM: JSON 副本避免调用方修改返回值反向改变事务记录。
# 函数用途: 复制纯 JSON 数据。
def _copy(value):
    return json.loads(canonical_json(value))


# LLM: 摘要绑定动作、原始参数与 CAS，不把请求号混入同笔身份。
# 函数用途: 计算业务幂等指纹。
def _params_digest(call):
    return hashlib.sha256(canonical_json(
        [call.action, call.params, call.expected_revision]).encode()).hexdigest()


# LLM: 名称规范化仅用于同主体同 surface 的冲突判断，不跨租户复用。
# 函数用途: 验证并规范化视图名称。
def _name(value):
    normalized = unicodedata.normalize('NFKC', value).strip()
    if not normalized or len(normalized) > 120:
        raise HostAPIError('INVALID_SCHEMA')
    return normalized


# LLM: 对外修订是集合级 CAS 令牌；记录内部修订不冒充当前集合版本。
# 函数用途: 投影 SavedView，去掉私有 owner 字段。
def _view(row, revision):
    result = {key: _copy(row[key]) for key in (
        'view_id', 'surface', 'name', 'filters', 'sort', 'shared', 'is_default',
        'created_by', 'created_at', 'updated_at')}
    result['revision'] = revision
    return result


# LLM: 无锁读取前后复核集合版本，不能给旧内容配新 CAS 令牌。
# 函数用途: 取得一致的保存视图快照和集合修订。
def _views_snapshot(store):
    for attempt in range(3):
        revision = store.revision('saved_views')
        rows = store.list('saved_views')
        if store.revision('saved_views') == revision:
            return rows, revision
    raise HostAPIError('HOST_NOT_READY', 503)


# LLM: 可见性不能授权写入；shared 只影响当前 owner 内的读取。
# 函数用途: 列出本人的视图和共享视图。
def views_list(call: V13Call) -> dict:
    from ..gateway_parts.business_api_v13 import v13_require_access

    rows, revision = _views_snapshot(v13_store_for(call))
    rows = [row for row in rows
        if row.get('owner_id') == call.owner_id and row['surface'] == call.params['surface']
        and (row['created_by'] == call.subject_id or row['shared'])]
    rows.sort(key=lambda row: (not row['is_default'], row['name'], row['view_id']))
    if len(rows) > 200:
        raise HostAPIError('LIMIT_EXCEEDED')
    v13_require_access(call)
    return {'items': [_view(row, revision) for row in rows]}


# LLM: 冲突与唯一默认值必须在同一 F2 锁内完成，不能锁外读取后整条覆盖。
# 函数用途: 检查重名并清除同创建者其它默认视图。
def _prepare_view(rows, candidate, at, revision):
    for row in rows.values():
        if (row['view_id'] == candidate['view_id'] or row.get('owner_id') != candidate['owner_id']
                or row['created_by'] != candidate['created_by'] or row['surface'] != candidate['surface']):
            continue
        if _name(row['name']).casefold() == candidate['name'].casefold():
            raise HostAPIError('STATE_CONFLICT', 409)
    if candidate['is_default']:
        for row in rows.values():
            if (row['view_id'] != candidate['view_id'] and row.get('owner_id') == candidate['owner_id']
                    and row['created_by'] == candidate['created_by'] and row['surface'] == candidate['surface']
                    and row['is_default']):
                row.update(is_default=False, revision=revision, updated_at=at)


# LLM: 新建和清默认值同笔提交，回执保留第一次结果而不重复生成 ID。
# 函数用途: 创建当前主体的保存视图。
def views_create(call: V13Call) -> dict:
    store = v13_store_for(call)

    # LLM: 只在原存储锁内取得当前集合修订，避免锁外快照覆盖。
    # 函数用途: 校验重名、生成视图并在同一事务维护默认值。
    def mutate(rows):
        at, revision = _timestamp(call.clock()), store.revision('saved_views') + 1
        if sum(row.get('owner_id') == call.owner_id and row['surface'] == call.params['surface']
               for row in rows.values()) >= 200:
            raise HostAPIError('LIMIT_EXCEEDED')
        row = {'view_id': 'view-' + uuid.uuid4().hex, 'owner_id': call.owner_id,
            'surface': call.params['surface'], 'name': _name(call.params['name']),
            'filters': _copy(call.params['filters']), 'sort': _copy(call.params.get('sort')),
            'shared': call.params.get('shared', False), 'is_default': call.params.get('is_default', False),
            'created_by': call.subject_id, 'revision': revision, 'created_at': at, 'updated_at': at}
        _prepare_view(rows, row, at, revision)
        rows[row['view_id']] = row
        return {'view': _view(row, revision)}

    return store.transaction('saved_views', mutate, idempotency_key=call.idempotency_key,
        params_digest=_params_digest(call))


# LLM: shared 不扩大写权限；同 owner 的其它创建者也只能看到 404。
# 函数用途: 在事务内取得当前主体可写的视图。
def _owned_view(rows, call):
    row = rows.get(call.params['view_id'])
    if row is None or row.get('owner_id') != call.owner_id or row.get('created_by') != call.subject_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return row


# LLM: 在 F2 锁内先核归属再核集合 CAS，防止修订错误泄露其它主体的记录存在性。
# 函数用途: 校验显式修订号并返回下一集合修订。
def _view_revision(store, call):
    revision = store.revision('saved_views')
    if type(call.expected_revision) is not int or call.expected_revision < 0:
        raise HostAPIError('INVALID_SCHEMA')
    if revision != call.expected_revision:
        raise HostAPIError('REVISION_CONFLICT', 409)
    return revision + 1


# LLM: 回执由 F2 先处理；CAS 留在持锁 mutate 内，以保留删除后重放与跨主体 404。
# 函数用途: 修改当前主体的保存视图。
def views_update(call: V13Call) -> dict:
    store = v13_store_for(call)

    # LLM: 从锁内最新记录合并显式字段，不覆盖并发新增的无关字段。
    # 函数用途: 更新视图及同主体的默认值。
    def mutate(rows):
        row = _owned_view(rows, call)
        at, revision = _timestamp(call.clock()), _view_revision(store, call)
        candidate = _copy(row)
        candidate.update({key: _copy(value) for key, value in call.params.items() if key != 'view_id'})
        candidate['name'] = _name(candidate['name'])
        _prepare_view(rows, candidate, at, revision)
        candidate.update(revision=revision, updated_at=at)
        rows[candidate['view_id']] = candidate
        return {'view': _view(candidate, revision)}

    return store.transaction('saved_views', mutate, idempotency_key=call.idempotency_key,
        params_digest=_params_digest(call))


# LLM: 删除重放必须先让 F2 查回执；未知和其它主体记录都在 mutate 内返回 404。
# 函数用途: 用集合 CAS 删除当前主体的保存视图。
def views_delete(call: V13Call) -> dict:
    store = v13_store_for(call)

    # LLM: 删除只动当前主体的目标，不把其它人的共享视图当可写资源。
    # 函数用途: 在锁内验证归属、修订并移除视图。
    def mutate(rows):
        _owned_view(rows, call)
        _view_revision(store, call)
        del rows[call.params['view_id']]
        return {'view_id': call.params['view_id'], 'deleted': True}

    return store.transaction('saved_views', mutate, idempotency_key=call.idempotency_key,
        params_digest=_params_digest(call))


# LLM: 临时游标签名不是授权或业务状态；进程重启后明确过期，与 F8 相同。
# 函数用途: 取得本进程的审计过滤游标密钥。
@lru_cache(maxsize=1)
def _audit_cursor_key():
    return secrets.token_bytes(32)


# LLM: actor、时间、前缀、范围与主体都参与绑定，页长不影响已消费位置。
# 函数用途: 生成本次审计查询的范围摘要。
def _audit_binding(call):
    values = [str(call.owner_home), call.owner_id, call.subject_id,
        call.params.get('scope', 'owner'), call.params.get('since'), call.params.get('until'),
        call.params.get('actor'), call.params.get('action_prefix')]
    return hashlib.sha256(canonical_json(values).encode()).hexdigest()


# LLM: 包装位置仅指向 F8 的原签名游标；不得携带截断页或自行排序的影子记录。
# 函数用途: 签署前缀过滤的继续位置。
def _audit_cursor(call, raw_cursor):
    if raw_cursor is None:
        return None
    raw = canonical_json([_audit_binding(call), raw_cursor]).encode()
    token = base64.urlsafe_b64encode(raw + hmac.digest(_audit_cursor_key(), raw, 'sha256')).decode()
    if len(token) > 2048:
        raise HostAPIError('HOST_NOT_READY', 503)
    return token


# LLM: 拒绝修改条件或篡改游标，不悄悄回退第一页。
# 函数用途: 验证过滤绑定并恢复 F8 的不透明位置。
def _audit_position(call):
    cursor = call.params.get('cursor')
    if cursor is None:
        return None
    try:
        if type(cursor) is not str or not 1 <= len(cursor) <= 2048:
            raise ValueError('cursor')
        value = base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True)
        raw, signature = value[:-32], value[-32:]
        if not hmac.compare_digest(signature, hmac.digest(_audit_cursor_key(), raw, 'sha256')):
            raise ValueError('signature')
        binding, position = json.loads(raw)
        if binding != _audit_binding(call) or type(position) is not str or not position:
            raise ValueError('binding')
        return position
    except (ValueError, TypeError, UnicodeError):
        raise HostAPIError('CURSOR_EXPIRED', 503) from None


# LLM: host 读取只接受原生 admin 决策；业务角色、shared 或请求字段均不能扩大范围。
# 函数用途: 核验本次审计读取权限。
def _audit_access(call):
    from ..gateway_parts.business_api_v13 import v13_require_access, v13_require_admin

    v13_require_access(call)
    if call.params.get('scope', 'owner') == 'host':
        try:
            v13_require_admin(call)
        except HostAPIError as error:
            if error.code == 'CAPABILITY_DENIED':
                raise HostAPIError('BUSINESS_ACCESS_DENIED', 403) from None
            raise


# LLM: F8 是唯一数据来源，真实 host 与 admin_context 只提供权限与来源位置。
# 函数用途: 从共享审计读口取一页完整记录。
def _audit_page(call, cursor, limit):
    return audit_records(call.owner_home, call.owner_id, scope=call.params.get('scope', 'owner'),
        since=call.params.get('since'), until=call.params.get('until'), actor=call.params.get('actor'),
        cursor=cursor, limit=limit, admin_context=call, host=call.context.host)


# LLM: F8 的来源对象必须投影到冻结字符串数组，保留失败依据而不回显任意正文。
# 函数用途: 合并审计来源不可用信息。
def _audit_unavailable(items):
    values = set()
    for item in items:
        text = item if type(item) is str else str(item['source']) + ':' + str(item['reason_code'])
        values.add(text[:128])
    return sorted(values)[:20]


# LLM: 稀疏过滤按百条原页推进；若匹配溢出，只重读到最后已交付匹配的原位置，不丢页尾记录。
# 函数用途: 有界扫描前缀过滤后的审计页。
def audit_list(call: V13Call) -> dict:
    from ..gateway_parts.business_api_v13 import v13_require_access

    _audit_access(call)
    cursor, rows, scanned, unavailable = _audit_position(call), [], 0, []
    limit = call.params.get('limit', 50)
    prefix, seen, unknown = call.params.get('action_prefix'), set(), False
    while len(rows) < limit and scanned < 1000:
        page = _audit_page(call, cursor, min(100, 1000 - scanned))
        records = page['records']
        matched = [row for row in records if prefix is None or row['action'].startswith(prefix)]
        if len(matched) > limit - len(rows):
            boundary = records.index(matched[limit - len(rows) - 1]) + 1
            narrowed = _audit_page(call, cursor, boundary)
            if narrowed['records'] != records[:boundary]:
                raise HostAPIError('CURSOR_EXPIRED', 503)
            page, records = narrowed, narrowed['records']
        unavailable.extend(page['unavailable'])
        unknown = unknown or page['coverage'] in ('unknown', 'expired')
        rows.extend(row for row in records if prefix is None or row['action'].startswith(prefix))
        scanned += len(records)
        next_cursor = page['next_cursor']
        if next_cursor is not None and (not records or next_cursor == cursor or next_cursor in seen):
            raise HostAPIError('HOST_NOT_READY', 503)
        seen.add(cursor)
        cursor = next_cursor
        if cursor is None:
            break
    unavailable = _audit_unavailable(unavailable)
    v13_require_access(call)
    return {'records': rows, 'next_cursor': _audit_cursor(call, cursor), 'unavailable': unavailable,
        'coverage': 'unknown' if unknown else ('partial' if unavailable or cursor else 'complete')}


# LLM: 导出不把一页当全部，超出硬上限或来源覆盖不足时不发表合规存档。
# 函数用途: 取完 F8 全部分页，最多接受一万条记录。
def _export_records(call):
    rows, cursor, seen = [], None, set()
    while True:
        page = _audit_page(call, cursor, min(100, 10001 - len(rows)))
        if page['unavailable'] or page['coverage'] in ('unknown', 'expired'):
            raise HostAPIError('HOST_NOT_READY', 503)
        rows.extend(page['records'])
        if len(rows) > 10000:
            raise HostAPIError('LIMIT_EXCEEDED')
        next_cursor = page['next_cursor']
        if next_cursor is None:
            return rows
        if not page['records'] or next_cursor == cursor or next_cursor in seen:
            raise HostAPIError('HOST_NOT_READY', 503)
        seen.add(cursor)
        cursor = next_cursor


# LLM: HTML 中所有动态键和值都必须转义，输出只含内联 CSS 和静态标签，无脚本。
# 函数用途: 把 JSON 值递归渲染成可打印的安全 HTML。
def _html_value(value):
    if isinstance(value, dict):
        rows = ''.join('<tr><th>' + html.escape(str(key), quote=True) + '</th><td>'
            + _html_value(item) + '</td></tr>' for key, item in value.items())
        return '<table>' + rows + '</table>'
    if isinstance(value, list):
        return '<ol>' + ''.join('<li>' + _html_value(item) + '</li>' for item in value) + '</ol>'
    return html.escape('未知' if value is None else str(value), quote=True)


# LLM: 可打印文档只有静态样式；标题与全部内容使用同一转义入口。
# 函数用途: 封装独立 HTML 文档。
def _html_document(title, content):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>'
        + html.escape(title, quote=True) + '</title><style>'
        'body{font:14px sans-serif;margin:2rem;color:#17202a}table{border-collapse:collapse;width:100%}'
        'th,td{border:1px solid #ccd;padding:.4rem;vertical-align:top;text-align:left}'
        '@media print{body{margin:0}tr{break-inside:avoid}}'
        '</style></head><body><h1>' + html.escape(title, quote=True) + '</h1>'
        + content + '</body></html>')


# LLM: 公式前缀也可能藏在空白后，使用原始文本检查并添加单引号，而非改变审计正文。
# 函数用途: 防止 CSV 单元格被电子表格当作公式执行。
def _csv_cell(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text


# LLM: RFC4180 引号由 csv 模块负责，HTML 与 CSV 共用完全相同的六列。
# 函数用途: 生成审计导出的 UTF-8 字节。
def _export_content(rows, format_name):
    columns = ('at', 'actor', 'action', 'outcome', 'source', 'record_id')
    if format_name == 'csv':
        output = StringIO(newline='')
        writer = csv.writer(output, lineterminator='\r\n')
        writer.writerow(columns)
        writer.writerows([_csv_cell(row[key]) for key in columns] for row in rows)
        return output.getvalue().encode('utf-8')
    header = '<tr>' + ''.join('<th>' + html.escape(key, quote=True) + '</th>' for key in columns) + '</tr>'
    body = ''.join('<tr>' + ''.join('<td>' + html.escape(str(row[key] or ''), quote=True)
        + '</td>' for key in columns) + '</tr>' for row in rows)
    return _html_document('审计日志', '<table>' + header + body + '</table>').encode('utf-8')


# LLM: 默认空间只能选原 W1 当前可见记录，不凭空创建空间或接受任意 ID。
# 函数用途: 解析导出使用的真实业务空间。
def _export_space(call):
    from ..gateway_parts.business_api_v13 import v13_call_frozen, v13_resolve_space

    selected = call.params.get('space_id')
    if selected is None:
        listed = v13_call_frozen(call, 'spaces.list', {'limit': 2, 'state': 'active'})
        if len(listed['items']) != 1 or listed['next_cursor'] is not None:
            raise HostAPIError('INVALID_SCHEMA')
        selected = listed['items'][0]['space_id']
    return v13_resolve_space(call, selected)['space_id']


# LLM: 原 upload_artifact 按 owner/subject/key 生成稳定 ID；命名空间隔离其它媒体操作。
# 函数用途: 生成原上传口所需的稳定键与产物编号。
def _export_identity(call):
    key = 'audit-export-' + hashlib.sha256(canonical_json(
        [call.action, call.idempotency_key]).encode()).hexdigest()
    identity = hashlib.sha256(canonical_json([call.owner_id, call.subject_id, key]).encode()).hexdigest()
    return key, 'artifact-' + identity[:40]


# LLM: 首次上传仅刷新轻量服务发现；规范观察口的完整缺记录与资源不存在均代表首次导出，拒绝或未知源仍透传。
# 函数用途: 通过冻结 artifacts.get 查询真实登记，缺记录返回空值。
def _export_artifact(call, artifact_id):
    from ..gateway_parts.business_api_v13 import v13_call_frozen

    if not call.services.ready('artifacts.get'):
        from ..gateway_parts.business_api import BusinessServices
        call = replace(call, services=BusinessServices(call.services.host))
    if not call.services.ready('artifacts.get'):
        return None
    try:
        result = v13_call_frozen(call, 'artifacts.get', {'artifact_id': artifact_id})
    except (HostAPIError, BusinessError) as error:
        if error.code in {'RESOURCE_NOT_FOUND', 'OBSERVATION_NOT_FOUND'}:
            return None
        raise
    if not result['available']:
        raise HostAPIError('HOST_NOT_READY', 503)
    return result['artifact']


# LLM: 原登记的不可变名称绑定参数摘要和行数，重放直接读原产物，不重新取变化中的审计快照。
# 函数用途: 核验原登记的参数身份并生成闭合导出结果。
def _export_result(call, artifact):
    from ..gateway_parts.business_api_v13 import v13_resolve_space
    from ..gateway_parts.business_artifacts_http import artifact_content

    match = re.fullmatch(r'audit-([0-9a-f]{64})-([0-9]+)\.(csv|html)', artifact['name'])
    if (match is None or match[1] != _params_digest(call) or match[3] != call.params['format']
            or int(match[2]) > 10000):
        raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
    v13_resolve_space(call, artifact['space_id'])
    status, _headers, content = artifact_content(artifact['artifact_id'], artifact['version_ref'],
        call.context, call.services)
    if (status != 200 or len(content) != artifact['size_bytes']
            or hashlib.sha256(content).hexdigest() != artifact['content_sha256']):
        raise HostAPIError('HOST_NOT_READY', 503)
    result = {key: artifact[key] for key in (
        'artifact_id', 'version_ref', 'name', 'mime_type', 'size_bytes', 'content_sha256')}
    result['format'] = call.params['format']
    return {'export': result, 'rows': int(match[2])}


# LLM: 原上传口是唯一产物与回执权威；同键竞争也只可能登记同一个 ID，不另建导出账。
# 函数用途: 将完整审计快照导出为真实 CSV 或 HTML 产物。
def audit_export(call: V13Call) -> dict:
    from ..gateway_parts.business_api_v13 import v13_require_access
    from ..gateway_parts.business_artifacts_http import upload_artifact

    _audit_access(call)
    key, artifact_id = _export_identity(call)
    previous = _export_artifact(call, artifact_id)
    if previous is not None:
        return _export_result(call, previous)
    space_id = _export_space(call)
    rows = _export_records(call)
    content = _export_content(rows, call.params['format'])
    filename = 'audit-' + _params_digest(call) + '-' + str(len(rows)) + '.' + call.params['format']
    v13_require_access(call)
    try:
        uploaded = upload_artifact(BytesIO(content), {'space_id': space_id, 'idempotency_key': key},
            filename, call.context, call.services)
    except HostAPIError as error:
        if error.code != 'IDEMPOTENCY_CONFLICT':
            raise
        # 同参数并发的另一个请求可能先发表了更早的审计快照；只能读回同一 ID。
        uploaded = _export_artifact(call, artifact_id)
        if uploaded is None:
            raise
    _export_result(call, uploaded)
    artifact = _export_artifact(call, artifact_id)
    if artifact is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    return _export_result(call, artifact)


# LLM: 默认前一完整 UTC 周一周，不把滚动七天或当前未完成周伪装周报。
# 函数用途: 解析周报的左闭右开时间范围。
def _weekly_period(call):
    if 'week_start' in call.params:
        try:
            start = datetime.strptime(call.params['week_start'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise HostAPIError('INVALID_SCHEMA') from None
    else:
        today = datetime.fromtimestamp(call.clock(), timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=today.weekday() + 7)
    return start.timestamp(), (start + timedelta(days=7)).timestamp()


# LLM: 指标动作保留本次 reports 权限和相同空间，只替换纯窗口参数和注入时钟。
# 函数用途: 调用指标模块的相同 KPI 与排名实现。
def _weekly_metrics(call, end):
    from .v13_w12_metrics import alert_stats, dashboard

    params = {'window': '7d'}
    if call.params.get('space_id') is not None:
        params['space_id'] = call.params['space_id']
    selected = replace(call, params=params, clock=lambda: end)
    return dashboard(selected), alert_stats(selected)


# LLM: 案件也复用指标模块的 owner/space、脏记录和样本排除，不按展示文本挑选。
# 函数用途: 取得本周按严重度和时间排序的案件摘要。
def _notable_cases(call, start, end):
    from .v13_w12_metrics import load_soc_records

    selected = []
    severity = {'info': 0, 'low': 1, 'medium': 2, 'high': 3, 'critical': 4}
    for row in load_soc_records(call, 'soc_cases'):
        created = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')).timestamp()
        if start <= created < end:
            selected.append((severity[row['severity']], created, row))
    selected.sort(key=lambda item: (-item[0], -item[1], item[2]['case_id']))
    return [{key: row[key] for key in ('case_id', 'title', 'severity', 'status')}
        for _severity, _created, row in selected[:10]]


# LLM: 缺用量来源返回 null 与 unknown；真实来源失败不被吞成零用量。
# 函数用途: 经冻结 usage.get 读取本周真实用量。
def _weekly_usage(call, start, end):
    from ..gateway_parts.business_api_v13 import v13_call_frozen

    if not call.services.ready('usage.get'):
        return None
    selected = {'since': _timestamp(start), 'until': _timestamp(end)}
    if call.params.get('space_id') is not None:
        selected['space_id'] = call.params['space_id']
    return v13_call_frozen(call, 'usage.get', {'filter': selected})['actual']


# LLM: 前后两周与 dashboard 共用 KPI 定义；HTML 只渲染已返回的 JSON 值，不执行模型。
# 函数用途: 生成周报 JSON 与可打印、全转义的 HTML。
def weekly_digest(call: V13Call) -> dict:
    from ..gateway_parts.business_api_v13 import v13_require_access

    v13_require_access(call)
    start, end = _weekly_period(call)
    current, stats = _weekly_metrics(call, end)
    previous, _previous_stats = _weekly_metrics(call, start)
    usage = _weekly_usage(call, start, end)
    unknown = []
    if usage is None:
        unknown.append('usage')
    elif usage['cost'] is None:
        unknown.append('cost')
    if current['coverage'] != 'complete' or previous['coverage'] != 'complete' or stats['coverage'] != 'complete':
        unknown.append('soc_records')
    if current['kpis']['sla_breaches'] is None:
        unknown.append('sla_breaches')
    if previous['kpis']['sla_breaches'] is None:
        unknown.append('previous_sla_breaches')
    digest = {'kpis': current['kpis'], 'previous_kpis': previous['kpis'],
        'top_rules': current['top_rules'], 'top_entities': stats['top_entities'],
        'notable_cases': _notable_cases(call, start, end), 'usage': usage, 'unknown': unknown}
    period = {'start': _timestamp(start), 'end': _timestamp(end)}
    rendered = _html_document('安全运营周报', _html_value({'period': period, 'digest': digest})) \
        if call.params.get('format', 'json') == 'html' else None
    if rendered is not None and len(rendered) > 500000:
        raise HostAPIError('LIMIT_EXCEEDED')
    v13_require_access(call)
    return {'period': period, 'digest': digest, 'html': rendered}
