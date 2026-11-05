"""W1真实历史存储到引擎/W6 seam的冻结引用合同。"""

import ast
import inspect
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.assets.revision_history import (
    get_version,
    record_version,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.workflows.engine import WorkflowEngine, _script_pin
from agent_py_agent.agent.workflows.models import ScriptExecutionResult
from agent_py_agent.tests.test_v15_w6_pinned_code_run import (
    CountingLocalRunner,
    save_transient_version,
)
from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow


# LLM: 版本由W1原接口发表，不手写历史记录。
# 函数用途: 冻结脚本真实正文和执行身份。
def publish_script(home):
    row = ScriptStore(home).get_script('collect')
    pin = _script_pin('collect', row)
    version = record_version(home, 'script', 'collect', payload=row, code=row['code'].encode(),
                             execution_definition_ref=pin['execution_definition_ref'], storage_revision=row.get('revision', 0))
    return pin, version['record']


# LLM: 只替代尚未集成的W6历史代码执行；严格核真实引擎传入pin。
# 函数用途: 观察运行seam实际收到的冻结代码摘要。
def observe_seam(monkeypatch):
    seen = []

    # LLM: 结果是组件替身，不声称执行了历史脚本字节。
    # 函数用途: 回传本次seam收到的代码身份。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        seen.append(expected_script_pin)
        return ScriptExecutionResult(script_id, True, 0, 1, 1, 2,
                                     {'value': expected_script_pin['code_sha256']}, '', '')

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources', execute)
    return seen


# LLM: 必须在创建运行之前拒绝未知版本，不能留下幽灵run。
# 函数用途: 验证公开keyword和结构化版本缺失回执。
def test_unknown_workflow_version_has_no_run(tmp_path):
    parameter = inspect.signature(WorkflowEngine.run_workflow).parameters.get('definition_version')
    assert parameter is not None and parameter.kind == parameter.KEYWORD_ONLY and parameter.default is None
    WorkflowStore(tmp_path).save_workflow(workflow(step('judge', mode='llm_skill_analysis')))
    result = WorkflowEngine(tmp_path).run_workflow('daily', definition_version=99)
    assert result['error_code'] == 'ASSET_VERSION_NOT_FOUND' and result['effect_outcome'] == 'not_started'
    assert not list((tmp_path / 'data/workflow_runs').glob('*/run.json'))


# LLM: 当前脚本及工作流均改变后，版本运行仍从历史定义、Markdown与脚本pins恢复。
# 函数用途: 验证workflow@v1连锁固定script@v1及持久化字段。
@pytest.mark.parametrize('flat', [False, True])
def test_workflow_version_uses_history_definition_and_chained_script_pin(tmp_path, monkeypatch, flat):
    assert 'definition_version' in inspect.signature(WorkflowEngine.run_workflow).parameters
    script(tmp_path)
    old_pin, old = publish_script(tmp_path)
    store = WorkflowStore(tmp_path)
    saved = store.save_workflow(workflow(step(on_error='fail')))
    payload = saved.to_dict()
    if flat:
        payload = {**saved.descriptor.to_dict(), 'steps': [item.to_dict() for item in saved.steps], 'body_markdown': ''}
    record_version(tmp_path, 'workflow', 'daily', payload=payload, side_files={'WORKFLOW.md': 'historical body'},
                   pins={'scripts': {'collect': {'version': old['version'], 'code_sha256': old_pin['code_sha256'],
                                                'execution_definition_ref': old_pin['execution_definition_ref']}}})
    script(tmp_path, 'print(\'{"value":2}\')')
    publish_script(tmp_path)
    store.save_workflow(workflow(step('current', mode='llm_skill_analysis')))
    seen = observe_seam(monkeypatch)
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: pytest.fail('current definition executed'))
    run = engine.run_workflow('daily', definition_version=1)
    assert run.status == 'succeeded' and run.definition_version == 1
    assert len(seen) == 1 and seen[0]['code_sha256'] == old_pin['code_sha256']
    assert run.final_output == {'value': old_pin['code_sha256']}
    snapshot = engine.store.load_workflow_snapshot(run.workflow_run_id)
    assert snapshot['body_markdown'] == 'historical body'
    assert snapshot['_frozen_scripts']['collect']['code_sha256'] == old_pin['code_sha256']
    assert engine.store.load_run(run.workflow_run_id).definition_version == 1


