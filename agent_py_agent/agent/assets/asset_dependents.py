"""资产清单的只读依赖和L1投影；不执行资产或授予权限。"""
from __future__ import annotations

from dataclasses import fields

from .asset_metadata import (
    normalize_member_of,
    normalize_trigger_matchers,
    strip_host_managed_metadata,
)
from .descriptor import AssetDescriptor
from .monitor_adapter import list_monitor_descriptors
from .pitfalls import read_known_pitfalls
from .revision_history import get_draft, latest_version, list_versions
from .script_store import ScriptStore
from .skill_adapter import list_owner_skill_descriptors
from .topology import build_asset_topology
from .workflow_store import WorkflowStore


class AssetActionError(ValueError):
    """携带结构化错误与恢复所需的字段，不按展示文案分支。"""

    # LLM: 错误码属于工具合同，不把普通说明当状态。
    # 函数用途: 保存机器可读动作失败。
    def __init__(self, code, data=None):
        self.code, self.data = code, {'error_code': code, **(data or {})}
        super().__init__(code)


# LLM: 模型元数据不含宿主字段；归属和匹配提示只归一化，不授权。
# 函数用途: 清理并规范保存请求的metadata。
def model_metadata(value):
    clean, stripped = strip_host_managed_metadata(value)
    if 'member_of' in clean:
        clean['member_of'] = normalize_member_of(clean['member_of'])
    if 'trigger_matchers' in clean:
        clean['trigger_matchers'] = normalize_trigger_matchers(clean['trigger_matchers'])
    return clean, stripped


# LLM: 列表投影的代码、路径等不是描述符字段，不能送进严格拓扑codec。
# 函数用途: 取得独立的标准描述符字典。
def _descriptor(value):
    names = {field.name for field in fields(AssetDescriptor)} | {'schema_version'}
    return {key: item for key, item in value.items() if key in names}


# LLM: 每类清单读取一次，不扫历史或实例化重型Agent；所有根来自工具绑定。
# 函数用途: 生成拓扑使用的当前资产清单。
def inventory(owner_home, *, workspace_root=None, skill_snapshot=None):
    scripts = ScriptStore(owner_home, workspace_root=workspace_root).list_scripts()
    workflows = [item.descriptor.to_dict() for item in WorkflowStore(owner_home, workspace_root=workspace_root).list_workflows()]
    skills = list_owner_skill_descriptors(owner_home, workspace_root=workspace_root, skill_snapshot=skill_snapshot)
    monitors = list_monitor_descriptors(owner_home)
    return [_descriptor(row) for row in [*scripts, *workflows, *skills, *monitors]]


# LLM: 只沿结构化拓扑边反向查询；未来拓扑新增关系不要求维护另一份关系枚举。
# 函数用途: 返回真正引用目标的资产，而非模型自报used_by。
def dependents(owner_home, kind, asset_id, *, workspace_root=None, skill_snapshot=None, descriptors=None):
    rows = inventory(owner_home, workspace_root=workspace_root, skill_snapshot=skill_snapshot) if descriptors is None else descriptors
    graph = build_asset_topology(rows)
    nodes = {node['key']: node for node in graph['nodes']}
    target = f'{kind}:{asset_id}'
    sources = {edge['from'] for edge in graph['edges'] if edge['to'] == target and edge['from'] != target}
    return [{'kind': nodes[key]['kind'], 'id': nodes[key]['id'], 'name': nodes[key]['name']}
            for key in sorted(sources) if key in nodes and not nodes[key].get('missing')]


# LLM: 原Store仍负责锁内CAS，这个预检只要求共享资产必须显式带revision。
# 函数用途: 对有依赖资产的无CAS更新返回结构化拒绝。
def require_revision(owner_home, kind, asset_id, current_revision, expected_revision, *, workspace_root=None, skill_snapshot=None):
    if expected_revision is not None:
        return
    used = dependents(owner_home, kind, asset_id, workspace_root=workspace_root, skill_snapshot=skill_snapshot)
    if used:
        raise AssetActionError('ASSET_REVISION_REQUIRED', {'current_revision': current_revision,
            'dependents': [{key: row[key] for key in ('kind', 'id')} for row in used]})


# LLM: 版本只加载五条元信息，count取无删号发布序列的最新编号；不加载全量blob。
# 函数用途: 生成两个get共用的L1字段。
def asset_projection(owner_home, kind, asset_id, metadata, *, workspace_root=None, skill_snapshot=None):
    latest = latest_version(owner_home, kind, asset_id)
    return {'used_by': dependents(owner_home, kind, asset_id, workspace_root=workspace_root, skill_snapshot=skill_snapshot),
            'known_pitfalls': read_known_pitfalls(owner_home, kind, asset_id, limit=5),
            'versions': {'latest': latest['version'] if latest else 0, 'count': latest['version'] if latest else 0,
                         'recent': list_versions(owner_home, kind, asset_id, limit=5)},
            'trigger_matchers': normalize_trigger_matchers(metadata.get('trigger_matchers')),
            'member_of': normalize_member_of(metadata.get('member_of')), 'source_system': metadata.get('source_system', '')}


# LLM: 沿唯一描述符适配公开列表refs；只认准确workflow身份，保留旧绑定和版本展示，不写绑定或升级。
# 函数用途: 为基础工作流及隐藏组合返回真实监控绑定。
def bound_monitors(owner_home, workflow_id):
    result = []
    for row in list_monitor_descriptors(owner_home):
        metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
        binding = metadata.get('workflow_binding')
        binding = binding if isinstance(binding, dict) else {}
        refs = AssetDescriptor.from_dict(_descriptor(row)).refs
        workflows = set(refs.workflows)
        if isinstance(binding.get('workflow_id'), str):
            workflows.add(binding['workflow_id'])
        for key in ('base_target', 'execution_target'):
            target = metadata.get(key)
            if isinstance(target, dict) and target.get('kind') == 'workflow':
                workflows.add(target.get('asset_id'))
        composition = metadata.get('composition')
        if isinstance(composition, dict):
            workflows.add(composition.get('base_workflow_id'))
        if workflow_id not in workflows:
            continue
        item = {'monitor_id': row['id']}
        for key in ('alias', 'pinned_version', 'upgrade_policy'):
            value = binding.get(key, metadata.get(key, row.get(key)))
            if key == 'pinned_version' and type(value) is not int:
                continue
            if key != 'pinned_version' and not isinstance(value, str):
                continue
            item[key] = value
        result.append(item)
    return result


# LLM: 只读W1草稿元信息和当前方法清单，历史正文不注入稳定提示。
# 函数用途: 给workflow get补充绑定方法、草稿和监控投影。
def workflow_projection(owner_home, definition, *, workspace_root=None, skill_snapshot=None):
    asset_id = definition.descriptor.id
    result = asset_projection(owner_home, 'workflow', asset_id, definition.descriptor.metadata,
                              workspace_root=workspace_root, skill_snapshot=skill_snapshot)
    rows = inventory(owner_home, workspace_root=workspace_root, skill_snapshot=skill_snapshot)
    refs = definition.descriptor.refs.to_dict()
    result['bound_methods'] = [{key: row[key] for key in ('kind', 'id', 'name', 'summary')}
        for row in rows if row['kind'] in ('script', 'skill', 'workflow') and row['id'] in refs.get(row['kind'] + 's', [])]
    draft = get_draft(owner_home, 'workflow', asset_id)
    result['draft'] = {'exists': draft is not None}
    if draft is not None:
        result['draft'].update({key: draft.get(key) for key in ('updated_at', 'base_version')})
    result['monitors'] = bound_monitors(owner_home, asset_id)
    return result
