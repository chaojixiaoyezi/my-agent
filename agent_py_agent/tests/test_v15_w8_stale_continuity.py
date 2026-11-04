"""历史版本延续：精确内容身份、缺失原因及显式兼容升级。"""
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.monitor_adapter import get_monitor_descriptor, save_monitor_binding
from agent_py_agent.agent.assets.revision_history import (
    latest_version,
    record_compat_edge,
    record_version,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.scheduler.execution_policy import (
    ExecutionTargetError,
    execution_definition_ref,
    resolve_execution_target,
)


# LLM: 原工具store提供同owner定义，版本操作使用真实W1文件而非替代历史字典。
# 函数用途: 为原解析入口提供一个最小、同owner的宿主。
def _agent(home, store):
    return SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home)), SimpleNamespace(
        handler=SimpleNamespace(owner_home=home, _store=lambda: store))


# LLM: 冻结scheduler引用与版本记录使用相同精确摘要。
# 函数用途: 发布工作流并返回它的scheduler目标。
def _workflow(home, *, step_id='one'):
    store = WorkflowStore(home)
    definition = store.save_workflow(WorkflowDefinition(AssetDescriptor(id='flow', kind='workflow', name='流程'),
        (WorkflowStepSpec(step_id, step_id, 'llm_skill_analysis'),)))
    reference = execution_definition_ref('workflow', 'flow', definition)
    record_version(home, 'workflow', 'flow', payload=definition.to_dict(), execution_definition_ref=reference['definition_sha256'])
    return store, definition, {'kind': 'workflow', 'asset_id': 'flow', 'execution_definition_ref': reference}


# LLM: 当前定义变化不改变旧冻结身份，执行者取得的是旧发布快照。
# 函数用途: 验证工作流定义变化后仍返回固定v1。
def test_frozen_workflow_resolves_old_version(tmp_path):
    store, definition, target = _workflow(tmp_path)
    store.save_workflow(replace(definition, steps=(WorkflowStepSpec('two', 'two', 'llm_skill_analysis'),)))
    agent, runtime = _agent(tmp_path, store)
    resolved, frozen = resolve_execution_target(agent, target, runtime=runtime)
    assert resolved['definition_version'] == 1
    assert resolved['execution_definition_ref'] == target['execution_definition_ref']
    assert [step.step_id for step in frozen.steps] == ['one']


# LLM: 删除仅影响当前定义，不能撤销保留的已发布版本。
# 函数用途: 核验删除资产后仍可执行明确固定的历史。
def test_deleted_workflow_keeps_history(tmp_path):
    store, _definition, target = _workflow(tmp_path)
    store.delete_workflow('flow')
    agent, runtime = _agent(tmp_path, store)
    resolved, frozen = resolve_execution_target(agent, target, runtime=runtime)
    assert resolved['definition_version'] == 1 and frozen.descriptor.id == 'flow'


# LLM: 缺少blob不能偷偷执行新版本；明确版本不存在同样失败关闭。
# 函数用途: 验证STALE保留机器可读原因。
def test_missing_blob_is_structured_stale(tmp_path):
    store, definition, target = _workflow(tmp_path)
    version = record_version(tmp_path, 'workflow', 'flow', payload=definition.to_dict(),
                             execution_definition_ref=target['execution_definition_ref']['definition_sha256'])['record']
    blob = version['payload_blob']
    (tmp_path / 'data/asset_evolution/blobs' / blob[:2] / blob).unlink()
    store.save_workflow(replace(definition, steps=()))
    agent, runtime = _agent(tmp_path, store)
    with pytest.raises(ExecutionTargetError) as caught:
        resolve_execution_target(agent, target, runtime=runtime)
    assert caught.value.error_code == 'SCHEDULER_EXECUTION_DEFINITION_STALE'
    assert caught.value.reason == 'blob_missing'


