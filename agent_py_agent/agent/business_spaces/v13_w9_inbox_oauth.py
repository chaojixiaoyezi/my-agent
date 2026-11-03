"""W9 收件箱令牌及 OAuth；秘密沿原 Vault，推送配置沿原全局写门。"""
from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import secrets
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..common.global_write_guard import require_global_writer
from ..common.json_io import locked_json_path
from ..gateway_parts.remote_push_service import persist_push_json, read_push_json
from ..plugin_host.errors import HostAPIError
from ..settings.secret_ref import is_secret_ref, resolve_secret_ref
from .v13_w9_datasources import (
    fleet_health,
    w9_datasource,
    w9_datasource_store,
    w9_get,
    w9_now,
    w9_push_entries,
    w9_require_space,
    w9_write,
)


# LLM: 日期只投影已存 epoch，缺测值保留为空。
# 函数用途: 转换原生推送的时间格式。
def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


# LLM: 头名与原 authenticate_push 相同，不返回真实密钥。
# 函数用途: 构造原生推送签名说明。
def _signing():
    return {'endpoint': '/remote-agent/push',
            'headers': ['X-Agent-Push-Token', 'X-Agent-Timestamp', 'X-Agent-Nonce', 'X-Agent-Signature'],
            'signature': 'HMAC-SHA256(secret, timestamp + "." + nonce + "." + SHA256(raw_body)).hexdigest()'}


# LLM: 根仅来自实时宿主，不从请求或默认家目录推导。
# 函数用途: 定位原推送配置。
def _push_root(call):
    root = getattr(call.context.host.home_paths, 'root', None)
    if root is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    return root / 'remote_push'


# LLM: 多 owner ACL 的旧令牌不隐式缩成单 owner 令牌。
# 函数用途: 判断原生令牌严格归属。
def _owned_token(call, row):
    return (isinstance(row, dict) and row.get('allowed_target_owners') == [call.owner_id]
            and row.get('allowed_actions') == ['push_event'])


# LLM: 只消费共用读取器已核对的原生来源关联，不假定原 receipt 自带 token_id。
# 函数用途: 将令牌投影为冻结公共 DTO。
def _token_view(call, row):
    stamps = [entry['receipt']['queued_at'] for entry in w9_push_entries(call)
              if entry.get('token_id') == row['token_id']]
    return {'fingerprint': row['token_sha256'][:16], 'token_id': row['token_id'],
            'label': row.get('label', row['token_id']), 'template_id': row.get('template_id'),
            'datasource_id': row.get('datasource_id'), 'enabled': row['enabled'],
            'expires_at': _timestamp(row.get('expires_at')), 'created_at': row.get('created_at'),
            'last_used_at': _timestamp(max(stamps)) if stamps else None}


# LLM: 首次响应之外的所有回执不含 secret 值。
# 函数用途: 裁剪 secret-once 回执。
def _secret_receipt(result):
    return {**result, 'secret': '<redacted>'} if 'secret' in result else result


# LLM: native 令牌与防重复副作用回执同锁提交；W0 外层事务提供审计和公开幂等回执。
# 函数用途: 在原 tokens.json 锁和 global writer 门内提交一次令牌变更。
def _token_transaction(call, mutate):
    require_global_writer('remote_push_tokens')

    # LLM: 原生回执只存摘要和脱敏结果，覆盖 native 成功但外层回执未落盘的恢复窗口。
    # 函数用途: 在实时 owner 授权后执行原生原子事务。
    def commit(records):
        root = _push_root(call)
        key = hashlib.sha256(json.dumps([call.owner_id, call.idempotency_key]).encode()).hexdigest()
        digest = hashlib.sha256(json.dumps([call.action, call.params, call.expected_revision],
                                          sort_keys=True, allow_nan=False).encode()).hexdigest()
        with locked_json_path(root / 'tokens.json'):
            call.context.check(call.action, {'owner_id': call.owner_id})
            require_global_writer('remote_push_tokens')
            config = read_push_json(root, 'tokens.json')
            rows = config.setdefault('tokens', [])
            receipts = config.setdefault('business_receipts', {})
            if not isinstance(rows, list) or not isinstance(receipts, dict):
                raise HostAPIError('HOST_NOT_READY', 503)
            previous = receipts.get(key)
            if previous is not None:
                if previous['digest'] != digest:
                    raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
                return copy.deepcopy(previous['result'])
            result = mutate(rows)
            receipts[key] = {'digest': digest, 'result': _secret_receipt(result)}
            persist_push_json(root / 'tokens.json', config)
            return result

    return w9_write(call, 'datasource_health', commit, receipt_view=_secret_receipt)


