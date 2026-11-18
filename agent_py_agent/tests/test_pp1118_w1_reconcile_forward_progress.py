"""对账预览与索引字节游标在预算首行前耗尽、超长单行、撕裂尾行时仍单调前进。"""
import json
import time

from agent_py_agent.agent.common.json_io import seal_private_jsonl_tail_unlocked
from agent_py_agent.agent.conversation.runtime_reconciliation import (
    reconcile_capability_page,
    reconcile_owner_runtime,
)
from agent_py_agent.agent.user_space.home_index_compact import compact_index_file
from agent_py_agent.agent.user_space.home_index_rebuild import (
    maintain_home_indexes_lane,
    reconcile_home_index_page,
)
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
    page_budget = len(good) + 512
    prefix, suffix = b'{"owner_id":"', b'"}\n'
    oversized = prefix + b'x' * (3 * page_budget - len(prefix) - len(suffix)) + suffix
    assert len(oversized) == 3 * page_budget
    _write_index(home, oversized + good)
    reports = _drain(home, lambda: StorageLaneBudget(128, page_budget, None))
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


def test_index_cursor_resumes_after_compaction_inode_swap_and_drains_lane(tmp_path):
    from types import SimpleNamespace

    home, root = setup_workspace(tmp_path)
    path = _write_index(home, _row(home, root) * 3)
    stat_before = path.stat()

    first = reconcile_home_index_page(home, owner_id=home.owner_id, limit=1)
    assert first['items'] == 1 and first['repaired'] == 1 and first['next_cursor']

    compact_res = compact_index_file(path)
    assert compact_res.compacted is True
    stat_after = path.stat()
    assert stat_after.st_ino != stat_before.st_ino
    assert stat_after.st_size < stat_before.st_size
    compacted_bytes = path.read_bytes()

    seen = {first['next_cursor']}
    cursor = first['next_cursor']
    followups = []
    for _ in range(6):
        report = reconcile_home_index_page(home, owner_id=home.owner_id, cursor=cursor, limit=1)
        followups.append(report)
        if not report['next_cursor']:
            break
        assert report['next_cursor'] not in seen, followups
        seen.add(report['next_cursor'])
        cursor = report['next_cursor']
    else:
        raise AssertionError(f'compacted index cursor did not terminate: {followups}')

    codes = [err['code'] for rep in followups for err in rep['errors']]
    assert 'INDEX_SOURCE_CHANGED' in codes, followups
    assert sum(rep['repaired'] for rep in followups) == 1, followups
    assert path.read_bytes() == compacted_bytes

    # 协调器维护通道在索引压缩换 inode 后同样不能卡死在同一 page 游标上。
    path2 = _write_index(home, _row(home, root) * 3)
    page1 = reconcile_home_index_page(home, owner_id=home.owner_id, limit=1)
    assert compact_index_file(path2).compacted is True
    lane_cursor = {'page': page1['next_cursor']}
    seen_lane = {json.dumps(lane_cursor, sort_keys=True)}
    for _ in range(12):
        req = SimpleNamespace(
            home=home,
            kind='global_indexes',
            coordinator=True,
            cursor=lane_cursor,
            budget=StorageLaneBudget(2, 8388608, None),
            run_write=lambda fn: fn(),
        )
        receipt = maintain_home_indexes_lane(req)
        if receipt['complete']:
            assert receipt['cursor'] == {}
            break
        encoded = json.dumps(receipt['cursor'], sort_keys=True)
        assert encoded not in seen_lane, receipt
        seen_lane.add(encoded)
        lane_cursor = receipt['cursor']
    else:
        raise AssertionError('maintain_home_indexes_lane did not complete after inode swap')


def test_index_cursor_resumes_after_same_inode_torn_tail_seal_and_truncate(tmp_path):
    home, root = setup_workspace(tmp_path)
    good = _row(home, root)
    path = _write_index(home, good + b'{"owner_id":"local/main","task_id":"incomplete')
    ino_before = path.stat().st_ino

    first = reconcile_home_index_page(home, owner_id=home.owner_id, limit=1)
    assert first['items'] == 1 and first['next_cursor']
    saved = json.loads(first['next_cursor'])
    assert saved['offset'] == len(good) < saved['end']

    assert seal_private_jsonl_tail_unlocked(path) == 'quarantined'
    stat_sealed = path.stat()
    assert stat_sealed.st_ino == ino_before
    assert stat_sealed.st_size == len(good) < saved['end']
    sealed_bytes = path.read_bytes()

    seen = {first['next_cursor']}
    cursor = first['next_cursor']
    followups = []
    for _ in range(6):
        report = reconcile_home_index_page(home, owner_id=home.owner_id, cursor=cursor, limit=1)
        followups.append(report)
        if not report['next_cursor']:
            break
        assert 'discard' not in json.loads(report['next_cursor']), report
        assert report['next_cursor'] not in seen, followups
        seen.add(report['next_cursor'])
        cursor = report['next_cursor']
    else:
        raise AssertionError(f'sealed same-inode cursor did not terminate: {followups}')

    codes = [err['code'] for rep in followups for err in rep['errors']]
    assert 'INDEX_SOURCE_CHANGED' in codes, followups
    assert sum(rep['repaired'] for rep in followups) == 1, followups
    assert path.read_bytes() == sealed_bytes

    # 上页处于 discard=True 时若同 inode 被截短为空文件，也必须清除旧 discard/end 并正常结束。
    page_budget = len(good) + 512
    oversized = b'{"owner_id":"' + b'x' * (2 * page_budget) + b'"}\n'
    _write_index(home, oversized)
    discard_page = reconcile_home_index_page(
        home, owner_id=home.owner_id, budget=StorageLaneBudget(128, page_budget, None)
    )
    assert json.loads(discard_page['next_cursor']).get('discard') is True
    with path.open('r+b') as handle:
        handle.truncate(0)
    assert path.stat().st_size == 0
    done_page = reconcile_home_index_page(
        home,
        owner_id=home.owner_id,
        cursor=discard_page['next_cursor'],
        budget=StorageLaneBudget(128, page_budget, None),
    )
    assert done_page['next_cursor'] == '', done_page
    assert any(err['code'] == 'INDEX_SOURCE_CHANGED' for err in done_page['errors']), done_page

    # 同一页读取中途发生同 inode 截短（超出读缓冲后遇到真实 OS EOF < end）时同样不得误入 discard。
    filler = b'{"owner_id":"local/other","task_id":"skip"}\n' * 600
    _write_index(home, good + filler)
    truncated_once = False

    def truncate_mid_page(operation):
        nonlocal truncated_once
        res = operation()
        if not truncated_once:
            truncated_once = True
            with path.open('r+b') as handle:
                handle.truncate(len(good))
        return res

    mid_eof = reconcile_home_index_page(
        home,
        owner_id=home.owner_id,
        apply=True,
        budget=StorageLaneBudget(1000, 1024 * 1024, None),
        commit_transition=truncate_mid_page,
    )
    assert truncated_once is True
    assert any(err['code'] == 'INDEX_SOURCE_CHANGED' for err in mid_eof['errors']), mid_eof
    if mid_eof['next_cursor']:
        assert 'discard' not in json.loads(mid_eof['next_cursor']), mid_eof
        tail_reports = _drain(home, lambda: StorageLaneBudget(4, 65536, None))
        assert tail_reports[-1]['next_cursor'] == ''
