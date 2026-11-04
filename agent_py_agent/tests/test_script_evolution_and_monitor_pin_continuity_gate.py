"""P5.GATE3：真实存储、进程执行、依赖回放与监控固定版本的九步集成门。

只使用 tmp_path 的资产、工作流和工作文件；不替换任何被测产品行为。
诊断输入返回正在执行文件的 SHA，业务回放输入保持确定性的相同输出。
"""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.assets.monitor_adapter import (
    get_monitor_descriptor,
    save_monitor_binding,
)
from agent_py_agent.agent.assets.mutation_ledger import (
    AssetMutationLedger,
    evolution_value_hash,
    freeze_asset_mutations,
)
from agent_py_agent.agent.assets.revision_history import (
    blob_get,
    get_version,
    latest_version,
    list_versions,
    read_compat_edges,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.capability.asset_evolution import AssetEvolutionService
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.execution import _execute_target_tool
from agent_py_agent.agent.scheduler.execution_policy import (
    ExecutionTargetError,
    resolve_execution_target,
)
from agent_py_agent.agent.scheduler.pinned_targets import resolve_monitor_target
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.engine import WorkflowEngine


# LLM: 标记变化形成不同代码 SHA；诊断分支实读自身文件，不拿输入自报版本当执行证明。
# 函数用途: 生成没有网络或持久副作用的本地脚本。
def _source(label):
    return f'''"""Local continuity fixture {label}."""
import hashlib
import json
import sys
from pathlib import Path


def main(inputs):
    value = inputs.get("value", 0)
    identity = hashlib.sha256(Path(__file__).read_bytes()).hexdigest() if inputs.get("identify") else ""
    return {{"value": value + 1, "code_sha256": identity}}


if __name__ == "__main__":
    print(json.dumps(main(json.loads(sys.stdin.read() or "{{}}"))))
'''


# LLM: 不把工具调用成功当业务成功，所有后续断言读取原始结构化回执。
# 函数用途: 执行真实资产工具并解析 JSON。
def _call(tool, arguments):
    result = tool.execute(arguments)
    assert result.ok, result.output
    return json.loads(result.output)


# LLM: 只替代模型分析提供者；执行、冻结、恢复及脚本沙箱均保持产品实现。
# 函数用途: 在固定分析步骤产生一次可恢复断点。
def _pause_analysis(spec, context, skills):
    assert spec['step_id'] == 'pause'
    return {'_pause_for_resume': True}


# LLM: 所有路径都属于本用例的临时 Owner；真实 Agent 仅承载宿主工具轮，不调用模型或部署通道。
# 函数用途: 装配真实工具、存储、工作流引擎及调度宿主，退出回收监控池。
@pytest.fixture
def gate(tmp_path):
    home = (tmp_path / 'app' / 'owners' / 'local' / 'main').resolve()
    workspace = home / 'tasks' / 'gate3'
    workspace.mkdir(parents=True)
    descriptor = {'name': 'Continuity', 'summary': 'Local state invariant check',
                  'access_targets': [], 'data_sources': [], 'credential_refs': [],
                  'io_contract': {}, 'metadata': {'test_scope': 'gate3'}}
    scripts = ScriptsTool(home)
    code = _source('v1')
    _call(scripts, {'action': 'register', 'script_id': 'continuity',
                    'descriptor': descriptor, 'code': code})
    engine = WorkflowEngine(home, analysis_handler=_pause_analysis)
    workflows = WorkflowsTool(home, engine=engine)
    access = FileSystemAccessOptions(path_access_mode='full', owner_scope_root=str(home))
    host = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home.parents[2]),
        enable_plugins=False), workspace)
    assert host.home_paths.owner_home_dir == home
    yield SimpleNamespace(home=home, workspace=workspace, descriptor=descriptor, host=host,
        scripts=scripts, workflows=workflows, engine=engine, code_v1=code,
        store=ScriptStore(home), writer=WriteFileTool(workspace,
            options=WriteFileToolOptions(access_options=access)),
        editor=EditFileTool(workspace, access_options=access))
    host.scheduler_service.monitor_pool.shutdown()


