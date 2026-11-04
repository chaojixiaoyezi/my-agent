"""P5.GATE2：真实脚本→数据源运行时→工作流门→演进冻结的离线集成验收。

只替代分析回调、时钟和 IM 接收端，不替代健康裁决、预检、重试或引擎。
原 ScriptRunner 使用本地子进程与 Python 根守卫；本文件不验原生 OS 沙箱。
"""

import json
import os
import random
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.agent.assets.descriptor import (
    AccessTarget,
    AssetDescriptor,
    DataSourceSpec,
    IOContract,
)
from agent_py_agent.agent.assets.mutation_ledger import (
    AssetMutationLedger,
    freeze_asset_mutations,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.source_manifest import load_manifest, save_manifest
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.capability.asset_evolution import AssetEvolutionService
from agent_py_agent.agent.capability.asset_evolution_routes import read_evolution_json
from agent_py_agent.agent.workflows import datasource_runtime
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.script_runner import ScriptRunner

_SCRIPT_SOURCE = '''import json, os, sys
inputs = json.load(sys.stdin)
output = {"values": [], "execution_pid": os.getpid()}
report = inputs.get("report")
if "batch" in inputs:
    batch = inputs["batch"]
    failed = batch[0] == 2
    report = {"stage": "probe+fetch", "discovered_sources": ["events-a", "events-b"],
              "sources": [{"source_id": name,
                           "status": "transient_error" if failed else "ok",
                           "hit_count": 0 if failed else 1,
                           "http_status": None,
                           "error_code": "ETIMEDOUT" if failed else ""}
                          for name in ("events-a", "events-b")]}
    output["values"] = [] if failed else batch
elif isinstance(report, dict) and isinstance(report.get("sources"), list):
    output["values"] = [row["source_id"] for row in report["sources"]
                        if isinstance(row, dict) and row.get("status") == "ok"
                        and type(row.get("hit_count")) is int and row["hit_count"] > 0]
if "report" in inputs or "batch" in inputs:
    output["source_report"] = report
print(json.dumps(output))
'''


# LLM: 每例隔离 Owner、环境及随机源，结束时恢复全局种子，不接真实服务。
# 函数用途: 给所有 gate 案例提供独立临时家目录。
@pytest.fixture
def gate_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    state = random.getstate()
    random.seed(12)
    try:
        yield tmp_path
    finally:
        random.setstate(state)


# LLM: 观察是合成本地输入，不声称已访问真实端点或解析真实凭据。
# 函数用途: 构造一个标准来源观察。
def _source(source_id, status='ok', hits=1, *, http_status=None, error_code=''):
    return {'source_id': source_id, 'status': status, 'hit_count': hits,
            'http_status': http_status, 'error_code': error_code}


# LLM: probe 集合与 fetch 结果分别输入，避免把两者相等写死进测试。
# 函数用途: 构造脚本将实际打印的两阶段报告。
def _report(rows, *, discovered=None):
    return {'sources': rows, 'discovered_sources': [row['source_id'] for row in rows]
            if discovered is None else discovered, 'stage': 'probe+fetch'}


# LLM: 所有 Store、runner 和 engine 均为原实现，只注入外部分析器。
# 函数用途: 建立有真实定义、清单和运行账本的最小工作流。
def _make_gate(home, *, declared=('events-a', 'events-b'), available=None,
               bound=True, core=(), access='read', idempotent=False):
    home.mkdir(parents=True, exist_ok=True)
    connections = DatasourceStore(home)
    for identity in declared if available is None else available:
        connections.save_datasource({'datasource_id': identity, 'kind': 'custom', 'title': identity})
    descriptor = AssetDescriptor('collect', 'script', '本地来源观察',
        data_sources=tuple(DataSourceSpec(identity, 'custom', query_scope={'access': 'read'})
                           for identity in declared),
        access_targets=(AccessTarget('fixture', 'file', 'fixture.json',
                                     extra={} if access is None else {'access': access}),),
        io_contract=IOContract(outputs=('values', 'execution_pid')),
        metadata={'retry_policy': {'idempotent': idempotent},
                  **({'source_system': 'events'} if bound else {})})
    scripts = ScriptStore(home)
    scripts.save_script(descriptor, _SCRIPT_SOURCE, timeout_seconds=30)
    if bound:
        save_manifest(home, 'events', [
            {'source_id': identity, 'core': identity in core,
             'credential_ref': 'cref-' + identity, 'endpoint_ref': 'endpoint-' + identity}
            for identity in declared])
    seen = []

    # LLM: 只记录真正分派到分析端的上下文，不替引擎作健康判断。
    # 函数用途: 让是否误分派及健康数据是否保留均能被断言推翻。
    def analyze(spec, context, skills):
        seen.append({'step_id': spec['step_id'], 'inputs': deepcopy(context.step_inputs),
                     'prior_outputs': deepcopy(context.prior_outputs),
                     'run_params': deepcopy(context.run_params)})
        return {'values': deepcopy(context.step_inputs.get('batch',
                                      context.step_inputs.get('values', [])))}

    engine = WorkflowEngine(home, analysis_handler=analyze)
    # 使用公开配置的原进程执行器，不 mock runner，也不依赖嵌套 OS 沙箱。
    engine.runner = ScriptRunner(home, use_sandbox=False, restrict_to_roots=True)
    WorkflowStore(home).save_workflow({
        'descriptor': {'id': 'guardrails', 'kind': 'workflow', 'name': '来源健康验收'},
        'steps': [
            {'step_id': 'collect', 'name': 'collect', 'mode': 'deterministic_script',
             'script_id': 'collect', 'on_error': 'fail'},
            {'step_id': 'analyze', 'name': 'analyze', 'mode': 'llm_skill_analysis',
             'inputs_map': {'values': {'step_id': 'collect', 'key': 'values'}},
             'on_error': 'fail'}]})
    return SimpleNamespace(home=home, engine=engine, seen=seen, scripts=scripts)


# LLM: 通过保存后的定义 ID 运行，回读同一个 run，而非调用纯值 helper。
# 函数用途: 执行脚本和下游分析，并核对持久化往返。
def _run(gate, report=None, *, include_report=True):
    run = gate.engine.run_workflow('guardrails', {'report': report} if include_report else {})
    assert gate.engine.store.load_run(run.workflow_run_id) == run
    return run


# LLM: 脚本必须真的成功打印有效 JSON，不能以预检失败冒充健康门通过。
# 函数用途: 从真实步骤回执取健康裁决并证明执行发生于另一进程。
def _health(run):
    collect = run.steps[0]
    assert collect.status == 'succeeded', (collect.error_code, collect.error_message)
    assert collect.error_code == '' and collect.llm_involved is False
    pid = collect.output_snapshot['execution_pid']
    assert type(pid) is int and pid > 0 and pid != os.getpid()
    health = collect.escalation_result['query_health']
    assert health['schema_version'] == 'query_health.v1'
    assert health['coverage'] == 'declared'
    return health


# LLM: 新源结果和退休源差集必须保留，执行不能自行登记或删改清单。
# 函数用途: 验证 DS-1 跨 Store、脚本、健康回执和分析入口的完整链。
def test_ds1_drift_preserves_new_source_results_without_registration(gate_home):
    gate = _make_gate(gate_home)
    path = gate.home / 'data/source_manifests/events.json'
    before = path.read_bytes()
    manifest = load_manifest(gate.home, 'events')
    raw = _report([_source('events-a'), _source('events-new', hits=7)],
                  discovered=['events-new', 'events-a', 'events-new'])
    run = _run(gate, raw)
    health = _health(run)
    assert run.status == 'succeeded'
    assert health['classification'] == 'PARTIAL_SOURCE_FAILURE'
    assert health['unregistered_sources_detected'] == ['events-new']
    assert health['registered_sources_missing'] == ['events-b']
    assert health['degraded_sources'] == [{'source_id': 'events-b', 'error_code': 'SOURCE_MISSING'}]
    assert health['analysis_blocked'] is False
    assert gate.seen[0]['inputs']['values'] == ['events-a', 'events-new']
    assert run.steps[0].output_snapshot['source_report'] == raw
    assert path.read_bytes() == before and load_manifest(gate.home, 'events') == manifest
    with pytest.raises(KeyError):
        DatasourceStore(gate.home).get_datasource_spec('events-new')


# LLM: 只替换时钟与 IM 接收端，去重仍由真实健康账原子完成。
# 函数用途: 验证 DS-2 窗口内不重发、恰满24小时重发且每次回执保留红灯。
def test_ds2_auth_alert_is_persistent_and_deduplicated_for_24_hours(gate_home, monkeypatch):
    gate = _make_gate(gate_home)
    # 固定未来时刻，避免真实子进程 SDK 将测试时钟签发的清单误判为过期。
    clock = SimpleNamespace(now=4102444800.0)
    monkeypatch.setattr(datasource_runtime, 'time', SimpleNamespace(
        time=lambda: clock.now, monotonic=time.monotonic, sleep=time.sleep))
    raw = _report([_source('events-a', 'auth_expired', 0, http_status=401), _source('events-b')])
    raw['sources'][0]['credential_ref'] = 'untrusted-report-ref'
    notices = []
    with datasource_runtime.bind_asset_im_notifier(gate.home, notices.append):
        for elapsed, sent in ((0, 1), (86399, 1), (86400, 2)):
            clock.now = 4102444800.0 + elapsed
            run = _run(gate, raw)
            health = _health(run)
            assert run.status == 'succeeded'
            assert health['auth_alerts'] == [{
                'source_id': 'events-a', 'credential_ref': 'cref-events-a',
                'source_health_status': 'AUTH_OR_ENDPOINT_EXPIRED'}]
            assert len(notices) == sent
    assert [notice['event_type'] for notice in notices] == ['AUTH_OR_ENDPOINT_EXPIRED'] * 2
    assert all(notice['payload']['credential_ref'] == 'cref-events-a' for notice in notices)
    state = json.loads((gate.home / 'data/source_manifests/events.health.json').read_text())
    assert state['schema_version'] == 'datasource_health.v1'
    assert set(state['alerts']) == {'events-a|cref-events-a'}


# LLM: 非核心失败允许健康结果继续，核心失败即使别源命中也不能分派分析。
# 函数用途: 验证 DS-3 降级数据、保留原报告及 core 硬门。
@pytest.mark.parametrize('core,blocked', [((), False), (('events-b',), True)])
def test_ds3_degraded_source_is_isolated_and_core_failure_blocks(gate_home, core, blocked):
    gate = _make_gate(gate_home, core=core)
    raw = _report([_source('events-a'), _source('events-b', 'offline', 99)])
    run = _run(gate, raw)
    health = _health(run)
    assert health['classification'] == 'PARTIAL_SOURCE_FAILURE'
    assert health['healthy_sources_queried'] == 1
    assert health['degraded_sources'] == [{'source_id': 'events-b', 'error_code': 'SOURCE_MISSING'}]
    assert health['analysis_blocked'] is blocked
    assert run.steps[0].output_snapshot['source_report'] == raw
    assert run.steps[0].output_snapshot['values'] == ['events-a']
    if blocked:
        assert run.status == 'failed' and gate.seen == []
        assert run.steps[1].error_code == 'ANALYSIS_BLOCKED_NO_DATA'
        assert run.steps[1].output_snapshot['block_reason'] == 'QUERY_FAILED_NO_DATA'
    else:
        assert run.status == 'succeeded' and len(gate.seen) == 1
        assert gate.seen[0]['inputs']['values'] == ['events-a']
        assert gate.seen[0]['run_params'] == {'analysis_isolation': 'stateless_skill'}


# LLM: 多源 manifest 部分缺失必须真正运行脚本，且不能把预检缺失洗成健康。
# 函数用途: 验证 DS-3 partial preflight 与下游健康结果连续执行。
def test_ds3_partial_preflight_runs_with_one_available_source(gate_home):
    gate = _make_gate(gate_home, available=('events-a',))
    run = _run(gate, _report([_source('events-a')]))
    health = _health(run)
    assert run.status == 'succeeded' and len(gate.seen) == 1
    assert health['classification'] == 'PARTIAL_SOURCE_FAILURE'
    assert health['healthy_sources_queried'] == 1 and health['analysis_blocked'] is False
    assert health['degraded_sources'] == [{'source_id': 'events-b', 'error_code': 'SOURCE_MISSING'}]
    assert gate.seen[0]['inputs']['values'] == ['events-a']
    assert not list((gate.home / 'tmp/script_exec').iterdir())


# LLM: 脚本自报成功不能覆盖真实预检；核心缺失不因报告中伪造命中而消失。
# 函数用途: 验证跨预检、运行时和分析门的事实优先级。
def test_ds3_preflight_missing_core_cannot_be_overridden_by_report(gate_home):
    gate = _make_gate(gate_home, available=('events-a',), core=('events-b',))
    raw = _report([_source('events-a'), _source('events-b', hits=99)])
    run = _run(gate, raw)
    health = _health(run)
    assert run.steps[0].output_snapshot['source_report'] == raw
    assert health['healthy_sources_queried'] == 1 and health['analysis_blocked'] is True
    assert health['degraded_sources'] == [{'source_id': 'events-b', 'error_code': 'SOURCE_MISSING'}]
    assert run.status == 'failed' and gate.seen == []
    assert run.steps[1].error_code == 'ANALYSIS_BLOCKED_NO_DATA'


# LLM: partial 不是单源、全部源缺失或未绑定清单的通用绕过开关。
# 函数用途: 锁住 DS-3 与原预检硬停的兼容边界。
@pytest.mark.parametrize('declared,available,bound', [
    (('events-a',), (), True),
    (('events-a', 'events-b'), (), True),
    (('events-a', 'events-b'), ('events-a',), False),
])
def test_ds3_partial_mode_does_not_bypass_existing_preflight_stops(
        gate_home, declared, available, bound):
    gate = _make_gate(gate_home, declared=declared, available=available, bound=bound)
    run = _run(gate, _report([_source('events-a')]))
    assert run.status == 'failed' and gate.seen == []
    assert run.steps[0].error_code == 'DATASOURCE_MISSING'
    assert run.steps[0].output_snapshot == {}
    assert not (gate.home / 'tmp/script_exec').exists()
    assert run.steps[1].status == 'pending' and not run.steps[1].llm_involved


# LLM: 输入只改变显式只读/幂等声明，实际脚本和瞬态观察保持相同。
# 函数用途: 验证 DS-4 最多两次重试，写/未知访问不重放，最终空失败不派分析。
@pytest.mark.parametrize('access,idempotent,retry_count', [
    ('read', False, 2), ('read_write', False, 0), (None, False, 0),
    ('read_write', 'true', 0), ('read_write', True, 2),
])
def test_ds4_retry_policy_and_analysis_block_are_end_to_end(
        gate_home, access, idempotent, retry_count):
    gate = _make_gate(gate_home, access=access, idempotent=idempotent)
    raw = _report([_source(identity, 'transient_error', 0, error_code='ETIMEDOUT')
                   for identity in ('events-a', 'events-b')])
    run = _run(gate, raw)
    health = _health(run)
    assert health['classification'] == 'TRANSIENT_FAILURE_EMPTY'
    assert health['analysis_blocked'] is True and health['block_reason'] == 'QUERY_FAILED_NO_DATA'
    assert [row['attempt'] for row in health['retries']] == list(range(1, retry_count + 1))
    assert [row['error_code'] for row in health['retries']] == ['ETIMEDOUT'] * retry_count
    assert all(row['delay_ms'] >= 0 for row in health['retries'])
    # 原摘要账统计真实进程执行次数，不能仅凭 retries 列表宣称已重跑。
    summary = gate.scripts.fold_run_summaries('collect')['runtime_summary']
    assert summary['run_count'] == 1 + retry_count
    assert run.status == 'failed' and gate.seen == []
    assert run.steps[1].error_code == 'ANALYSIS_BLOCKED_NO_DATA'
    assert run.steps[1].llm_involved is False
    assert run.steps[1].output_snapshot == {
        'data_retrieval_failed': True, 'analysis_blocked': True,
        'block_reason': 'QUERY_FAILED_NO_DATA', 'source_ids': ['events-a', 'events-b'],
        'input_step_ids': ['collect']}
    assert not list((gate.home / 'tmp/script_exec').iterdir())


# LLM: 相同空 values 下，健康零命中可以分析，失败/漏报源不能冒充真零。
# 函数用途: 锁住 DS-4 SUCCESS_ZERO_HITS 的全源健康前置条件。
@pytest.mark.parametrize('other_status,classification,blocked,healthy', [
    ('zero_hits', 'SUCCESS_ZERO_HITS', False, 2),
    ('ok', 'SUCCESS_ZERO_HITS', False, 2),
    ('offline', 'TRANSIENT_FAILURE_EMPTY', True, 1),
    ('transient_error', 'TRANSIENT_FAILURE_EMPTY', True, 1),
    (None, 'TRANSIENT_FAILURE_EMPTY', True, 1),
])
def test_ds4_success_zero_hits_requires_every_source_to_be_healthy(
        gate_home, other_status, classification, blocked, healthy):
    gate = _make_gate(gate_home)
    rows = [_source('events-a', 'zero_hits', 0)]
    if other_status is not None:
        rows.append(_source('events-b', other_status, 0))
    run = _run(gate, _report(rows, discovered=['events-a', 'events-b']))
    health = _health(run)
    assert run.steps[0].output_snapshot['values'] == []
    assert health['classification'] == classification
    assert health['analysis_blocked'] is blocked and health['healthy_sources_queried'] == healthy
    if blocked:
        assert gate.seen == [] and run.status == 'failed'
        assert run.steps[1].error_code == 'ANALYSIS_BLOCKED_NO_DATA'
    else:
        assert run.status == 'succeeded' and len(gate.seen) == 1
        assert health['degraded_sources'] == [] and health['retries'] == []
        assert gate.seen[0]['inputs']['values'] == []


# LLM: 脚本进程和 IO 合同成功仍不能替代 manifest 要求的来源报告。
# 函数用途: 验证真正缺少 source_report 时的 SOURCE_REPORT_MISSING 硬门。
def test_ds4_missing_source_report_blocks_analysis_after_successful_script(gate_home):
    gate = _make_gate(gate_home)
    run = _run(gate, include_report=False)
    health = _health(run)
    assert 'source_report' not in run.steps[0].output_snapshot
    assert health['analysis_blocked'] is True
    assert health['classification'] == 'TRANSIENT_FAILURE_EMPTY'
    assert health['block_reason'] == 'SOURCE_REPORT_MISSING'
    assert run.status == 'failed' and gate.seen == []
    assert run.steps[1].error_code == 'ANALYSIS_BLOCKED_NO_DATA'
    assert run.steps[1].output_snapshot['block_reason'] == 'SOURCE_REPORT_MISSING'


# LLM: 报告键存在但形状错误仍按缺报告处理，不以正常退出或空列表洗绿。
# 函数用途: 验证畸形报告跨进程到引擎的保守判门。
@pytest.mark.parametrize('raw', [None, {}, [], {'sources': [], 'stage': 'probe+fetch'}])
def test_ds4_malformed_report_cannot_be_a_clean_zero_result(gate_home, raw):
    gate = _make_gate(gate_home)
    run = _run(gate, raw)
    health = _health(run)
    assert health['block_reason'] == 'SOURCE_REPORT_MISSING'
    assert health['analysis_blocked'] is True and gate.seen == []
    assert run.steps[1].error_code == 'ANALYSIS_BLOCKED_NO_DATA'


# LLM: 新报告要求仅适用 manifest 绑定，旧未声明脚本不应被新硬门误伤。
# 函数用途: 以同一实际脚本作为缺报告硬门的兼容反例。
def test_ds4_unbound_legacy_script_keeps_undeclared_coverage(gate_home):
    gate = _make_gate(gate_home, bound=False)
    run = _run(gate, include_report=False)
    health = run.steps[0].escalation_result['query_health']
    assert health['coverage'] == 'undeclared' and health['analysis_blocked'] is False
    assert run.status == 'succeeded' and len(gate.seen) == 1
    assert 'source_report' not in run.steps[0].output_snapshot


# LLM: 每批仍沿原 hybrid 脚本、健康门和账本执行，只用原分析回调替代模型。
# 函数用途: 建立中间批失败、前后批成功的真实单次工作流。
def _run_batches(gate, *, downstream=False):
    steps = [{'step_id': 'batch', 'name': 'batch', 'mode': 'hybrid',
              'script_id': 'collect', 'on_error': 'fail',
              'foreach': {'from': 'inputs.rows', 'batch_size': 2, 'max_parallel': 1,
                          'on_batch_error': 'continue', 'mode': 'aggregate', 'as': 'batch'}}]
    if downstream:
        steps.append({'step_id': 'tail', 'name': 'tail', 'mode': 'llm_skill_analysis',
                      'inputs_map': {'values': {'step_id': 'batch', 'key': 'values'}},
                      'on_error': 'fail'})
    definition = WorkflowStore(gate.home).save_workflow({
        'descriptor': {'id': 'batch-guardrails', 'kind': 'workflow', 'name': '逐批来源健康验收'},
        'steps': steps})
    run = gate.engine.run_workflow(definition, {'rows': [0, 1, 2, 3, 4, 5]})
    assert gate.engine.store.load_run(run.workflow_run_id) == run
    return run


# LLM: 同一个实际脚本给中间批提供瞬态失败，不替换 W6 seam 或人工填回执。
# 函数用途: 验证失败批不派分析、健康批继续、聚合排除失败批并保留父级阻断。
def test_foreach_gates_each_batch_without_losing_healthy_results(gate_home):
    gate = _make_gate(gate_home)
    run = _run_batches(gate)
    assert run.status == 'succeeded'
    assert [call['inputs']['batch'] for call in gate.seen] == [[0, 1], [4, 5]]
    assert run.final_output == {'values': [0, 1, 4, 5]}
    step = run.steps[0]
    assert step.batches_summary == {
        'total': 3, 'completed': 2, 'failed': 0, 'blocked': 1, 'skipped': 0}
    assert [row['status'] for row in step.batches] == ['succeeded', 'blocked', 'succeeded']
    assert [row['analysis_blocked'] for row in step.batches] == [False, True, False]
    assert step.batches[1]['error_code'] == 'ANALYSIS_BLOCKED_NO_DATA'
    assert step.batches[1]['output']['data_retrieval_failed'] is True
    assert step.batches[1]['query_health']['block_reason'] == 'QUERY_FAILED_NO_DATA'
    assert step.batches[0]['query_health']['classification'] == 'SUCCESS_WITH_HITS'
    assert step.batches[2]['query_health']['classification'] == 'SUCCESS_WITH_HITS'
    assert step.escalation_result['query_health']['analysis_blocked'] is True
    assert not list((gate.home / 'tmp/script_exec').iterdir())


# LLM: 最后一个健康批不能覆盖前面失败批的父级健康事实。
# 函数用途: 验证 continue 聚合后的普通下游分析仍能看见阻断。
def test_foreach_parent_failure_also_gates_downstream_analysis(gate_home):
    gate = _make_gate(gate_home)
    run = _run_batches(gate, downstream=True)
    assert run.status == 'failed'
    assert [call['step_id'] for call in gate.seen] == ['batch', 'batch']
    assert run.steps[0].status == 'succeeded'
    assert run.steps[1].error_code == 'ANALYSIS_BLOCKED_NO_DATA'
    assert run.steps[1].llm_involved is False
    assert run.steps[1].output_snapshot['input_step_ids'] == ['batch']


# LLM: 直接消费真实工作流回执；整体成功的部分批失败也必须携带 query_failed。
# 函数用途: 验证从引擎健康投影到 W7 冻结、持久请求及自主演进硬门的接线。
@pytest.mark.parametrize('batched', [False, True], ids=['failed-run', 'partial-success'])
def test_query_failed_receipt_cannot_produce_an_evolution_candidate(gate_home, batched):
    gate = _make_gate(gate_home)
    run = _run_batches(gate) if batched else _run(gate, _report([
        _source('events-a', 'transient_error', 0), _source('events-b', 'transient_error', 0)]))
    assert run.status == ('succeeded' if batched else 'failed')
    state = {}
    AssetMutationLedger().observe(
        state, 'workflows', {'action': 'run', 'workflow_id': run.workflow_id}, run.to_dict(),
        owner_home=gate.home, workspace_root=gate.home, call_id='guardrails-call')
    request = freeze_asset_mutations(gate.home, state, run_id=run.workflow_run_id,
                                     thread_id='guardrails-thread')
    assert request is not None
    assert request['evidence_tags'] == ['query_failed']
    assert request['targets'] == [] and request['executions'] == []
    parts = ('data', 'asset_evolution', 'requests', run.workflow_run_id + '.json')
    assert read_evolution_json(gate.home, parts) == request
    service = AssetEvolutionService(gate.home, maintenance_mode='autonomous')
    result = service.process(run.workflow_run_id)
    assert result == {'status': 'query_failed', 'notices': [], 'routes': []}
    persisted = read_evolution_json(gate.home, parts)
    assert persisted['evidence_tags'] == ['query_failed'] and persisted['result'] == result
    repeated = service.process(run.workflow_run_id)
    assert repeated == {**result, 'duplicate': True}
    assert not (gate.home / 'data/asset_evolution/candidates').exists()
    assert not (gate.home / 'data/asset_extractions').exists()
