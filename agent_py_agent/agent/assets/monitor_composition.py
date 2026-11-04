"""宿主监控组合：从发布快照读取基础步骤，通过原WorkflowStore保存隐藏流程。"""
import json
from dataclasses import replace

from . import safe_asset_id
from .revision_history import blob_get, get_version, record_version
from .workflow_store import WorkflowDefinition, WorkflowStepSpec, WorkflowStore


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


# LLM: 使用原存储CAS，hidden在每次写入都设真；正式版本由W1内容去重，W4相同hook不会重复编号。
# 函数用途: 创建或更新一个固定基础版本加追加步骤的隐藏工作流。
def save_monitor_composition(owner_home, monitor_id, base_workflow_id, base_version, appended_steps):
    identifier = safe_asset_id('monitor-' + safe_asset_id(monitor_id))
    base = published_workflow(owner_home, base_workflow_id, base_version)
    appended = tuple(WorkflowStepSpec.from_dict(step) for step in appended_steps)
    composition = {'monitor_id': monitor_id, 'base_workflow_id': base_workflow_id,
                   'base_version': base_version, 'appended_step_ids': [step.step_id for step in appended]}
    descriptor = replace(base.descriptor, id=identifier, name='监控组合 ' + monitor_id, revision=0,
        metadata={'hidden': True, 'composition': composition,
                  'base_workflow_pin': {'id': base_workflow_id, 'version': base_version}})
    definition = replace(base, descriptor=descriptor, steps=base.steps + appended, schedule_ids=())
    store = WorkflowStore(owner_home)
    old = store.get_workflow(identifier)
    saved = old if old is not None and replace(old, descriptor=replace(old.descriptor,
        revision=0, metadata=descriptor.metadata)).to_dict() == definition.to_dict() else store.save_workflow(
            definition, expected_revision=old.descriptor.revision if old else None)
    payload = saved.descriptor.to_dict() | {'steps': [step.to_dict() for step in saved.steps],
                                          'body_markdown': saved.body_markdown}
    for key in ('edges', 'datasource_ids'):
        if getattr(saved, key):
            payload[key] = saved.to_dict()[key]
    version = record_version(owner_home, 'workflow', identifier, payload=payload,
        execution_definition_ref=saved.execution_definition_ref, storage_revision=saved.descriptor.revision,
        pins={'scripts': {}, 'skills': {}, 'workflows': {base_workflow_id: {'version': base_version}}})
    return {'composition': composition, 'workflow_id': identifier, 'version': version['version']}
