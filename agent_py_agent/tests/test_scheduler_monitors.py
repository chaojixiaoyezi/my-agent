# LLM: 使用真实调度账本与后台夹具，工具失败在原执行器边界注入，不请求真实模型或外发。
# 模块用途: 验证监控独立规则、结构化条件、失败修复升级和资产拓扑引用。
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
)
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError


def _repo(path):
    return SchedulerRepository(path / 'scheduler', owner_provider='feishu', owner_kind='user', owner_id='test', default_timezone='UTC')


def _create(repo):
    rules = [dict(id='rule1', name='分析', schedule={'kind':'every','every_seconds':1800,'anchor_at':1000,'timezone':'UTC'}, use_llm=True, prompt='分析证据'),
             dict(id='rule2', name='探测', schedule={'kind':'every','every_seconds':300,'anchor_at':1000,'timezone':'UTC'}, use_llm=False,
                  tool={'name':'run_command','arguments':{'command':'printf ok'}}),
             dict(id='rule3', name='汇报', schedule={'kind':'cron','expression':'0 9,18 * * *','timezone':'UTC'}, use_llm=True, prompt='汇报')]
    request = SchedulerJobCreateRequest(name='测试监控', prompt='', schedule={}, thread_id='thread-1', rules=rules, now=900, source_request_id='synthetic')
    return repo.create_job(request)[0], request


def test_rules_independent_claim_counts_and_restart(tmp_path):
    repo = _repo(tmp_path)
    job, _ = _create(repo)
    assert job['alias'] == 'job01' and 'schedule' not in job and 'prompt' not in job
    runs = repo.reserve_due_runs(now=1000)
    assert {r['rule_id'] for r in runs} == {'rule1','rule2'}
    assert len({r['thread_id'] for r in runs}) == 2
    assert repo.get_job(job['job_id'])['run_count'] == 0
    first = repo.claim_run(runs[0]['run_id'], lease_seconds=120, now=1001)
    repo.mark_run_running(first['run_id'], first['claim_id'], now=1002)
    repo.mark_run_running(first['run_id'], first['claim_id'], now=1003)
    assert repo.get_job(job['job_id'])['run_count'] == 1
    second = repo.claim_run(runs[1]['run_id'], lease_seconds=120, now=1001)
    repo.mark_run_running(second['run_id'], second['claim_id'], now=1002)
    repo.record_rule_result(second['run_id'], second['claim_id'], {'alive': True})
    repo.finish_run(second['run_id'], second['claim_id'], SchedulerRunFinish(status='done', now=1010))
    restarted = _repo(tmp_path)
    assert len(restarted.list_jobs()[0]) == 1
    next_runs = restarted.reserve_due_runs(now=1300)
    assert [r['rule_id'] for r in next_runs] == ['rule2']
    assert restarted.get_job(job['job_id'])['run_count'] == 2
    assert restarted.get_job(job['job_id'])['rules'][1]['result']['value'] == {'alive': True}


def test_paused_and_updates_keep_frozen_run(tmp_path):
    repo = _repo(tmp_path)
    job, req = _create(repo)
    run = repo.reserve_due_runs(now=1000)[0]
    version = repo.get_job(job['job_id'])['version']
    changed = [dict(r, prompt='新内容') if r['use_llm'] else r for r in req.rules]
    updated = repo.update_job(job['job_id'], patch={'rules':changed,'delivery':{'mode':'none'}}, expected_version=version, now=1001)
    assert repo.get_active_run(run['run_id'])['prompt'] == '分析证据'
    assert repo.get_active_run(run['run_id'])['delivery']['mode'] == 'origin'
    repo.pause_job(job['job_id'], expected_version=updated['version'], now=1002)
    assert _repo(tmp_path).reserve_due_runs(now=10000) == []


def test_alias_never_reused_and_manual_rule_explicit(tmp_path):
    repo = _repo(tmp_path)
    job, req = _create(repo)
    repo.delete_job(job['job_id'], expected_version=job['version'], now=901)
    new = repo.create_job(replace(req, source_request_id='second'))[0]
    assert new['alias'] == 'job02'
    with pytest.raises(ScheduleValidationError):
        repo.reserve_manual_run(new['job_id'], now=950)
    assert repo.reserve_manual_run(new['job_id'], rule_id='rule2', now=950)['use_llm'] is False


