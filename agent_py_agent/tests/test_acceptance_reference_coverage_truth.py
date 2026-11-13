"""V08-04/V08-05 回归：引用覆盖读不全时不得认证 complete。

修复点（agent/workflows/reference_sources.py）：
- scheduler.list_jobs 的 errors 曾被丢弃；非二元组返回也曾被当成空结果。
- SessionTask 只读一页 list_page(limit=100)，第二页的活跃引用看不见；
  load_errors / coverage / next_cursor 也未消费，于是"没扫完"被当成"没有阻断"。

本测试只用替身数据驱动 WorkflowReferenceReader，不启 Gateway、不跑模型、不触发真实回收。
"""
from types import SimpleNamespace as N

from agent_py_agent.agent.workflows.reference_sources import WorkflowReferenceReader


def test_scheduler_errors_make_coverage_unknown():
    reader = WorkflowReferenceReader(
        scheduler_repo=N(list_jobs=lambda **kw: ([], ['SCHEDULER_JOB_INVALID: corrupt-record'])))
    result = reader('target')
    assert result['coverage'] == 'unknown'


def test_scheduler_clean_record_still_blocks():
    job = {'job_id': 'job-1', 'status': 'active',
           'execution_target': {'kind': 'workflow', 'asset_id': 'target'}}
    reader = WorkflowReferenceReader(scheduler_repo=N(list_jobs=lambda **kw: ([job], [])))
    result = reader('target')
    assert result['coverage'] == 'complete'
    assert any(b['reason_code'] == 'REFERENCED_BY_SCHEDULE' for b in result['blockers'])


def test_second_page_running_reference_is_found():
    first_page = [N(status='done', task_id=f'done-{i}', dedupe_key='', metadata={}) for i in range(100)]
    running = N(status='running', task_id='target-task', dedupe_key='workflow:target:step', metadata={})
    calls = []

    def page(**kw):
        calls.append(sorted(kw))
        if not kw.get('cursor'):
            return {'items': first_page, 'next_cursor': 'page-2', 'coverage': 'complete', 'load_errors': []}
        return {'items': [running], 'next_cursor': None, 'coverage': 'complete', 'load_errors': []}

    result = WorkflowReferenceReader(session_task_store=N(list_page=page))('target')
    # 必须真的往第二页走过，而不是只读第一页就宣布完成。
    assert ['cursor', 'limit'] in calls
    assert any(b['ref'] == 'target-task' for b in result['blockers'])


def test_partial_pagination_is_unknown():
    reader = WorkflowReferenceReader(session_task_store=N(list_page=lambda **kw: {
        'items': [], 'next_cursor': 'pending-page', 'coverage': 'partial', 'load_errors': []}))
    assert reader('target')['coverage'] == 'unknown'


def test_load_errors_are_unknown():
    reader = WorkflowReferenceReader(session_task_store=N(list_page=lambda **kw: {
        'items': [], 'next_cursor': None, 'coverage': 'complete',
        'load_errors': [{'code': 'SESSION_TASK_INVALID'}]}))
    assert reader('target')['coverage'] == 'unknown'


def test_cursor_cycle_is_unknown():
    reader = WorkflowReferenceReader(session_task_store=N(list_page=lambda **kw: {
        'items': [], 'next_cursor': 'same', 'coverage': 'complete', 'load_errors': []}))
    assert reader('target')['coverage'] == 'unknown'


def test_later_page_load_error_keeps_blocker_and_unknown():
    running = N(status='running', task_id='target-task', dedupe_key='workflow:target:step', metadata={})

    def page(**kw):
        if not kw.get('cursor'):
            return {'items': [running], 'next_cursor': 'page-2', 'coverage': 'complete', 'load_errors': []}
        return {'items': [], 'next_cursor': None, 'coverage': 'complete',
                'load_errors': [{'code': 'SESSION_TASK_INVALID'}]}

    result = WorkflowReferenceReader(session_task_store=N(list_page=page))('target')
    assert result['coverage'] == 'unknown'
    assert any(b['ref'] == 'target-task' for b in result['blockers'])
