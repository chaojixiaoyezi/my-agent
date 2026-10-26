# LLM: 投递只冻结发送方canonical资产的原始字节，先扫描再调用任何描述符clean_json；不执行代码、不物化存储或复制凭据库。
# 模块用途: 为跨用户投递提供同根只读冻结、原始依赖发现和规范化负载，不建立平行资产存储。
from __future__ import annotations

import base64
import json
import os
import stat
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from ..capability.skills import split_skill_frontmatter
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import list_names_beneath, open_directory_beneath, read_bytes_beneath
from . import canonical_skill_name, safe_asset_id, skill_asset_id
from .delivery_contracts import MAX_BUNDLE_BYTES, AssetDeliveryError
from .delivery_sanitizer import datasource_secret_values, sanitize_bundle_files
from .descriptor import AssetDescriptor, AssetRefs, RuntimeSummary
from .topology import AssetClosureError, extract_asset_closure
from .workflow_store import WorkflowDefinition, with_workflow_step_refs
from .workspace_assets import _DESCRIPTOR_FIELDS, _read_lock, _resource_parts, _strict_skill_paths


# LLM: 固定JSON编码拒绝NaN；只用于已扫描内存内容，不触发IO或额外脱敏。
# 函数用途: 为投递文件和摘要生成确定性字节。
def delivery_json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


# LLM: 原始JSON只解析而不clean，缺失/坏对象失败关闭，路径只来自canonical相对组件。
# 函数用途: 在秘密扫描之前读取资产的未经脱敏元数据。
def _raw_object(home, parts):
    raw = read_bytes_beneath(home, parts, max_bytes=MAX_BUNDLE_BYTES)
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise AssetDeliveryError('ASSET_SOURCE_INVALID', '资产元数据必须是对象')
    return value


# LLM: 只收集发送方自己的脚本/工作流/Skill/监控；目录身份和种类由实际位置决定，正文不能重定位。
# 函数用途: 收集原始资产索引以计算待扫描的精确可达集合。
def _raw_catalog(home):
    records = {}
    for kind in ('script', 'workflow'):
        for identifier in list_names_beneath(home, (kind + 's',)):
            if identifier.startswith('.'):
                continue
            safe_asset_id(identifier)
            parts = (kind + 's', identifier)
            meta = _raw_object(home, (*parts, kind + '.json'))
            if meta is not None:
                if meta.get('id') != identifier or meta.get('kind') != kind:
                    raise AssetDeliveryError('ASSET_SOURCE_INVALID', '资产身份不匹配')
                records[f'{kind}:{identifier}'] = {'kind': kind, 'id': identifier, 'parts': parts, 'meta': meta}
    for path in _strict_skill_paths(home):
        parts = path.parent.relative_to(home).parts
        raw = read_bytes_beneath(home, (*parts, 'SKILL.md'), max_bytes=MAX_BUNDLE_BYTES)
        header = split_skill_frontmatter(raw.decode())[0]
        meta = {**header, **(_raw_object(home, (*parts, 'skill_meta.json')) or {})}
        identifier = skill_asset_id(path.parent.name) if skill_asset_id(path.parent.name) != path.parent.name else meta.get('id', path.parent.name)
        safe_asset_id(identifier)
        records[f'skill:{identifier}'] = {'kind': 'skill', 'id': identifier, 'parts': parts, 'meta': meta}
    for name in list_names_beneath(home, ('data', 'monitors')):
        if name.endswith('.json'):
            identifier = safe_asset_id(name[:-5])
            meta = _raw_object(home, ('data', 'monitors', name))
            if meta.get('id') != identifier or meta.get('kind') != 'monitor':
                raise AssetDeliveryError('ASSET_SOURCE_INVALID', '监控身份不匹配')
            records[f'monitor:{identifier}'] = {'kind': 'monitor', 'id': identifier,
                'parts': ('data', 'monitors'), 'filename': name, 'meta': meta}
    _merge_scheduler_monitors(home, records)
    return records