def test_disabling_rule_cancels_queued_and_claimed_but_preserves_other_and_running(tmp_path):
    repo = _repo(tmp_path)
    job, req = _create(repo)
    runs = {r['rule_id']: r for r in repo.reserve_due_runs(now=1000)}
    claimed = repo.claim_run(runs['rule1']['run_id'], lease_seconds=60, now=1001)
    rules = [dict(r, enabled=False) if r['id'] == 'rule1' else r for r in req.rules]
    changed = repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=repo.get_job(job['job_id'])['version'], now=1002)
    assert repo.get_active_run(claimed['run_id']) is None
    assert repo.history()[0][0]['error_code'] == 'rule_disabled'
    assert repo.get_active_run(runs['rule2']['run_id'])['status'] == 'queued'
    rules = [dict(r, enabled=True) for r in rules]
    changed = repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=changed['version'], now=1003)
    running = repo.reserve_manual_run(job['job_id'], rule_id='rule1', now=1004)
    running = repo.claim_run(running['run_id'], lease_seconds=60, now=1004)
    repo.mark_run_running(running['run_id'], running['claim_id'], now=1005)
    rules = [dict(r, enabled=False) for r in rules]
    repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=repo.get_job(job['job_id'])['version'], now=1006)
    assert repo.get_active_run(running['run_id'])['status'] == 'running'
    assert repo.get_active_run(runs['rule2']['run_id']) is None


def test_condition_only_confirmed_json_and_staleness(tmp_path):
    repo = _repo(tmp_path)
    job, req = _create(repo)
    rules = [req.rules[1], dict(req.rules[0], depends_on=['rule2'], condition={'rule_id':'rule2','pointer':'/alive','op':'eq','value':False,'max_age_seconds':300})]
    job = repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=job['version'], now=950)
    runs = repo.reserve_due_runs(now=1000)
    assert [r['rule_id'] for r in runs] == ['rule2']
    claim = repo.claim_run(runs[0]['run_id'], lease_seconds=60, now=1001)
    repo.mark_run_running(claim['run_id'], claim['claim_id'], now=1002)
    repo.record_rule_result(claim['run_id'], claim['claim_id'], {'alive':False})
    repo.finish_run(claim['run_id'], claim['claim_id'], SchedulerRunFinish(status='done', now=1010))
    # 在规则定义里安排同一监控的分析轮，已确认依赖允许触发。
    rules[1] = dict(rules[1], schedule={'kind':'every','every_seconds':60,'anchor_at':1060,'timezone':'UTC'})
    job = repo.get_job(job['job_id'])
    repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=job['version'], now=1050)
    assert [r['rule_id'] for r in repo.reserve_due_runs(now=1060)] == ['rule1']


def test_no_llm_rule_uses_real_executor_and_scheduler_closeout(tmp_path):
    from agent_py_agent.tests.test_scheduler_execution import execution_case
    work=tmp_path/'home/owners/local/main/workspace/probe-work'
    work.mkdir(parents=True)
    case=execution_case(tmp_path,cwd=work)
    repository=case.agent.scheduler_repository
    job=repository.get_job(case.job['job_id'])
    # 已预留的旧 LLM 运行取消，随后使用同一监控的新规则试跑。
    old=repository.get_active_run(case.signal.root_task_id)
    claim=repository.claim_run(old['run_id'],lease_seconds=30,now=1001)
    repository.finish_run(claim['run_id'],claim['claim_id'],SchedulerRunFinish(status='cancelled',now=1002))
    case.store.wakes.mark_handled(case.signal.wake_signal_id,now=1002)
    definition={'id':'probe','name':'读取合成探测','use_llm':False,'tool':{'name':'read_file','arguments':{'path':'probe.json'}},
                'schedule':{'kind':'every','every_seconds':60,'anchor_at':1060,'timezone':'UTC'}}
    (work/'probe.json').write_text('{"alive":true}')
    job=repository.get_job(job['job_id'])
    repository.update_job(job['job_id'],patch={'rules':[definition],'delivery':{'mode':'none'}},expected_version=job['version'],now=1050)
    case.agent.scheduler_service.enqueue_ready_runs(now=1060)
    case.scheduler.tick(now=1060)
    history,errors=repository.history(limit=3)
    assert not errors and history[0]['rule_id']=='probe'
    assert history[0]['status']=='done' and history[0]['structured_result'] is not None, (history[0]['error_code'], history[0]['error_message'], history[0]['response'])
    assert not case.backend.prompts


