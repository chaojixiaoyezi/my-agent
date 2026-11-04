"""监控以发布版本绑定；隐藏组合在公共工作流之外承载追加步骤。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.monitor_adapter import (
    get_monitor_descriptor,
    list_monitor_descriptors,
    save_monitor_binding,
)
from agent_py_agent.agent.assets.revision_history import (
    blob_get,
    latest_version,
    record_version,
    save_draft,
)
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)


# LLM: 用真实版本原语模拟尚未合入的W4发布hook，不替换历史读取函数。
# 函数用途: 创建一份正式发布的基线工作流。
def _publish(home, *, step_id='one'):
    store = WorkflowStore(home)
    definition = store.save_workflow(WorkflowDefinition(AssetDescriptor(id='base', kind='workflow', name='基础流程'),
        (WorkflowStepSpec(step_id, step_id, 'llm_skill_analysis'),)))
    record_version(home, 'workflow', 'base', payload=definition.to_dict(),
                   execution_definition_ref=definition.execution_definition_ref)
    return store, definition


# LLM: 仅显式升级修改pin；后续发布和草稿都不能改变现有绑定。
# 函数用途: 验证默认固定、版本提示、手动升级和原ID去重。
def test_binding_pins_published_version_until_manual_upgrade(tmp_path):
    store, base = _publish(tmp_path)
    first = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'])
    assert first['metadata'].get('pinned_version') == 1
    assert first['metadata']['upgrade_policy'] == 'pinned'
    updated = store.save_workflow(replace(base, steps=base.steps + (WorkflowStepSpec('two', 'two', 'llm_skill_analysis'),)))
    record_version(tmp_path, 'workflow', 'base', payload=updated.to_dict(), execution_definition_ref=updated.execution_definition_ref)
    save_draft(tmp_path, 'workflow', 'base', payload={'steps': []}, base_version=2)
    current = get_monitor_descriptor(tmp_path, 'job48')
    assert current['metadata']['pinned_version'] == 1
    assert current['metadata']['version_projection'] == '当前 v1 · 最新 v2'
    upgraded = save_monitor_binding(tmp_path, 'job48', pinned_version=2)
    assert upgraded['metadata']['pinned_version'] == 2
    assert upgraded['refs'] == [{'kind': 'workflow', 'id': 'base'}]
    assert len(list_monitor_descriptors(tmp_path)) == 1


# LLM: 基础步骤来自不可变payload，不从公共当前定义复制；所有宿主写入保留hidden。
# 函数用途: 验证组合元数据、追加顺序、版本链及重复保存去重。
def test_hidden_composition_uses_pinned_base_and_rewrites_on_upgrade(tmp_path):
    store, base = _publish(tmp_path)
    appended = [{'step_id': 'tail', 'name': '追加分析', 'mode': 'llm_skill_analysis'}]
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    assert binding['metadata']['composition']['base_version'] == 1
    hidden = store.get_workflow('monitor-job48')
    assert hidden.descriptor.metadata['hidden'] is True
    assert hidden.descriptor.metadata['composition'] == {'monitor_id': 'job48', 'base_workflow_id': 'base',
        'base_version': 1, 'appended_step_ids': ['tail']}
    assert [step.step_id for step in hidden.steps] == ['one', 'tail']
    first_version = latest_version(tmp_path, 'workflow', 'monitor-job48')
    assert first_version['pins']['workflows'] == {'base': {'version': 1}}
    save_monitor_binding(tmp_path, 'job48')
    assert latest_version(tmp_path, 'workflow', 'monitor-job48')['version'] == first_version['version']
    changed = store.save_workflow(replace(base, steps=(WorkflowStepSpec('new', 'new', 'llm_skill_analysis'),)))
    record_version(tmp_path, 'workflow', 'base', payload=changed.to_dict(), execution_definition_ref=changed.execution_definition_ref)
    save_monitor_binding(tmp_path, 'job48', pinned_version=2)
    assert [step.step_id for step in store.get_workflow('monitor-job48').steps] == ['new', 'tail']
    version = latest_version(tmp_path, 'workflow', 'monitor-job48')
    assert version['version'] == first_version['version'] + 1
    assert version['pins']['workflows']['base']['version'] == 2
    payload = json.loads(blob_get(tmp_path, version['payload_blob']))
    # W1有意剔除host-managed hidden/composition；规范存储每次写入仍必须hidden。
    assert payload.get('descriptor', payload)['metadata']['base_workflow_pin'] == {'id': 'base', 'version': 2}
    assert store.get_workflow('monitor-job48').descriptor.metadata['hidden'] is True


# LLM: 草稿没有发布编号，不能通过绑定把它变成正式版本。
# 函数用途: 验证显式不存在的pin在写绑定前拒绝。
def test_draft_only_workflow_cannot_be_pinned(tmp_path):
    save_draft(tmp_path, 'workflow', 'draft', payload={'steps': []})
    with pytest.raises(ValueError):
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['draft'], pinned_version=1)
    assert not (tmp_path / 'data/monitors/job48.json').exists()