# LLM: 调度器原始JSON只读不clean；与绑定按准确ID合并，deleted不复活；引用来自明确动作字段而非prompt。
# 函数用途: 把仅存Scheduler及双存的监控纳入同一个投递索引。
def _merge_scheduler_monitors(home, records):
    from .monitor_adapter import _scheduler_skill_ids, _scheduler_tool_asset_ids

    jobs = (_raw_object(home, ('data', 'scheduler', 'store.json')) or {}).get('jobs', {})
    for identifier, job in jobs.items():
        safe_asset_id(identifier)
        if job.get('job_id') != identifier:
            raise AssetDeliveryError('ASSET_SOURCE_INVALID', 'Scheduler监控身份不匹配')
        key = f'monitor:{identifier}'
        if job.get('status') == 'deleted':
            records.pop(key, None)
            continue
        binding = records.get(key, {}).get('meta', {})
        refs = dict(binding.get('refs', {}))
        for kind, values in _scheduler_tool_asset_ids(job).items():
            refs[kind] = list(dict.fromkeys([*refs.get(kind, ()), *values]))
        refs['skills'] = list(dict.fromkeys([*refs.get('skills', ()), *_scheduler_skill_ids(job.get('skill_refs', ()))]))
        schedule = job.get('schedule') or next((rule['schedule'] for rule in job.get('rules', ())), None)
        meta = {'id': identifier, 'kind': 'monitor', 'name': job.get('name') or identifier, **binding,
            'refs': refs, 'scheduler_job': job, 'schedule_spec': schedule or binding.get('schedule_spec', {})}
        records[key] = {'kind': 'monitor', 'id': identifier, 'meta': meta, 'parts': ('data', 'monitors')}


# LLM: 仅读结构化refs/steps，不解释正文或运行工具；工作流脚本和Skill边与原with_workflow_step_refs一致。
# 函数用途: 在描述符脱敏前提取要连带扫描的资产键。
def _raw_refs(record):
    refs = dict(record['meta'].get('refs', {}))
    if record['kind'] == 'workflow':
        steps = record['meta'].get('steps', ())
        refs.update(scripts=[step['script_id'] for step in steps if step.get('script_id')],
            skills=[canonical_skill_name(reference) for step in steps for reference in step.get('skill_ids', ())])
    return [f'{kind}:{safe_asset_id(identifier)}' for kind in ('script', 'workflow', 'skill', 'monitor')
        for identifier in refs.get(kind + 's', ())]


# LLM: 仅原始元数据遍历，先确定精确可达集合才扫描；缺依赖用投递领域码及missing_refs贯通服务和工具。
# 函数用途: 在不调用clean_json的前提下求取待冻结源集合，缺依赖时给出准确诊断。
def _raw_reachable(records, root_key, include_dependencies):
    if root_key not in records:
        raise AssetDeliveryError('ASSET_NOT_FOUND', f'根资产不存在: {root_key}', missing_refs=[root_key])
    seen, pending, missing = set(), [root_key], []
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        if key not in records:
            missing.append(key)
        elif include_dependencies:
            pending.extend(_raw_refs(records[key]))
    if missing:
        raise AssetDeliveryError('ASSET_DELIVERY_DEPENDENCY_MISSING',
            'Asset dependency closure is incomplete', missing_refs=sorted(missing))
    return [records[key] for key in sorted(seen)]


