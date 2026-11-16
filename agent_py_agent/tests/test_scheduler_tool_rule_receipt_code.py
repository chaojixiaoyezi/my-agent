# 模块用途: 显式公开回执(delivery origin + reply_policy=explicit)下的零模型工具规则没有结构化回执，
#   原合同判为未投递；运行历史须带已登记的 BACKGROUND_RECEIPT_CONTRACT_INVALID，而不是空错误码。只用 tmp 隔离 home。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish


def _tool_rule_case(tmp_path, delivery):
    from agent_py_agent.tests.test_scheduler_execution import execution_case

    case = execution_case(tmp_path)
    repo = case.agent.scheduler_repository
    old = repo.claim_run(case.signal.root_task_id, lease_seconds=60, now=1001)
    repo.finish_run(old['run_id'], old['claim_id'], SchedulerRunFinish(status='cancelled', now=1002))
    case.store.wakes.mark_handled(case.signal.wake_signal_id, now=1002)
    rules = [dict(id='probe', name='确定性测试', use_llm=False, prompt='',
        tool={'name': 'run_command', 'arguments': {'command': 'printf ok'}},
        schedule={'kind': 'every', 'every_seconds': 3600, 'anchor_at': 1060, 'timezone': 'UTC'})]
    job = repo.get_job(case.job['job_id'])
    repo.update_job(job['job_id'], patch={'rules': rules, 'delivery': delivery}, expected_version=job['version'], now=1050)
    case.agent.scheduler_service.enqueue_ready_runs(now=1060)
    case.signal = case.store.wakes.pending()[0]
    case.claim = case.agent.scheduler_service.claim_wake(case.signal, now=1060).claim
    repo.mark_run_running(case.claim.run_id, case.claim.claim_id, now=1060)
    case.kwargs = {'thread_id': case.claim.run['thread_id'], 'task_id': case.claim.run_id, 'reason': 'scheduled_run', 'now': 1060}
    return case


def _succeed_tool(monkeypatch):
    from agent_py_agent.agent.agent_core import tool_only_execution
    from agent_py_agent.agent.scheduler import tool_work
    from agent_py_agent.agent.tooling.runtime_contracts import ToolResult

    result = ToolResult(call_id='probe_call', tool_name='run_command', status='succeeded', handler_executed=True, error_code='')
    monkeypatch.setattr(tool_only_execution, 'execute_tool_work', lambda *args, **kwargs:
        (SimpleNamespace(result=result, call=SimpleNamespace(call_id='probe_call')), 'return_code=0\nstdout=ok'))
    monkeypatch.setattr(tool_work.time, 'time', lambda: 1070)


@pytest.mark.parametrize(('delivery', 'expected_code'), [
    ({'mode': 'origin', 'reply_policy': 'explicit'}, 'BACKGROUND_RECEIPT_CONTRACT_INVALID'),
    ({'mode': 'none'}, ''),
])
def test_tool_rule_receipt_failure_keeps_registered_code(tmp_path, monkeypatch, delivery, expected_code):
    from agent_py_agent.agent.scheduler.tool_work import execute_scheduled_tool

    case = _tool_rule_case(tmp_path, delivery)
    _succeed_tool(monkeypatch)
    execute_scheduled_tool(case.agent, case.agent.scheduler_service, case.claim, case.signal, case.kwargs)
    row = next(row for row in case.agent.scheduler_repository.history()[0] if row['run_id'] == case.claim.run_id)
    # 业务工具已成功：运行仍为done，失败只属于投递，且不重跑工具或调用模型。
    assert row['status'] == 'done' and row['error_code'] == expected_code, row
    assert (row['delivery_status'] == 'failed') is bool(expected_code)
    assert not case.backend.prompts