# LLM: 使用原 Store 发布钩子的 storage_revision；历史 payload 不补当前授权字段，真正内容固定为原字节 SHA。
# 函数用途: 验证历史脚本 pin、存储修订和返回快照的代码一致。
def test_script_history_builds_exact_expected_pin(tmp_path):
    store = ScriptStore(tmp_path)
    descriptor = AssetDescriptor(id='collect', kind='script', name='采集')
    old = store.save_script(descriptor, 'print(1)')
    reference = execution_definition_ref('script', 'collect', old)
    assert latest_version(tmp_path, 'script', 'collect')['storage_revision'] == old['revision']
    store.save_script(descriptor, 'print(2)')
    agent, runtime = _agent(tmp_path, store)
    target, frozen = resolve_execution_target(agent, {'kind': 'script', 'asset_id': 'collect',
        'execution_definition_ref': reference}, runtime=runtime)
    assert target['expected_script_pin']['code_sha256'] == hashlib.sha256(b'print(1)').hexdigest()
    assert target['expected_script_pin']['revision'] == old['revision'] == 1
    assert 'revision' not in frozen
    assert frozen['code'] == 'print(1)'


# LLM: 兼容链只通过W1接口读取；非显式策略从不自动推进。
# 函数用途: 核对固定策略和通过1→2、失败2→3的升级边界。
@pytest.mark.parametrize('policy,expected', [('pinned', 1), ('follow_compatible', 2)])
def test_follow_compatible_only_advances_passing_chain(tmp_path, policy, expected, caplog):
    from agent_py_agent.agent.scheduler.pinned_targets import resolve_monitor_target

    store, definition, target = _workflow(tmp_path)
    save_monitor_binding(tmp_path, 'job48', workflow_ids=['flow'], upgrade_policy=policy)
    for step_id in ('two', 'three'):
        definition = store.save_workflow(replace(definition, steps=(WorkflowStepSpec(step_id, step_id, 'llm_skill_analysis'),)))
        record_version(tmp_path, 'workflow', 'flow', payload=definition.to_dict(),
                       execution_definition_ref=execution_definition_ref('workflow', 'flow', definition)['definition_sha256'])
    record_compat_edge(tmp_path, 'workflow', 'flow', from_version=1, to_version=2, verdict='replay_passed')
    record_compat_edge(tmp_path, 'workflow', 'flow', from_version=2, to_version=3, verdict='replay_failed')
    caplog.set_level('INFO', logger='agent_py_agent.agent.scheduler.pinned_targets')
    resolved = resolve_monitor_target(tmp_path, 'job48', target)
    assert resolved['pinned_version'] == expected
    assert get_monitor_descriptor(tmp_path, 'job48')['metadata']['pinned_version'] == expected
    assert ('upgrade_notice' in resolved) is (policy == 'follow_compatible')
    notices = [record for record in caplog.records if getattr(record, 'event', '') == 'monitor_pin_advanced']
    assert len(notices) == (1 if policy == 'follow_compatible' else 0)
    if notices:
        assert (notices[0].from_version, notices[0].to_version) == (1, 2)


# LLM: 工具权限链不替换，仅复制本次handler并在原engine调用处固定版本参数。
# 函数用途: 验证工作流实际调用传definition_version且不修改共享engine。
def test_workflow_tool_passes_definition_version_at_engine_seam(tmp_path):
    from agent_py_agent.agent.scheduler import execution
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    bind = getattr(execution, '_bind_pinned_tool', None)
    assert callable(bind)
    store, definition, _target = _workflow(tmp_path)
    calls = []

    # LLM: W5同波次接口在切片测试按合同桩接，真实链路由W12验证。
    # 函数用途: 观察准确的版本和并发策略。
    def run(workflow, inputs=None, *, definition_version=None, concurrency_policy='allow'):
        calls.append((workflow, definition_version, concurrency_policy))
        return {'status': 'succeeded', 'final_output': {'version': definition_version}}

    engine = SimpleNamespace(owner_home=tmp_path, run_workflow=run)
    original = WorkflowsTool(tmp_path, store=store, engine=engine)
    pinned = bind(original, {'kind': 'workflow', 'asset_id': 'flow', 'definition_version': 1}, definition)
    result = pinned.execute({'action': 'run', 'workflow_id': 'flow', 'inputs': {}, 'concurrency_policy': 'skip_if_running'})
    assert result.ok and calls[0][1:] == (1, 'skip_if_running')
    assert original.engine is engine and original.engine.run_workflow is run


