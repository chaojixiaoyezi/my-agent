"""宿主监控组合：从发布快照读取基础步骤，通过原WorkflowStore保存隐藏流程。"""
import json
from copy import deepcopy
from dataclasses import replace

from . import next_asset_revision, safe_asset_id
from .revision_history import (
    AssetHistoryError,
    asset_fingerprint,
    blob_get,
    get_version,
    latest_version,
    record_version,
)
from .workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
    with_workflow_step_refs,
)


class _CompositionStore(WorkflowStore):
    """沿原canonical事务写入；仅替换本次宿主组合的发布回调，不先发表当前依赖版本。"""

    # LLM: 调用方已持原根锁及集合锁并冻结pins；canonical提交后只发表一次，失败不回传可用版本。
    # 函数用途: 让组合的存储修订和正式发布记录使用同一份精确依赖。
    def _history_notify(self, action, *, asset_id=None, payload=None, previous=None, updates=None):
        if action != 'save':
            return super()._history_notify(action, asset_id=asset_id, payload=payload, previous=previous, updates=updates)
        self.publication = record_version(self.owner_home, 'workflow', payload['id'], payload=payload,
            side_files={'WORKFLOW.md': updates.get('WORKFLOW.md')},
            execution_definition_ref=WorkflowStore._decode_definition(payload).execution_definition_ref,
            storage_revision=payload['revision'], pins=self.publication_pins)
        return self.publication


# LLM: 精确编号读取发布快照；草稿、当前定义和缺失blob都不能充当历史。
# 函数用途: 恢复监控引用的一份已发布工作流。
def published_workflow(owner_home, workflow_id, version):
    record = get_version(owner_home, 'workflow', safe_asset_id(workflow_id), version)
    raw = blob_get(owner_home, record['payload_blob']) if record else None
    if raw is None:
        raise ValueError('monitor published workflow snapshot unavailable')
    payload = json.loads(raw)
    return (WorkflowDefinition.from_any(payload) if 'descriptor' in payload
            else WorkflowStore._decode_definition(payload))


# LLM: 归属/缺失检查、CAS和canonical写同持原根→集合锁；继承依赖只读历史，尾部新增依赖沿原发布原语冻结。
# 函数用途: 原子创建或更新同一监控的隐藏组合，返回与存储一致且可解析的正式版本。
def save_monitor_composition(owner_home, monitor_id, base_workflow_id, base_version, appended_steps):
    monitor_id, base_workflow_id = safe_asset_id(monitor_id), safe_asset_id(base_workflow_id)
    identifier = safe_asset_id('monitor-' + monitor_id)
    base = published_workflow(owner_home, base_workflow_id, base_version)
    base_record = get_version(owner_home, 'workflow', base_workflow_id, base_version)
    appended = tuple(WorkflowStepSpec.from_dict(step) for step in appended_steps)
    composition = {'monitor_id': monitor_id, 'base_workflow_id': base_workflow_id,
                   'base_version': base_version, 'appended_step_ids': [step.step_id for step in appended]}
    descriptor = replace(base.descriptor, id=identifier, name='监控组合 ' + monitor_id, revision=0,
        metadata={'hidden': True, 'composition': composition,
                  'base_workflow_pin': {'id': base_workflow_id, 'version': base_version}})
    definition = with_workflow_step_refs(replace(base, descriptor=descriptor, steps=base.steps + appended, schedule_ids=()))
    store = _CompositionStore(owner_home)
    with store._locked():
        old = store._get(identifier)
        if old is not None and (old.descriptor.metadata.get('hidden') is not True
                or not isinstance(old.descriptor.metadata.get('composition'), dict)
                or old.descriptor.metadata['composition'].get('monitor_id') != monitor_id):
            raise ValueError('MONITOR_COMPOSITION_ID_CONFLICT')
        next_asset_revision(old.descriptor.to_dict() if old else None, old.descriptor.revision if old else 0)
        fingerprint = asset_fingerprint(payload=definition.to_dict(),
            side_files={'WORKFLOW.md': definition.body_markdown or None})
        latest = latest_version(owner_home, 'workflow', identifier)
        unchanged = (old is not None and latest is not None and latest['fingerprint'] == fingerprint
            and asset_fingerprint(payload=old.to_dict(),
                side_files={'WORKFLOW.md': old.body_markdown or None}) == fingerprint)
        store.publication_pins = _composition_pins(store, base, base_record, appended, base_workflow_id, base_version,
            previous_pins=latest.get('pins', {}).get('scripts', {}) if unchanged else None)
        # 重复保存不重读已冻结的尾部依赖；原发布记录完整可解析时才复用其编号。
        if unchanged and latest.get('pins') == store.publication_pins:
            from ..workflows.pinned_definitions import resolve_definition
            resolve_definition(owner_home, identifier, latest['version'])
            return {'composition': composition, 'workflow_id': identifier, 'version': latest['version']}
        store._save(definition)
        return {'composition': composition, 'workflow_id': identifier, 'version': store.publication['version']}