# LLM: 在途运行冻结旧sha，显式allow才可改用当前；缺blob时保留原漂移门。
# 函数用途: 验证EC39恢复、缺失历史及显式重新钉住。
@pytest.mark.parametrize('remove_blob,allow,expected_status', [
    (False, False, 'succeeded'), (True, False, 'paused_for_resume'), (True, True, 'succeeded')])
def test_resume_preserves_historical_pin_unless_explicitly_allowed(tmp_path, monkeypatch, remove_blob, allow, expected_status):
    script(tmp_path)
    old_pin, _ = publish_script(tmp_path)
    seen = observe_seam(monkeypatch)
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: {'_pause_for_resume': True})
    run = engine.run_workflow(workflow(step('pause', mode='llm_skill_analysis'), step(on_error='fail')))
    assert run.status == 'paused_for_resume' and not seen
    script(tmp_path, 'print(\'{"value":2}\')')
    current_pin, _ = publish_script(tmp_path)
    if remove_blob:
        sha = old_pin['code_sha256']
        (tmp_path / 'data/asset_evolution/blobs' / sha[:2] / sha).unlink()
    resumed = engine.resume_workflow_run(run.workflow_run_id, patched_step_output={'ready': True},
                                          allow_script_drift=allow)
    assert resumed.status == expected_status
    if expected_status == 'succeeded':
        expected = current_pin if allow else old_pin
        assert seen[-1]['code_sha256'] == expected['code_sha256']
        assert resumed.final_output['value'] == expected['code_sha256']
    else:
        assert not seen and resumed.steps[1].error_code == 'SCRIPT_REVISION_DRIFT'


# LLM: 正式Store及发布钩子冻结v1，不直接拼写历史文件或执行结果。
# 函数用途: 创建同代码/ref、不同重试许可的工作流历史。
def retry_workflow_versions(home, *, old_policy=False, current_policy=True, pause=False, foreach=None):
    first = save_transient_version(home, idempotent=old_policy)
    specs = ([step('pause', mode='llm_skill_analysis')] if pause else [])
    specs.append(step(script_id='probe', on_error='fail', **({'foreach': foreach} if foreach else {})))
    WorkflowStore(home).save_workflow(workflow(*specs))
    published = get_version(home, 'workflow', 'daily', 1)
    assert published['pins']['scripts']['probe']['version'] == 1
    current = save_transient_version(home, idempotent=current_policy)
    assert first['code'] == current['code']
    assert first['execution_definition_ref'] == current['execution_definition_ref']
    assert first['revision'] != current['revision']
    return first, current


# LLM: 目录枚举本身的FD每次以with关闭；测量仅覆盖本进程，不外推平台沙箱或真实服务。
# 函数用途: 统计当前进程的真实打开FD，供成功及拒绝路径前后对照。
def fd_count():
    with os.scandir('/proc/self/fd' if Path('/proc/self/fd').exists() else '/dev/fd') as entries:
        return sum(1 for _ in entries)


# LLM: 独立打开/关闭控制证明观察器对一个实际FD有响应，防止把常量计数当作零泄漏。
# 函数用途: 验证FD观察点自身有效。
def test_fd_observation_detects_open_and_close():
    before = fd_count()
    with open(os.devnull) as stream:
        assert stream.fileno() >= 0 and fd_count() == before + 1
    assert fd_count() == before


# LLM: 真实编号历史必须一路保留到原runner；无异常的健康红灯不能成为额外执行许可。
# 函数用途: 验证正式、恢复及批执行的历史策略，和反向当前策略收窄。
@pytest.mark.parametrize('route,old_policy,current_policy', [
    ('normal', False, True), ('resume', False, True), ('foreach', False, True), ('normal', True, False)])
