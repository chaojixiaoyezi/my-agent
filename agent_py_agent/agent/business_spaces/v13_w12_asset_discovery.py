"""外部资产仅来自显式 provider；权限、范围和回执都沿 V13 原生边界。"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json, validate_object


# LLM: 不能把任意 URL、通配符或路径清洗成授权域名。
# 函数用途: 规范单个 DNS 名；非法值返回 None。
def _domain(value):
    if type(value) is not str:
        return None
    name = value.lower().removesuffix('.')
    try:
        name = name.encode('idna').decode('ascii')
    except UnicodeError:
        return None
    if (not name or len(name) > 253 or any(not re.fullmatch(
            r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in name.split('.'))):
        return None
    return name


# LLM: IP/CIDR 与 DNS 不混用，子网必须整体包含而非只核网络首地址。
# 函数用途: 规范并分类请求目标。
def _target(value):
    if type(value) is not str:
        return None
    value = value.lower().removesuffix('.')
    try:
        network = ipaddress.ip_network(value, strict=False)
        return ('network', network) if '/' in value else ('ip', ipaddress.ip_address(value))
    except ValueError:
        name = _domain(value) if '/' not in value else None
        return ('domain', name) if name is not None else None


# LLM: scope 来自 operator 绑定的提供者，不从请求参数扩大。
# 函数用途: 读取并严格规范 provider 的资产权限范围。
def _scope(provider):
    scope = provider.allowed_scope()
    if (type(scope) is not dict or set(scope) != {'domains', 'cidrs'}
            or type(scope['domains']) is not list or type(scope['cidrs']) is not list
            or any(type(value) is not str for value in (*scope['domains'], *scope['cidrs']))):
        raise BusinessError('HOST_NOT_READY')
    domains = [_domain(value) for value in scope['domains']]
    try:
        networks = [ipaddress.ip_network(value, strict=False) for value in scope['cidrs']]
    except (ValueError, TypeError):
        raise BusinessError('HOST_NOT_READY') from None
    if any(value is None for value in domains):
        raise BusinessError('HOST_NOT_READY')
    return domains, networks


# LLM: 域名用点边界，IP 用相同协议族的包含关系，不做 DNS 或网络探测。
# 函数用途: 检查一个目标是否完全位于给定范围内。
def _contained(value, domains, networks):
    parsed = _target(value)
    if parsed is None:
        return False
    kind, selected = parsed
    if kind == 'domain':
        return any(selected == domain or selected.endswith('.' + domain) for domain in domains)
    if kind == 'ip':
        return any(selected.version == network.version and selected in network for network in networks)
    return any(selected.version == network.version and selected.subnet_of(network) for network in networks)


# LLM: readiness 只查权限配置，绝不触发扫描或创建记录。
# 函数用途: 供 registry 查询 asset_discovery 的真实就绪条件。
def asset_discovery_ready(call):
    try:
        provider = getattr(call.services.host, 'business_v13_providers', {}).get('asset_discovery')
        if provider is None:
            return False
        domains, networks = _scope(provider)
        return callable(getattr(provider, 'scan', None)) and bool(domains or networks)
    except (HostAPIError, BusinessError, OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


# LLM: 时间按真实瞬间排序，不把不同时区的字符串顺序当作先后。
# 函数用途: 解析提供者最后观察时间。
def _seen(value):
    if value is None:
        return float('-inf')
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if at.tzinfo is None:
            raise ValueError('timezone')
        return at.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise BusinessError('HOST_NOT_READY') from None


# LLM: 复用 Store 签名游标并逐记录授权；远端未完成的扫描不因本地分页读尽就宣称完整。
# 函数用途: 返回风险、最后观察时间降序的资产页，未扫描保持 unknown，续扫未完保持 partial。
def assets_list(call):
    from ..gateway_parts.business_api_v13 import v13_require_access, v13_schema
    from .v13_store import _page_rows, v13_store_for
    store = v13_store_for(call)
    state = store._read('asset_discovery_assets')
    rows = []
    filters = {key: value for key, value in call.params.items() if key != 'cursor'}
    for record in state['records'].values():
        v13_require_access(call, record)
        row = validate_object({key: value for key, value in record.items()
            if key not in {'owner_id', 'revision', 'created_at', 'updated_at'}},
            v13_schema({'$ref': '#/$defs/V13ExposedAsset'}))
        if any(row[key] != filters[key] for key in ('kind', 'risk') if key in filters):
            continue
        if filters.get('query', '').casefold() not in row['value'].casefold():
            continue
        rows.append(row)
    risk = {name: index for index, name in enumerate(('info', 'low', 'medium', 'high', 'critical'))}
    rows.sort(key=lambda row: (risk[row['risk']], _seen(row['last_seen_at']), row['asset_id']), reverse=True)
    page = _page_rows(rows, [str(store.owner_home), call.owner_id, call.subject_id, call.action,
        state['revision'], filters, rows], cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))
    scans = {}
    for receipt in state['receipts'].values():
        result = receipt['result']
        if 'scan_ref' in result and 'assets_upserted' in result:
            ref = result['scan_ref']
            # 回执按指纹保存，不能依赖键序；同一扫描任一完成回执可关闭之前的续扫。
            scans[ref] = scans.get(ref, False) or result.get('complete') is True
    if scans and not all(scans.values()):
        page['coverage'] = 'partial'
    elif not state['records'] and not scans:
        page['coverage'] = 'unknown'
    return page


# LLM: service/certificate 没有额外主机字段，只接受可解析的 value，不能猜归属。
# 函数用途: 从资产规范值取出可核对范围的主机或地址。
def _asset_host(row):
    value = row['value']
    if row['kind'] in {'domain', 'subdomain', 'ip'} or _target(value) is not None:
        return value
    try:
        parsed = urlsplit(value if '://' in value else '//' + value)
        if parsed.username is not None or parsed.password is not None:
            return None
        return parsed.hostname
    except ValueError:
        return None


# LLM: 缺字段或范围冲突不能生成默认资产；整批验证后才变更原集合。
# 函数用途: 验证单批 provider 结果并生成稳定资产标识。
def _provider_assets(result, domains, networks, targets, at):
    from ..gateway_parts.business_api_v13 import v13_schema
    if (type(result) is not dict or set(result) != {'assets', 'scan_ref', 'complete'}
            or type(result['assets']) is not list or type(result['complete']) is not bool
            or type(result['scan_ref']) is not str or not 1 <= len(result['scan_ref']) <= 256):
        raise BusinessError('HOST_NOT_READY')
    if len(result['assets']) > 100:
        raise BusinessError('LIMIT_EXCEEDED')
    target_domains = [parsed[1] for target in targets if (parsed := _target(target))[0] == 'domain']
    target_networks = [ipaddress.ip_network(target, strict=False) for target in targets
        if _target(target)[0] != 'domain']
    assets = {}
    for raw in result['assets']:
        if type(raw) is not dict or 'asset_id' in raw or 'first_seen_at' in raw:
            raise BusinessError('HOST_NOT_READY')
        try:
            row = validate_object({**raw, 'asset_id': 'asset-validation', 'first_seen_at': at},
                v13_schema({'$ref': '#/$defs/V13ExposedAsset'}))
        except HostAPIError:
            raise BusinessError('HOST_NOT_READY') from None
        host = _asset_host(row)
        if (host is None or not _contained(host, domains, networks)
                or not _contained(host, target_domains, target_networks)):
            raise BusinessError('BUSINESS_ACCESS_DENIED', details={'out_of_scope': [row['value']]})
        if row['scan_ref'] != result['scan_ref']:
            raise BusinessError('HOST_NOT_READY')
        _seen(row['last_seen_at'])
        if row['kind'] in {'domain', 'subdomain', 'ip'}:
            row['value'] = str(_target(row['value'])[1])
        row['asset_id'] = 'asset-' + hashlib.sha256(canonical_json([row['kind'], row['value']]).encode()).hexdigest()[:48]
        assets[row['asset_id']] = row
    return assets


# LLM: 原 Store 在回放前核指纹；扫描只发生在首次 mutate 中且最多调用 provider 一次。
# 函数用途: 先核管理员和目标范围，再有界扫描、验证并 upsert 同 owner 资产。
def scan_run(call):
    from ..gateway_parts.business_api_v13 import (
        v13_provider,
        v13_require_access,
        v13_require_admin,
        v13_resolve_space,
    )
    from .v13_store import v13_store_for
    v13_require_admin(call)
    provider = v13_provider(call, 'asset_discovery')
    if 'space_id' in call.params:
        v13_resolve_space(call)
    targets = []
    for value in call.params['targets']:
        parsed = _target(value)
        targets.append(str(parsed[1]) if parsed is not None else value)
    if not targets:
        raise BusinessError('INVALID_SCHEMA')
    targets = list(dict.fromkeys(targets))
    store = v13_store_for(call)

    # LLM: 范围在真正扫描前读取；提供者输出同样不能把关联的外部资产写入。
    # 函数用途: 在原幂等事务中执行单次扫描及同键资产更新。
    def mutate(records):
        v13_require_admin(call)
        domains, networks = _scope(provider)
        if not domains and not networks:
            raise BusinessError('HOST_NOT_READY')
        rejected = [target for target in targets if not _contained(target, domains, networks)]
        if rejected:
            raise BusinessError('BUSINESS_ACCESS_DENIED', details={'out_of_scope': rejected})
        result = provider.scan(targets=targets, ports=call.params.get('ports'), resume_ref=call.params.get('resume_ref'))
        at = datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()
        assets = _provider_assets(result, domains, networks, targets, at)
        for asset_id, row in assets.items():
            old = records.get(asset_id)
            if old is not None:
                v13_require_access(call, old)
            records[asset_id] = {**row, 'owner_id': call.owner_id,
                'first_seen_at': old['first_seen_at'] if old else at,
                'revision': old['revision'] + 1 if old else 1,
                'created_at': old['created_at'] if old else at, 'updated_at': at}
        return {'scan_ref': result['scan_ref'], 'complete': result['complete'],
            'assets_upserted': len(assets), 'scanned_targets': targets}

    return store.transaction('asset_discovery_assets', mutate, idempotency_key=call.idempotency_key,
        params_digest=hashlib.sha256(canonical_json(call.params).encode()).hexdigest())