# LLM: pin表按资产ID共享身份；基础SHA不可被当前代码重定义，同正文尾部复用旧pin但仍核验全部blob。
# 函数用途: 合并继承pin和追加独有脚本pin，无法表示或缺失的历史在canonical提交前拒绝。
def _composition_pins(store, base, base_record, appended, base_workflow_id, base_version, *, previous_pins=None):
    from ..workflows.pinned_definitions import historical_pin_available
    from .version_hook import _pins

    pins = deepcopy(base_record.get('pins', {}))
    for kind in ('scripts', 'skills', 'workflows'):
        if not isinstance(pins.setdefault(kind, {}), dict):
            raise ValueError('MONITOR_COMPOSITION_INVALID_PINS')
    for script_id in dict.fromkeys(step.script_id for step in base.steps if step.script_id):
        pin = pins['scripts'].get(script_id)
        if not isinstance(pin, dict) or not historical_pin_available(store.owner_home, {**pin, 'script_id': script_id}):
            raise ValueError('MONITOR_COMPOSITION_SCRIPT_PIN_UNAVAILABLE')
    additions = [step.to_dict() for step in appended if step.script_id and step.script_id not in pins['scripts']]
    if previous_pins is None:
        try:
            extra = _pins(store, {'steps': additions})
        except AssetHistoryError as exc:
            # 缺失或损坏的追加历史统一走组合错误合同；配额和其它历史错误仍保留原分类。
            if exc.code != 'ASSET_HISTORY_BLOB_MISSING':
                raise
            raise ValueError('MONITOR_COMPOSITION_SCRIPT_PIN_UNAVAILABLE') from exc
        pins['scripts'].update(extra['scripts'])
    else:
        if not isinstance(previous_pins, dict):
            raise ValueError('MONITOR_COMPOSITION_INVALID_PINS')
        for script_id in dict.fromkeys(step['script_id'] for step in additions):
            pins['scripts'][script_id] = deepcopy(previous_pins.get(script_id))
    # 额外继承pin原先可能未被基础步骤使用，尾部启用它时也必须核验，不得因键已存在漏验。
    for script_id in dict.fromkeys(step.script_id for step in appended if step.script_id):
        pin = pins['scripts'].get(script_id)
        if not isinstance(pin, dict) or not historical_pin_available(store.owner_home, {**pin, 'script_id': script_id}):
            raise ValueError('MONITOR_COMPOSITION_SCRIPT_PIN_UNAVAILABLE')
    base_pin = {'version': base_version}
    if base_workflow_id in pins['workflows'] and pins['workflows'][base_workflow_id] != base_pin:
        raise ValueError('MONITOR_COMPOSITION_CONFLICTING_PINS')
    pins['workflows'][base_workflow_id] = base_pin
    return pins