# LLM: ACL 固定为本 owner 的单向事件，明文不落盘。
# 函数用途: 在已持原锁的集合内追加新令牌。
def _mint_row(call, rows, metadata):
    if metadata.get('space_id'):
        w9_require_space(call, metadata['space_id'])
    if metadata.get('datasource_id'):
        w9_datasource(call, metadata['datasource_id'])
    secret = secrets.token_urlsafe(32)
    row = {**metadata, 'token_id': 'inbox-' + secrets.token_hex(16),
           'token_sha256': hashlib.sha256(secret.encode()).hexdigest(), 'enabled': True,
           'allowed_target_owners': [call.owner_id], 'allowed_actions': ['push_event'],
           'created_at': w9_now(call)}
    rows.append(row)
    return {'token': _token_view(call, row), 'secret': secret, 'signing': _signing()}


# LLM: 只在同 owner ACL 内匹配指纹；禁用或过期令牌不能借轮换复活。
# 函数用途: 查找唯一可操作的令牌。
def _find_token(call, rows, fingerprint, active=False):
    matches = [row for row in rows if _owned_token(call, row)
               and row.get('token_sha256', '')[:16] == fingerprint]
    if len(matches) != 1:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    row = matches[0]
    if active and (row.get('enabled') is not True
                   or row.get('expires_at') is not None and row['expires_at'] <= call.clock()):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return row


# LLM: 旧令牌只缩短有效期，不延长已有期限；新行沿用原绑定与 IP 限制。
# 函数用途: 在原锁内完成宽限轮换。
def _rotate_row(call, rows, row):
    expiry = call.clock() + call.params.get('grace_hours', 24) * 3600
    row['expires_at'] = min(row['expires_at'], expiry) if row.get('expires_at') is not None else expiry
    metadata = {key: row.get(key) for key in
                ('label', 'template_id', 'datasource_id', 'space_id', 'allowed_source_ips')}
    metadata['expires_at'] = None
    return _mint_row(call, rows, metadata)


# LLM: 内建模板描述原 validate_push_payload；完整关联约束仍由原 validator 裁决。
# 函数用途: 返回真实原生模板，并按冻结包目录可观察范围报告覆盖。
def list_templates(call):
    from ..gateway_parts.business_api_v13 import v13_call_frozen

    if call.params.get('space_id'):
        w9_require_space(call, call.params['space_id'])
    properties = {key: {'type': 'string', 'maxLength': limit} for key, limit in (
        ('target_owner', 256), ('instruction', 32768), ('title', 256), ('idempotency_key', 256),
        ('event_id', 256), ('source_kind', 128), ('thread_reuse_key', 256),
        ('subscription_id', 128), ('source_ref', 128))}
    properties.update(thread_mode={'enum': ['new_thread', 'reuse_key']}, context_payload={'type': 'object'},
                      context_snapshot={'type': 'array', 'maxItems': 256, 'items': {'type': 'object',
                          'properties': {'role': {'enum': ['user', 'assistant', 'tool']},
                                         'content': {'type': 'string'}},
                          'required': ['role', 'content'], 'additionalProperties': False}},
                      event_revision={'type': 'integer', 'minimum': 0})
    items = [{'template_id': 'generic_push_event', 'name': '原生单向事件推送', 'source': 'builtin',
              'package_id': None, 'body_schema': {'type': 'object', 'properties': properties,
                  'required': ['target_owner', 'instruction'], 'additionalProperties': False,
                  'anyOf': [{'required': ['idempotency_key']}, {'required': ['event_id']}]},
              'sample': {'target_owner': call.owner_id, 'instruction': '外部事件',
                         'idempotency_key': 'sample-event-one'}, 'signing': _signing()}]
    catalog = v13_call_frozen(call, 'spaces.packages.catalog', {})
    data = catalog.data if hasattr(catalog, 'data') else catalog
    # 冻结 PackageCatalogView 无模板或 manifest 字段；不能捏造未读取的包模板。
    return {'items': items, 'coverage': 'partial' if data.get('items') or data.get('next_cursor') else 'complete'}