def test_versioned_workflow_keeps_equal_code_retry_provenance(tmp_path, route, old_policy, current_policy):
    batching = {'from': 'inputs.items', 'as': 'batch', 'batch_size': 1, 'max_parallel': 1,
                'mode': 'map'} if route == 'foreach' else None
    _, current = retry_workflow_versions(tmp_path, old_policy=old_policy, current_policy=current_policy,
        pause=route == 'resume', foreach=batching)
    script_json = tmp_path / 'scripts/probe/script.json'
    before_bytes = script_json.read_bytes()
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'_pause_for_resume': True})
    engine.runner = CountingLocalRunner(tmp_path)
    before_fd = fd_count()
    run = engine.run_workflow('daily', {'items': [1, 2]} if batching else {}, definition_version=1)
    if route == 'resume':
        assert run.status == 'paused_for_resume' and not engine.runner.calls
        run = engine.resume_workflow_run(run.workflow_run_id, patched_step_output={'ready': True})
    assert run.status == 'succeeded' and run.definition_version == 1
    assert len(engine.runner.calls) == (2 if batching else 1)
    assert all(isinstance(call['history'], dict) and call['history'].get('version') == 1
               for call in engine.runner.calls)
    assert all(call['record']['metadata']['retry_policy']['idempotent'] is False for call in engine.runner.calls)
    assert all(value.ok and value.exit_code == 0 for value in engine.runner.results)
    assert all(value.source_check['pin_resolution']['version'] == 1 for value in engine.runner.results)
    health = run.steps[-1].escalation_result['query_health']
    assert health['classification'] == 'TRANSIENT_FAILURE_EMPTY' and health['analysis_blocked'] is True
    assert health['retries'] == []
    snapshot = engine.store.load_workflow_snapshot(run.workflow_run_id)
    assert snapshot['_frozen_scripts']['collect']['version'] == 1
    assert engine.store.load_run(run.workflow_run_id).definition_version == 1
    assert script_json.read_bytes() == before_bytes
    assert ScriptStore(tmp_path).get_script('probe')['revision'] == current['revision']
    assert get_version(tmp_path, 'script', 'probe', 2)['version'] == 2
    assert not list((tmp_path / 'tmp/script_exec').iterdir())
    assert all(not Path(value.output_data['stage']).exists() for value in engine.runner.results)
    assert fd_count() - before_fd == 0


# LLM: 定义读取后才移走历史blob，确保触达_script_step接缝，不把入口已拒绝算作调用修复。
# 函数用途: 相同当前代码存在时，历史code/payload缺失仍零执行且不改v2。
@pytest.mark.parametrize('missing', ['code_sha256', 'payload_blob'])
def test_equal_code_history_missing_after_definition_resolution_refuses_run(tmp_path, monkeypatch, missing):
    retry_workflow_versions(tmp_path)
    history = get_version(tmp_path, 'script', 'probe', 1)
    digest = history[missing]
    blob = tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest
    original = WorkflowEngine._script_step
    reached = []

    # LLM: 仅控制历史在入口和步骤之间消失的时机；真正步骤、runtime和runner均不替换结果。
    # 函数用途: 在已解析历史定义之后制造一次明确的缺失观察。
    def remove_history(engine, record, spec, run, definition, *, record_summary=True):
        reached.append(spec['step_id'])
        blob.unlink()
        return original(engine, record, spec, run, definition, record_summary=record_summary)

    monkeypatch.setattr(WorkflowEngine, '_script_step', remove_history)
    engine = WorkflowEngine(tmp_path)
    engine.runner = CountingLocalRunner(tmp_path)
    current_bytes = (tmp_path / 'scripts/probe/script.json').read_bytes()
    before_fd = fd_count()
    run = engine.run_workflow('daily', definition_version=1)
    assert reached == ['collect']
    assert run.status == 'failed' and run.steps[0].error_code == 'SCRIPT_REVISION_DRIFT'
    assert engine.runner.calls == []
    assert (tmp_path / 'scripts/probe/script.json').read_bytes() == current_bytes
    assert fd_count() - before_fd == 0


