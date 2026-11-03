"""W11 能力包与合规治理动作：包源目录、静态内容清单、原生安装生命周期与合规证据。

本模块只做编排：包字节仍由原 plugin_package 校验器检查，安装/卸载/升级仍走
PluginManagement 的原安装表与命令生命周期，挂载仍走冻结的 spaces.packages.*，
导出的产物仍由原业务媒体上传口登记。包内合规声明按结构化路径约定读取（见
_compliance_mappings），不按自然语言文字推断控制项或状态。
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..conversation.business_access import BusinessError
from ..gateway_parts.business_api_v13 import (
    v13_call_frozen,
    v13_provider,
    v13_require_access,
    v13_resolve_space,
)
from ..gateway_parts.business_artifacts_http import upload_artifact
from ..plugin_host.errors import HostAPIError
from ..plugin_installation import PluginInstallationError
from ..plugin_management import PluginManagement, plugin_management_context
from ..plugin_manifest import PluginPackageError
from ..plugin_package import HOST_PACKAGE_LIMITS, inspect_plugin_package, read_zip_member
from ..user_space.owner_access import is_complete_local_admin_owner
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .v13_store import v13_store_for

# 包内合规声明只认这个目录前缀和固定三段路径，别处的文件不参与合规控制项目录。
_COMPLIANCE_PREFIX = 'compliance/'
# 包内内容按一级目录归属，未登记的目录不猜成任何资产类型。
_CONTENT_BUCKETS = {'skills': 'skills', 'workflows': 'workflows', 'scripts': 'scripts',
                    'rules': 'detection_rules', 'detections': 'detection_rules'}
# 合规映射只能引用这四类运行资产，与 V13ControlMapping 的枚举一致。
_ASSET_KINDS = ('rule', 'workflow', 'script', 'skill')


# LLM: 只做稳定 JSON 指纹，不解析业务语义；用于幂等参数绑定与确定性证据 ID。
# 函数用途: 生成键序无关的 SHA-256 摘要。
def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


# LLM: 时间只来自注入时钟，输出可排序 UTC 时间，不读展示时间作为执行事实。
# 函数用途: 生成一次调用内的统一时间戳。
def _timestamp(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: 只按结构化 owner 身份重建规范 home，不信任调用方传入目录字符串。
# 函数用途: 由宿主 home 上的 owner 三要素还原 OwnerIdentity。
def _owner_identity(paths):
    provider = getattr(paths, 'owner_provider', None)
    kind = getattr(paths, 'owner_kind', None)
    owner_id = getattr(paths, 'owner_id', None)
    if not all(isinstance(value, str) and value.strip() for value in (provider, kind, owner_id)):
        raise HostAPIError('HOST_NOT_READY', 503)
    if provider == 'local' and kind == 'main':
        return OwnerIdentity.local_main()
    owner_id = owner_id.rsplit('/', 1)[-1]
    if provider == 'local' and kind == 'user':
        return OwnerIdentity.local_user(owner_id)
    if provider == 'local' and kind == 'group':
        return OwnerIdentity.provider_group(provider, owner_id)
    if kind == 'group':
        return OwnerIdentity.provider_group(provider, owner_id)
    if kind == 'user':
        return OwnerIdentity.provider_user(provider, owner_id)
    raise HostAPIError('HOST_NOT_READY', 503)


# LLM: 本机管理员按原owner身份判定；耐久操作的会话绑定不随网络重试换request_id。
# 函数用途: 按现有 /client/plugins 路由的同一做法组装 PluginManagement。
def _management(call):
    host = getattr(call, 'services', None)
    host = getattr(host, 'host', None)
    store = getattr(host, 'conversation_store', None)
    paths = getattr(host, 'home_paths', None)
    if store is None or paths is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    owner = resolve_owner_home(paths.root, _owner_identity(paths))
    if Path(owner.home_dir) != Path(paths.owner_home_dir):
        raise HostAPIError('HOST_NOT_READY', 503)
    conversation = 'v13-package-' + _digest([call.owner_id, call.subject_id,
        call.action, call.idempotency_key or call.request_id])[:32]
    context = plugin_management_context(owner, paths, host.config, store.threads,
        actor_id=call.subject_id, channel='business', conversation_id=conversation,
        is_admin=is_complete_local_admin_owner(paths))
    return PluginManagement(context)


# LLM: 安装表是唯一安装事实；同名只按 plugin_id 精确匹配，不按展示名猜。
# 函数用途: 在安装快照中查找本 owner 的包记录。
def _installed_entry(manager, package_id):
    return next((row for row in manager.installations.snapshot()
                 if row.manifest.plugin_id == package_id), None)


# LLM: 来源字节必须通过原静态校验并绑定请求ID/版本，合法的另一包同样拒绝。
# 函数用途: 从包源取一份包字节。
def _fetch_package(call, package_id, version):
    provider = v13_provider(call, 'package_source')
    try:
        raw = provider.fetch(package_id, version)
    except (LookupError, KeyError) as error:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from error
    except (OSError, ValueError) as error:
        raise HostAPIError('HOST_NOT_READY', 503) from error
    if not isinstance(raw, bytes):
        raise HostAPIError('HOST_NOT_READY', 503)
    snapshot = _snapshot(raw)
    if snapshot.manifest.plugin_id != package_id or snapshot.manifest.version != version:
        raise HostAPIError('HOST_NOT_READY', 503)
    return raw


# LLM: 只存结果回执，不复制安装权威；原Store绑定主体/输入，并在副作用前后实时复核。
# 函数用途: 幂等执行一次包生命周期编排。
def _package_receipted(call, mutate):
    v13_require_access(call)

    # LLM: 相同回执重放及异参拒绝都在实际操作之前完成。
    # 函数用途: 在原生耐久事务内执行尚未完成的操作。
    def apply(records):
        v13_require_access(call)
        return mutate(call)

    return v13_store_for(call).transaction('package_operations', apply,
        idempotency_key=call.idempotency_key,
        params_digest=_digest([call.action, call.params, call.expected_revision]))


# LLM: 原生安装表操作身份绑定当前主体与规范动作，不跨主体或动作碰撞。
# 函数用途: 生成稳定的原生包操作编号。
def _operation_id(call):
    return 'v13-' + _digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])[:40]


# LLM: 静态检查走宿主同一个读包校验器；坏包属于来源故障而不是空成功。
# 函数用途: 校验包字节并返回不可变快照。
def _snapshot(raw):
    try:
        return inspect_plugin_package(raw, limits=HOST_PACKAGE_LIMITS)
    except PluginPackageError as error:
        raise HostAPIError('HOST_NOT_READY', 503) from error


# LLM: 只按结构化包源协议读取目录；缺规范字段属于来源故障，不用默认值伪造条目。
# 函数用途: 读取并规范化一页包目录。
def _index_page(call, *, query, kind, cursor, limit):
    provider = v13_provider(call, 'package_source')
    try:
        page = provider.index(query=query, kind=kind, cursor=cursor, limit=limit)
    except (OSError, ValueError, TypeError) as error:
        raise HostAPIError('HOST_NOT_READY', 503) from error
    if not isinstance(page, dict) or not isinstance(page.get('items'), list):
        raise HostAPIError('HOST_NOT_READY', 503)
    next_cursor = page.get('next_cursor')
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise HostAPIError('HOST_NOT_READY', 503)
    return page['items'], next_cursor


# LLM: 目录条目只投影冻结字段；kind/框架按合同枚举过滤，缺 package_id 即来源故障。
# 函数用途: 把包源条目投影为 V13PackageIndexItem 形状。
def _index_item(row, installed_versions):
    if not isinstance(row, dict) or not isinstance(row.get('package_id'), str) or not row['package_id']:
        raise HostAPIError('HOST_NOT_READY', 503)
    package_id = row['package_id']
    versions = [item for item in row.get('versions') or [] if isinstance(item, str) and item]
    latest = row.get('latest_version')
    if not isinstance(latest, str) or not latest:
        latest = versions[-1] if versions else ''
    if not latest:
        raise HostAPIError('HOST_NOT_READY', 503)
    kind = row.get('kind') if row.get('kind') in ('capability_pack', 'plugin') else 'capability_pack'
    frameworks = [item for item in row.get('compliance_frameworks') or []
                  if isinstance(item, str) and item and item[0].islower()
                  and item.replace('_', '').isalnum()][:20]
    summary = row.get('summary')
    title = row.get('title')
    return {'package_id': package_id, 'title': title if isinstance(title, str) and title else package_id,
            'summary': summary if isinstance(summary, str) else '', 'kind': kind,
            'versions': versions[:100] if versions else [latest], 'latest_version': latest,
            'compliance_frameworks': frameworks,
            'installed_version': installed_versions.get(package_id)}


# LLM: 已安装版本只来自安装表，不把目录条目自报的版本当安装事实。
# 函数用途: 汇总本 owner 已安装包到版本映射。
def _installed_versions(manager):
    result = {}
    for row in manager.installations.snapshot():
        result.setdefault(row.manifest.plugin_id, row.manifest.version)
    return result


# LLM: 包内条目名只取自路径本身，摘要留空而不是从文件正文猜用途。
# 函数用途: 为一个内容条目生成稳定名称。
def _entry_name(bucket, rest):
    if bucket == 'skills':
        return rest.split('/', 1)[0]
    return rest.rsplit('/', 1)[-1].rsplit('.', 1)[0] or rest


# LLM: 只读包内已声明成员的字节，预算沿用宿主读包上限，不解压到磁盘或执行。
# 函数用途: 读取归档中一个已声明成员。
def _member_bytes(snapshot, path):
    with zipfile.ZipFile(io.BytesIO(snapshot.archive_bytes)) as archive:
        return read_zip_member(archive, archive.getinfo(path), HOST_PACKAGE_LIMITS.member_bytes)


# LLM: 合规控制项只认包内 compliance/<framework>/<control>.json 的资产声明；缺声明就不产生控制项。
#   该文件只允许 title/family/assets，assets 每项只允许 asset_kind/asset_ref，坏声明返回不完整。
# 函数用途: 解析包内结构化合规映射声明。
def _compliance_mappings(snapshot):
    mappings, complete = [], True
    for item in snapshot.manifest.files:
        path = item.path
        if not path.startswith(_COMPLIANCE_PREFIX) or not path.endswith('.json'):
            continue
        parts = path.split('/')
        if len(parts) != 3:
            complete = False
            continue
        framework, control_id = parts[1], parts[2][:-5]
        if (not framework or not framework[0].islower() or not framework.replace('_', '').isalnum()
                or not control_id):
            complete = False
            continue
        try:
            payload = json.loads(_member_bytes(snapshot, path))
        except (KeyError, ValueError, TypeError, PluginPackageError):
            complete = False
            continue
        if not isinstance(payload, dict) or set(payload) - {'title', 'family', 'assets'}:
            complete = False
            continue
        assets = payload.get('assets')
        if not isinstance(assets, list):
            complete = False
            continue
        title, family = payload.get('title'), payload.get('family')
        for asset in assets:
            if (not isinstance(asset, dict) or set(asset) != {'asset_kind', 'asset_ref'}
                    or asset['asset_kind'] not in _ASSET_KINDS
                    or not isinstance(asset['asset_ref'], str) or not asset['asset_ref']):
                complete = False
                continue
            mappings.append({'framework': framework, 'control_id': control_id,
                             'title': title if isinstance(title, str) and title else control_id,
                             'family': family if isinstance(family, str) and family else None,
                             'asset_kind': asset['asset_kind'], 'asset_ref': asset['asset_ref']})
    return mappings, complete


# LLM: 内容清单只按一级目录归属，未声明目录不猜资产；合规映射来自包内结构化声明。
# 函数用途: 把包快照投影成 V13PackageContents 与声明完整性。
def _contents(snapshot):
    grouped = {'skills': [], 'workflows': [], 'scripts': [], 'detection_rules': []}
    seen = set()
    for item in snapshot.manifest.files:
        first, _, rest = item.path.partition('/')
        bucket = _CONTENT_BUCKETS.get(first)
        if bucket is None or not rest or item.path in seen:
            continue
        seen.add(item.path)
        grouped[bucket].append({'ref': item.path, 'name': _entry_name(bucket, rest), 'summary': ''})
    for name in snapshot.manifest.skills:
        ref = 'skills/' + name + '/SKILL.md'
        if ref not in seen:
            seen.add(ref)
            grouped['skills'].append({'ref': ref, 'name': name, 'summary': ''})
    mappings, complete = _compliance_mappings(snapshot)
    return {'skills': grouped['skills'][:500], 'workflows': grouped['workflows'][:500],
            'scripts': grouped['scripts'][:500], 'detection_rules': grouped['detection_rules'][:1000],
            'compliance_mappings': [{key: row[key] for key in
                ('framework', 'control_id', 'asset_kind', 'asset_ref')} for row in mappings[:2000]]}, complete


# LLM: 控制项目录只来自已安装包的结构化声明；读不出的安装包如实降为部分覆盖。
# 函数用途: 汇总某框架的控制项及其映射资产。
def _control_catalog(manager, framework):
    catalog, complete = {}, True
    for entry in manager.installations.snapshot():
        try:
            raw = manager.installations.package_bytes(entry)
            snapshot = inspect_plugin_package(raw, limits=HOST_PACKAGE_LIMITS)
        except (PluginInstallationError, PluginPackageError):
            complete = False
            continue
        mappings, declared = _compliance_mappings(snapshot)
        complete = complete and declared
        for row in mappings:
            if row['framework'] != framework:
                continue
            control = catalog.setdefault(row['control_id'],
                {'title': row['title'], 'family': row['family'], 'mappings': []})
            control['mappings'].append({key: row[key] for key in
                ('framework', 'control_id', 'asset_kind', 'asset_ref')})
    return catalog, ('complete' if complete else 'partial')


# LLM: 运行事实只从已授权端口读取；规则读 v13 集合、流程读冻结 workflows.get，其余无权威端口即未知。
# 函数用途: 评估单个控制项映射资产的真实启用状态。
def _control_facts(call, control, space_id):
    facts, readable, enabled = [], 0, 0
    store = None
    for mapping in control['mappings']:
        kind, ref = mapping['asset_kind'], mapping['asset_ref']
        state = None
        if kind == 'rule':
            if store is None:
                store = v13_store_for(call)
            row = store.get('monitor_rules', ref)
            if row is not None and (space_id is None or row.get('space_id') == space_id):
                state = row.get('enabled') is True
        elif kind == 'workflow':
            try:
                data = v13_call_frozen(call, 'workflows.get', {'workflow_ref': ref})
            except HostAPIError:
                data = None
            workflow = data.get('workflow') if isinstance(data, dict) else None
            if isinstance(workflow, dict):
                state = workflow.get('enabled') is True
        facts.append({'asset_kind': kind, 'asset_ref': ref, 'enabled': state})
        if state is not None:
            readable += 1
            enabled += 1 if state else 0
    total = len(control['mappings'])
    if not total or not readable:
        status = 'unknown'
    elif enabled == total:
        status = 'met'
    elif enabled:
        status = 'partial'
    elif readable == total:
        status = 'unmet'
    else:
        status = 'unknown'
    return status, {'assets': facts}


# LLM: 控制项状态与证据计数来自同一份映射和当前 owner 的证据集合，不读展示缓存。
# 函数用途: 组装某个框架的控制项状态与汇总。
def _status_data(call, framework, space_id):
    manager = _management(call)
    catalog, coverage = _control_catalog(manager, framework)
    if not catalog:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    store = v13_store_for(call)
    evidence = [row for row in store.list('compliance_evidence')
                if row.get('framework') == framework
                and (space_id is None or row.get('space_id') == space_id)]
    grouped = {}
    for row in evidence:
        grouped.setdefault(row.get('control_id'), []).append(row)
    controls, summary = [], {'met': 0, 'partial': 0, 'unmet': 0, 'unknown': 0}
    for control_id in sorted(catalog):
        control = catalog[control_id]
        rows = sorted(grouped.get(control_id, []), key=lambda row: str(row.get('collected_at') or ''))
        latest = rows[-1] if rows else None
        status = latest.get('status') if latest else 'unknown'
        if status not in summary:
            status = 'unknown'
        summary[status] += 1
        controls.append({'control_id': control_id, 'title': control['title'],
                         'family': control['family'], 'status': status,
                         'evidence_count': len(rows),
                         'last_collected_at': latest.get('collected_at') if latest else None,
                         'mapped_assets': control['mappings']})
    return {'framework': framework, 'controls': controls, 'summary': summary, 'coverage': coverage}


# LLM: 包目录只投影宿主包源与本 owner 安装表，缺包源时 ready 已拒；游标契约沿用原冻结 Cursor。
# 函数用途: 返回 GET /marketplace/index.json 的能力包目录。
def index(call):
    limit = call.params.get('limit', 50)
    rows, next_cursor = _index_page(call, query=call.params.get('query', ''),
        kind=call.params.get('kind'), cursor=call.params.get('cursor'), limit=limit)
    installed = _installed_versions(_management(call))
    return {'items': [_index_item(row, installed) for row in rows][:limit],
            'next_cursor': next_cursor, 'coverage': 'partial' if next_cursor else 'complete'}


# LLM: 已安装优先读原安装表字节，未安装才向包源取同一版本；两者都走宿主静态校验器，不执行包内容。
# 函数用途: 返回一个包的结构化内容清单。
def inspect(call):
    package_id, version = call.params['package_id'], call.params.get('version')
    manager = _management(call)
    entry = _installed_entry(manager, package_id)
    if entry is not None and (version is None or version == entry.manifest.version):
        snapshot = _snapshot(manager.installations.package_bytes(entry))
    else:
        if version is None:
            rows, _next = _index_page(call, query=package_id, kind=None, cursor=None, limit=100)
            match = next((row for row in rows if isinstance(row, dict)
                          and row.get('package_id') == package_id), None)
            if match is None:
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            version = _index_item(match, {})['latest_version']
        snapshot = _snapshot(_fetch_package(call, package_id, version))
    contents, _complete = _contents(snapshot)
    installed = entry is not None and entry.manifest.version == snapshot.manifest.version
    return {'package_id': package_id, 'version': snapshot.manifest.version, 'installed': installed,
            'manifest': snapshot.manifest.to_payload(), 'contents': contents}


# LLM: 安装仍由原安装表按 operation_id 幂等；同版本重放复读原事实，不重复写安装表或再挂载。
# 函数用途: 安装一个包并按需一键挂载到当前业务空间。
def install(call):
    return _package_receipted(call, _install_once)


# LLM: 空间先授权后安装，字节身份由fetch复核，安装与挂载仍走原生端口。
# 函数用途: 执行一次尚未完成的安装和挂载。
def _install_once(call):
    package_id, version = call.params['package_id'], call.params['version']
    space_id, mount = call.params.get('space_id'), call.params.get('mount', True)
    space = v13_resolve_space(call, space_id) if space_id else None
    manager = _management(call)
    entry = _installed_entry(manager, package_id)
    if entry is not None:
        if entry.manifest.version != version:
            raise HostAPIError('STATE_CONFLICT', 409)
        snapshot = _snapshot(manager.installations.package_bytes(entry))
        revision = entry.revision
    else:
        raw = _fetch_package(call, package_id, version)
        snapshot = _snapshot(raw)
        v13_require_access(call)
        revision = manager.install_package_bytes(raw, operation_id=_operation_id(call),
                                                 expected_revision=0).installation.revision
    mounted = None
    if space_id and mount:
        space = v13_resolve_space(call, space_id)
        v13_call_frozen(call, 'spaces.packages.attach',
                        {'space_id': space['space_id'], 'package_refs': [package_id]},
                        expected_revision=space['revision'],
                        idempotency_key=_operation_id(call) + '-attach')
        mounted = space['space_id']
    contents, _complete = _contents(snapshot)
    return {'package_id': package_id, 'version': snapshot.manifest.version,
            'installation_revision': revision, 'mounted_space_id': mounted, 'contents': contents}


# LLM: 先摘除所有真实挂载再走原卸载命令，命令不确认就不声称已卸载；全部空间来自冻结 spaces 视图。
# 函数用途: 卸载一个已安装包并返回被摘除的空间。
def uninstall(call):
    return _package_receipted(call, _uninstall_once)


# LLM: 完整枚举且有界拒绝截断，稳定空间键摘除后检查原生卸载回执。
# 函数用途: 执行一次尚未完成的卸载编排。
def _uninstall_once(call):
    package_id = call.params['package_id']
    manager = _management(call)
    if _installed_entry(manager, package_id) is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    detached, revisions, cursor = [], {}, None
    for _page in range(20):
        params = {'limit': 100}
        if cursor:
            params['cursor'] = cursor
        page = v13_call_frozen(call, 'spaces.list', params)
        for row in page['items']:
            if any(item.get('package_ref') == package_id for item in row.get('mounts') or ()):
                detached.append(row['space_id'])
                revisions[row['space_id']] = row['revision']
        cursor = page.get('next_cursor')
        if not cursor:
            break
    if cursor or len(detached) > 100:
        raise BusinessError('LIMIT_EXCEEDED')
    for space_id in detached:
        v13_call_frozen(call, 'spaces.packages.detach',
                        {'space_id': space_id, 'package_refs': [package_id]},
                        expected_revision=revisions[space_id],
                        idempotency_key=_operation_id(call) + '-' + _digest(space_id)[:16])
    v13_require_access(call)
    current, result = manager.package_lifecycle(package_id, 'uninstall',
                                                 operation_id=_operation_id(call))
    if current is not None or not isinstance(result, dict) or result.get('state') != 'succeeded':
        raise HostAPIError('STATE_CONFLICT', 409)
    return {'package_id': package_id, 'uninstalled': True, 'detached_spaces': detached}


# LLM: 升级必须携带当前安装版本，原生安装表再做同一 CAS；版本相同不重复写安装表。
# 函数用途: 把一个已安装包升级到目标版本。
def upgrade(call):
    return _package_receipted(call, _upgrade_once)


# LLM: 先重放原键回执，再对新操作按原安装服务CAS与manifest身份裁决。
# 函数用途: 执行一次尚未完成的包升级。
def _upgrade_once(call):
    package_id, version = call.params['package_id'], call.params['version']
    manager = _management(call)
    entry = _installed_entry(manager, package_id)
    if entry is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if call.expected_revision != entry.revision:
        raise BusinessError('REVISION_CONFLICT')
    if entry.manifest.version == version:
        return {'package_id': package_id, 'from_version': version, 'version': version,
                'installation_revision': entry.revision}
    raw = _fetch_package(call, package_id, version)
    _snapshot(raw)
    v13_require_access(call)
    updated = manager.install_package_bytes(raw, operation_id=_operation_id(call),
                                            expected_revision=call.expected_revision)
    return {'package_id': package_id, 'from_version': entry.manifest.version,
            'version': updated.installation.manifest.version,
            'installation_revision': updated.installation.revision}


# LLM: 控制项目录只来自已安装包声明；框架无人声明时 404，不返回空热图冒充已覆盖。
# 函数用途: 返回某合规框架的控制项热图与汇总。
def compliance_status(call):
    data = _status_data(call, call.params['framework'], call.params.get('space_id'))
    return {'framework': data['framework'], 'controls': data['controls'],
            'summary': data['summary'], 'coverage': data['coverage']}


# LLM: 证据写入限定在 v13 证据集合，同一键重放返回原回执；控制项状态只来自当前资产事实。
# 函数用途: 采集一次某框架的合规证据快照。
def compliance_collect(call):
    framework, space_id = call.params['framework'], call.params.get('space_id')
    manager = _management(call)
    catalog, coverage = _control_catalog(manager, framework)
    if not catalog:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    evaluated = {control_id: _control_facts(call, control, space_id)
                 for control_id, control in sorted(catalog.items())}
    collected_at = _timestamp(call)
    store = v13_store_for(call)

    # LLM: 单笔事务内按确定性证据 ID 更新，重放不重复计数；状态和事实都参与变更判断。
    # 函数用途: 写入该框架全部控制项的证据记录并返回采集回执。
    def mutate(rows):
        changed = 0
        for control_id, (status, facts) in evaluated.items():
            evidence_id = 'evidence-' + _digest([call.owner_id, framework, space_id, control_id])[:32]
            row = rows.get(evidence_id)
            if row is None:
                rows[evidence_id] = {'evidence_id': evidence_id, 'owner_id': call.owner_id,
                    'space_id': space_id, 'framework': framework, 'control_id': control_id,
                    'status': status, 'facts': facts, 'collected_at': collected_at, 'revision': 0,
                    'created_at': collected_at, 'updated_at': collected_at}
                changed += 1
            elif row.get('status') != status or row.get('facts') != facts:
                row.update(status=status, facts=facts, collected_at=collected_at,
                           updated_at=collected_at, revision=row.get('revision', 0) + 1)
                changed += 1
        return {'framework': framework, 'collected': len(evaluated), 'controls_updated': changed,
                'collected_at': collected_at, 'coverage': coverage}

    return store.transaction('compliance_evidence', mutate, idempotency_key=call.idempotency_key,
                             params_digest=_digest([framework, space_id]))


# LLM: 导出正文只由同一状态投影生成，确定性序列化便于核对摘要；产物仍由原媒体上传口登记。
# 函数用途: 把某框架的合规状态导出为 json/csv/html 产物。
def compliance_export(call):
    framework, format_name = call.params['framework'], call.params['format']
    space_id = call.params.get('space_id') or _default_space(call)
    data = _status_data(call, framework, space_id if call.params.get('space_id') else None)
    rows = [{'control_id': row['control_id'], 'title': row['title'],
             'family': row['family'] or '', 'status': row['status'],
             'evidence_count': row['evidence_count'],
             'last_collected_at': row['last_collected_at'] or ''} for row in data['controls']]
    payload, name = _export_payload(framework, format_name, rows, data['summary'])
    view = upload_artifact(io.BytesIO(payload),
        {'space_id': space_id, 'idempotency_key': call.idempotency_key}, name, call.context, call.services)
    return {'export': {'artifact_id': view['artifact_id'], 'version_ref': view['version_ref'],
                       'name': view['name'], 'mime_type': view['mime_type'],
                       'size_bytes': view['size_bytes'], 'content_sha256': view['content_sha256'],
                       'format': format_name}}


# LLM: 导出需要一个原业务空间承载产物；未指定时按冻结 spaces 视图取排序后的第一个真实空间。
# 函数用途: 解析导出产物所属空间。
def _default_space(call):
    page = v13_call_frozen(call, 'spaces.list', {'limit': 1})
    if not page['items']:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return page['items'][0]['space_id']


# LLM: 三种格式都由同一行集合确定性生成，不按正文猜状态；文件名只含框架与扩展名。
# 函数用途: 生成导出字节与展示文件名。
def _export_payload(framework, format_name, rows, summary):
    name = 'compliance-' + framework + '.' + format_name
    if format_name == 'json':
        body = json.dumps({'framework': framework, 'summary': summary, 'controls': rows},
                          ensure_ascii=False, sort_keys=True, indent=2)
        return (body + '\n').encode('utf-8'), name
    if format_name == 'csv':
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=['control_id', 'title', 'family', 'status',
                                                    'evidence_count', 'last_collected_at'])
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().encode('utf-8'), name
    cells = ''.join('<tr>' + ''.join('<td>' + html.escape(str(row[key])) + '</td>'
        for key in ('control_id', 'title', 'family', 'status', 'evidence_count', 'last_collected_at'))
        + '</tr>' for row in rows)
    body = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>'
        + html.escape(framework) + '</title></head><body><table><thead><tr>'
        + ''.join('<th>' + html.escape(key) + '</th>' for key in
                  ('control_id', 'title', 'family', 'status', 'evidence_count', 'last_collected_at'))
        + '</tr></thead><tbody>' + cells + '</tbody></table></body></html>')
    return body.encode('utf-8'), name