# LLM: 空参数是 issue 请求；读取不创建原生文件或完整 Agent。
# 函数用途: 按显式条件列出同 owner 令牌。
def list_tokens(call):
    if call.params.get('space_id'):
        w9_require_space(call, call.params['space_id'])
    if call.params.get('datasource_id'):
        w9_datasource(call, call.params['datasource_id'])
    rows = read_push_json(_push_root(call), 'tokens.json').get('tokens', [])
    if not isinstance(rows, list):
        raise HostAPIError('HOST_NOT_READY', 503)
    items = []
    for row in rows:
        if not _owned_token(call, row):
            continue
        if any(call.params.get(key) and row.get(key) != call.params[key] for key in ('space_id', 'datasource_id')):
            continue
        if not call.params.get('include_disabled', False) and row.get('enabled') is not True:
            continue
        items.append(_token_view(call, row))
    return {'items': items[:500], 'coverage': 'partial' if len(items) > 500 else 'complete'}


# LLM: 数据源、空间和 IP 在原生副作用前校验。
# 函数用途: 签发严格同 owner 的推送令牌。
def mint_token(call):
    space_id = w9_require_space(call, call.params.get('space_id'))
    if call.params.get('datasource_id'):
        w9_datasource(call, call.params['datasource_id'])
    ips = call.params.get('allowed_source_ips', [])
    try:
        for address in ips:
            ipaddress.ip_network(address, strict=False)
    except ValueError:
        raise HostAPIError('INVALID_SCHEMA', 400) from None
    template_id = call.params.get('template_id')
    if template_id and template_id not in {item['template_id'] for item in list_templates(call)['items']}:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    metadata = {'label': call.params['label'], 'template_id': template_id,
                'datasource_id': call.params.get('datasource_id'), 'space_id': space_id,
                'allowed_source_ips': ips, 'expires_at': (
                    call.clock() + call.params['expires_in_days'] * 86400 if 'expires_in_days' in call.params else None)}

    # LLM: 从锁内当前集合追加，不覆盖锁外快照。
    # 函数用途: 提交新令牌。
    def mutate(rows):
        return _mint_row(call, rows, metadata)

    return _token_transaction(call, mutate)


# LLM: 指纹查找与变更同锁，防止并发吊销后旧快照复活。
# 函数用途: 按指纹轮换令牌。
def rotate_token(call):
    # LLM: 数据源仍须属于当前 owner 且未删除。
    # 函数用途: 轮换锁内的有效行。
    def mutate(rows):
        row = _find_token(call, rows, call.params['fingerprint'], active=True)
        if row.get('datasource_id'):
            w9_datasource(call, row['datasource_id'])
        return _rotate_row(call, rows, row)

    return _token_transaction(call, mutate)


# LLM: 直接禁用原认证行，不另建只影响展示的状态。
# 函数用途: 即时吊销本 owner 令牌。
def revoke_token(call):
    # LLM: 修改当前行，不以锁外读值覆盖其它修改。
    # 函数用途: 提交即时禁用。
    def mutate(rows):
        row = _find_token(call, rows, call.params['fingerprint'])
        row['enabled'] = False
        return {'token': _token_view(call, row)}

    return _token_transaction(call, mutate)


# LLM: 刷新同时核 owner、数据源及 active 状态。
# 函数用途: 轮换指定数据源最近签发的有效令牌，没有绑定时返回 404。
def refresh_push_token(call):
    w9_datasource(call, call.params['datasource_id'])

    # LLM: 在原锁内选择最新行，不命中其他 owner 的同名数据源。
    # 函数用途: 轮换当前绑定。
    def mutate(rows):
        candidates = [row for row in rows if _owned_token(call, row)
                      and row.get('datasource_id') == call.params['datasource_id']
                      and row.get('enabled') is True
                      and (row.get('expires_at') is None or row['expires_at'] > call.clock())]
        if not candidates:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return _rotate_row(call, rows, max(candidates, key=lambda row: row.get('created_at') or ''))

    return _token_transaction(call, mutate)


# LLM: 公共 App 不返回原引用、明文或底层路径。
# 函数用途: 构造冻结的应用公开视图。
def _app_view(row):
    return {key: row[key] for key in ('connector_type', 'client_id', 'authorize_url', 'token_url',
                                    'scopes', 'redirect_uri', 'revision', 'updated_at')} | {
        'has_secret': bool(row.get('client_secret_ref')),
        'secret_ref_redacted': '<redacted>' if row.get('client_secret_ref') else None}


# LLM: 跨 owner 记录统一按不存在处理。
# 函数用途: 检查 OAuth 记录归属。
def _owned_record(call, row):
    if row is None or row.get('owner_id') != call.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return row


