# LLM: Only pytest temporary owners and real retention/writer entry points; state changes are explicit and no production safety gate is replaced.
# 模块用途: 反证记忆与Compact永不按龄回收、绑定豁免和退休台账的安全边界。
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import (
    append_private_jsonl_records,
    read_json_object_report,
    write_private_json_file_atomic,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store import retention_apply as apply
from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store import retired_thread_journal as journal
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.retention import (
    MemoryRetentionService,
    apply_owner_retention,
)
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionAction
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


# LLM: All paths originate from the real temporary owner layout.
# 函数用途: 构造隔离Owner与真实留存服务；非分片专项用例使用宽裕单片毫秒预算避免慢速CI半片中断。
def _runtime(tmp_path, *, slice_ms: int | None = 10_000):
    home = ensure_my_agent_home(tmp_path / 'home')
    config = SimpleNamespace(retention_scan_slice_ms=slice_ms) if slice_ms is not None else None
    service = MemoryRetentionService(
        home_paths=home,
        candidates=CandidateService(home.owner_memory_candidates_jsonl),
        config=config,
    )
    return home, service


# LLM: 慢速CI若单片仍触发时间边界则沿真实游标续跑至本轮完成，出现错误或终态动作立即返回供断言核查。
# 函数用途: 汇总单轮完整留存执行结果，消除墙钟抖动导致的空分片假阴性。
def _apply_complete(run_slice, *, max_slices: int = 64):
    actions = []
    errors = []
    report = None
    for _ in range(max_slices):
        report = run_slice()
        actions.extend(report.actions)
        errors.extend(report.errors)
        if (
            not report.scan_partial
            or report.errors
            or any(item.status in {'failed', 'busy', 'state_changed'} for item in report.actions)
        ):
            return replace(report, actions=tuple(actions), errors=tuple(errors))
    pytest.fail(f'{max_slices}个留存扫描分片内未完成完整扫描')


# LLM: The normal private JSONL writer creates bytes; only the temporary file's mtime is advanced into the past.
# 函数用途: 写入并老化一个真实文件，不替换读取或删除过程。
def _aged(path, days):
    append_private_jsonl_records(path, [{'source': 'retention-test'}])
    stamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


# LLM: Real ConversationStore creates the thread/messages. Explicitly remove this fixture's routes only when testing an unbound retired conversation.
# 函数用途: 创建指定年龄的线程，并按测试前提选择是否保留绑定。
def _thread(home, identity='old', *, bound=False, days=400):
    root = Path(home.owner_home_dir) / 'conversations'
    store = ConversationStore(root)
    stamp = (NOW - timedelta(days=days)).timestamp()
    thread = store.threads.get_or_create({'canonical_user_id': identity, 'channel': 'internal',
        'channel_conversation_id': identity, 'channel_user_id': identity, 'now': stamp})
    store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': 'retention fixture', 'now': stamp})
    if not bound:
        for name in ('channel_bindings.json', 'user_latest_threads.json'):
            path = root / name
            payload = read_json_object_report(path).payload
            write_private_json_file_atomic(path, {key: value for key, value in payload.items() if value != thread.thread_id})
    return store, store.threads.require(thread.thread_id)


# LLM: Positive cache cleanup remains enabled; retaining memory must not globally disable retention.
# 函数用途: 验证400天后记忆与100天Curator运行记录仍逐字节存在。
def test_memory_assets_survive_old_mtime_with_cache_control(tmp_path):
    home, _service = _runtime(tmp_path)
    daily = _aged(home.owner_memory_daily_dir / '2025-01-01.jsonl', 400)
    curator = _aged(home.owner_memory_curator_runs_dir / 'x.jsonl', 100)
    cache = _aged(home.owner_cache_dir / 'old.jsonl', 400)
    before = {path: path.read_bytes() for path in (daily, curator)}
    report = _apply_complete(
        lambda: apply_owner_retention(
            home,
            now=NOW,
            config=SimpleNamespace(retention_scan_slice_ms=10_000),
        )
    )
    assert report.ok
    assert {path: path.read_bytes() for path in before} == before
    assert not {action.category for action in report.actions} & {'daily', 'curator_run'}
    assert not cache.exists()


# LLM: Both live CoW nodes and checkpoint/cold copies are protected independently of the reference heuristics.
# 函数用途: 验证Compact三类旧文件均不再进入mtime清理。
def test_compact_nodes_checkpoints_and_ledgers_survive(tmp_path):
    home, service = _runtime(tmp_path)
    root = Path(home.owner_compact_dir)
    paths = [root / 'evidence/thread-live/digest.json', root / 'archive/thread-live/x.checkpoint.json',
             root / 'conversations/thread-live.jsonl']
    for path in paths:
        _aged(path, 400)
    before = {path: path.read_bytes() for path in paths}
    plan = service.plan(now=NOW)
    assert not [action for action in plan.actions if action.category == 'compact']
    _apply_complete(lambda: service.apply(now=NOW))
    assert {path: path.read_bytes() for path in paths} == before


# LLM: A stale action is validly fingerprinted but cannot authorize deletion of a retired policy category.
# 函数用途: 绕过规划端，验证执行端独立拒绝旧版记忆与Compact动作。
@pytest.mark.parametrize('category', ['daily', 'curator_run', 'compact'])
def test_stale_memory_category_action_is_refused(tmp_path, category):
    home, _service = _runtime(tmp_path)
    roots = {'daily': home.owner_memory_daily_dir, 'curator_run': home.owner_memory_curator_runs_dir,
             'compact': home.owner_compact_dir}
    path = _aged(Path(roots[category]) / 'old.jsonl', 400)
    action = MemoryRetentionAction(action_id='stale-plan', category=category, operation='delete_file', path=path,
        reason='old policy plan', source_fingerprint=scan.file_fingerprint(path), cutoff_timestamp=NOW.timestamp())
    result = apply._apply_action(home=home, action=action, now=NOW, allowed_external_roots=())
    assert result.status == 'state_changed' and path.exists()


# LLM: INV-01 is path-based too; a category label cannot grant a move/delete of permanent owner assets.
# 函数用途: 验证错误类别标签不能让执行器删除正式记忆文件。
def test_permanent_memory_path_is_protected_despite_category_drift(tmp_path):
    home, _service = _runtime(tmp_path)
    path = _aged(home.owner_memory_daily_dir / 'retained.jsonl', 400)
    action = MemoryRetentionAction(action_id='category-drift', category='cache', operation='delete_file', path=path,
        reason='category changed', source_fingerprint=scan.file_fingerprint(path), cutoff_timestamp=NOW.timestamp())
    result = apply._apply_action(home=home, action=action, now=NOW, allowed_external_roots=())
    assert result.status == 'state_changed' and path.exists()


# LLM: Restore-window copies from the old sweep remain restorable; a normal cache tombstone is a live positive control.
# 函数用途: 验证旧记忆/Compact回收副本不再规划永久删除。
def test_legacy_memory_trash_is_kept_while_cache_trash_is_planned(tmp_path):
    home, service = _runtime(tmp_path)
    containers = {}
    for category in ('daily', 'curator_run', 'compact', 'cache'):
        container = Path(home.owner_trash_dir) / 'retention' / '2025-01-01' / category
        _aged(container / 'payload' / 'copy.jsonl', 400)
        write_private_json_file_atomic(container / 'tombstone.json', {'schema_version': 'my-agent.memory-retention-tombstone.v2',
            'status': 'trashed', 'category': category, 'source_path': str(home.owner_cache_dir / 'old.jsonl'),
            'payload_path': str(container / 'payload'), 'moved_at': (NOW - timedelta(days=60)).isoformat()})
        containers[category] = container
    plan = service.plan(now=NOW)
    selected = {action.path for action in plan.actions if action.operation == 'delete_tree'}
    assert containers['cache'] in selected
    assert not selected.intersection(containers[category] for category in ('daily', 'curator_run', 'compact'))
    protected = containers['daily']
    stale = MemoryRetentionAction(action_id='old-trash-plan', category='trash', operation='delete_tree', path=protected,
        reason='old purge plan', authority_path=protected / 'tombstone.json',
        authority_fingerprint=scan.file_fingerprint(protected / 'tombstone.json'),
        source_fingerprint=scan.tree_fingerprint(protected), cutoff_timestamp=NOW.timestamp())
    result = apply._apply_action(home=home, action=stale, now=NOW, allowed_external_roots=())
    assert result.status == 'state_changed' and protected.exists()


# LLM: Each binding index independently preserves an old thread; an unbound old thread remains reclaimable.
# 函数用途: 验证绑定豁免不关闭正常会话回收。
@pytest.mark.parametrize('index_name', ['channel_bindings.json', 'user_latest_threads.json'])
@pytest.mark.parametrize('expired_first_slice', [False, True])
def test_bound_old_thread_is_kept_and_unbound_control_is_trashed(tmp_path, monkeypatch, index_name, expired_first_slice):
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

    home, service = _runtime(tmp_path, slice_ms=None)
    store, bound = _thread(home, 'bound')
    _store, unbound = _thread(home, 'unbound')
    binding_path = store.storage.root / index_name
    write_private_json_file_atomic(binding_path, {'binding': bound.thread_id})
    bound_path = store.storage.thread_path(bound.thread_id)
    original_thread, original_binding = bound_path.read_bytes(), json.loads(binding_path.read_bytes())
    original_budget, budgets = RetentionScanBudget.default, []

    def scan_budget(cls, config=None):
        budget = original_budget(config=config)
        if expired_first_slice and not budgets:
            # 只模拟首片已经耗尽，不扩大原25ms或条目/字节额度。
            budget.deadline = time.monotonic()
        budgets.append(budget)
        return budget

    monkeypatch.setattr(RetentionScanBudget, 'default', classmethod(scan_budget))
    actions = {}
    for attempt in range(512):
        report = service.apply(now=NOW)
        assert report.ok and not report.legal_hold, report
        budget = budgets[-1]
        assert budget.entries <= budget.max_entries and budget.size <= budget.max_bytes
        assert bound_path.read_bytes() == original_thread
        assert json.loads(binding_path.read_bytes()) == original_binding
        for action in report.actions:
            assert action.action_id not in actions, '续片不得重复执行已提交动作'
            assert action.status not in {'failed', 'busy', 'state_changed'}, action
            actions[action.action_id] = action
        if expired_first_slice and attempt == 0:
            assert report.scan_partial and not report.actions and store.threads.load(unbound.thread_id) is not None
        if not report.scan_partial:
            break
    else:
        pytest.fail('512个原预算片内未完成绑定保护和未绑定线程退休')
    assert not expired_first_slice or len(budgets) > 1
    assert store.threads.load(bound.thread_id) is not None
    assert store.threads.load(unbound.thread_id) is None
    retired = [action for action in actions.values() if action.record_id == unbound.thread_id]
    assert len(retired) == 1 and retired[0].status == 'trashed'


# LLM: A binding published after planning must survive the apply-time check without index removal or file movement.
# 函数用途: 验证规划与执行之间新绑定会让旧动作变成state_changed。
@pytest.mark.parametrize('index_name', ['channel_bindings.json', 'user_latest_threads.json'])
def test_late_binding_keeps_thread_and_index(tmp_path, index_name):
    home, service = _runtime(tmp_path)
    store, thread = _thread(home)
    plan = service.plan(now=NOW)
    assert any(action.record_id == thread.thread_id for action in plan.actions)
    path = store.storage.root / index_name
    write_private_json_file_atomic(path, {'binding': thread.thread_id})
    report = apply.execute_retention_plan(home=home, candidates=service.candidates, plan=plan, now=NOW)
    assert any(action.record_id == thread.thread_id and action.status == 'state_changed' for action in report.actions)
    assert store.threads.load(thread.thread_id) is not None
    assert read_json_object_report(path).payload == {'binding': thread.thread_id}


# LLM: A non-object/unparseable index cannot be interpreted as an empty binding map.
# 函数用途: 验证不可读取的绑定索引保护该根全部线程。
@pytest.mark.parametrize('index_name', ['channel_bindings.json', 'user_latest_threads.json'])
def test_unreadable_binding_index_keeps_root(tmp_path, index_name):
    home, service = _runtime(tmp_path)
    store, thread = _thread(home)
    (store.storage.root / index_name).write_text('{', encoding='utf-8')
    report = _apply_complete(lambda: service.apply(now=NOW))
    assert store.threads.load(thread.thread_id) is not None
    assert any(error.error_code == 'MEMORY_RETENTION_BINDING_INDEX_UNREADABLE' for error in report.errors)


# LLM: The journal is emitted only after a successful move/commit; its positive state proof never follows invalid owner-relative paths.
# 函数用途: 验证退休行与等待、清除、恢复、未知四种真实文件状态。
def test_journal_tracks_retirement_and_restoration(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retired_thread_journal as journal

    home, service = _runtime(tmp_path)
    store, thread = _thread(home)
    report = _apply_complete(lambda: service.apply(now=NOW))
    action = next(action for action in report.actions if action.record_id == thread.thread_id)
    assert action.status == 'trashed'
    page = journal.iter_retired_threads(home)
    assert page.complete and len(page.rows) == 1
    row = page.rows[0]
    assert row['schema'] == 'retired_thread.v1' and row['action_id'] == action.action_id
    assert row['conversation_root'] == str(store.storage.root.relative_to(home.owner_home_dir))
    assert not Path(row['trash_container']).is_absolute()
    assert journal.retired_thread_state(home, row) == 'waiting'
    assert journal.retired_thread_purged(home, row) is False
    shutil.rmtree(action.destination)
    assert journal.retired_thread_state(home, row) == 'purged'
    assert journal.retired_thread_purged(home, row) is True
    write_private_json_file_atomic(store.storage.thread_path(thread.thread_id), thread.to_dict())
    assert journal.retired_thread_state(home, row) == 'restored'
    assert journal.retired_thread_purged(home, row) is False
    for update in ({'trash_container': '/outside/container'}, {'conversation_root': '../other'},
                   {'thread_id': '../thread'}, {'schema': 'unknown'}):
        assert journal.retired_thread_state(home, {**row, **update}) == 'unknown'
    real_lstat = os.lstat

    # LLM: Only simulate one journal state lookup failure; other real filesystem observations remain unchanged.
    # 函数用途: 模拟回收容器无法读取，不能把PermissionError当成已清除。
    def unavailable(path, **kwargs):
        if Path(path).name == action.destination.name:
            raise PermissionError('simulated container read error')
        return real_lstat(path, **kwargs)

    monkeypatch.setattr(journal, 'os', SimpleNamespace(lstat=unavailable, close=os.close))
    assert journal.retired_thread_state(home, row) == 'unknown'
    assert journal.retired_thread_purged(home, row) is False


# LLM: Byte offsets must survive UTF-8 and partial appends; invalid complete lines consume the page and advance, unfinished lines do neither.
# 函数用途: 验证台账分页、坏行前进及未完成尾行的续读边界。
def test_journal_pages_by_complete_physical_lines_and_byte_offsets(tmp_path):
    from agent_py_agent.agent.memory_store import retired_thread_journal as journal

    home, _service = _runtime(tmp_path)
    path = Path(home.owner_data_dir) / 'retention' / 'retired_threads.jsonl'
    empty = journal.iter_retired_threads(home)
    assert empty.rows == () and empty.complete and empty.next_offset == 0
    journal.append_retired_thread(home, thread_id='thread-中文', conversation_root=Path(home.owner_home_dir) / 'conversations',
        action_id='retention-one', trash_container=Path(home.owner_trash_dir) / 'one', retired_at=NOW.timestamp())
    first_bytes = path.read_bytes()
    row = {**json.loads(first_bytes), 'thread_id': 'thread-two', 'action_id': 'retention-two'}
    partial = json.dumps(row, ensure_ascii=False).encode('utf-8')
    with path.open('ab') as output:
        output.write(b'not-json\n' + partial)
    first = journal.iter_retired_threads(home, max_rows=1)
    assert len(first.rows) == 1 and first.next_offset == len(first_bytes) and not first.complete
    second = journal.iter_retired_threads(home, offset=first.next_offset, max_rows=2)
    assert second.rows == () and second.next_offset == len(first_bytes) + len(b'not-json\n') and not second.complete
    with path.open('ab') as output:
        output.write(b'\n')
    last = journal.iter_retired_threads(home, offset=second.next_offset, max_rows=2)
    assert last.rows == (row,) and last.complete and last.next_offset == path.stat().st_size
    assert path.stat().st_mode & 0o777 == 0o600


# LLM: A failed append cannot undo committed retirement; absence of a journal row gives consumers no deletion authority.
# 函数用途: 验证台账追加失败只告警，不把成功回收倒回或伪造台账。
@pytest.mark.parametrize('expired_first_slice', [False, True])
def test_journal_append_failure_keeps_committed_trash(tmp_path, monkeypatch, caplog, expired_first_slice):
    home, service = _runtime(tmp_path, slice_ms=None)
    store, thread = _thread(home)
    original_thread = store.storage.thread_path(thread.thread_id).read_bytes()
    from agent_py_agent.agent.memory_store.retention_scan_budget import (
        RetentionScanBudget,
        load_scan_cursor,
    )
    original_budget = RetentionScanBudget.default
    budgets = []
    journal_calls = []
    def scan_budget(cls, config=None):
        budget = original_budget(config=config)
        budgets.append(budget)
        if expired_first_slice and len(budgets) == 1:
            # 模拟冷I/O或调度等待耗尽首片；保留真实128项/8MiB/25ms预算及真实续点。
            threading.Event().wait(max(0.0, budget.deadline - time.monotonic()) + 0.005)
        return budget
    monkeypatch.setattr(RetentionScanBudget, 'default', classmethod(scan_budget))

    # LLM: Replace the actual append call site only; real source moves, tombstone publication and index checks still execute.
    # 函数用途: 在提交后的台账追加入口模拟普通写入失败。
    def unavailable(*args, **kwargs):
        journal_calls.append(kwargs['thread_id'])
        raise OSError('simulated journal append failure')

    monkeypatch.setattr(apply, 'append_retired_thread', unavailable, raising=False)
    actions = {}
    for turn in range(512):
        report = service.apply(now=NOW)
        if expired_first_slice and turn == 0:
            assert report.scan_partial and not report.actions and not journal_calls
            assert (Path(home.owner_data_dir) / 'retention_scan_cursor.json').exists()
            assert load_scan_cursor(home) == report.scan_cursor
        for current in report.actions:
            assert current.action_id not in actions, '续扫不得重复应用已提交动作'
            actions[current.action_id] = current
        assert not report.errors and not report.legal_hold
        # 只续正常partial；锁冲突、状态变化和写失败不能被轮询洗成通过。
        assert not any(current.status in {'state_changed', 'failed', 'busy'} for current in report.actions)
        matches = [current for current in actions.values() if current.record_id == thread.thread_id]
        if matches:
            break
        assert report.scan_partial, '扫描已完成却未产生目标动作'
    else:
        pytest.fail('512个原预算片内未产生目标动作')
    action, = matches
    assert not expired_first_slice or len(budgets) > 1
    assert journal_calls == [thread.thread_id]
    assert action.status == 'trashed' and action.destination.exists()
    assert (action.destination / 'payload' / 'threads' / f'{thread.thread_id}.json').read_bytes() == original_thread
    assert store.threads.load(thread.thread_id) is None
    assert 'RETIRED_THREAD_JOURNAL_APPEND_FAILED' in caplog.text
    assert not (Path(home.owner_data_dir) / 'retention' / 'retired_threads.jsonl').exists()


# LLM: 真实临时home下生成字段合法但容器已不存在的记录，不借文件缺失伪造行结构。
# 函数用途: 装配四态判定的有效输入与隔离路径。
def _row(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    row = {"schema": "retired_thread.v1", "thread_id": "thread-one", "action_id": "retention-one",
           "conversation_root": "conversations", "trash_container": "trash/retention/conversation/one",
           "retired_at": 1.0}
    return home, row


# LLM: 无效字段不能产生正面purged证明；bool、NaN、路径上跳和非对象均须未知。
# 函数用途: 逐类反证非法退休行不会授权消费者删除侧状态。
@pytest.mark.parametrize("update", [
    {"schema": "future"}, {"thread_id": "../other"}, {"thread_id": "a/b"},
    {"retired_at": True}, {"retired_at": float("nan")}, {"retired_at": float("inf")},
    {"retired_at": -1}, {"action_id": ""}, {"conversation_root": "/other/conversations"},
    {"conversation_root": "conversations/../other"}, {"trash_container": "/other/trash"},
    {"trash_container": "../other"},
])
def test_invalid_row_is_never_positive_purge_proof(tmp_path, update):
    home, row = _row(tmp_path)
    assert journal.retired_thread_state(home, {**row, **update}) == "unknown"
    assert not journal.retired_thread_purged(home, {**row, **update})


# LLM: 父目录链接不能被解释为目标缺失；消费者必须保护未知路径，不跟随到其他所有者。
# 函数用途: 验证退休容器与线程父目录的链接边界。
@pytest.mark.parametrize("level", ["trash", "conversations"])
def test_linked_state_parent_is_unknown(tmp_path, level):
    home, row = _row(tmp_path)
    owner = Path(home.owner_home_dir)
    external = tmp_path / "outside"
    external.mkdir()
    target = owner / level
    if target.exists():
        target.rmdir()
    target.symlink_to(external, target_is_directory=True)
    assert journal.retired_thread_state(home, row) == "unknown"
    assert not journal.retired_thread_purged(home, row)
    assert list(external.iterdir()) == []


# LLM: 失败发生在真实提交叶之前，原回滚必须恢复线程；台账不能先于提交成为删除权威。
# 函数用途: 验证未提交的会话回收不发布退休证明。
def test_failed_retirement_has_no_journal_row(tmp_path, monkeypatch):
    home, _ = _row(tmp_path)
    store = ConversationStore(Path(home.owner_home_dir) / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "old", "channel": "internal",
        "channel_conversation_id": "old", "channel_user_id": "old", "now": 1.0})
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "original", "now": 1.0})
    for name in ("channel_bindings.json", "user_latest_threads.json"):
        path = store.storage.root / name
        payload = json.loads(path.read_text())
        write_private_json_file_atomic(path, {key: value for key, value in payload.items() if value != thread.thread_id})
    service = MemoryRetentionService(
        home_paths=home,
        candidates=CandidateService(home.owner_memory_candidates_jsonl),
        config=SimpleNamespace(retention_scan_slice_ms=10_000),
    )

    # LLM: 只模拟最终墓碑提交的普通I/O失败，真实规划、移动和原回滚不变。
    # 函数用途: 在退休成功证明发布之前拒绝本次提交。
    def unavailable(*args, **kwargs):
        raise OSError("simulated retirement commit failure")

    monkeypatch.setattr(apply, "_commit_trash", unavailable)
    report = _apply_complete(lambda: service.apply(now=datetime(2026, 11, 5, tzinfo=timezone.utc)))
    assert any(action.record_id == thread.thread_id and action.status == "failed" for action in report.actions)
    assert store.threads.load(thread.thread_id) is not None
    assert store.storage.message_path(thread.thread_id).is_file()
    assert journal.iter_retired_threads(home).rows == ()


# LLM: 过深但有完整换行的JSON仍是坏完整行，必须前进并继续读后续有效记录。
# 函数用途: 反证解码递归错误不能永久卡住台账字节游标。
def test_deep_complete_line_does_not_block_next_record(tmp_path):
    home, row = _row(tmp_path)
    path = Path(home.owner_data_dir) / "retention/retired_threads.jsonl"
    path.parent.mkdir(parents=True)
    depth = max(sys.getrecursionlimit() * 2, 20000)
    path.write_bytes(b"[" * depth + b"0" + b"]" * depth + b"\n" + json.dumps(row).encode() + b"\n")
    page = journal.iter_retired_threads(home, max_rows=2)
    assert page.rows == (row,) and page.complete
    assert page.next_offset == path.stat().st_size