# LLM: 沿真实创建、预留、领取和TaskLink准备，仅取消夹具旧运行；文件全部在临时Owner。
# 函数用途: 准备零模型探测和依赖needs_repair的到期修复规则。
def _monitor_work_case(tmp_path, *, tool_name='workflows', prompt='', claim_run=True):
    from agent_py_agent.tests.test_scheduler_execution import execution_case

    case = execution_case(tmp_path)
    repo = case.agent.scheduler_repository
    old = repo.claim_run(case.signal.root_task_id, lease_seconds=60, now=1001)
    repo.finish_run(old['run_id'], old['claim_id'], SchedulerRunFinish(status='cancelled', now=1002))
    case.store.wakes.mark_handled(case.signal.wake_signal_id, now=1002)
    rules = [dict(id='probe', name='确定性工作流', use_llm=False, prompt=prompt,
        tool={'name': tool_name, 'arguments': {'workflow_id': 'wf_1', 'action': 'run'}},
        schedule={'kind': 'every', 'every_seconds': 3600, 'anchor_at': 1060, 'timezone': 'UTC'}),
        dict(id='repair', name='失败自愈', use_llm=True, prompt='按结构化断点修复并续跑', depends_on=['probe'],
        condition={'rule_id': 'probe', 'pointer': '/needs_repair', 'op': 'eq', 'value': True},
        schedule={'kind': 'every', 'every_seconds': 60, 'anchor_at': 1120, 'timezone': 'UTC'})]
    job = repo.get_job(case.job['job_id'])
    repo.update_job(job['job_id'], patch={'rules': rules, 'delivery': {'mode': 'none'}}, expected_version=job['version'], now=1050)
    case.agent.scheduler_service.enqueue_ready_runs(now=1060)
    case.signal = case.store.wakes.pending()[0]
    case.tool_name = tool_name
    if not claim_run:
        return case
    case.claim = case.agent.scheduler_service.claim_wake(case.signal, now=1060).claim
    repo.mark_run_running(case.claim.run_id, case.claim.claim_id, now=1060)
    case.kwargs = {'thread_id': case.claim.run['thread_id'], 'task_id': case.claim.run_id, 'reason': 'scheduled_run', 'now': 1060}
    return case


# LLM: 只替换工具执行边界，返回canonical ToolResult；入账、收口和条件判断仍用产品代码。
# 函数用途: 重放成功或断点失败回执，不执行真实工具副作用。
def _monitor_tool_reply(monkeypatch, case, output, *, ok=False):
    from agent_py_agent.agent.agent_core import tool_only_execution
    from agent_py_agent.agent.scheduler import tool_work
    from agent_py_agent.agent.tooling.runtime_contracts import ToolResult

    result = ToolResult(call_id='probe_call', tool_name=case.tool_name,
        status='succeeded' if ok else 'failed', handler_executed=True, error_code='' if ok else 'WORKFLOW_PAUSED_FOR_RESUME')
    monkeypatch.setattr(tool_only_execution, 'execute_tool_work', lambda *args, **kwargs:
        (SimpleNamespace(result=result, call=SimpleNamespace(call_id='probe_call')), output))
    monkeypatch.setattr(tool_work.time, 'time', lambda: 1070)
    return result


# LLM: 断点回执经真实finish成为依赖规则确认结果，不把工作流本身改成成功。
# 函数用途: 验证零模型探测持久保存断点后正常预留下游LLM修复规则。
@pytest.mark.parametrize('tool_name', ['workflows', 'scripts'])
def test_paused_tool_result_persists_and_triggers_repair_rule(tmp_path, monkeypatch, tool_name):
    from agent_py_agent.agent.scheduler.tool_work import execute_scheduled_tool

    case = _monitor_work_case(tmp_path, tool_name=tool_name)
    paused = {'status': 'paused_for_resume', 'needs_repair': True, 'workflow_run_id': 'run_1', 'failed_step_id': 'step_1'}
    result = _monitor_tool_reply(monkeypatch, case, json.dumps(paused))
    execute_scheduled_tool(case.agent, case.agent.scheduler_service, case.claim, case.signal, case.kwargs)
    repo = case.agent.scheduler_repository
    history, errors = repo.history(limit=5)
    row = next(row for row in history if row['run_id'] == case.claim.run_id)
    assert not errors and row.get('structured_result') == paused
    assert row['status'] == 'done' and result.ok is False
    assert repo.get_job(case.job['job_id'])['rules'][0]['result']['value'] == paused
    assert [(row['rule_id'], row['use_llm']) for row in repo.reserve_due_runs(now=1120)] == [('repair', True)]
    assert not case.backend.prompts