# LLM: 获取配置不解析密钥、不触发网络。
# 函数用途: 读取本 owner 的 OAuth 配置。
def get_oauth_app(call):
    row = _owned_record(call, w9_get(call, 'oauth_apps', call.params['connector_type']))
    return {'app': _app_view(row)}


# LLM: 只接受宿主 SecretRef 语法，实际值不进入公开记录和回执。
# 函数用途: 幂等创建或替换本 owner 的 OAuth App。
def upsert_oauth_app(call):
    p = call.params
    if 'client_secret_ref' in p and not is_secret_ref(p['client_secret_ref']):
        raise HostAPIError('INVALID_SCHEMA', 400)

    # LLM: PUT 在锁内取当前 revision，不覆盖其他 owner 记录。
    # 函数用途: 更新完整应用配置。
    def mutate(records):
        previous = records.get(p['connector_type'])
        if previous is not None:
            _owned_record(call, previous)
        row = {key: p[key] for key in ('connector_type', 'client_id', 'authorize_url', 'token_url', 'redirect_uri')}
        row.update(client_secret_ref=p.get('client_secret_ref'), scopes=p.get('scopes', []),
                   owner_id=call.owner_id, revision=(previous or {}).get('revision', 0) + 1,
                   created_at=(previous or {}).get('created_at', w9_now(call)), updated_at=w9_now(call))
        records[p['connector_type']] = row
        return {'app': _app_view(row)}

    return w9_write(call, 'oauth_apps', mutate)


# LLM: 删除仅影响本 owner 的同名应用。
# 函数用途: 删除 OAuth App。
def delete_oauth_app(call):
    # LLM: 记录存在性在原事务内核验。
    # 函数用途: 提交配置删除。
    def mutate(records):
        key = call.params['connector_type']
        _owned_record(call, records.get(key))
        del records[key]
        return {'connector_type': key, 'deleted': True}

    return w9_write(call, 'oauth_apps', mutate)


# LLM: 保留其它 URL 参数，覆盖 OAuth 控制字段以避免重复 state 歧义。
# 函数用途: 构造 S256 PKCE 授权 URL。
def _authorize_url(app, state, verifier):
    url = urlsplit(app['authorize_url'])
    fields = {'response_type': 'code', 'client_id': app['client_id'], 'redirect_uri': app['redirect_uri'],
              'scope': ' '.join(app['scopes']), 'state': state,
              'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),
              'code_challenge_method': 'S256'}
    query = [(key, value) for key, value in parse_qsl(url.query, keep_blank_values=True) if key not in fields]
    return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query + list(fields.items())), url.fragment))


# LLM: state/verifier 绑定本 owner 与数据源；十分钟到期，verifier 不进入 URL 或回执。
# 函数用途: 持久创建一次 OAuth 授权尝试。
def oauth_start(call):
    w9_datasource(call, call.params['datasource_id'])
    app = _owned_record(call, w9_get(call, 'oauth_apps', call.params['connector_type']))

    # LLM: 随机值仅在首次事务产生，幂等重放复用已签 state。
    # 函数用途: 提交 PKCE 状态并返回跳转信息。
    def mutate(records):
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        expiry = _timestamp(call.clock() + 600)
        url = _authorize_url(app, state, verifier)
        if len(url) > 4096:
            raise HostAPIError('LIMIT_EXCEEDED', 400)
        records[state] = {'state': state, 'owner_id': call.owner_id,
                          'connector_type': app['connector_type'], 'datasource_id': call.params['datasource_id'],
                          'code_verifier': verifier, 'used': False, 'expires_at': expiry, 'revision': 1,
                          'created_at': w9_now(call), 'updated_at': w9_now(call)}
        return {'authorize_url': url, 'state': state, 'expires_at': expiry}

    return w9_write(call, 'oauth_states', mutate)


# LLM: 在 provider 前单次消费并耐久提交，失败或进程退出不能重复交换。
# 函数用途: 领取同 owner、未过期且未用的 OAuth state。
def _consume_state(call):
    state_call = replace(call, idempotency_key=hashlib.sha256(
        ('oauth-consume:' + call.idempotency_key).encode()).hexdigest())
    state = {}

    # LLM: 在状态锁内重读，仅首次领取者取得 verifier 内存副本。
    # 函数用途: 标记 state 已用并擦除存储 verifier。
    def mutate(records):
        row = records.get(call.params['state'])
        if (row is None or row.get('owner_id') != call.owner_id or row.get('used') is not False
                or not row.get('expires_at')
                or datetime.fromisoformat(row['expires_at']).timestamp() <= call.clock()):
            raise HostAPIError('STATE_CONFLICT', 409)
        state.update(row)
        row.update(used=True, code_verifier='<redacted>', revision=row['revision'] + 1, updated_at=w9_now(call))
        return {'consumed': True}

    w9_write(state_call, 'oauth_states', mutate)
    return state