# LLM: 工作流保存走公开工具和唯一版本钩子；快照只能由引擎真实执行生成。
# 函数用途: 保存一条固定依赖脚本的流程，可选增加前置断点。
def _save_flow(gate, workflow_id='continuity-flow', *, pause=False):
    steps = ([{'step_id': 'pause', 'name': '暂停', 'mode': 'llm_skill_analysis'}]
             if pause else [])
    steps.append({'step_id': 'calculate', 'name': '计算', 'mode': 'deterministic_script',
        'script_id': 'continuity', 'on_error': 'fail', 'inputs_map': {
            'value': {'step_id': '$inputs', 'key': 'value'},
            'identify': {'step_id': '$inputs', 'key': 'identify'}}})
    return _call(gate.workflows, {'action': 'save', 'workflow_id': workflow_id,
        'definition': {'descriptor': {'name': workflow_id, 'access_targets': [],
            'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': steps}})


# LLM: 用两个不同输入取得真实 StepExecutionRecord，不手写回放快照或签发 passed。
# 函数用途: 给依赖回放准备已执行的成功记录。
def _seed_replay_runs(gate):
    _save_flow(gate)
    runs = []
    for value in (4, 9):
        run = _call(gate.workflows, {'action': 'run', 'workflow_id': 'continuity-flow',
            'inputs': {'value': value, 'identify': False}})
        assert run['status'] == 'succeeded', run
        assert run['final_output'] == {'value': value + 1, 'code_sha256': ''}
        persisted = gate.engine.store.load_run(run['workflow_run_id'])
        assert persisted.steps[0].input_snapshot == {'value': value, 'identify': False}
        assert persisted.steps[0].output_snapshot == run['final_output']
        runs.append(run)
    return runs


# LLM: 文件结果来自原工具，执行结果来自真实子进程；不生成虚构退出码或替换脚本执行器。
# 函数用途: 在一次回合中编辑工作区副本、运行它并观察原字节事实。
def _edit_and_execute(gate, code):
    ledger, state = AssetMutationLedger(), {}
    write = {'path': 'candidate.py', 'content': gate.code_v1}
    edit = {'path': 'candidate.py', 'old_string': gate.code_v1, 'new_string': code}
    for name, tool, arguments in (('write_file', gate.writer, write),
                                  ('edit_file', gate.editor, edit)):
        result = tool.execute(arguments)
        assert result.ok, result.output
        ledger.observe(state, name, arguments, {'ok': result.ok, 'output': result.output},
            owner_home=gate.home, workspace_root=gate.workspace, call_id='g3-' + name,
            observed_at='2026-11-04T12:00:00+00:00')
    path = gate.workspace / 'candidate.py'
    assert path.read_text() == code
    completed = subprocess.run([sys.executable, str(path)], cwd=gate.workspace,
        input='{}', capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stderr
    receipt = {'ok': completed.returncode == 0, 'status': 'exited',
        'exit_code': completed.returncode, 'stdout': completed.stdout, 'stderr': completed.stderr}
    ledger.observe(state, 'run_command', {'command': shlex.join([sys.executable, str(path)]),
        'working_dir': str(gate.workspace)}, receipt, owner_home=gate.home,
        workspace_root=gate.workspace, call_id='g3-run',
        observed_at='2026-11-04T12:00:01+00:00')
    return state, json.loads(completed.stdout)


# LLM: 监控选择后走完整宿主轮、runtime/handler 和原 pin resolver；不能用仅调用绑定 helper 的旁路冒充生产。
# 函数用途: 沿真实调度工具栈执行监控固定版本，并核对实际进程回执。
def _run_monitor(gate, monitor_id):
    selected = resolve_monitor_target(gate.home, monitor_id,
        {'kind': 'script', 'asset_id': 'continuity',
         'input_ref': {'inputs': {'value': 7, 'identify': True}}})
    notice = selected.pop('upgrade_notice', None)
    target, _definition = resolve_execution_target(gate.host, selected,
        runtime=SimpleNamespace(handler=gate.scripts))
    identifier = 'gate3-monitor-' + uuid4().hex
    execution, output = _execute_target_tool(gate.host, RunParams(request_id=identifier,
        run_id=identifier, task_id=identifier, source='scheduler_tool',
        root_user_prompt='运行固定监控'), selected, monitor_id=monitor_id)
    assert execution.result.ok, output
    result = json.loads(output)
    assert result['ok'] is True and result['exit_code'] == 0, result
    assert result['output_data']['value'] == 8
    if notice is not None:
        target['upgrade_notice'] = notice
    return target, result


# LLM: 九个检查点共用同一真实历史链；不手写版本、兼容边、run 快照或修补产品函数。
# 函数用途: 核验版本发布、回放、两种监控、在途恢复、保护门、回滚及缺失历史的完整连续性。
def test_gate3_script_evolution_and_monitor_pin_continuity(gate):
    # 1. 首次保存的脚本由 W4 钩子生成 v1；工作流和两种监控都固定它。
    v1 = latest_version(gate.home, 'script', 'continuity')
    assert v1['version'] == v1['storage_revision'] == 1
    sha1 = hashlib.sha256(gate.code_v1.encode()).hexdigest()
    assert v1['code_sha256'] == sha1
    runs = _seed_replay_runs(gate)
    flow_v1 = get_version(gate.home, 'workflow', 'continuity-flow', 1)
    assert flow_v1['pins']['scripts']['continuity'] == {'version': 1, 'code_sha256': sha1}
    for identifier, policy in (('fixed-monitor', 'pinned'), ('following-monitor', 'follow_compatible')):
        binding = save_monitor_binding(gate.home, identifier, script_ids=['continuity'], upgrade_policy=policy)
        assert binding['metadata']['pinned_version'] == 1
        assert binding['metadata']['upgrade_policy'] == policy
    _save_flow(gate, 'inflight-flow', pause=True)
    paused = _call(gate.workflows, {'action': 'run', 'workflow_id': 'inflight-flow',
        'inputs': {'value': 12, 'identify': True}})
    assert paused['status'] == 'paused_for_resume'
    frozen = gate.engine.store.load_workflow_snapshot(paused['workflow_run_id'])
    assert frozen['_frozen_scripts']['calculate']['code_sha256'] == sha1
    assert paused['steps'][1]['status'] == 'pending'

    # 2. 编辑的是工作区副本，不能提前覆盖正式脚本，否则 Route 1 无从回放旧 base。
    code_v2 = _source('v2')
    state, output = _edit_and_execute(gate, code_v2)
    request = freeze_asset_mutations(gate.home, state, run_id='gate3-turn',
        owner_id='main', thread_id='gate3-thread', member_of=[{'kind': 'workflow', 'id': 'continuity-flow'}])
    assert request['schema_version'] == 'asset_evolution_request.v1'
    assert request['evidence_tags'] == [] and len(request['targets']) == 1
    captured = request['targets'][0]
    sha2 = hashlib.sha256(code_v2.encode()).hexdigest()
    assert sha2 != sha1 and captured['final_sha256'] == sha2
    assert captured['verified'] is True and captured['sha_history'] == [sha1, sha2]
    assert captured['execution']['input_hash'] == evolution_value_hash({})
    assert captured['execution']['output_hash'] == evolution_value_hash(output)
    assert blob_get(gate.home, sha2) == code_v2.encode()
    assert gate.store.get_script('continuity')['code'] == gate.code_v1

    # 3. 后台服务真实选择 Route 1，回放依赖快照后发布 v2 并写通过边。
    service = AssetEvolutionService(gate.home, maintenance_mode='autonomous')
    promoted = service.process('gate3-turn')
    assert promoted['status'] == 'processed'
    route = promoted['routes'][0]
    assert route['status'] == 'promoted' and route['asset_id'] == 'continuity', promoted
    assert route['version'] == 2 and route['replay']['verdict'] == 'passed'
    assert route['replay']['replayed'] == len(runs) == 2
    assert {row['input'] for row in route['replay']['hashes']} == {
        evolution_value_hash({'value': value, 'identify': False}) for value in (4, 9)}
    assert all(row['actual'] == row['expected'] for row in route['replay']['hashes'])
    v2 = latest_version(gate.home, 'script', 'continuity')
    assert v2['version'] == 2 and v2['code_sha256'] == sha2
    edges = read_compat_edges(gate.home, 'script', 'continuity')
    assert len(edges) == 1
    assert (edges[0]['from_version'], edges[0]['to_version'], edges[0]['verdict'], edges[0]['replayed']) == (
        1, 2, 'replay_passed', 2)
    assert edges[0]['evidence_ref'] == 'gate3-turn'
    assert service.process('gate3-turn')['duplicate'] is True
    assert read_compat_edges(gate.home, 'script', 'continuity') == edges

    # 4. 固定策略执行历史字节；跟随策略仅沿通过边升级，且重复解析不再升级。
    projection = get_monitor_descriptor(gate.home, 'fixed-monitor')['metadata']
    assert projection['version_projection'] == '当前 v1 · 最新 v2'
    fixed, old_result = _run_monitor(gate, 'fixed-monitor')
    assert fixed['pinned_version'] == fixed['definition_version'] == 1
    assert old_result['source_check']['pin_resolution'] == {'mode': 'history', 'version': 1, 'code_sha256': sha1}
    assert old_result['output_data']['code_sha256'] == sha1
    following, new_result = _run_monitor(gate, 'following-monitor')
    assert following['pinned_version'] == following['definition_version'] == 2
    assert following['upgrade_notice'] == {'from_version': 1, 'to_version': 2, 'reason': 'replay_passed'}
    assert new_result['output_data']['code_sha256'] == sha2
    assert get_monitor_descriptor(gate.home, 'following-monitor')['metadata']['pinned_version'] == 2
    assert 'upgrade_notice' not in _run_monitor(gate, 'following-monitor')[0]

    # 5. 重新构造引擎读取断点，证明恢复靠持久快照，而非旧实例保留的数据。
    restarted = WorkflowEngine(gate.home, analysis_handler=_pause_analysis)
    resumed = restarted.resume_workflow_run(paused['workflow_run_id'], patched_step_output={'ready': True})
    assert resumed.status == 'succeeded', resumed.to_dict()
    assert resumed.final_output == {'value': 13, 'code_sha256': sha1}
    assert restarted.store.load_workflow_snapshot(resumed.workflow_run_id)['_frozen_scripts'] == frozen['_frozen_scripts']

    # 6. 当前 workflow 也发布 v2 后，公开 version=1 入口仍执行旧流程和旧脚本。
    changed = gate.workflows._store().get_workflow('continuity-flow').to_dict()
    changed['body_markdown'] = 'Current v2 definition'
    _call(gate.workflows, {'action': 'save', 'workflow_id': 'continuity-flow',
        'definition': changed, 'expected_revision': flow_v1['storage_revision']})
    assert latest_version(gate.home, 'workflow', 'continuity-flow')['version'] == 2
    assert latest_version(gate.home, 'workflow', 'continuity-flow')['pins']['scripts']['continuity']['code_sha256'] == sha2
    historical = _call(gate.workflows, {'action': 'run', 'workflow_id': 'continuity-flow',
        'version': 1, 'inputs': {'value': 20, 'identify': True}})
    assert historical['status'] == 'succeeded' and historical['definition_version'] == 1, historical
    assert historical['final_output'] == {'value': 21, 'code_sha256': sha1}
    snapshot = gate.engine.store.load_workflow_snapshot(historical['workflow_run_id'])
    assert snapshot['_frozen_scripts']['calculate']['code_sha256'] == sha1
    assert snapshot['body_markdown'] != changed['body_markdown']
    assert get_version(gate.home, 'workflow', 'continuity-flow', 1) == flow_v1

    # 7. G1 需要 CAS；G2 真正调用写工具但不动正式正文；G3 只剥离宿主字段。
    current = gate.store.get_script('continuity')
    missing_revision = gate.scripts.execute({'action': 'update', 'script_id': 'continuity', 'code': code_v2})
    assert not missing_revision.ok
    denied = json.loads(missing_revision.output)
    assert gate.store.get_script('continuity')['revision'] == current['revision']
    direct = gate.writer.execute({'path': str(gate.home / 'scripts' / 'continuity' / 'main.py'),
        'content': 'print("direct write must be refused")\n'})
    assert not direct.ok and direct.error_code == 'PATH_HOST_MANAGED_STORE_BLOCKED', direct.output
    assert direct.effect_outcome == 'not_started'
    assert 'scripts(action=update)' in direct.output
    assert gate.store.get_script('continuity')['code'] == code_v2
    supplied = {**gate.descriptor, 'metadata': {**gate.descriptor['metadata'],
        'used_by': [{'kind': 'workflow', 'id': 'uncommitted-parent'}]}}
    cleaned = _call(gate.scripts, {'action': 'update', 'script_id': 'continuity',
        'expected_revision': current['revision'], 'descriptor': supplied})
    assert cleaned['stripped_metadata_keys'] == ['used_by']
    assert 'used_by' not in gate.store.get_script('continuity')['metadata']
    assert gate.store.get_script('continuity')['metadata']['test_scope'] == 'gate3'
    assert latest_version(gate.home, 'script', 'continuity')['version'] == 2

    # 8. 通过正式 rollback 追加 v3，旧版本、代码 blob 和工作流 pin 不被重写。
    _call(gate.scripts, {'action': 'rollback', 'script_id': 'continuity', 'to_version': 1,
        'expected_revision': gate.store.get_script('continuity')['revision']})
    restored = latest_version(gate.home, 'script', 'continuity')
    assert restored['version'] == 3 and restored['restored_from'] == 1
    assert restored['code_sha256'] == sha1
    assert gate.store.get_script('continuity')['code'] == gate.code_v1
    assert [row['version'] for row in list_versions(gate.home, 'script', 'continuity')] == [3, 2, 1]
    assert get_version(gate.home, 'script', 'continuity', 1) == v1
    assert get_version(gate.home, 'script', 'continuity', 2) == v2
    assert resolve_monitor_target(gate.home, 'following-monitor',
        {'kind': 'script', 'asset_id': 'continuity'})['pinned_version'] == 2

    # 9. 当前正文仍存在时移除历史 blob，必须明确 STALE，绝不能静默执行当前正文。
    blob = gate.home / 'data' / 'asset_evolution' / 'blobs' / sha1[:2] / sha1
    blob.unlink()
    assert gate.store.get_script('continuity')['code'] == gate.code_v1
    with pytest.raises(ExecutionTargetError) as stale:
        _run_monitor(gate, 'fixed-monitor')
    assert stale.value.error_code == 'SCHEDULER_EXECUTION_DEFINITION_STALE'
    assert stale.value.reason == 'blob_missing'
    assert get_monitor_descriptor(gate.home, 'fixed-monitor')['metadata']['pinned_version'] == 1

    # 拒绝回执的合同断言保留在链尾，使同次运行也能核对后续 G2/G3、回滚及缺失历史。
    assert denied.get('error_code') == 'ASSET_REVISION_REQUIRED', (missing_revision.error_code, denied)
    assert {'kind': 'workflow', 'id': 'continuity-flow'} in denied['dependents']


# LLM: 相同更新只增加一个规范监控绑定，拒绝合同不应由正确的 CAS 提示变成通用参数错误。
# 函数用途: 为跨模块依赖查询保留最小正反对照，不能用独立无监控单测掩盖集成错误。
def test_gate3_g1_preserves_structured_rejection_with_monitor(gate):
    _save_flow(gate)
    arguments = {'action': 'update', 'script_id': 'continuity', 'code': _source('candidate')}
    before = gate.scripts.execute(arguments)
    control = json.loads(before.output)
    assert not before.ok and control.get('error_code') == 'ASSET_REVISION_REQUIRED', control
    save_monitor_binding(gate.home, 'fixed-monitor', script_ids=['continuity'])
    after = gate.scripts.execute(arguments)
    observed = json.loads(after.output)
    assert not after.ok
    assert observed.get('error_code') == 'ASSET_REVISION_REQUIRED', (after.error_code, observed)
    assert {'kind': 'workflow', 'id': 'continuity-flow'} in observed['dependents']
    assert gate.store.get_script('continuity')['revision'] == 1


# LLM: 候选本身运行成功也不能代替依赖回放，输出不同必须留下提案而非签发兼容边。
# 函数用途: 证伪只凭成功退出就晋升或推进监控的实现。
def test_gate3_changed_replay_output_cannot_advance_monitor(gate):
    _seed_replay_runs(gate)
    save_monitor_binding(gate.home, 'following-monitor', script_ids=['continuity'],
        upgrade_policy='follow_compatible')
    candidate = _source('changed').replace('value + 1', 'value + 2')
    state, output = _edit_and_execute(gate, candidate)
    assert output['value'] == 2
    request = freeze_asset_mutations(gate.home, state, run_id='gate3-output-change')
    assert request['targets'][0]['verified'] is True
    result = AssetEvolutionService(gate.home, maintenance_mode='autonomous').process('gate3-output-change')
    route = result['routes'][0]
    assert route['status'] == 'proposal', result
    assert route['replay']['verdict'] == 'failed' and route['replay']['reason'] == 'output_changed'
    assert route['replay']['replayed'] == 1
    assert route['replay']['hashes'][0]['actual'] != route['replay']['hashes'][0]['expected']
    assert latest_version(gate.home, 'script', 'continuity')['version'] == 1
    assert gate.store.get_script('continuity')['code'] == gate.code_v1
    assert read_compat_edges(gate.home, 'script', 'continuity') == []
    selected = resolve_monitor_target(gate.home, 'following-monitor',
        {'kind': 'script', 'asset_id': 'continuity'})
    assert selected['pinned_version'] == 1 and 'upgrade_notice' not in selected


# LLM: 成功回执之后未经执行的新正文不是可学习候选，不可复用旧退出证明。
# 函数用途: 证伪冻结只相信缓存 SHA 而不核对当前字节的实现。
def test_gate3_post_execution_edit_cannot_freeze_unexecuted_code(gate):
    state, _output = _edit_and_execute(gate, _source('verified'))
    result = gate.editor.execute({'path': 'candidate.py', 'old_string': 'fixture verified.',
        'new_string': 'fixture not-executed.'})
    assert result.ok, result.output
    assert freeze_asset_mutations(gate.home, state, run_id='gate3-unexecuted') is None
    assert not (gate.home / 'data' / 'asset_evolution' / 'requests' / 'gate3-unexecuted.json').exists()
    assert latest_version(gate.home, 'script', 'continuity')['version'] == 1