# LLM: 仅显式结构化升级和非空prompt借原notify，必须先入账再通知且不能提前终结。
# 函数用途: 验证同一领取内的修复唤醒与返回值透传。
@pytest.mark.parametrize('paused', [{'needs_repair': True}, {'status': 'paused_for_resume'}])
def test_paused_tool_notifies_after_recording_before_closeout(tmp_path, monkeypatch, paused):
    from agent_py_agent.agent.scheduler.tool_work import execute_scheduled_tool

    case = _monitor_work_case(tmp_path, prompt='修复当前断点后继续')
    _monitor_tool_reply(monkeypatch, case, json.dumps(paused))
    observed = []

    # LLM: 只观察原run的持久结构，不修改账本；缺失入账或过早收口直接失败。
    # 函数用途: 捕获一次修复续跑原参数，确认任务仍active。
    def notify(kwargs):
        run = case.agent.scheduler_repository.get_active_run(case.claim.run_id)
        assert run['structured_result'] == paused
        assert case.store.tasks.load(case.claim.run_id).status == 'active'
        observed.append(kwargs)
        return 'repair-dispatched'

    returned = execute_scheduled_tool(case.agent, case.agent.scheduler_service, case.claim, case.signal, case.kwargs, notify=notify)
    assert returned == 'repair-dispatched' and observed == [case.kwargs]
    assert case.store.wakes.pending_one(case.signal.wake_signal_id) is not None


# LLM: 模糊真值、其它工具和非对象JSON不能伪装修复；无prompt时保持零模型。
# 函数用途: 锁住修复升级的工具名、数据形状和显式提示词边界。
@pytest.mark.parametrize('tool_name,prompt,output,confirmed', [
    ('workflows', '  ', '{"needs_repair":true}', True),
    ('scripts', '', '{"status":"paused_for_resume"}', True),
    ('workflows', 'repair', '{"needs_repair":1}', False),
    ('read_file', 'repair', '{"needs_repair":true}', False),
    ('workflows', 'repair', '[{"needs_repair":true}]', False),
    ('workflows', 'repair', 'not json', False),
])
def test_tool_repair_requires_explicit_object_and_workflow_action(tmp_path, monkeypatch, tool_name, prompt, output, confirmed):
    from unittest.mock import Mock

    from agent_py_agent.agent.scheduler.tool_work import execute_scheduled_tool

    case = _monitor_work_case(tmp_path, tool_name=tool_name, prompt=prompt)
    if confirmed:
        # 仓库会把空规则prompt物化成名称；此处专测入口显式空白分支，真实物化路径另测。
        assert case.claim.run['prompt'] == case.job['name']
        case.claim = replace(case.claim, run={**case.claim.run, 'prompt': prompt})
    _monitor_tool_reply(monkeypatch, case, output)
    notify = Mock(side_effect=AssertionError('不能唤醒模型'))
    execute_scheduled_tool(case.agent, case.agent.scheduler_service, case.claim, case.signal, case.kwargs, notify=notify)
    row = next(row for row in case.agent.scheduler_repository.history()[0] if row['run_id'] == case.claim.run_id)
    assert row.get('structured_result') == (json.loads(output) if confirmed else None)
    assert row['status'] == ('done' if confirmed else 'failed')
    notify.assert_not_called()
    assert not case.backend.prompts


# LLM: 成功的非对象输出不能进入规则条件，保留call_id/ok确认证据而非列表或标量。
# 函数用途: 验证成功回执解析失败或不是对象时沿用确认摘要。
@pytest.mark.parametrize('output', ['not json', '[1, 2]', 'null', '"ok"', '{"alive":true}'])
def test_successful_monitor_only_records_json_object(tmp_path, monkeypatch, output):
    from agent_py_agent.agent.scheduler.tool_work import execute_scheduled_tool

    case = _monitor_work_case(tmp_path)
    _monitor_tool_reply(monkeypatch, case, output, ok=True)
    execute_scheduled_tool(case.agent, case.agent.scheduler_service, case.claim, case.signal, case.kwargs)
    row = next(row for row in case.agent.scheduler_repository.history()[0] if row['run_id'] == case.claim.run_id)
    assert row['structured_result'] == ({'alive': True} if output.startswith('{') else {'call_id': 'probe_call', 'ok': True})
    assert row['status'] == 'done'


