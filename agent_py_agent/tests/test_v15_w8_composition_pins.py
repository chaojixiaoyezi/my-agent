"""监控以发布版本绑定；隐藏组合在公共工作流之外承载追加步骤。"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event, current_thread

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.monitor_adapter import (
    get_monitor_descriptor,
    list_monitor_descriptors,
    save_monitor_binding,
)
from agent_py_agent.agent.assets.monitor_composition import save_monitor_composition
from agent_py_agent.agent.assets.revision_history import (
    blob_get,
    get_version,
    latest_version,
    list_versions,
    record_version,
    save_draft,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.scheduler.pinned_targets import resolve_monitor_target
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.pinned_definitions import resolve_definition


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


# LLM: 脚本及其发布记录均来自真实Store，不替换历史、钩子或版本解析器。
# 函数用途: 发布一个带真实脚本依赖的基础流程。
def _script_base(home):
    scripts = ScriptStore(home)
    descriptor = AssetDescriptor(id='collect', kind='script', name='采集')
    scripts.save_script(descriptor, 'print(\'{"value":1}\')')
    store = WorkflowStore(home)
    base = store.save_workflow(WorkflowDefinition(AssetDescriptor(id='base', kind='workflow', name='基础流程'),
        (WorkflowStepSpec('one', '采集', 'deterministic_script', script_id='collect'),)))
    return scripts, store, base, latest_version(home, 'workflow', 'base')


# LLM: 使用绑定返回的目标和编号走真实解析器；编辑当前脚本不能重定义基础历史。
# 函数用途: 复现组合的空pin后继，并要求每份发表记录保持可解析且历史不变。
def test_composition_returned_version_resolves_inherited_script_pin(tmp_path):
    scripts, store, base, publication = _script_base(tmp_path)
    inherited = publication['pins']['scripts']['collect']
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(2)')
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '追加分析', 'mode': 'llm_skill_analysis'}])
    metadata = binding['metadata']
    target = resolve_monitor_target(tmp_path, 'job48', {'kind': 'workflow', 'asset_id': 'base'})
    assert target['pinned_version'] == metadata['composition_version']
    assert target['asset_id'] == metadata['execution_target']['asset_id']
    definition, frozen = resolve_definition(tmp_path, target['asset_id'], target['pinned_version'])
    assert frozen['one']['code_sha256'] == inherited['code_sha256']
    assert [step['step_id'] for step in definition['steps']] == ['one', 'tail']
    assert get_version(tmp_path, 'workflow', 'base', publication['version']) == publication
    records = list_versions(tmp_path, 'workflow', target['asset_id'])
    assert len(records) == 1
    exact = get_version(tmp_path, 'workflow', target['asset_id'], target['pinned_version'])
    assert exact == latest_version(tmp_path, 'workflow', target['asset_id'])
    assert exact['pins']['scripts']['collect'] == inherited
    assert exact['storage_revision'] == store.get_workflow(target['asset_id']).descriptor.revision
    assert base.steps[0].script_id == 'collect'


# LLM: 原引擎及脚本runner实际执行历史字节，仅分析端口由本地函数提供，不请求模型或调度服务。
# 函数用途: 验证真实绑定返回的准确组合版本可以跑完，编辑当前脚本不改变执行结果。
def test_exact_composition_version_runs_historical_script(tmp_path):
    scripts, _, _, publication = _script_base(tmp_path)
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(\'{"value":2}\')')
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '追加分析', 'mode': 'llm_skill_analysis'}])
    target = resolve_monitor_target(tmp_path, 'job48', {'kind': 'workflow', 'asset_id': 'base'})
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: {'tail': True})
    run = engine.run_workflow(target['asset_id'], definition_version=target['pinned_version'])
    assert not isinstance(run, dict), run
    assert run.status == 'succeeded'
    assert run.definition_version == binding['metadata']['composition_version']
    assert run.steps[0].output_snapshot == {'value': 1}
    snapshot = engine.store.load_workflow_snapshot(run.workflow_run_id)
    assert snapshot['_frozen_scripts']['one']['code_sha256'] == publication['pins']['scripts']['collect']['code_sha256']


# LLM: 独立工作流先占用生成ID；允许显式拒绝，但不得改变其正文、可见性或发布历史。
# 函数用途: 复现合法CAS覆盖无关流程，核验冲突失败没有留下监控绑定。
def test_composition_never_overwrites_independent_generated_id(tmp_path):
    store, _ = _publish(tmp_path)
    independent = store.save_workflow(WorkflowDefinition(
        AssetDescriptor(id='monitor-job48', kind='workflow', name='独立可见流程'),
        (WorkflowStepSpec('independent', '独立', 'llm_skill_analysis'),)))
    publication = latest_version(tmp_path, 'workflow', 'monitor-job48')
    try:
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
            {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    except ValueError as exc:
        assert 'COMPOSITION' in str(exc)
    assert store.get_workflow('monitor-job48').to_dict() == independent.to_dict()
    assert not store.get_workflow('monitor-job48').descriptor.metadata.get('hidden')
    assert latest_version(tmp_path, 'workflow', 'monitor-job48') == publication
    assert not (tmp_path / 'data/monitors/job48.json').exists()


# LLM: 普通保存不是宿主组合入口；同名metadata既不能认领独立资产，也不能撤销既有宿主归属。
# 函数用途: 验证隐藏字段不能充当替换另一份工作流的授权令牌。
def test_ordinary_save_cannot_claim_or_remove_composition_provenance(tmp_path):
    store, _ = _publish(tmp_path)
    forged = {'hidden': True, 'composition': {'monitor_id': 'job48'}}
    independent = store.save_workflow(WorkflowDefinition(
        AssetDescriptor(id='monitor-job48', kind='workflow', name='普通流程', metadata=forged), ()))
    assert not independent.descriptor.metadata.get('hidden')
    assert 'composition' not in independent.descriptor.metadata
    store.delete_workflow('monitor-job48')
    save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    owned = store.get_workflow('monitor-job48')
    replaced = store.save_workflow(replace(owned, descriptor=replace(owned.descriptor, metadata={})))
    assert replaced.descriptor.metadata['hidden'] is True
    assert replaced.descriptor.metadata['composition'] == owned.descriptor.metadata['composition']


# LLM: 新脚本依赖只经真实发布钩子冻结；当前依赖编辑后重复保存不得推新号或换掉旧SHA。
# 函数用途: 验证尾部新增脚本的冻结、无重复后继、真实解析及重复创建幂等。
def test_appended_only_script_is_frozen_once_and_resolves(tmp_path):
    store, _ = _publish(tmp_path)
    scripts = ScriptStore(tmp_path)
    descriptor = AssetDescriptor(id='tail-script', kind='script', name='追加脚本')
    scripts.save_script(descriptor, 'print(3)')
    script_publication = latest_version(tmp_path, 'script', 'tail-script')
    appended = [{'step_id': 'tail', 'name': '追加', 'mode': 'deterministic_script', 'script_id': 'tail-script'}]
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    target = binding['metadata']['execution_target']['asset_id']
    version = binding['metadata']['composition_version']
    _, pins = resolve_definition(tmp_path, target, version)
    assert pins['tail']['code_sha256'] == script_publication['code_sha256']
    previous = get_version(tmp_path, 'workflow', target, version)
    revision = store.get_workflow(target).descriptor.revision
    scripts.save_script(descriptor, 'print(4)')
    again = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    assert again['metadata']['execution_target']['asset_id'] == target
    assert again['metadata']['composition_version'] == version
    assert get_version(tmp_path, 'workflow', target, version) == previous
    assert store.get_workflow(target).descriptor.revision == revision
    assert len(list_versions(tmp_path, 'workflow', target)) == 1


# LLM: pin表以script_id为键，同ID继承同一历史身份，不得再读可编辑脚本替换基础SHA。
# 函数用途: 验证基础和尾部共享脚本时采用唯一、可表达的继承身份。
def test_appended_shared_script_keeps_inherited_identity(tmp_path):
    scripts, _, _, publication = _script_base(tmp_path)
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(2)')
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '再采集', 'mode': 'deterministic_script', 'script_id': 'collect'}])
    metadata = binding['metadata']
    _, pins = resolve_definition(tmp_path, metadata['execution_target']['asset_id'], metadata['composition_version'])
    assert pins['one']['code_sha256'] == pins['tail']['code_sha256'] == publication['pins']['scripts']['collect']['code_sha256']


# LLM: 错误依赖必须在canonical写入前拒绝；异常回收通过实际FD计数而非finally文本推断。
# 函数用途: 验证缺失追加脚本不发表不可用组合且没有FD泄漏。
def test_missing_appended_script_rejected_before_composition_commit(tmp_path):
    store, _ = _publish(tmp_path)
    before = _fd_count()
    with pytest.raises((FileNotFoundError, ValueError)):
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
            {'step_id': 'tail', 'name': '缺失', 'mode': 'deterministic_script', 'script_id': 'missing'}])
    assert _fd_count() - before == 0
    assert store.get_workflow('monitor-job48') is None
    assert latest_version(tmp_path, 'workflow', 'monitor-job48') is None
    assert not (tmp_path / 'data/monitors/job48.json').exists()


# LLM: 只统计本进程实际打开的FD；无目录接口的平台用CRT表，不跳过资源断言。
# 函数用途: 给新成功和错误路径提供独立资源观察点。
def _fd_count():
    if os.name == 'nt':
        import msvcrt
        count = 0
        for descriptor in range(2048):
            try:
                msvcrt.get_osfhandle(descriptor)
                count += 1
            except OSError:
                pass
        return count
    return len(os.listdir('/dev/fd' if os.path.isdir('/dev/fd') else '/proc/self/fd'))


# LLM: 透明同步仅控制取得真实存储锁的时刻，不替代Store、发布记录或版本解析器。
# 函数用途: 验证竞争创建者提交后，组合写者在同一事务边界拒绝无关canonical。
def test_competing_create_is_rechecked_at_storage_boundary(tmp_path, monkeypatch):
    store, _ = _publish(tmp_path)
    ready, proceed = Event(), Event()
    original = WorkflowStore._locked

    # LLM: 等待发生在根锁外，独立写者可先提交；实际锁与全部读写仍由原实现负责。
    # 函数用途: 确定性安排竞争创建先于组合存储检查。
    @contextmanager
    def gated(self):
        if current_thread().name.startswith('composition'):
            ready.set()
            assert proceed.wait(10)
        with original(self):
            yield

    monkeypatch.setattr(WorkflowStore, '_locked', gated)
    before = _fd_count()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='composition') as executor:
        future = executor.submit(save_monitor_composition, tmp_path, 'job48', 'base', 1,
            [{'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
        try:
            assert ready.wait(10)
            independent = store.save_workflow(WorkflowDefinition(
                AssetDescriptor(id='monitor-job48', kind='workflow', name='竞争创建'), ()))
        finally:
            proceed.set()
        with pytest.raises(ValueError, match='COMPOSITION'):
            future.result(timeout=10)
    assert store.get_workflow('monitor-job48').to_dict() == independent.to_dict()
    assert _fd_count() - before == 0


# LLM: 相同监控的显式升级改变基础版本，尾部正常替换每次只添一个历史，旧记录不改。
# 函数用途: 验证保存、换尾、升级及成功路径FD守恒。
def test_composition_replacement_upgrade_and_success_fd_balance(tmp_path):
    scripts, store, base, publication = _script_base(tmp_path)
    appended = [{'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}]
    before = _fd_count()
    first = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    assert _fd_count() - before == 0
    identifier = first['metadata']['execution_target']['asset_id']
    old_record = get_version(tmp_path, 'workflow', identifier, first['metadata']['composition_version'])
    replacement = save_monitor_binding(tmp_path, 'job48', appended_steps=[{**appended[0], 'name': '新尾部'}])
    assert replacement['metadata']['composition_version'] == first['metadata']['composition_version'] + 1
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(5)')
    store.save_workflow(replace(base, body_markdown='新版说明'))
    upgraded = save_monitor_binding(tmp_path, 'job48', pinned_version=2)
    assert upgraded['metadata']['execution_target']['asset_id'] == identifier
    assert upgraded['metadata']['composition_version'] == replacement['metadata']['composition_version'] + 1
    assert get_version(tmp_path, 'workflow', identifier, old_record['version']) == old_record
    assert get_version(tmp_path, 'workflow', 'base', 1) == publication
    latest = latest_version(tmp_path, 'workflow', identifier)
    assert latest['pins']['scripts'] == latest_version(tmp_path, 'workflow', 'base')['pins']['scripts']
    assert resolve_definition(tmp_path, identifier, latest['version'])[0]['body_markdown'] == '新版说明'
    assert _fd_count() - before == 0


# LLM: 可编辑脚本删除后历史仍有效；不能让发布钩子的当前读取偷偷成为继承依赖。
# 函数用途: 验证完全没有当前脚本时仍可组合已发布基础版本。
def test_deleted_current_script_does_not_redefine_inherited_pin(tmp_path):
    scripts, _, _, publication = _script_base(tmp_path)
    scripts.delete_script('collect')
    saved = save_monitor_composition(tmp_path, 'job48', 'base', 1, [
        {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    _, pins = resolve_definition(tmp_path, saved['workflow_id'], saved['version'])
    assert pins['one']['code_sha256'] == publication['pins']['scripts']['collect']['code_sha256']


# LLM: 已持有的名字不能替代精确监控归属，即使对象是另一监控的隐藏组合也拒绝。
# 函数用途: 核验同名错误归属及FD平衡，且不可变记录未被覆盖。
def test_other_monitor_provenance_is_rejected(tmp_path):
    store, _ = _publish(tmp_path)
    saved = save_monitor_composition(tmp_path, 'job48', 'base', 1, [
        {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    with store._locked():
        current = store._get(saved['workflow_id'])
        metadata = {**current.descriptor.metadata, 'composition': {**current.descriptor.metadata['composition'], 'monitor_id': 'other'}}
        store._save(replace(current, descriptor=replace(current.descriptor, metadata=metadata)))
    previous = store.get_workflow(saved['workflow_id']).to_dict()
    before = _fd_count()
    with pytest.raises(ValueError, match='COMPOSITION_ID_CONFLICT'):
        save_monitor_composition(tmp_path, 'job48', 'base', 1, [
            {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    assert store.get_workflow(saved['workflow_id']).to_dict() == previous
    assert _fd_count() - before == 0


# LLM: FD观察器先经真实open/close对照，不能用一个恒定返回值假证明资源平衡。
# 函数用途: 独立核实资源计数器确实看得到文件描述符。
def test_fd_observer_primitive_control(tmp_path):
    before = _fd_count()
    descriptor = os.open(tmp_path / 'fd-control', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert _fd_count() == before + 1
    finally:
        os.close(descriptor)
    assert _fd_count() == before


# LLM: 普通流程编辑形成同正文但不同依赖的后继时，组合重存仍必须核对基础发布的pin。
# 函数用途: 验证内容去重不会借同指纹接受重新钉到当前脚本的基础步骤。
def test_repeat_save_checks_inherited_pin_even_if_content_matches(tmp_path):
    scripts, store, _, publication = _script_base(tmp_path)
    appended = [{'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}]
    binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    identifier = binding['metadata']['execution_target']['asset_id']
    saved = store.get_workflow(identifier)
    scripts.save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(8)')
    store.save_workflow(replace(saved, body_markdown='暂时编辑'))
    store.save_workflow(saved)
    again = save_monitor_binding(tmp_path, 'job48')
    _, pins = resolve_definition(tmp_path, identifier, again['metadata']['composition_version'])
    assert pins['one']['code_sha256'] == publication['pins']['scripts']['collect']['code_sha256']


# LLM: 历史正文缺失时不能把解析器仅恢复元数据的成功称为可执行，重复去重也须核验必需blob。
# 函数用途: 验证重复保存遇缺失的历史代码明确拒绝且FD不泄漏。
def test_repeat_save_rejects_missing_script_code_blob(tmp_path):
    _, _, _, publication = _script_base(tmp_path)
    save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
        {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    digest = publication['pins']['scripts']['collect']['code_sha256']
    (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
    before = _fd_count()
    with pytest.raises(ValueError, match='COMPOSITION_SCRIPT_PIN_UNAVAILABLE'):
        save_monitor_binding(tmp_path, 'job48')
    assert _fd_count() == before


# LLM: 首次追加依赖也必须核验原发布记录的完整blob，不能仅凭SHA索引行宣称冻结成功。
# 函数用途: 验证新尾部脚本历史代码缺失在canonical提交前拒绝。
@pytest.mark.parametrize('blob_key,corrupt', [
    ('payload_blob', False), ('code_sha256', False), ('payload_blob', True), ('code_sha256', True),
])
def test_new_composition_rejects_missing_appended_history_blob(tmp_path, blob_key, corrupt):
    store, _ = _publish(tmp_path)
    scripts = ScriptStore(tmp_path)
    scripts.save_script(AssetDescriptor(id='tail-script', kind='script', name='尾部'), 'print(7)')
    publication = latest_version(tmp_path, 'script', 'tail-script')
    digest = publication[blob_key]
    blob = tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest
    if corrupt:
        blob.write_bytes(b'corrupt')
    else:
        blob.unlink()
    before = _fd_count()
    with pytest.raises(ValueError, match='COMPOSITION_SCRIPT_PIN_UNAVAILABLE'):
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
            {'step_id': 'tail', 'name': '追加', 'mode': 'deterministic_script', 'script_id': 'tail-script'}])
    assert store.get_workflow('monitor-job48') is None
    assert latest_version(tmp_path, 'workflow', 'monitor-job48') is None
    assert latest_version(tmp_path, 'script', 'tail-script') == publication
    assert not (tmp_path / 'data/monitors/job48.json').exists()
    assert _fd_count() == before


# LLM: 同一个workflow依赖键不能同时表达两个版本；冲突只允许明确拒绝，不覆盖继承版本。
# 函数用途: 验证真实发布的依赖身份冲突没有canonical及监控绑定写入。
def test_unrepresentable_inherited_workflow_pin_is_rejected(tmp_path):
    store, base = _publish(tmp_path)
    # 普通发布入口拒绝自身基础引用；用真实历史原语构造合法旧记录里的额外继承pin。
    publication = record_version(tmp_path, 'workflow', 'base', payload=base.to_dict(),
        execution_definition_ref=base.execution_definition_ref,
        pins={'scripts': {}, 'skills': {}, 'workflows': {'base': {'version': 1}}})['record']
    assert publication['version'] == 2
    assert publication['pins']['workflows'] == {'base': {'version': 1}}
    before = _fd_count()
    with pytest.raises(ValueError, match='COMPOSITION_CONFLICTING_PINS'):
        save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=[
            {'step_id': 'tail', 'name': '追加', 'mode': 'llm_skill_analysis'}])
    assert store.get_workflow('monitor-job48') is None
    assert not (tmp_path / 'data/monitors/job48.json').exists()
    assert get_version(tmp_path, 'workflow', 'base', 2) == publication
    assert _fd_count() == before


# LLM: 透明Event把晚到更新排在真实绑定事务内；两个请求共用原CAS，不能丢写或多发表一份组合。
# 函数用途: 验证同监控竞争更新仅一个提交，晚到旧revision拒绝且FD平衡。
def test_competing_binding_update_keeps_cas_and_history(tmp_path, monkeypatch):
    store, _ = _publish(tmp_path)
    appended = [{'step_id': 'tail', 'name': '原尾部', 'mode': 'llm_skill_analysis'}]
    first = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
    identifier = first['metadata']['execution_target']['asset_id']
    publication = latest_version(tmp_path, 'workflow', identifier)
    entered, late_ready = Event(), Event()
    original = WorkflowStore._get

    # LLM: 只观察原锁内读取，不替换返回定义或落盘逻辑；晚到者仍须取得原绑定协调锁。
    # 函数用途: 安排首更新持锁时另一请求带旧revision到达。
    def observed_get(self, workflow_id):
        result = original(self, workflow_id)
        if current_thread().name.startswith('early') and workflow_id == identifier:
            entered.set()
            assert late_ready.wait(10)
        return result

    # LLM: 仅发起公开绑定更新，预期版本取真实首个回执，不制造存储替身。
    # 函数用途: 模拟另一客户端持旧revision竞争更新。
    def late_update():
        late_ready.set()
        return save_monitor_binding(tmp_path, 'job48', expected_revision=first['revision'],
            appended_steps=[{**appended[0], 'name': '晚到尾部'}])

    monkeypatch.setattr(WorkflowStore, '_get', observed_get)
    before = _fd_count()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='early') as early, ThreadPoolExecutor(max_workers=1) as late:
        winner = early.submit(save_monitor_binding, tmp_path, 'job48', expected_revision=first['revision'],
            appended_steps=[{**appended[0], 'name': '首个更新'}])
        try:
            assert entered.wait(10)
            loser = late.submit(late_update)
            result = winner.result(timeout=10)
            with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
                loser.result(timeout=10)
        finally:
            late_ready.set()
    assert get_monitor_descriptor(tmp_path, 'job48')['revision'] == first['revision'] + 1
    assert result['metadata']['composition_version'] == publication['version'] + 1
    assert store.get_workflow(identifier).steps[-1].name == '首个更新'
    assert get_version(tmp_path, 'workflow', identifier, publication['version']) == publication
    assert _fd_count() == before


# LLM: record_version真实允许额外pin；尾部启用该引用时必须检查历史，不读当前脚本来替换它。
# 函数用途: 复现评审指出的未用继承pin在追加步骤使用时漏验代码blob。
@pytest.mark.parametrize('missing_blob', [False, True])
def test_appended_dependency_already_in_unused_base_pins(tmp_path, missing_blob):
    store, base = _publish(tmp_path)
    scripts = ScriptStore(tmp_path)
    descriptor = AssetDescriptor(id='tail-script', kind='script', name='尾部')
    scripts.save_script(descriptor, 'print(7)')
    script_publication = latest_version(tmp_path, 'script', 'tail-script')
    pin = {'version': script_publication['version'], 'code_sha256': script_publication['code_sha256']}
    record_version(tmp_path, 'workflow', 'base', payload=base.to_dict(),
        execution_definition_ref=base.execution_definition_ref,
        pins={'scripts': {'tail-script': pin}, 'skills': {}, 'workflows': {}})
    scripts.save_script(descriptor, 'print(9)')
    appended = [{'step_id': 'tail', 'name': '追加', 'mode': 'deterministic_script', 'script_id': 'tail-script'}]
    if missing_blob:
        digest = pin['code_sha256']
        (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
        before = _fd_count()
        with pytest.raises(ValueError, match='COMPOSITION_SCRIPT_PIN_UNAVAILABLE'):
            save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
        assert store.get_workflow('monitor-job48') is None
        assert _fd_count() == before
    else:
        binding = save_monitor_binding(tmp_path, 'job48', workflow_ids=['base'], appended_steps=appended)
        metadata = binding['metadata']
        _, frozen = resolve_definition(tmp_path, metadata['execution_target']['asset_id'], metadata['composition_version'])
        assert frozen['tail']['code_sha256'] == pin['code_sha256']