# LLM: 历史enabled并不授当前Owner执行权，停用在引擎原门拒绝，当前记录保持停用。
# 函数用途: 验证版本运行不能复活当前关闭的脚本。
def test_equal_code_current_disablement_blocks_historical_workflow(tmp_path):
    retry_workflow_versions(tmp_path)
    ScriptStore(tmp_path).set_enabled('probe', False)
    engine = WorkflowEngine(tmp_path)
    engine.runner = CountingLocalRunner(tmp_path)
    before_fd = fd_count()
    run = engine.run_workflow('daily', definition_version=1)
    assert run.status == 'failed' and run.steps[0].error_code == 'ASSET_DISABLED_BY_OWNER'
    assert engine.runner.calls == []
    assert ScriptStore(tmp_path).get_script('probe')['enabled'] is False
    assert fd_count() - before_fd == 0


# LLM: debug别名共用_script_step，但无历史选择时仍执行当前；record_summary=False禁止正式写账。
# 函数用途: 验证两条debug脚本入口继续隔离运行、摘要及输出目标。
@pytest.mark.parametrize('mode', ['deterministic_script', 'script_transform'])
def test_current_script_debug_keeps_isolation_and_summary_false(tmp_path, mode):
    save_transient_version(tmp_path, idempotent=True)
    WorkflowStore(tmp_path).save_workflow(workflow(step(script_id='probe', mode=mode)))
    script_json = tmp_path / 'scripts/probe/script.json'
    before_bytes = script_json.read_bytes()
    engine = WorkflowEngine(tmp_path)
    engine.runner = CountingLocalRunner(tmp_path)
    before_fd = fd_count()
    result = engine.debug_step('daily', 'collect', allow_side_effects=True)
    assert result['status'] == 'succeeded'
    assert len(engine.runner.calls) == 3
    assert all(call['history'] is None and call['record_summary'] is False for call in engine.runner.calls)
    assert script_json.read_bytes() == before_bytes
    assert ScriptStore(tmp_path).get_script('probe')['runtime_summary']['run_count'] == 0
    assert not list((tmp_path / 'data/workflow_runs').glob('*/run.json'))
    assert not list((tmp_path / 'tmp/script_exec').iterdir())
    assert fd_count() - before_fd == 0


# LLM: 结构门只锁定本次传参合同，不扩大_same_pin的执行身份比较或修改共享runtime。
# 函数用途: 静态拦截重新引入按drift替换历史来源的同类错误。
def test_engine_execution_pin_ast_keeps_frozen_provenance():
    tree = ast.parse(inspect.getsource(__import__(WorkflowEngine.__module__, fromlist=['WorkflowEngine'])))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'run_script_with_datasources']
    assert len(calls) == 1
    choice = next(item.value for item in calls[0].keywords if item.arg == 'expected_script_pin')
    expected = ast.parse('frozen if frozen is not None else current', mode='eval').body
    assert ast.dump(choice) == ast.dump(expected)


# LLM: Event固定旧步骤读取早于终态发表；晚到步骤及普通run写不得复活成功、失败或取消。
# 函数用途: 在真实版本工作流恢复/取消后验证终态单调。
@pytest.mark.parametrize('terminal', ['succeeded', 'failed', 'cancelled'])
def test_versioned_terminal_rejects_event_ordered_late_writes(tmp_path, terminal):
    retry_workflow_versions(tmp_path, pause=True)
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'_pause_for_resume': True})
    engine.runner = CountingLocalRunner(tmp_path)
    run = engine.run_workflow('daily', definition_version=1)
    assert run.status == 'paused_for_resume'
    loaded, publish = threading.Event(), threading.Event()
    before_fd = fd_count()

    # LLM: 旧DTO来自真实Store，等待主线程完成终态后才交回原持久化入口。
    # 函数用途: 确定性复现晚到的旧步骤发表及普通状态倒退尝试。
    def late_writer():
        old = engine.store.load_run(run.workflow_run_id)
        loaded.set()
        assert publish.wait(15)
        with pytest.raises(ValueError, match='WORKFLOW_RUN_TERMINAL'):
            engine.store.save_step_result(run.workflow_run_id, replace(old.steps[-1], status='running'))
        current = engine.store.load_run(run.workflow_run_id)
        with pytest.raises(ValueError, match='WORKFLOW_RUN_TERMINAL'):
            engine.store.save_run(replace(current, status='running'))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(late_writer)
        try:
            assert loaded.wait(15)
            if terminal == 'cancelled':
                assert engine.cancel_workflow_run(run.workflow_run_id,
                    expected_revision=run.revision, operation_key='cancel-pinned') == []
            else:
                if terminal == 'failed':
                    ScriptStore(tmp_path).set_enabled('probe', False)
                resumed = engine.resume_workflow_run(run.workflow_run_id, patched_step_output={'ready': True})
                assert resumed.status == terminal
        finally:
            publish.set()
        future.result(timeout=15)
    assert engine.store.load_run(run.workflow_run_id).status == terminal
    assert len(engine.runner.calls) == (1 if terminal == 'succeeded' else 0)
    assert fd_count() - before_fd == 0