# LLM: 单链接普通文件沿root-relative nofollow读取，先检查累计预算；不导出锁、恢复记录、隐藏宿主配置或凭据密钥。
# 函数用途: 冻结一份资产的所有允许资源，包括未知扩展的二进制附件。
def _raw_files(home, record):
    if record['kind'] == 'monitor':
        return {'monitor.json': delivery_json(record['meta'])}
    files, pending, size = {}, [()], 0
    while pending:
        relative = pending.pop()
        for name in list_names_beneath(home, (*record['parts'], *relative)):
            if name.startswith('.') or name.endswith('.lock') or name == 'asset-transaction.json':
                continue
            parts = (*relative, name)
            _resource_parts('/'.join(parts))
            info = os.stat(home.joinpath(*record['parts'], *parts), follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                pending.append(parts)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AssetDeliveryError('ASSET_RESOURCE_INVALID', '投递资源必须为普通单链接文件')
            size += info.st_size
            if size > MAX_BUNDLE_BYTES:
                raise AssetDeliveryError('ASSET_DELIVERY_QUOTA_EXCEEDED', '投递包超过字节上限')
            files['/'.join(parts)] = read_bytes_beneath(home, (*record['parts'], *parts), max_bytes=MAX_BUNDLE_BYTES)
    return files


# LLM: 先前原始字节已扫描；这里只清除发送方执行态、凭据引用和会话目标，不赋予接收方任何授权。
# 函数用途: 将资产描述符归一成可安全复制的静态定义。
def _portable_descriptor(record, meta):
    values = {key: value for key, value in meta.items() if key in _DESCRIPTOR_FIELDS}
    if record['kind'] == 'skill':
        values.update(id=record['id'], kind='skill', name=meta.get('name') or record['id'],
            summary=meta.get('description', meta.get('summary', '')), metadata={**meta.get('metadata', {}),
                **{key: meta[key] for key in ('tools_required', 'risk_level') if key in meta}})
    descriptor = AssetDescriptor.from_dict(values)
    return replace(descriptor, runtime_summary=RuntimeSummary(), used_by=AssetRefs(), credential_refs=(),
        revision=0, scope_layer='owner', source_scope='owner', workspace_root='')


# LLM: 私有目标只从结构化字段剥离，不按prompt文案推断；enabled初态由接收决定，源文件仅供完整性验收。
# 函数用途: 清除发送方会话、任务和通知绑定。
def _without_private_bindings(value):
    excluded = {'target_conversation_id', 'target_chat_id', 'thread_id', 'source_thread_id', 'source_task_id',
        'source_request_id', 'notification_binding', 'notification_binding_history', 'last_run_id'}
    if isinstance(value, dict):
        return {key: _without_private_bindings(item) for key, item in value.items() if key not in excluded}
    if isinstance(value, list):
        return [_without_private_bindings(item) for item in value]
    return value


# LLM: 解码只消费已扫描冻结字节，不重读源；规范定义沿原schema，数据源规格保留而凭据库不在源范围。
# 函数用途: 生成可再次校验、原目录安装的单个投递项。
def _normalize_export(record, files):
    kind = record['kind']
    filename = kind + '.json' if kind != 'skill' else 'skill_meta.json'
    if kind == 'skill':
        header = split_skill_frontmatter(files['SKILL.md'].decode())[0]
        meta = {**header, **(json.loads(files[filename]) if filename in files else {})}
    else:
        meta = json.loads(files[filename])
    descriptor = _portable_descriptor(record, meta)
    required_grants = sorted({binding['credential_ref'] for binding in meta.get('credential_refs', ())}
        | set(descriptor.metadata.get('required_grants', ())))
    if kind == 'workflow':
        definition = with_workflow_step_refs(WorkflowDefinition.from_any({'descriptor': descriptor.to_dict(),
            'steps': meta['steps'], 'body_markdown': meta.get('body_markdown', '')}))
        descriptor = definition.descriptor
        meta = descriptor.to_dict() | {'steps': [step.to_dict() for step in definition.steps], 'body_markdown': definition.body_markdown}
    elif kind == 'script':
        meta = descriptor.to_dict() | {key: meta[key] for key in ('entrypoint', 'interpreter', 'timeout_seconds')}
    elif kind == 'monitor':
        meta = descriptor.to_dict() | {'schedule_spec': meta.get('schedule_spec', {}),
            'scheduler_job': _portable_monitor_job(meta.get('scheduler_job', {}))}
    else:
        meta = descriptor.to_dict()
    files = dict(files)
    files[filename] = delivery_json(_without_private_bindings(meta))
    return {'descriptor': descriptor, 'files': files, 'required_grants': required_grants}


# LLM: 已扫描原始job才筛选静态定义；不携带owner、creator、来源线程、运行游标或通知目标，规则也剔除运行态。
# 函数用途: 保留可重建的监控定义，而非复制发送方执行身份。
def _portable_monitor_job(job):
    from ..scheduler.rules import _RULE_FIELDS

    result = {key: deepcopy(job[key]) for key in
        ('schedule', 'prompt', 'rules', 'precheck', 'policy', 'skill_refs', 'name') if key in job}
    if 'rules' in result:
        result['rules'] = [{key: value for key, value in rule.items() if key in _RULE_FIELDS}
            for rule in result['rules']]
    return _without_private_bindings(result)


# LLM: 持原根只读锁；原字节及本Owner已配置秘密先扫描再建描述符，不创建存储；拓扑缺依赖转换为投递领域码。
# 函数用途: 为预览和发送冻结同一安全闭包，凭据只在扫描内存中使用而不导出。
def snapshot_delivery_bundle(home, root_kind, root_id, *, include_dependencies=True, allow_secret_placeholder_replace=False):
    safe_asset_id(root_id)
    if root_kind not in {'script', 'workflow', 'monitor', 'skill'} or type(include_dependencies) is not bool:
        raise ValueError('资产kind或include_dependencies无效')
    root_key = f'{root_kind}:{root_id}'
    with _read_lock(home, ('.asset-batch.lock',)):
        if read_bytes_beneath(home, ('.asset-batch.json',), max_bytes=MAX_BUNDLE_BYTES) is not None:
            raise AssetDeliveryError('ASSET_TRANSACTION_PENDING', '存在未完成资产事务')
        selected = _raw_reachable(_raw_catalog(home), root_key, include_dependencies)
        raw = {f"{record['kind']}/{record['id']}/{name}": content for record in selected
            for name, content in _raw_files(home, record).items()}
        if sum(map(len, raw.values())) > MAX_BUNDLE_BYTES:
            raise AssetDeliveryError('ASSET_DELIVERY_QUOTA_EXCEEDED', '投递包超过字节上限')
        safe = sanitize_bundle_files(raw, owner_home=home, extra_secrets=datasource_secret_values(home),
            allow_secret_placeholder_replace=allow_secret_placeholder_replace)
        exports = {}
        for record in selected:
            prefix = f"{record['kind']}/{record['id']}/"
            exports[f"{record['kind']}:{record['id']}"] = _normalize_export(record,
                {name[len(prefix):]: content for name, content in safe.items() if name.startswith(prefix)})
    try:
        closure = extract_asset_closure((item['descriptor'] for item in exports.values()), root_key) if include_dependencies else (exports[root_key]['descriptor'],)
    except AssetClosureError as exc:
        raise AssetDeliveryError('ASSET_DELIVERY_DEPENDENCY_MISSING',
            'Asset dependency closure is incomplete', missing_refs=list(exc.missing_refs)) from None
    return [exports[f'{descriptor.kind}:{descriptor.id}'] for descriptor in closure]


# LLM: 所有大小/hash按实际冻结字节计算，展示摘要不带原内容或本机路径。
# 函数用途: 生成包裹内单个资产的逐文件完整性清单。
def delivery_item_manifest(item, root_key):
    descriptor = item['descriptor']
    files = [{'path': name, 'size': len(raw), 'sha256': sha256(raw).hexdigest()} for name, raw in sorted(item['files'].items())]
    content_hash = sha256(delivery_json(files)).hexdigest()
    return {'kind': descriptor.kind, 'id': descriptor.id, 'name': descriptor.name, 'is_root': root_key == f'{descriptor.kind}:{descriptor.id}',
        'descriptor': descriptor.to_dict(), 'files': files, 'content_sha256': content_hash}


# LLM: 需求摘要仅展示ID/类型，不复制datasource/grant实体、不签发授权，风险按可执行资产保守分级。
# 函数用途: 汇总接收方需要自己重绑定的资源和工具。
def required_delivery_capabilities(items):
    descriptors = [item['descriptor'] for item in items]
    sources = {(source.source_id, source.system) for descriptor in descriptors for source in descriptor.data_sources}
    tools = {tool for descriptor in descriptors for tool in descriptor.metadata.get('tools_required', ())}
    domains = {urlsplit(target.endpoint).hostname for descriptor in descriptors for target in descriptor.access_targets if '://' in target.endpoint}
    grants = {grant for item in items for grant in item.get('required_grants', ())}
    return {'required_grants': sorted(grants), 'required_datasources': [{'source_id': key, 'system': system} for key, system in sorted(sources)],
        'required_tools': sorted(tools), 'external_domains': sorted(domain for domain in domains if domain),
        'estimated_risk_level': 'high' if any(row.kind == 'script' for row in descriptors) else 'medium' if any(row.kind in {'monitor', 'workflow'} for row in descriptors) else 'low'}


# LLM: 二进制沿原批量事务data_base64合同，不通过文本解码破坏资源或建立旁路资产文件。
# 函数用途: 将已核验冻结字节编码为原事务支持的内容值。
def _batch_content(raw):
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return {'data_base64': base64.b64encode(raw).decode('ascii')}


# LLM: 映射只作用于结构化资产引用，不对代码/Markdown做文本替换；Skill迁入接收方owner命名域。
# 函数用途: 为保留双方副本重写依赖ID与工作流步骤引用。
def rewrite_delivery_refs(value, mapping, skill_names):
    plural = {'scripts': 'script', 'workflows': 'workflow', 'skills': 'skill', 'monitors': 'monitor'}
    if not isinstance(value, dict):
        return value
    result = dict(value)
    refs = value.get('refs')
    if isinstance(refs, dict):
        result['refs'] = {ref_kind: [mapping.get(f'{plural[ref_kind]}:{identifier}', identifier) for identifier in identifiers]
            if ref_kind in plural else identifiers for ref_kind, identifiers in refs.items()}
    if isinstance(value.get('steps'), list):
        result['steps'] = [_rewrite_delivery_step(step, mapping, skill_names) for step in value['steps']]
    for key in ('scheduler_job', 'schedule_spec', 'rules', 'precheck', 'skill_refs'):
        if key in value:
            result[key] = _rewrite_monitor_refs(value[key], mapping, skill_names,
                skill_refs=key == 'skill_refs')
    return result


# LLM: 仅监控结构化定义递归映射明确引用键，prompt/正文不替换；版本Skill引用保留对象形状。
# 函数用途: 重写Scheduler动作和规则依赖，让冲突重命名保持闭包一致。
def _rewrite_monitor_refs(value, mapping, skill_names, *, skill_refs=False):
    if isinstance(value, list):
        return [_rewrite_monitor_refs(item, mapping, skill_names, skill_refs=skill_refs) for item in value]
    if skill_refs and isinstance(value, str):
        return _mapped_reference('skill', value, mapping, skill_names)
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {'script_id', 'workflow_id'} and isinstance(item, str):
            result[key] = _mapped_reference(key[:-3], item, mapping, skill_names)
        elif key == 'skill_ids':
            result[key] = [_mapped_reference('skill', ref, mapping, skill_names) for ref in item]
        elif skill_refs and key == 'stable_id':
            result[key] = _mapped_reference('skill', item, mapping, skill_names)
        else:
            result[key] = _rewrite_monitor_refs(item, mapping, skill_names, skill_refs=key == 'skill_refs')
    return result


# LLM: WorkflowStepSpec中只有script_id和skill_ids是引用；inputs_map及其它业务字典不递归改写。
# 函数用途: 对一个工作流步骤执行明确的依赖映射。
def _rewrite_delivery_step(step, mapping, skill_names):
    result = dict(step)
    if step.get('script_id'):
        result['script_id'] = _mapped_reference('script', step['script_id'], mapping, skill_names)
    if 'skill_ids' in step:
        result['skill_ids'] = [_mapped_reference('skill', reference, mapping, skill_names) for reference in step['skill_ids']]
    return result


# LLM: 只有显式结构化引用使用本转换，不能按自然语言或字符串子串匹配身份。
# 函数用途: 为四类引用取得接收方准确的新ID或Skill逻辑名。
def _mapped_reference(kind, reference, mapping, skill_names):
    if kind == 'skill':
        key = 'skill:' + canonical_skill_name(reference)
        if key in skill_names:
            return ('owner:' if ':' in reference else '') + skill_names[key]
        return reference
    return mapping.get(f'{kind}:{reference}', reference)


# LLM: 锁内只读接收方canonical索引，重名按kind与大小写折叠判定，避免不敏感文件系统覆盖；最终CAS由原批量事务复核。
# 函数用途: 规划稳定的新ID和需跳过的既有资产，不执行安装。
def delivery_conflict_mapping(home, envelope, strategy):
    existing = _raw_catalog(home)
    actual = {}
    for key, record in existing.items():
        actual.setdefault(key.casefold(), set()).add(record['id'])
    occupied = set(actual)
    mapping, skipped = {}, set()
    sender = envelope['sender_owner_id'].rsplit('/', 1)[-1][:12]
    for item in envelope['bundled_assets']:
        key = f"{item['kind']}:{item['id']}"
        target = item['id']
        collision = key.casefold() in occupied
        if collision and strategy == 'keep_both_rename':
            target = _renamed_delivery_id(item, sender, envelope['delivery_id'], occupied)
        if collision and strategy in {'skip', 'overwrite'}:
            target = _existing_delivery_target(key, existing, actual)
        if collision and strategy == 'skip':
            skipped.add(key)
        mapping[key] = target
        actual.setdefault(f"{item['kind']}:{target}".casefold(), set()).add(target)
        occupied.add(f"{item['kind']}:{target}".casefold())
    return mapping, skipped, existing


# LLM: 大小写敏感原Store可已有双ID；精确命中不得重定向，无精确匹配的多候选必须拒绝，不按遍历顺序选写入对象。
# 函数用途: 为skip或overwrite选取唯一且真实的canonical目标。
def _existing_delivery_target(key, existing, actual):
    if key in existing:
        return existing[key]['id']
    candidates = actual[key.casefold()]
    if len(candidates) != 1:
        raise AssetDeliveryError('ASSET_DELIVERY_CONFLICT_AMBIGUOUS', '大小写等价目标不唯一，不能自动选择覆盖或复用')
    return next(iter(candidates))


# LLM: 固定投递ID生成确定性后缀，检查整个事务占用集合，不依赖文件系统大小写别名。
# 函数用途: 为保留双方资产选择安全且唯一的新ID。
def _renamed_delivery_id(item, sender, delivery_id, occupied):
    counter = 0
    while True:
        token = sha256(f"{delivery_id}:{item['kind']}:{item['id']}:{counter}".encode()).hexdigest()[:8]
        suffix = f'_from_{sender}_{token}'
        target = item['id'][:128 - len(suffix)] + suffix
        if f"{item['kind']}:{target}".casefold() not in occupied:
            return target
        counter += 1


# LLM: Skill名称重命名同时更新头部与描述符，正文保留；只生成JSON兼容YAML标量，不执行头部或模板。
# 函数用途: 规范接收后技能的唯一名称与禁用状态。
def _received_skill_body(raw, name, enabled):
    header, body = split_skill_frontmatter(raw.decode('utf-8'))
    header.update(name=name, enabled=enabled)
    return '---\n' + ''.join(json.dumps(key, ensure_ascii=False) + ': ' + json.dumps(value, ensure_ascii=False)
        + '\n' for key, value in header.items()) + '---\n' + body + '\n'


# LLM: 只消费已验hash负载和冲突映射；适配器缺依赖错误转投递领域码；provenance/禁用状态不赋予执行权限。
# 函数用途: 生成canonical事务草稿与附件，安装准备阶段也保留缺依赖的准确诊断。
def prepare_delivery_items(home, envelope, payloads, strategy, enabled, accepted_at):
    try:
        mapping, skipped, existing = delivery_conflict_mapping(home, envelope, strategy)
    except AssetClosureError as exc:
        raise AssetDeliveryError('ASSET_DELIVERY_DEPENDENCY_MISSING',
            'Asset dependency closure is incomplete', missing_refs=list(exc.missing_refs)) from None
    skill_names = {f"skill:{item['id']}": mapping[f"skill:{item['id']}"] if mapping[f"skill:{item['id']}"] != item['id']
        else item['name'] for item in envelope['bundled_assets'] if item['kind'] == 'skill'}
    items, extras, installed = [], {}, []
    for item in envelope['bundled_assets']:
        key, kind = f"{item['kind']}:{item['id']}", item['kind']
        target = mapping[key]
        if key in skipped:
            installed.append({'kind': kind, 'id': target, 'source_id': item['id'], 'status': 'skipped'})
            continue
        descriptor = AssetDescriptor.from_dict(rewrite_delivery_refs(item['descriptor'], mapping, skill_names))
        provenance = {'delivery_id': envelope['delivery_id'], 'sender_owner_id': envelope['sender_owner_id'],
            'accepted_at': accepted_at, 'source_sha256': item['content_sha256'], 'auto_enabled': enabled}
        descriptor = replace(descriptor, id=target, owner_id=envelope['recipient_owner_id'], enabled=enabled,
            name=skill_names[key] if kind == 'skill' else descriptor.name,
            metadata={**descriptor.metadata, 'delivery_provenance': provenance})
        draft = {'descriptor': descriptor.to_dict(), 'expected_revision': existing.get(f'{kind}:{target}', {}).get('meta', {}).get('revision', 0)}
        files = payloads[key]
        content, resources = _delivery_draft_content(item, files, mapping, skill_names, enabled)
        draft.update(content)
        extras[(kind, target)] = resources
        items.append(draft)
        installed.append({'kind': kind, 'id': target, 'source_id': item['id'], 'status': 'installed'})
    return items, extras, installed


# LLM: 脚本/Skill资源全部留在原batch；监控定义只作为Scheduler事务附加输入，不能当脚本资源发表。
# 函数用途: 将核验负载转成单项canonical草稿，不进行正式写入。
def _delivery_draft_content(item, files, mapping, skill_names, enabled):
    kind = item['kind']
    if kind == 'script':
        meta = json.loads(files['script.json'])
        content = {key: meta[key] for key in ('entrypoint', 'interpreter', 'timeout_seconds')}
        content['code'] = files[meta['entrypoint']].decode('utf-8')
        return content, {name: raw for name, raw in files.items() if name not in {'script.json', meta['entrypoint']}}
    if kind == 'workflow':
        meta = rewrite_delivery_refs(json.loads(files['workflow.json']), mapping, skill_names)
        return {'steps': meta['steps'], 'body_markdown': meta.get('body_markdown', ''), 'enabled': enabled}, {}
    if kind == 'skill':
        name = skill_names[f"skill:{item['id']}"]
        return {'body_markdown': _received_skill_body(files['SKILL.md'], name, enabled),
            'files': {name: _batch_content(raw) for name, raw in files.items() if name not in {'SKILL.md', 'skill_meta.json'}}}, {}
    meta = rewrite_delivery_refs(json.loads(files['monitor.json']), mapping, skill_names)
    spec = _delivery_monitor_schedule(meta.get('schedule_spec'))
    spec['enabled'] = enabled
    return {'schedule_spec': spec}, {'scheduler_job': meta.get('scheduler_job') or meta}


# LLM: 兼容原绑定适配器的展示expression，只对准确kind还原原Schedule字段；所有值仍交原schema严格校验。
# 函数用途: 将历史监控绑定的调度摘要恢复为可登记的计划。
def _delivery_monitor_schedule(spec):
    result = dict(spec or {'kind': 'every', 'every_seconds': 3600})
    if result.get('kind') == 'every' and 'every_seconds' not in result and result.get('expression'):
        result['every_seconds'] = int(result['expression'])
    if result.get('kind') == 'at' and 'at' not in result and result.get('expression'):
        result['at'] = result['expression']
    return result


# LLM: 复用原计划校验和通用资源路径门，不在投递层绕过canonical journal恢复允许的路径集合。
# 函数用途: 将已扫描脚本附属文件加入同一个两阶段事务，尚不支持的路径明确拒绝而非漏复制。
def add_delivery_script_resources(plans, extras):
    from .workspace_assets import _validate_batch_path

    for plan in plans:
        descriptor = plan['descriptor']
        if descriptor.kind != 'script':
            continue
        for name, raw in extras.get((descriptor.kind, descriptor.id), {}).items():
            parts = ('scripts', descriptor.id, *_resource_parts(name))
            _validate_batch_path(parts)
            plan['updates'][parts] = _batch_content(raw)


# LLM: 调用方已持Owner quota与投递锁，本函数继续取原资产根/集合锁；两阶段写及恢复只复用现有canonical journal。
# 函数用途: 原子安装整个包裹，在任何正式资产写前冻结可复核的安装清单。
def commit_delivery_items(home, drafts, extras, admission, prepared):
    from ..user_space.owner_quota import OwnerQuotaChange
    from . import asset_root_lock
    from .workspace_assets import (
        _BATCH_FILE,
        _batch_entries,
        _canonical_locks,
        _commit_staged,
        _content_bytes,
        _finalize_plans,
        _prepare_batch,
    )

    plans = _prepare_batch(drafts, 'owner', None)
    add_delivery_script_resources(plans, extras)
    _validate_final_delivery_plans(plans)
    with asset_root_lock(home):
        with _canonical_locks(home, [parts for plan in plans for parts in plan['updates']]):
            _finalize_plans(home, plans)
            add_delivery_script_resources(plans, extras)
            _validate_final_delivery_plans(plans)
            entries, journal = _batch_entries(home, plans)
            stages = [OwnerQuotaChange(home.joinpath(*entry['parts'][:-1], entry['stage']),
                len(_content_bytes(entry['content']))) for entry in entries if entry['content'] is not None]
            undo = OwnerQuotaChange(home / _BATCH_FILE, len(delivery_json(journal)))
            finals = [OwnerQuotaChange(home.joinpath(*entry['parts']),
                None if entry['content'] is None else len(_content_bytes(entry['content']))) for entry in entries]
            admission.check([undo, *stages])
            admission.check([undo, *stages, *finals])
            expected = [{'parts': entry['parts'], 'sha256': _delivery_content_hash(entry['content']),
                'previous_sha256': _delivery_content_hash(entry['previous'])} for entry in entries]
            with _delivery_monitor_commit(home, plans, extras, admission):
                admission.check([undo, *stages, *finals])
                prepared(expected)
                _commit_staged(home, entries, journal)
    return [plan['descriptor'].to_dict() for plan in plans]


# LLM: Owner来自已验证目标描述符；先nofollow核目录，再构造原Scheduler和宿主due索引，不重复取得quota准入。
# 函数用途: 按接收方真实身份打开canonical调度器及原唤醒投影。
def delivery_scheduler(home, owner_id):
    from ..scheduler import SchedulerDueIndex, SchedulerRepository
    from ..user_space.home_layout import home_paths
    from ..user_space.owner_quota import owner_quota_enforcer_from_policy

    parts = owner_id.split('/')
    host = home.parents[len(parts)]
    provider = 'local' if parts[0] == 'local' else parts[1]
    os.close(open_directory_beneath(home, ('data', 'scheduler'), create=True, require_dir_fd=True))
    return SchedulerRepository(home / 'data/scheduler', owner_provider=provider,
        owner_kind='main' if owner_id == 'local/main' else 'user', owner_id=owner_id,
        due_owner_id=parts[-1], quota_enforcer=owner_quota_enforcer_from_policy(home),
        due_index=SchedulerDueIndex(home_paths(host).global_index_dir / 'scheduler_due.sqlite3'))


# LLM: 使用原runtime路径解析器，以接收方home为默认执行工作区；不沿用源线程或任意其它Owner线程。
# 函数用途: 复用或创建一个接收方自己的真实会话供监控执行绑定。
def _delivery_monitor_thread(home, owner_id):
    from types import SimpleNamespace

    from ..conversation.store import ConversationStore
    from ..user_space.runtime_paths import runtime_paths_for_agent

    path = runtime_paths_for_agent(SimpleNamespace(), home, SimpleNamespace(owner_home_dir=home))['conversation_workspace']
    os.close(open_directory_beneath(home, path.relative_to(home).parts, create=True, require_dir_fd=True))
    threads = ConversationStore(path).threads
    for thread in threads.list_threads(limit=100):
        if (thread.owner_id == owner_id and thread.owner_home == str(home)
                and thread.workspace_root == str(home) and not thread.channel_bindings):
            return thread.thread_id
    return threads.create(owner_id=owner_id, owner_home=str(home), workspace_root=str(home),
        title='接收的资产监控').thread_id


# LLM: 新建载荷沿原校验器；只接收静态定义，强制目标ID/Owner/新线程/启停/代次，不复制源运行状态或通知目标。
# 函数用途: 构造真实可解析的接收方Scheduler任务。
def new_delivery_monitor_job(repo, descriptor, meta, store):
    from ..scheduler.event_source_lifecycle import validate_event_job
    from ..scheduler.event_sources import sync_event_sources
    from ..scheduler.monitor_state import next_alias
    from ..scheduler.repository import SchedulerJobCreateRequest, _new_job_payload

    home, now = repo.root.parent.parent, time.time()
    spec = _delivery_monitor_schedule(meta.get('schedule') or meta.get('schedule_spec'))
    if spec.get('kind') == 'every':
        spec.setdefault('anchor_at', 0)
    request = SchedulerJobCreateRequest(name=meta.get('name') or descriptor.name,
        prompt=meta.get('prompt') or descriptor.summary or descriptor.name,
        thread_id=_delivery_monitor_thread(home, descriptor.owner_id), schedule=spec,
        rules=meta.get('rules'), precheck=meta.get('precheck'), policy=meta.get('policy'),
        skill_refs=meta.get('skill_refs', []))
    job = _new_job_payload(owner=repo.owner, request=request, now=now)
    previous = store['jobs'].get(descriptor.id, {})
    job.update(job_id=descriptor.id, enabled=descriptor.enabled,
        status='active' if descriptor.enabled else 'paused',
        paused_reason='' if descriptor.enabled else 'asset_delivery_disabled_by_default',
        paused_at=0.0 if descriptor.enabled else now,
        alias=previous.get('alias') or next_alias(store), version=int(previous.get('version', 0)) + 1)
    job = sync_event_sources(job)
    validate_event_job(job)
    return job


# LLM: 外层已持quota→资产锁，只取得原scheduler锁；失败恢复完整旧store及due投影，不调用不可重入mutation_scope。
# 函数用途: 将真实监控登记包围原资产提交，原batch异常时回滚Scheduler。
@contextmanager
def _delivery_monitor_commit(home, plans, extras, admission):
    monitors = [plan for plan in plans if plan['descriptor'].kind == 'monitor']
    if not monitors:
        yield
        return
    repo = delivery_scheduler(home, monitors[0]['descriptor'].owner_id)
    with locked_json_path(repo.store_path):
        store = repo._load_store_unlocked()
        previous = deepcopy(store)
        try:
            for plan in monitors:
                descriptor = plan['descriptor']
                meta = deepcopy(extras[(descriptor.kind, descriptor.id)]['scheduler_job'])
                meta.setdefault('schedule_spec', plan['payload']['schedule_spec'])
                _bind_delivery_monitor_skills(home, meta, plans)
                store['jobs'][descriptor.id] = new_delivery_monitor_job(repo, descriptor, meta, store)
            repo._write_store_unlocked(store, admission)
            yield
        except BaseException:
            repo._write_store_unlocked(previous, admission)
            raise


# LLM: 版本摘要取最终计划字节或skip复用的接收方原文，不保留源hash冒充改名/禁用后的版本；包和builtin不重绑定。
# 函数用途: 将监控Skill版本引用绑定到本次实际安装的接收方正文。
def _bind_delivery_monitor_skills(home, meta, plans):
    from .workspace_assets import _content_bytes

    planned = {f"owner:{plan['descriptor'].name}": _content_bytes(
        plan['updates'][('skills', plan['descriptor'].id, 'SKILL.md')])
        for plan in plans if plan['descriptor'].kind == 'skill'}
    for reference in meta.get('skill_refs', ()):
        stable_id = reference['stable_id']
        if not stable_id.startswith('owner:'):
            continue
        raw = planned.get(stable_id)
        if raw is None:
            raw = read_bytes_beneath(home, ('skills', canonical_skill_name(stable_id), 'SKILL.md'),
                max_bytes=MAX_BUNDLE_BYTES, require_dir_fd=True)
        if raw is not None:
            reference['content_sha256'] = sha256(raw).hexdigest()


# LLM: 附件与旧入口整理后再次执行原batch同等目标/文件限制，保证发表与恢复接受同一journal路径集合。
# 函数用途: 拒绝完整计划的大小写重复、非法路径和超限内容。
def _validate_final_delivery_plans(plans):
    from .workspace_assets import MAX_ASSET_BYTES, _content_bytes, _validate_batch_path

    seen = set()
    for plan in plans:
        for parts, content in plan['updates'].items():
            _validate_batch_path(parts)
            folded = tuple(part.casefold() for part in parts)
            if folded in seen:
                raise ValueError('批量资产目标重复')
            seen.add(folded)
            if content is not None and len(_content_bytes(content)) > MAX_ASSET_BYTES:
                raise ValueError('资产文件超过安全预算')


# LLM: 复用原batch冻结的旧/新内容，不重复读取未变文件，也不在投递状态存旧代码副本。
# 函数用途: 计算提交或回滚辨识用的准确摘要，缺失叶保持None。
def _delivery_content_hash(content):
    from .workspace_assets import _content_bytes

    return None if content is None else sha256(_content_bytes(content)).hexdigest()
