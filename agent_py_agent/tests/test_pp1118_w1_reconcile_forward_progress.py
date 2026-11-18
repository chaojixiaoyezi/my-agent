"""对账预览与索引字节游标在预算首行前耗尽、超长单行、撕裂尾行时仍单调前进。"""
import json
import time

from agent_py_agent.agent.conversation.runtime_reconciliation import (
    reconcile_capability_page,
    reconcile_owner_runtime,
)
from agent_py_agent.agent.user_space.home_index_rebuild import reconcile_home_index_page
from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget
from agent_py_agent.tests.test_f155_w8_index_convergence import setup_workspace
from agent_py_agent.tests.test_f155_w9_runtime_reconciliation import _terminal_root
from agent_py_agent.tests.test_fw41_w2_capability_and_idle_approvals import _make_blocked_run
from agent_py_agent.tests.test_schedule_control import _agent


def test_task_runs_page_advances_when_slice_expires_before_first_row(tmp_path):
    agent = _agent(tmp_path)
    created = _terminal_root(agent)
    # 让时间片在打开权威库之前就到期：修复前三页同一游标、items 为空（live 2026-11-18）。
    agent.config.retention_scan_slice_ms = 1e-6
    page = reconcile_owner_runtime(agent, limit=10)
    rows = [item for item in page['items'] if item.get('task_run_id') == created['task_run_id']]
    assert rows, page
    seen = {page['next_cursor']}
    for _ in range(8):
        if not page['next_cursor']:
            break
        page = reconcile_owner_runtime(agent, limit=10, cursor=page['next_cursor'])
        assert page['next_cursor'] not in seen, page
        seen.add(page['next_cursor'])
    else:
        raise AssertionError(f'reconcile preview did not converge: {page}')


def test_capability_page_processes_at_least_one_entry_when_deadline_expired(tmp_path):
    agent = _agent(tmp_path)
    _terminal_root(agent, 'cap-root')
    first, _ = _make_blocked_run(agent, parent_id='cap-root', root_id='cap-root', depth=1)
    second, _ = _make_blocked_run(agent, parent_id='cap-root', root_id='cap-root', depth=1)
    ordered = sorted([first.id, second.id])
    page1 = reconcile_capability_page(
        agent,
        run_ids=tuple(ordered),
        limit=10,
        budget=StorageLaneBudget(10, 1024 * 1024, time.monotonic() - 1),
    )
    assert [item['run_id'] for item in page1['items']] == [ordered[0]], page1
    assert page1['next_after'] == ordered[0], page1
    page2 = reconcile_capability_page(
        agent,
        after=page1['next_after'],
        run_ids=tuple(ordered),
        limit=10,
        budget=StorageLaneBudget(10, 1024 * 1024, time.monotonic() - 1),
    )
    assert [item['run_id'] for item in page2['items']] == [ordered[1]], page2
    assert page2['next_after'] == '', page2


def _write_index(home, data: bytes):
    path = home.global_index_active_tasks_jsonl
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _row(home, root, status='active'):
    return json.dumps(dict(owner_id=home.owner_id, task_id='task', task_path=str(root),
                           status=status, title='task')).encode() + b'\n'


def _drain(home, budget_factory, pages=12):
    cursor, reports = '', []
    for _ in range(pages):
        report = reconcile_home_index_page(home, owner_id=home.owner_id, cursor=cursor, budget=budget_factory())
        reports.append(report)
        if not report['next_cursor']:
            return reports
        assert report['next_cursor'] != cursor, report
        cursor = report['next_cursor']
    raise AssertionError(f'index cursor did not converge: {reports}')


def test_index_line_larger_than_whole_page_budget_is_skipped_across_pages(tmp_path):
    home, root = setup_workspace(tmp_path)
    good = _row(home, root)
    _write_index(home, b'{"owner_id":"' + b'x' * 4000 + b'"}\n' + good)
    reports = _drain(home, lambda: StorageLaneBudget(128, len(good) + 512, None))
    codes = [error['code'] for report in reports for error in report['errors']]
    assert codes.count('INDEX_ROW_EXCEEDS_SLICE') == 1, reports
    assert sum(report['repaired'] for report in reports) == 1, reports


def test_unterminated_tail_line_does_not_pin_the_cursor(tmp_path):
    home, root = setup_workspace(tmp_path)
    _write_index(home, _row(home, root) + b'{"owner_id":"local/main","task_id":"t')
    reports = _drain(home, lambda: StorageLaneBudget(128, 4096, None))
    codes = [error['code'] for report in reports for error in report['errors']]
    assert 'INDEX_TAIL_INCOMPLETE' in codes, reports
    assert reports[0]['repaired'] == 1, reports


def test_index_page_reads_one_line_even_when_deadline_already_passed(tmp_path):
    home, root = setup_workspace(tmp_path)
    _write_index(home, _row(home, root) + _row(home, root))
    reports = _drain(home, lambda: StorageLaneBudget(128, 4096, time.monotonic() - 1))
    assert reports[0]['items'] == 1 and len(reports) <= 4, reports