# LLM: Event分别停在真实执行体和原ACK之前；证明先持久化并保留到确认，同Scope第二执行不准入。
# 函数用途: 验证版本脚本路径的互斥、退出证明单调保留及FD归还。
def test_versioned_scope_keeps_single_body_and_cleanup_proof_until_ack(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import concurrency_policy
    from agent_py_agent.agent.workflows.run_store import WorkflowRunStore

    retry_workflow_versions(tmp_path)
    engine = WorkflowEngine(tmp_path)
    engine.runner = CountingLocalRunner(tmp_path)
    entered, release = threading.Event(), threading.Event()
    at_ack, allow_ack = threading.Event(), threading.Event()
    active, maximum, leases = [0], [0], []
    original_run, original_ack = engine.runner.run_script, WorkflowRunStore._ack_concurrency_release

    # LLM: 只安排调用时序，真实本地runner每次均委托，finally维护观察计数不改产品状态。
    # 函数用途: 阻止首执行体退出，观察竞争准入时是否发生重叠。
    def execute(*args, **kwargs):
        active[0] += 1
        maximum[0] = max(maximum[0], active[0])
        entered.set()
        try:
            assert release.wait(15)
            return original_run(*args, **kwargs)
        finally:
            active[0] -= 1

    # LLM: ACK之前不伪造成功，也不清理任何原证明；事件放行后继续原持久化方法。
    # 函数用途: 暴露真实release_pending窗口供独立读取核对。
    def acknowledge(store, lease):
        leases.append(lease)
        at_ack.set()
        assert allow_ack.wait(15)
        return original_ack(store, lease)

    monkeypatch.setattr(engine.runner, 'run_script', execute)
    monkeypatch.setattr(WorkflowRunStore, '_ack_concurrency_release', acknowledge)
    before_fd = fd_count()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(engine.run_workflow, 'daily', definition_version=1, concurrency_policy='skip_if_running')
        try:
            assert entered.wait(15)
            competitor = WorkflowEngine(tmp_path)
            competitor.runner = engine.runner
            second = competitor.run_workflow('daily', definition_version=1, concurrency_policy='skip_if_running')
            assert second['status'] == 'skipped' and active[0] == 1
            assert competitor.store.load_run(second['workflow_run_id']).status == 'skipped'
            release.set()
            assert at_ack.wait(15)
            lease = leases[0]
            run_json = tmp_path / 'data/workflow_runs' / lease.record['run_id'] / 'run.json'
            proof = json.loads(run_json.read_text())['concurrency_claim']
            assert proof['phase'] == 'release_pending' and proof['record'] == lease.record
            scope = concurrency_policy._scope(tmp_path, 'workflow', 'daily', lease.record['run_id'])
            key = concurrency_policy._claim_key(scope, lease)
            assert concurrency_policy._PENDING_RELEASES[key] == lease and active[0] == 0
        finally:
            release.set()
            allow_ack.set()
        first = future.result(timeout=15)
    assert first.status == 'succeeded' and maximum[0] == 1
    assert len(engine.runner.calls) == 1
    confirmed = json.loads(run_json.read_text())['concurrency_claim']
    assert confirmed['phase'] == 'released' and confirmed['record'] == proof['record']
    assert key not in concurrency_policy._PENDING_RELEASES
    assert fd_count() - before_fd == 0