# LLM: 只从真实Scheduler归一化Job及公开绑定入口观察refs，不直接测试私有提取函数。
# 函数用途: 验证tool、规则precheck和顶层precheck自动建边，保留绑定顺序且去重。
def test_monitor_descriptor_extracts_rule_and_precheck_asset_refs(tmp_path):
    from agent_py_agent.agent.assets.monitor_adapter import (
        get_monitor_descriptor,
        save_monitor_binding,
    )

    repo = _repo(tmp_path)
    job, req = _create(repo)
    rules = [dict(req.rules[1], tool={'name': 'workflows', 'arguments': {'workflow_id': 'wf_1', 'action': 'run'}}),
        dict(req.rules[0], precheck={'tool_name': 'scripts', 'arguments': {'script_id': 'sc_1', 'action': 'get'}}),
        dict(req.rules[2], precheck={'tool_name': 'workflows', 'arguments': {'workflow_id': 'wf_2', 'action': 'get'}})]
    job = repo.update_job(job['job_id'], patch={'rules': rules}, expected_version=job['version'], now=950)
    home = tmp_path / 'owner'
    save_monitor_binding(home, job['job_id'], workflow_ids=['bound_wf', 'wf_1'], script_ids=['bound_sc'])
    job['precheck'] = {'tool_name': 'scripts', 'arguments': {'script_id': 'sc_top'}}
    descriptor = get_monitor_descriptor(home, job['job_id'], job)
    assert descriptor['refs']['workflows'] == ['bound_wf', 'wf_1', 'wf_2']
    assert descriptor['refs']['scripts'] == ['bound_sc', 'sc_1', 'sc_top']


# LLM: 工具名决定资产类型，非字符串和空值不建边，不安全字符串沿safe_asset_id拒绝。
# 函数用途: 验证脚本tool引用及错误ID边界，不修改源Job。
@pytest.mark.parametrize('identifier', ['script_1', '../escape', '', None, 7])
def test_monitor_descriptor_script_tool_id_validation(tmp_path, identifier):
    from agent_py_agent.agent.assets.monitor_adapter import get_monitor_descriptor

    repo = _repo(tmp_path)
    job, req = _create(repo)
    rule = dict(req.rules[1], tool={'name': 'scripts', 'arguments': {'script_id': identifier}})
    job = repo.update_job(job['job_id'], patch={'rules': [rule]}, expected_version=job['version'], now=950)
    if identifier == '../escape':
        with pytest.raises(ValueError, match='资产ID'):
            get_monitor_descriptor(tmp_path / 'owner', job['job_id'], job)
    else:
        refs = get_monitor_descriptor(tmp_path / 'owner', job['job_id'], job)['refs']
        assert refs['scripts'] == (['script_1'] if identifier == 'script_1' else [])


# LLM: 使用真实tick、默认名称prompt与原后台notify/模型收口，工具边界和模型提供者可控，不修改状态迁就结果。
# 函数用途: 验证物化后默认NO断点经真实修复入口完成，rule.result回填后下游能够预留。
def test_paused_workflow_real_background_notify_confirms_downstream_result(tmp_path, monkeypatch):
    case = _monitor_work_case(tmp_path, claim_run=False)
    paused = {'status': 'paused_for_resume', 'needs_repair': True, 'workflow_run_id': 'run_1', 'failed_step_id': 'step_1'}
    _monitor_tool_reply(monkeypatch, case, json.dumps(paused))
    case.scheduler.tick(now=1060)
    repo = case.agent.scheduler_repository
    row = next(row for row in repo.history()[0] if row.get('rule_id') == 'probe')
    assert row['status'] == 'done' and row['structured_result'] == paused
    assert repo.get_job(case.job['job_id'])['rules'][0]['result']['value'] == paused
    assert len(case.backend.prompts) == 1
    assert [(r['rule_id'], r['use_llm']) for r in repo.reserve_due_runs(now=1120)] == [('repair', True)]


# LLM: 使用原typed认证入口验证工具名排他，不用monkeypatch制造不可能的通知义务。
# 函数用途: 证明工作流或脚本的断点回执不会被仅限web_fetch的认证通知分支抢先返回。
@pytest.mark.parametrize('tool_name', ['workflows', 'scripts'])
def test_workflow_repair_cannot_enter_web_fetch_credential_notice(tool_name):
    from agent_py_agent.agent.conversation.required_user_action import required_action_from_result
    from agent_py_agent.agent.tooling.runtime_contracts import ToolResult

    auth = {'status': 'rejected', 'http_status': 401, 'record_updated': True, 'user_action_required': True,
        'credential_ref': 'cred_' + 'a' * 32, 'generation': 1}
    result = ToolResult(call_id='probe', tool_name=tool_name, status='failed', handler_executed=True,
        metadata={'handler_details': {'credential_authentication': auth}})
    assert required_action_from_result(result) is None
    web_result = replace(result, tool_name='web_fetch')
    assert required_action_from_result(web_result)['kind'] == 'credential_authentication_rejected'