# LLM: ScriptTool继续走原执行和结果封装，只有宿主复制的dispatch携带冻结pin。
# 函数用途: 验证数据源执行seam确实接收旧代码SHA。
def test_script_tool_passes_expected_pin_to_original_seam(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler import execution
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
    from agent_py_agent.agent.workflows import datasource_runtime

    bind = getattr(execution, '_bind_pinned_tool', None)
    assert callable(bind)
    store = ScriptStore(tmp_path)
    definition = store.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(1)')
    pin = {'script_id': 'collect', 'code_sha256': hashlib.sha256(b'print(1)').hexdigest()}
    calls = []

    # LLM: 只替代尚未合入的W6执行端口，参数必须到达真实工具调用边界。
    # 函数用途: 接收并记录脚本固定身份。
    def seam(owner_home, script_id, inputs=None, *, runner=None, workspace_root=None,
             expected_script_pin=None, timeout_seconds=None, override_data_sources=None):
        calls.append(expected_script_pin)
        return {'ok': True, 'output_data': {'version': 1}}

    monkeypatch.setattr(datasource_runtime, 'run_script_with_datasources', seam)
    original = ScriptsTool(tmp_path, store=store)
    pinned = bind(original, {'kind': 'script', 'asset_id': 'collect', 'expected_script_pin': pin}, definition)
    result = pinned.execute({'action': 'run', 'script_id': 'collect', 'inputs': {}})
    assert result.ok and calls == [pin]
    assert original._dispatch.__func__ is ScriptsTool._dispatch


# LLM: 独立审查指出pin与内容引用可能互相矛盾，不能输出自相矛盾的冻结身份。
# 函数用途: 用既有STALE错误拒绝版本和引用不匹配。
def test_explicit_pin_and_ref_must_identify_same_version(tmp_path):
    store, definition, target = _workflow(tmp_path)
    newer = store.save_workflow(replace(definition, steps=()))
    record_version(tmp_path, 'workflow', 'flow', payload=newer.to_dict(),
                   execution_definition_ref=execution_definition_ref('workflow', 'flow', newer)['definition_sha256'])
    agent, runtime = _agent(tmp_path, store)
    with pytest.raises(ExecutionTargetError) as caught:
        resolve_execution_target(agent, {**target, 'pinned_version': 2}, runtime=runtime)
    assert caught.value.error_code == 'SCHEDULER_EXECUTION_DEFINITION_STALE'


# LLM: 绑定可显式手工升级，但不能在消费已有明确pin时无提示覆盖它。
# 函数用途: 验证输入pin冲突不会被清空内容引用后隐藏。
def test_monitor_rejects_conflicting_explicit_pin(tmp_path):
    from agent_py_agent.agent.scheduler.pinned_targets import resolve_monitor_target
    store, definition, target = _workflow(tmp_path)
    save_monitor_binding(tmp_path, 'job48', workflow_ids=['flow'])
    newer = store.save_workflow(replace(definition, steps=()))
    record_version(tmp_path, 'workflow', 'flow', payload=newer.to_dict(),
                   execution_definition_ref=execution_definition_ref('workflow', 'flow', newer)['definition_sha256'])
    save_monitor_binding(tmp_path, 'job48', pinned_version=2)
    with pytest.raises(ExecutionTargetError):
        resolve_monitor_target(tmp_path, 'job48', {**target, 'pinned_version': 1})


# LLM: 使用base提交的真实旧算法，不拿新函数生成所谓兼容样本。
# 函数用途: 验证升级前冻结的旧SHA仍恢复同一历史定义，不替换成当前内容。
def test_legacy_frozen_reference_resolves_verified_history(tmp_path):
    store, definition, target = _workflow(tmp_path)
    payload = deepcopy(definition.to_dict())
    for key in ('revision', 'runtime_summary', 'used_by', 'entrypoint_path', 'source_check',
                'source_system', 'source_binding', 'last_check_result'):
        payload['descriptor'].pop(key, None)
    legacy = hashlib.sha256(json.dumps(payload, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    old_target = {**target, 'execution_definition_ref': {**target['execution_definition_ref'], 'definition_sha256': legacy}}
    store.save_workflow(replace(definition, steps=()))
    agent, runtime = _agent(tmp_path, store)
    resolved, frozen = resolve_execution_target(agent, old_target, runtime=runtime)
    assert resolved['definition_version'] == 1
    assert [step.step_id for step in frozen.steps] == ['one']