# LLM: provider 是唯一网络出口；消费与成功回执顺序提交，不嵌套不可重入的 W0 目录锁。
# 函数用途: 完成交换并把凭据送入原 Vault。
def oauth_complete(call):
    from ..gateway_parts.business_api_v13 import v13_provider

    provider = v13_provider(call, 'oauth_http')
    if provider is None or not callable(getattr(provider, 'exchange_code', None)):
        raise HostAPIError('HOST_NOT_READY', 503)

    try:
        pending = w9_get(call, 'oauth_states', call.params['state'])
    except HostAPIError as error:
        if error.status == 404:
            raise HostAPIError('STATE_CONFLICT', 409) from None
        raise
    if pending is None or pending.get('owner_id') != call.owner_id:
        raise HostAPIError('STATE_CONFLICT', 409)
    app = _owned_record(call, w9_get(call, 'oauth_apps', pending['connector_type']))
    spec = w9_datasource(call, pending['datasource_id'])
    state = _consume_state(call)
    result = None
    if state:
        try:
            call.context.check(call.action, {'owner_id': call.owner_id, 'datasource_id': spec['datasource_id']})
            client_secret = resolve_secret_ref(app['client_secret_ref'], required=True) if app['client_secret_ref'] else ''
            credentials = provider.exchange_code(token_url=app['token_url'], client_id=app['client_id'],
                client_secret=client_secret, code=call.params['code'], code_verifier=state['code_verifier'],
                redirect_uri=app['redirect_uri'])
            if not isinstance(credentials, dict) or not isinstance(credentials.get('access_token'), str) or not credentials['access_token']:
                raise ValueError('invalid_token_response')
            call.context.check(call.action, {'owner_id': call.owner_id, 'datasource_id': spec['datasource_id']})
            summary = w9_datasource_store(call).save_datasource(spec, credential_values=credentials,
                scope=spec['scope'], workspace_root=getattr(call.context.host, 'workspace', None),
                expected_revision=spec['revision'])
        except HostAPIError:
            raise
        except (OSError, ValueError, TypeError, RuntimeError, KeyError):
            raise HostAPIError('HOST_NOT_READY', 503) from None
        result = {'datasource_id': spec['datasource_id'], 'connected': True,
                  'credential_ref': summary.get('credential_ref') or None}

    # LLM: 消费回执重放时不再调用 provider；仅重放已有成功结果，恢复不确定窗口拒绝重复交换。
    # 函数用途: 提交或重放公开完成回执。
    def mutate(records):
        if result is None:
            raise HostAPIError('STATE_CONFLICT', 409)
        return result

    return w9_write(call, 'oauth_apps', mutate)


# LLM: 白名单事实不含 DSN、路径、凭据；症状正文可能含粘贴口令，不直接送模型。
# 函数用途: 启动异步连接诊断任务。
def troubleshoot(call):
    from ..assets.datasource_store import DatasourceConnectionSpec
    from ..gateway_parts.business_api_v13 import v13_start_model_job

    spec = w9_datasource(call, call.params['datasource_id'])
    summary = w9_datasource_store(call).public_summary(DatasourceConnectionSpec(**spec))
    health = w9_get(call, 'datasource_health', spec['datasource_id']) or {}
    scoped = replace(call, params={'datasource_id': spec['datasource_id']})
    facts = {'datasource': {key: summary[key] for key in
                           ('datasource_id', 'kind', 'has_secret', 'credential_keys')},
             'last_test': health.get('last_test'), 'fleet': fleet_health(scoped)['items'],
             'recent_push_error_codes': [], 'recent_push_errors_available': False,
             'symptom_provided': bool(call.params.get('symptom'))}
    instruction = '依据结构化连接观察诊断，输出约定 JSON；缺少证据时明确标注未知。\n' + json.dumps(facts, ensure_ascii=False)
    return v13_start_model_job(call, 'datasource_troubleshoot', instruction,
                               space_id=w9_require_space(call))


# LLM: 正文只按 JSON 解析，W0 负责按 job result_schema 验证。
# 函数用途: 解析诊断模型结果。
def finalize_troubleshoot(text):
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError('JOB_RESULT_INVALID')
    return result
