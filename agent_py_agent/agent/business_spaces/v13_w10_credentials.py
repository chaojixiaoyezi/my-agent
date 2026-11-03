"""模型凭据沿原生目录；访问令牌只存散列，签发秘密只返回一次。"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import re
import secrets
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from ..common.json_io import locked_json_path
from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from ..plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers
from ..settings import model_profiles as catalog
from ..settings.embedding_selection import EMBEDDING_HOST_CHANGE_REASON, catalog_write_check
from ..settings.model_credentials import provider_api_key
from ..settings.model_provider_operations import _save_provider as save_native_provider
from ..settings.model_provider_schema import (
    ModelProfileError,
    validate_model,
    validate_provider_id,
)


# LLM: 只使用可信宿主home，不接受请求选owner。
# 函数用途: 取得原生目录操作器。
def _settings(call):
    return ModelSettingsV2Handlers(call.context.host.home_paths)


# LLM: URL只公开host，不泄漏userinfo、路径或认证头。
# 函数用途: 生成服务商闭合投影。
def _provider_view(provider):
    return {'provider_id': provider['provider_id'], 'display_name': provider['display_name'] or None,
        'api_base_host': urlsplit(provider['api_base']).hostname, 'has_secret': provider['has_api_key'],
        'enabled': provider['enabled'], 'models': [row['model_name'] for row in provider['models']]}


# LLM: 在原目录锁内统一快照，has_secret按原生凭据解析器计算；超量不截断冒充完整。
# 函数用途: 读取全部用途的服务商并统一静态和环境凭据状态。
def _providers(call):
    settings = _settings(call)
    with locked_json_path(settings.path):
        data = settings.list_providers({'capability_filter': 'all'})
        providers = catalog.read_model_profiles(settings.path)['providers']
        for row in data['providers']:
            row['has_api_key'] = bool(provider_api_key(providers[row['provider_id']]))
    if len(data['providers']) > 100:
        raise HostAPIError('LIMIT_EXCEEDED', 400)
    return data


# LLM: 不执行DNS，普通hostname的私有性不能猜测。
# 函数用途: 分类字面地址的局部性。
def _locality(host):
    if not host:
        return 'unknown'
    if host.lower().rstrip('.') == 'localhost':
        return 'loopback'
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return 'unknown'
    if address.is_loopback:
        return 'loopback'
    if address.is_unspecified or address.is_multicast or address.is_reserved or address.is_link_local:
        return 'unknown'
    return 'private' if address.is_private else 'public' if address.is_global else 'unknown'


# LLM: 端点局部性不是出网探测；没有配置不能作为隔离证据。
# 函数用途: 展示配置事实和显式出网声明。
def airgap_status(call):
    endpoints = [{'provider_id': row['provider_id'], 'host': urlsplit(row['api_base']).hostname,
        'locality': _locality(urlsplit(row['api_base']).hostname)} for row in _providers(call)['providers']]
    localities = {row['locality'] for row in endpoints}
    airgapped = False if 'public' in localities else True if endpoints and localities <= {'private', 'loopback'} else None
    outbound = getattr(call.context.host, 'outbound_network', 'unknown')
    if outbound not in {'allowed', 'blocked', 'unknown'}:
        outbound = 'unknown'
    return {'airgapped': airgapped, 'outbound_network': outbound, 'model_endpoints': endpoints,
        'evidence': ['仅根据已配置模型端点的字面地址分类，未执行网络探测。',
            '宿主已显式声明出网策略。' if outbound != 'unknown' else '宿主未声明出网策略，状态未知。'],
        'coverage': 'complete' if endpoints and 'unknown' not in localities else 'unknown'}


# LLM: runtime不透传原目录，只包含选择与推理强度。
# 函数用途: 返回模型运行状态安全投影。
def llm_status(call):
    data = _providers(call)
    return {'selected_profile_id': None if data['selected_profile_id'] == 'default' else data['selected_profile_id'],
        'profiles': sum(len(row['models']) for row in data['providers']),
        'providers': [_provider_view(row) for row in data['providers']],
        'runtime': {'selected_profile_id': data['selected_profile_id'], 'reasoning_efforts': data['reasoning_efforts']},
        'coverage': 'complete'}


# LLM: 指定的服务商不存在则404，不伪装空成功。
# 函数用途: 读取只包含has_secret的模型凭据展示。
def credentials_get(call):
    providers = [_provider_view(row) for row in _providers(call)['providers']]
    if 'provider_id' in call.params:
        providers = [row for row in providers if row['provider_id'] == call.params['provider_id']]
        if not providers:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return {'providers': providers}


# LLM: 复用原目录锁、原生凭据合并及embedding保护；名称先规范化，最终结果/容量/实时授权均写前校验。
# 函数用途: 在唯一目录内合并凭据补丁或删除服务商，先确认可返回结果再保存。
def _credential_catalog_change(call, progress, *, deleting=False):
    from ..gateway_parts.business_api_v13 import v13_require_admin, v13_validate_result
    settings = _settings(call)
    try:
        pid = validate_provider_id(call.params.get('provider_id'))
        with locked_json_path(settings.path):
            data = catalog.read_model_profiles(settings.path)
            before = deepcopy(data)
            if deleting:
                if pid not in data['providers']:
                    raise HostAPIError('RESOURCE_NOT_FOUND', 404)
                data['providers'].pop(pid)
                data['profiles'] = {key: row for key, row in data['profiles'].items() if row['provider_id'] != pid}
                if (data['selected'] != 'default' and data['selected'] not in data['profiles']
                        and not data['selected'].startswith('shared:')):
                    data['selected'] = 'default'
            else:
                if pid not in data['providers'] and len(data['providers']) >= 100:
                    raise HostAPIError('LIMIT_EXCEEDED', 400)
                previous = data['providers'].get(pid, {})
                values = {key: call.params[key] for key in
                    ('display_name', 'api_base', 'api_key', 'enabled') if key in call.params}
                if values.get('api_key') in (None, '***'):
                    values.pop('api_key', None)
                save_native_provider(data, {'provider_id': pid, 'provider': values, 'editing': bool(previous)})
                # 未传models相当于原生models=[]：完全不修改已有profiles。
                # 显式名称补丁也从锁内最新行保留参数及稳定ID，不回写旧展示快照。
                models = call.params.get('models', [])
                if not isinstance(models, list) or len(models) > 500:
                    raise HostAPIError('INVALID_SCHEMA')
                for name in dict.fromkeys(name.strip() for name in models):
                    key = next((key for key, row in data['profiles'].items()
                        if row['provider_id'] == pid and row['model_name'] == name), None)
                    model = data['profiles'][key] if key else {'model_name': name,
                        'model_backend': 'openai_compatible', 'capability': 'agentic'}
                    data['profiles'][key or str(uuid4())] = validate_model({**model, 'provider_id': pid})
                if sum(row['provider_id'] == pid for row in data['profiles'].values()) > 500:
                    raise HostAPIError('LIMIT_EXCEEDED', 400)
            if deleting:
                result = {'provider_id': pid, 'deleted': True}
            else:
                provider = data['providers'][pid]
                result = {'provider': _provider_view({'provider_id': pid, **provider,
                    'has_api_key': bool(provider_api_key(provider)), 'models': [row for row in data['profiles'].values()
                        if row['provider_id'] == pid]})}
            v13_validate_result(call.action, result)
            catalog_write_check(call.context.host)(before, data)
            # 可能等原目录锁很久；真正保存前重核live和原生admin，不依赖业务锁入口的旧决定。
            call.context.check(call.action, {'owner_id': call.owner_id})
            v13_require_admin(call)
            progress['phase'] = 'unknown'
            catalog._save_profiles(settings.path, data)
            progress['phase'] = 'committed'
            return result
    except (ModelProfileError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, HostAPIError):
            raise
        if progress['phase'] != 'not_started':
            raise
        if getattr(error, 'reason', None) == EMBEDDING_HOST_CHANGE_REASON:
            raise HostAPIError('BUSINESS_ACCESS_DENIED', 403) from None
        raise HostAPIError('INVALID_SCHEMA') from None


# LLM: 原目录与W0回执不是一个原子事务；原生写入开始后的错误必须报告unknown。
# 函数用途: 保存模型凭据并区分写前拒绝与提交结果未知。
def _credentials_write(call, *, deleting=False):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    from .v13_w10_identity import write
    v13_require_admin(call)
    progress = {'phase': 'not_started'}

    # LLM: 外层W0事务只记录公开结果，原目录仍是唯一凭据存储。
    # 函数用途: 执行一次带进展标记的原生目录变更。
    def change(records):
        return _credential_catalog_change(call, progress, deleting=deleting)

    try:
        return write(call, 'api_keys', change)
    except Exception:
        if progress['phase'] != 'not_started':
            raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from None
        else:
            raise


# LLM: 模型凭据只保存原目录，业务回执仅存公开投影。
# 函数用途: 新增或更新一个模型服务商。
def credentials_upsert(call):
    return _credentials_write(call)


# LLM: 原生删除负责级联与当前选择，不在业务集合存配置。
# 函数用途: 幂等删除一个服务商。
def credentials_delete(call):
    return _credentials_write(call, deleting=True)


# LLM: 就绪只依赖宿主显式绑定，不构造假探测器。
# 函数用途: 提供探测动作的registry就绪判断。
def probe_ready(call):
    from ..gateway_parts.business_api_v13 import v13_provider
    try:
        provider = v13_provider(call, 'llm_probe')
    except HostAPIError as error:
        if error.code == 'HOST_NOT_READY':
            return False
        raise
    return provider is not None and callable(getattr(provider, 'test', None)) and callable(getattr(provider, 'list_models', None))


# LLM: 秘密只交给宿主绑定provider，不进入响应或日志。
# 函数用途: 读取本owner的探测配置。
def _probe_source(call):
    from ..settings.model_credentials import provider_api_key
    from ..settings.model_profiles import read_model_profiles
    provider = read_model_profiles(_settings(call).path)['providers'].get(call.params['provider_id'])
    if provider is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return provider, provider_api_key(provider) or None


# LLM: 只接受结构化短错误码，不能把异常文字或秘密返回给调用者。
# 函数用途: 校验探测响应字段。
def _probe_result(result, api_key):
    if not isinstance(result, dict) or type(result.get('ok')) is not bool:
        raise HostAPIError('HOST_NOT_READY', 503)
    latency = result.get('latency_ms')
    if latency is not None and (type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0):
        raise HostAPIError('HOST_NOT_READY', 503)
    code = result.get('error_code')
    if code is not None and (not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', code)
            or api_key and api_key in code):
        code = 'PROBE_FAILED'
    return {'ok': result['ok'], 'latency_ms': latency, 'error_code': code}


# LLM: ok=false保留真实探测失败；异常使用固定脱敏错误。
# 函数用途: 幂等调用已绑定的连接测试。
def credentials_test(call):
    from ..gateway_parts.business_api_v13 import v13_provider
    from .v13_w10_identity import now, write
    if not probe_ready(call):
        raise HostAPIError('HOST_NOT_READY', 503)

    # LLM: provider在live检查之后调用，回执仅保留公开结果。
    # 函数用途: 执行并校验连接探测。
    def change(records):
        provider, key = _probe_source(call)
        try:
            result = v13_provider(call, 'llm_probe').test(provider, key)
        except Exception:
            raise HostAPIError('HOST_NOT_READY', 503) from None
        return {'provider_id': call.params['provider_id'], **_probe_result(result, key), 'tested_at': now(call)}

    return write(call, 'api_keys', change)


# LLM: 不猜HTTP协议，不修改已保存模型；含秘密的结果不得返回。
# 函数用途: 幂等拉取模型列表。
def discover_models(call):
    from ..gateway_parts.business_api_v13 import v13_provider
    from .v13_w10_identity import now, write
    if not probe_ready(call):
        raise HostAPIError('HOST_NOT_READY', 503)

    # LLM: 真实请求仅由绑定provider负责，数量和字段都受合同限制。
    # 函数用途: 调用并验证模型列表。
    def change(records):
        provider, key = _probe_source(call)
        try:
            models = v13_provider(call, 'llm_probe').list_models(provider, key)
        except Exception:
            raise HostAPIError('HOST_NOT_READY', 503) from None
        if (not isinstance(models, list) or len(models) > 1000 or any(not isinstance(name, str)
                or not 1 <= len(name) <= 256 or key and key in name for name in models)):
            raise HostAPIError('HOST_NOT_READY', 503)
        return {'provider_id': call.params['provider_id'], 'models': models, 'fetched_at': now(call)}

    return write(call, 'api_keys', change)


# LLM: 不透传owner或散列，读响应与secret-once回执共用公开投影。
# 函数用途: 返回API key或SCIM token的封闭视图。
def _token_view(row, scim=False):
    fields = ('name', 'prefix', 'enabled', 'expires_at', 'last_used_at', 'revision', 'created_at', 'updated_at')
    fields += ('token_id', 'rotated_from') if scim else ('key_id', 'scopes', 'created_by')
    return {key: row[key] for key in fields}


# LLM: scope只认当前动作目录，不能根据自然语言或通配符扩权。
# 函数用途: 验证申请的API作用域。
def _scopes(call):
    from .v13_w10_identity import permission_catalog
    scopes = call.params.get('scopes', [])
    known = {entry['permission'] for entry in permission_catalog(call)}
    if any(scope not in known for scope in scopes):
        raise BusinessError('INVALID_SCHEMA', details={'path': 'params.scopes'})
    return sorted(set(scopes))


# LLM: 令牌必须不可预测；散列和短前缀之外不存秘密。
# 函数用途: 生成新令牌记录和仅本次返回的秘密。
def _new_token(call, *, scim=False, rotated_from=None, expires_at=None):
    from .v13_w10_identity import now
    secret = ('msc_' if scim else 'mak_') + secrets.token_urlsafe(32)
    stamp = now(call)
    if expires_at is None and 'expires_in_days' in call.params:
        expires_at = (datetime.fromtimestamp(call.clock(), timezone.utc)
            + timedelta(days=call.params['expires_in_days'])).isoformat().replace('+00:00', 'Z')
    row = {'name': call.params['name'], 'prefix': secret[:12], 'enabled': True,
        'expires_at': expires_at, 'last_used_at': None, 'revision': 1, 'created_at': stamp,
        'updated_at': stamp, 'owner_id': 'local/main' if scim else call.owner_id,
        'secret_sha256': hashlib.sha256(secret.encode()).hexdigest()}
    row['token_id' if scim else 'key_id'] = uuid4().hex
    row.update({'rotated_from': rotated_from} if scim else {'scopes': _scopes(call), 'created_by': call.subject_id})
    return row, secret


# LLM: 幂等回执不保留首次签发的秘密，也不会在重放时再次签发。
# 函数用途: 对secret-once响应生成可持久回执。
def _secret_receipt(result):
    return {**result, 'secret': '<redacted>'}


# LLM: API key始终绑定当前owner，记录级检查阻止脏归属记录泄漏。
# 函数用途: 列出当前owner的API keys。
def api_keys_list(call):
    from .v13_w10_identity import rows
    items = [_token_view(row) for row in rows(call, 'api_keys') if row['owner_id'] == call.owner_id
        and (call.params.get('include_revoked', False) or row['enabled'])]
    if len(items) > 500:
        raise HostAPIError('LIMIT_EXCEEDED', 400)
    return {'items': items}


# LLM: 配额在锁内与插入一起检查，秘密仅首次响应出现。
# 函数用途: 创建当前owner的API key。
def api_keys_create(call):
    from .v13_w10_identity import write
    _scopes(call)

    # LLM: 创建前不在锁外读取或覆盖集合快照。
    # 函数用途: 提交一条新key。
    def change(records):
        if len(records) >= 500:
            raise HostAPIError('LIMIT_EXCEEDED', 400)
        row, secret = _new_token(call)
        records[row['key_id']] = row
        return {'key': _token_view(row), 'secret': secret}

    return write(call, 'api_keys', change, receipt_view=_secret_receipt)


# LLM: 不存在和跨owner统一404；CAS在锁内执行，不基于旧快照。
# 函数用途: 更新API key的公开设置。
def api_keys_update(call):
    from .v13_w10_identity import cas, now, write
    scopes = _scopes(call) if 'scopes' in call.params else None

    # LLM: 被撤销秘密已删除，重新enabled也不能恢复旧凭据。
    # 函数用途: 合并当前key设置。
    def change(records):
        row = records.get(call.params['key_id'])
        if row is None or row['owner_id'] != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        cas(call, row)
        for key in ('name', 'enabled'):
            if key in call.params:
                row[key] = call.params[key]
        if scopes is not None:
            row['scopes'] = scopes
        row.update(revision=row['revision'] + 1, updated_at=now(call))
        return {'key': _token_view(row)}

    return write(call, 'api_keys', change)


# LLM: SCIM管理只接受原生管理员决定，不读取W10角色。
# 函数用途: 列出主owner的SCIM tokens。
def scim_list(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    from .v13_w10_identity import rows
    v13_require_admin(call)
    items = [_token_view(row, True) for row in rows(call, 'scim_tokens', 'main') if row['owner_id'] == 'local/main']
    if len(items) > 100:
        raise HostAPIError('LIMIT_EXCEEDED', 400)
    return {'items': items}


# LLM: SCIM token仅保存在main集合，秘密遵守首次响应规则。
# 函数用途: 创建企业目录同步token。
def scim_create(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    from .v13_w10_identity import write
    v13_require_admin(call)

    # LLM: 签发与容量检查位于同一短事务。
    # 函数用途: 插入主owner的新token。
    def change(records):
        if len(records) >= 100:
            raise HostAPIError('LIMIT_EXCEEDED', 400)
        row, secret = _new_token(call, scim=True)
        records[row['token_id']] = row
        return {'token': _token_view(row, True), 'secret': secret}

    return write(call, 'scim_tokens', change, scope='main', receipt_view=_secret_receipt)


# LLM: 新旧token在同一集合事务切换，轮转后旧秘密立即失效。
# 函数用途: CAS轮换一个有效SCIM token。
def scim_rotate(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    from .v13_w10_identity import cas, now, write
    v13_require_admin(call)

    # LLM: CAS与停用旧token均基于锁内记录。
    # 函数用途: 停用旧token并生成带rotated_from的新token。
    def change(records):
        old = records.get(call.params['token_id'])
        if old is None or old['owner_id'] != 'local/main':
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        cas(call, old)
        if not old['enabled']:
            raise HostAPIError('STATE_CONFLICT', 409)
        if len(records) >= 100:
            raise HostAPIError('LIMIT_EXCEEDED', 400)
        row, secret = _new_token(replace(call, params={'name': old['name']}), scim=True,
            rotated_from=old['token_id'], expires_at=old['expires_at'])
        old.update(enabled=False, secret_sha256=secrets.token_hex(32), revision=old['revision'] + 1, updated_at=now(call))
        records[row['token_id']] = row
        return {'token': _token_view(row, True), 'secret': secret}

    return write(call, 'scim_tokens', change, scope='main', receipt_view=_secret_receipt)


# LLM: 撤销删除散列，后续编辑不能恢复旧秘密。
# 函数用途: 撤销当前范围的一条令牌。
def _revoke(call, *, scim=False):
    from .v13_w10_identity import now, write
    collection, scope = ('scim_tokens', 'main') if scim else ('api_keys', 'owner')
    field = 'token_id' if scim else 'key_id'

    # LLM: 当前记录归属检查在锁内，不泄漏跨owner记录。
    # 函数用途: 原子停用并销毁令牌散列。
    def change(records):
        row = records.get(call.params[field])
        if row is None or row['owner_id'] != ('local/main' if scim else call.owner_id):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        row.update(enabled=False, secret_sha256=secrets.token_hex(32), revision=row['revision'] + 1, updated_at=now(call))
        return {'token' if scim else 'key': _token_view(row, scim)}

    return write(call, collection, change, scope=scope)


# LLM: 本owner的key撤销不影响其他owner。
# 函数用途: 撤销API key。
def api_keys_revoke(call):
    return _revoke(call)


# LLM: 只有原生管理员可撤销主集合token。
# 函数用途: 撤销SCIM token。
def scim_revoke(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    return _revoke(call, scim=True)


# LLM: 时间来自可信时钟，损坏的过期值不能视作永久有效。
# 函数用途: 解析UTC记录时间为epoch。
def _epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


# LLM: constant-time比较sha256，停用、过期及错owner记录一律无效。
# 函数用途: 检查一条记录的令牌有效性。
def _valid(call, row, digest, scim):
    matched = hmac.compare_digest(digest, row.get('secret_sha256', ''))
    return (matched and row['owner_id'] == ('local/main' if scim else call.owner_id)
        and row['enabled'] and (row['expires_at'] is None or _epoch(row['expires_at']) > call.clock()))


# LLM: last_used每分钟最多更新一次；写入口再检查live身份和锁内token。
# 函数用途: 验证当前范围token并有界更新使用时间。
def _verify(call, *, scim=False):
    from .v13_w10_identity import now, rows, write
    collection, scope = ('scim_tokens', 'main') if scim else ('api_keys', 'owner')
    field = 'token_id' if scim else 'key_id'
    digest = hashlib.sha256(call.params['secret'].encode()).hexdigest()
    call.context.check(call.action, {'owner_id': call.owner_id})
    matched = None
    for row in rows(call, collection, scope):
        if _valid(call, row, digest, scim):
            matched = row
    invalid = {'valid': False, field: None}
    if not scim:
        invalid['scopes'] = []
    if matched is None:
        return invalid

    # LLM: 结果不含秘密，scope来自刚验证的记录。
    # 函数用途: 构造验证结果。
    def result(row):
        return {'valid': True, field: row[field], **({} if scim else {'scopes': row['scopes']})}

    if matched['last_used_at'] is not None and call.clock() - _epoch(matched['last_used_at']) < 60:
        return result(matched)

    # LLM: 不重放read旧回执；锁内重查撤销、轮转、过期及使用时间。
    # 函数用途: 更新本分钟首次使用时间。
    def change(records):
        row = records.get(matched[field])
        if row is None or not _valid(call, row, digest, scim):
            return invalid
        if row['last_used_at'] is None or call.clock() - _epoch(row['last_used_at']) >= 60:
            row.update(last_used_at=now(call), updated_at=now(call), revision=row['revision'] + 1)
        return result(row)

    return write(replace(call, idempotency_key=uuid4().hex), collection, change, scope=scope)


# LLM: 这是BFF验证入口，不改变网关认证中间件。
# 函数用途: 验证当前owner的API key。
def api_keys_verify(call):
    return _verify(call)


# LLM: 桥接可验证main token，管理动作admin门不因此放宽。
# 函数用途: 验证SCIM token。
def scim_verify(call):
    return _verify(call, scim=True)
