"""W1 closing催收的真实审批/claim交错，不启动模型或生产服务。"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.conversation import agent_tool_approval as approvals
from agent_py_agent.agent.conversation.run_claim import (
    ConversationRunLaneRequest,
    conversation_run_lane,
)
from agent_py_agent.agent.gateway_parts import closing_deadline as closing
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.cli import gateway_loops
from agent_py_agent.tests.test_gateway_agent_control_service import (
    _bound_agent_tree,
    _child_approval_request,
)


# LLM: 夹具保留原owner、RuntimeDB子代理attempt与TaskLink；只构造隔离的Gateway请求，不猜历史会话。
# 函数用途: 为每条回归建立真实审批所属的root和子线程。
def closing_context(tmp_path):
    agent, scope, child = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    root = store.tasks.load("task-root")
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    row = {"id": "deadline", "status": "processing", "control_status": "stopping", "turn_phase": "closing",
           "cancel_requested": True, "cancel_requested_at": time.time() - 121, "execution_attempt_id": "transport",
           "conversation_runtime": {"request_id": "deadline", "thread_id": root.thread_id, "task_id": root.task_id},
           "conversation_claim": {"schema_version": "gateway_conversation_claim.v1", "request_id": "deadline",
                                  "task_id": "gateway:deadline", "thread_id": root.thread_id}}
    return SimpleNamespace(agent=agent, scope=scope, store=store, child=child, root=root, paths=paths,
                           path=paths.processing / "deadline.json", row=row)


@pytest.mark.parametrize("real_interrupt", [False, True])
def test_closing_timeout_retires_pending_approval_and_unblocks(tmp_path, monkeypatch, caplog, real_interrupt):
    ctx = closing_context(tmp_path)
    entered, child_returned, executor_entered, allow_exit = (threading.Event() for _ in range(4))
    allow_waiter_read = threading.Event()
    executor_observed_interrupt = threading.Event()
    decisions, failures = [], []
    request = _child_approval_request(ctx.child.id)
    from agent_py_agent.agent.concurrency.interrupt import is_interrupted
    from agent_py_agent.agent.gateway_parts.http_handlers import read_gateway_client_notices

    read_gateway_client_notices(ctx.agent, scope=ctx.scope, after=0, interactive_approvals=True)

    def child_waiter():
        try:
            # publish和wait必须在同一个真实执行线程，不能把原审批回调搬到其它线程。
            handle = approvals.publish_agent_tool_approval(ctx.agent, run_id=ctx.child.id,
                thread_id=ctx.child.agent_thread_id, request_value=request)
            assert handle.scope_valid()

            def valid():
                entered.set()
                assert allow_waiter_read.wait(15)
                return handle.scope_valid()

            decisions.append(approvals.wait_for_agent_tool_approval(replace(handle, scope_valid=valid)))
        except BaseException as exc:
            failures.append(exc)
        finally:
            child_returned.set()

    def executor():
        try:
            with conversation_run_lane(ConversationRunLaneRequest(store=ctx.store, thread_id=ctx.root.thread_id,
                claim_task_id="gateway:deadline", reason="gateway_foreground_turn", lease_seconds=60,
                heartbeat_interval_seconds=10, interrupt_check=lambda: False)) as claim:
                ctx.row["conversation_claim"]["claim_id"] = claim["claim_id"]
                write_json_file(ctx.path, ctx.row)
                executor_entered.set()
                assert child_returned.wait(15)
                if real_interrupt:
                    assert is_interrupted()
                    executor_observed_interrupt.set()
                assert allow_exit.wait(15)
        except BaseException as exc:
            failures.append(exc)

    child_thread, executor_thread = threading.Thread(target=child_waiter), threading.Thread(target=executor)
    # 保留原重发观察，并增加真实命名中断模式：必须由原claim注册把标志送到执行线程。
    interrupt = Mock(return_value=True)
    if not real_interrupt:
        monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    child_thread.start()
    executor_thread.start()
    try:
        assert entered.wait(3) and executor_entered.wait(3)
        assert not child_returned.is_set(), (decisions, failures)
        result = closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())
        assert result["timed_out"] == 1 and result["retired"] == 1 and result["errors"] == 0, result
        allow_waiter_read.set()
        assert child_returned.wait(3)
        if real_interrupt:
            assert executor_observed_interrupt.wait(3), failures
        assert decisions[0].decision == "cancelled" and decisions[0].feedback == "parent_turn_closing"
        row = read_json_file(ctx.path)
        assert row["closing_deadline"]["code"] == "CLOSING_HARD_TIMEOUT"
        assert row["closing_deadline"]["approval_retirement"]["reason"] == "parent_turn_closing"
        assert row["status"] == "processing" and row["turn_phase"] == "closing"
        # 执行器仍活着时第二个请求绝不能获得会话claim。
        assert executor_thread.is_alive()
        assert ctx.store.claims.acquire({"thread_id": ctx.root.thread_id, "task_id": "gateway:second"}) is None
        assert closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())["timed_out"] == 0
        if not real_interrupt:
            assert interrupt.call_count == 2
    finally:
        allow_waiter_read.set()
        allow_exit.set()
        child_thread.join(3)
        executor_thread.join(3)
    assert not child_thread.is_alive() and not executor_thread.is_alive() and not failures
    # 只有原lane的finally退出后，新请求才能正常进入。
    second = ctx.store.claims.acquire({"thread_id": ctx.root.thread_id, "task_id": "gateway:second"})
    assert second is not None and second["task_id"] == "gateway:second"


@pytest.mark.parametrize("change", [
    {"cancel_requested_at": time.time()}, {"turn_phase": "open"}, {"control_status": "running"},
    {"cancel_requested": False}, {"status": "done"}, {"cancel_requested_at": "0"},
])
def test_closing_not_expired_or_nonclosing_is_untouched(tmp_path, change, monkeypatch):
    ctx = closing_context(tmp_path)
    row = {**ctx.row, **change}
    write_json_file(ctx.path, row)
    before = ctx.path.read_bytes()
    interrupt = Mock()
    monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    assert closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())["timed_out"] == 0
    assert ctx.path.read_bytes() == before
    interrupt.assert_not_called()


def test_retirement_rejects_wrong_parent_thread_and_preserves_prior_decision(tmp_path):
    ctx = closing_context(tmp_path)
    request = _child_approval_request(ctx.child.id)
    handle = approvals.publish_agent_tool_approval(ctx.agent, run_id=ctx.child.id,
        thread_id=ctx.child.agent_thread_id, request_value=request)
    assert approvals.retire_pending_agent_tool_approvals(ctx.agent, thread_id="other", root_task_id="task-root")["retired"] == 0
    assert read_json_file(handle.path)["status"] == "pending"
    from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision

    approvals.resolve_agent_tool_approval(ctx.agent, run_id=ctx.child.id, request_value=request,
        decision_value=ToolApprovalDecision(request.permission_id, "denied"))
    before = handle.path.read_bytes()
    assert approvals.retire_pending_agent_tool_approvals(ctx.agent, thread_id=ctx.root.thread_id, root_task_id="task-root")["retired"] == 0
    assert handle.path.read_bytes() == before


def test_dispatcher_wires_closing_deadline(tmp_path, monkeypatch):
    ctx = closing_context(tmp_path)
    dispatcher = object.__new__(gateway_loops._RequestDispatcher)
    dispatcher.paths, dispatcher.bootstrap_agent = ctx.paths, ctx.agent
    evaluate = Mock()
    monkeypatch.setattr(closing, "evaluate_closing_deadlines", evaluate)
    dispatcher._closing_deadline_once()
    evaluate.assert_called_once()
    assert evaluate.call_args.args == (ctx.paths, ctx.agent)
    assert isinstance(evaluate.call_args.kwargs["now"], float)


# quota_fragment_merged
# LLM: 合并到closing测试时不引入future导入；辅助符号统一quota_前缀，所有文件使用pytest临时目录。
# 模块用途: 验证归档仍计费，扫描/原锁等待可取消且不吞ToolCancelled。
import sys
from threading import Event, Thread

import pytest

from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.user_space import owner_quota as quota_module


def test_quota_walk_cancels_quickly(tmp_path, monkeypatch):
    archive = tmp_path / 'archive-before-x'
    archive.mkdir()
    for index in range(5000):
        (archive / str(index)).write_bytes(b'abc')
    assert quota_module.owner_logical_usage_bytes(tmp_path, exact=True) == 15000
    original = quota_module.os.walk
    visited = []

    # LLM: 保持真实walk和文件内容，只观察已经访问的目录。
    # 函数用途: 在第一个目录交付后翻转取消谓词。
    def quota_walk(*args, **kwargs):
        for row in original(*args, **kwargs):
            visited.append(row[0])
            yield row

    monkeypatch.setattr(quota_module.os, 'walk', quota_walk)
    with pytest.raises(ToolCancelled):
        quota_module.owner_logical_usage_bytes(tmp_path, exact=True, should_cancel=lambda: bool(visited))
    assert len(visited) <= 2


def test_quota_scan_current_token_is_not_wrapped(tmp_path):
    token = CancellationToken()
    token.cancel()
    with bind_cancellation_token(token):
        with pytest.raises(ToolCancelled):
            quota_module.owner_logical_usage_bytes(tmp_path)
        with pytest.raises(ToolCancelled):
            quota_module._owner_usage_bytes_for_admission(tmp_path)
        with pytest.raises(ToolCancelled):
            with quota_module.OwnerQuotaEnforcer(tmp_path, max_bytes=1000).admission():
                pytest.fail('cancelled admission acquired')


def test_quota_lock_wait_is_cancellable(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.json_io import locked_json_path
    waiting, cancel, finished = Event(), Event(), Event()
    failures = []
    original = quota_module.locked_json_path

    # LLM: 仍取得原线程锁/OS锁，仅用Event证明等待者已碰到锁竞争。
    # 函数用途: 观察真实非阻塞锁尝试。
    def quota_lock(*args, **kwargs):
        waiting.set()
        return original(*args, **kwargs)

    # LLM: 线程中记录真实异常并用Event退出，不依赖任意sleep制造交错。
    # 函数用途: 在父线程持锁时等待并接受取消。
    def quota_waiter():
        try:
            with quota_module.OwnerQuotaEnforcer(tmp_path, max_bytes=1000).admission(should_cancel=cancel.is_set):
                failures.append(AssertionError('cancelled waiter acquired lock'))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    with locked_json_path(tmp_path / '.owner-quota'):
        monkeypatch.setattr(quota_module, 'locked_json_path', quota_lock)
        thread = Thread(target=quota_waiter)
        thread.start()
        assert waiting.wait(2)
        cancel.set()
        assert finished.wait(2)
    thread.join(2)
    assert len(failures) == 1 and isinstance(failures[0], ToolCancelled)


def test_quota_linux_find_chunk_cancellation_reaps_process(tmp_path, monkeypatch):
    original = quota_module.subprocess.Popen
    processes, reads = [], []
    real_read = quota_module.os.read

    # LLM: 用真实子进程持续输出模拟find管道，取消必须杀死且回收，不按假返回值证明退出。
    # 函数用途: 保存本次原生扫描的受测子进程。
    def quota_popen(command, **kwargs):
        process = original([sys.executable, '-c',
            "import os,time; os.write(1,b'1\\n'*20000); time.sleep(30)"], **kwargs)
        processes.append(process)
        return process

    # LLM: 不改变输出，首次真实块读取后触发取消。
    # 函数用途: 证明取消检查发生在块读取之间。
    def quota_read(*args):
        chunk = real_read(*args)
        reads.append(len(chunk))
        return chunk

    monkeypatch.setattr(quota_module.sys, 'platform', 'linux')
    monkeypatch.setattr(quota_module.subprocess, 'Popen', quota_popen)
    monkeypatch.setattr(quota_module.os, 'read', quota_read)
    with pytest.raises(ToolCancelled):
        quota_module._owner_usage_bytes_for_admission(tmp_path, should_cancel=lambda: bool(reads))
    assert processes and processes[0].poll() is not None
    assert reads and reads[0] <= 32768


def test_quota_chunk_exact_sizes_and_timeout(tmp_path, monkeypatch):
    assert quota_module._cancellable_find_usage([sys.executable, '-c', "print('4\\n7')"],
        should_cancel=lambda: False, chunk_bytes=2) == 11
    monkeypatch.setattr(quota_module, '_NATIVE_USAGE_SCAN_TIMEOUT_SECONDS', 0.01)
    with pytest.raises(quota_module.OwnerQuotaUnavailable) as error:
        quota_module._cancellable_find_usage([sys.executable, '-c', 'import time; time.sleep(30)'],
            should_cancel=lambda: False)
    assert error.value.reason_code == 'scan_failed'


def test_approval_retirement_continues_after_invalid_prefix(tmp_path, monkeypatch):
    ctx = closing_context(tmp_path)
    directory = approvals._approval_root(ctx.agent, "task-root")
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(100):
        (directory / f"000-invalid-{index:04}.json").write_text('{}')
    handle = approvals.publish_agent_tool_approval(ctx.agent, run_id=ctx.child.id,
        thread_id=ctx.child.agent_thread_id, request_value=_child_approval_request(ctx.child.id))
    # 真实原生scandir保持插入顺序；每片读32项而不是反复只看排序前缀。
    approvals._retirement_scans.close()
    retired = 0
    for _ in range(8):
        report = approvals.retire_pending_agent_tool_approvals(ctx.agent,
            thread_id=ctx.root.thread_id, root_task_id="task-root", deadline=time.monotonic() + 1)
        retired += report["retired"]
        if retired:
            break
    approvals._retirement_scans.close()
    assert retired == 1
    assert read_json_file(handle.path)["status"] == "decided"


def test_native_scan_budget_counts_non_json_prefix_and_closes(tmp_path, monkeypatch):
    scan = approvals._BoundedJsonDirectoryScan()
    real = approvals.os.scandir
    contexts, visits = [], []

    class Entries:
        def __init__(self, path):
            self.raw = real(path)
            self.read = 0
            self.closed = False
            contexts.append(self)
        def __enter__(self):
            return self
        def __iter__(self):
            return self
        def __next__(self):
            self.read += 1
            return next(self.raw)
        def __exit__(self, *args):
            self.closed = True
            self.raw.close()

    for index in range(100):
        (tmp_path / f"prefix-{index:03}.tmp").touch()
    (tmp_path / 'target.json').write_text('{}')
    monkeypatch.setattr(approvals.os, "scandir", Entries)
    try:
        for page in range(6):
            before = sum(row.read for row in contexts)
            complete = scan.scan(tmp_path, visits.append, max_entries=20, deadline=time.monotonic() + 1)
            assert sum(row.read for row in contexts) - before <= 20
            if complete:
                break
        assert complete and visits == [tmp_path / 'target.json']
        assert len(contexts) == 1 and contexts[0].closed
        before = contexts[0].read
        assert not scan.scan(tmp_path, visits.append, max_entries=20, deadline=time.monotonic() - 1)
        assert contexts[0].read == before
    finally:
        scan.close()


def test_resident_evicted_between_peek_and_pin_is_not_rebuilt(monkeypatch):
    from contextlib import ExitStack

    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool

    resident, owner = SimpleNamespace(), SimpleNamespace(provider="local", owner_kind="user", owner_id="synthetic")
    key = (owner.provider, owner.owner_kind, owner.owner_id)
    pool = object.__new__(OwnerScopedAgentPool)
    pool._lock, pool._mcp_lifecycle_lock = threading.Lock(), threading.RLock()
    pool._hard_agents, pool._soft_agents = {key: resident}, {}
    pool._pins, pool._retired_agents, pool._last_used = {}, {}, {key: 1.0}
    pool._require_worker_owner = Mock()
    agent = SimpleNamespace(_owner_pool=pool)
    peeked, evicted = threading.Event(), threading.Event()
    original_close = Mock()
    monkeypatch.setattr(pool, '_close_retired_agents', original_close)

    def evict():
        assert peeked.wait(2)
        assert pool.evict_idle(key, resident, 1.0)
        evicted.set()

    worker = threading.Thread(target=evict)
    worker.start()
    with ExitStack() as stack:
        # 原peek已经返回，另一个真实线程完成逐出后才进入新pin路径。
        peeked.set()
        assert evicted.wait(2)
        assert closing._pin_same_resident(agent, owner, resident, stack) is None
    worker.join(2)
    assert not worker.is_alive() and not pool._pins and not pool._hard_agents
    assert original_close.call_count == 1
    # 同一实例仍驻留时，原pin阻止真实逐出，异常退出后由ExitStack正常归还。
    pool._hard_agents[key] = resident
    with pytest.raises(ValueError), ExitStack() as stack:
        assert closing._pin_same_resident(agent, owner, resident, stack) is resident
        assert pool._pins[id(resident)] == 1
        assert not pool.evict_idle(key, resident, 1.0)
        raise ValueError('test unwind')
    assert not pool._pins


def test_closing_without_approval_directory_still_interrupts(tmp_path, monkeypatch):
    ctx = closing_context(tmp_path)
    assert not approvals._approval_root(ctx.agent, "task-root").exists()
    write_json_file(ctx.path, ctx.row)
    approvals._retirement_scans.close()
    interrupt = Mock(return_value=True)
    monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    result = closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())
    assert result["timed_out"] == 1 and result["errors"] == 0, result
    diagnostic = read_json_file(ctx.path)["closing_deadline"]
    assert diagnostic["code"] == "CLOSING_HARD_TIMEOUT"
    assert diagnostic["approval_retirement"]["complete"] is True
    assert diagnostic["approval_retirement"]["retired"] == 0
    interrupt.assert_called_once()


@pytest.mark.parametrize("failure", [PermissionError("retirement denied"), RuntimeError("retirement failed")])
def test_closing_approval_failure_does_not_suppress_interrupt(tmp_path, monkeypatch, failure):
    ctx = closing_context(tmp_path)
    write_json_file(ctx.path, ctx.row)
    interrupt = Mock(return_value=True)
    monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    monkeypatch.setattr(closing, "retire_pending_agent_tool_approvals", Mock(side_effect=failure))
    result = closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())
    assert result["timed_out"] == 1 and result["errors"] == 1, result
    diagnostic = read_json_file(ctx.path)["closing_deadline"]
    assert diagnostic["code"] == "CLOSING_HARD_TIMEOUT"
    assert diagnostic["approval_retirement"]["complete"] is False
    assert diagnostic["approval_retirement"]["error_type"] == type(failure).__name__
    assert diagnostic["approval_retirement"]["error"] == str(failure)
    interrupt.assert_called_once()


def test_native_scan_above_capacity_keeps_unfinished_progress(tmp_path, monkeypatch):
    scan = approvals._BoundedJsonDirectoryScan()
    directories, targets, live, peak = [], set(), set(), []
    for number in range(scan._ROOT_LIMIT + 1):
        directory = tmp_path / str(number)
        directory.mkdir()
        directories.append(directory)
        for index in range(65):
            (directory / f"prefix-{index:03}.tmp").touch()
        targets.add(directory / "zz-target.json")
        (directory / "zz-target.json").write_text('{}')

    def ordered_entries(directory):
        # 只在夹具中固定顺序；真实目录、DirEntry和scandir上下文仍由系统提供。
        with approvals.os.scandir(directory) as entries:
            live.add(directory)
            peak.append(len(live))
            try:
                yield from sorted(entries, key=lambda entry: entry.name)
            finally:
                live.remove(directory)

    monkeypatch.setattr(scan, "_entries", ordered_entries)
    visited = set()
    try:
        for _ in range(20):
            for directory in directories:
                scan.scan(directory, visited.add, max_entries=8, deadline=time.monotonic() + 1)
            if targets <= visited:
                break
        assert visited == targets
        assert max(peak) <= scan._ROOT_LIMIT
    finally:
        scan.close()
    assert not live and not scan._slices


def test_closing_pool_lock_contention_does_not_wait_on_peek(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_worker
    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    ctx = closing_context(tmp_path)
    write_json_file(ctx.path, ctx.row)
    owner = OwnerIdentity.provider_user("synthetic-provider", "synthetic-user")
    pool = object.__new__(OwnerScopedAgentPool)
    pool._lock = threading.Lock()
    pool._hard_agents, pool._soft_agents, pool._pins = {}, {}, {}
    pool._require_worker_owner = Mock()
    pool.peek = Mock(wraps=pool.peek)
    ctx.agent._owner_pool = pool
    monkeypatch.setattr(request_worker, "_resolve_request_owner_identity", lambda *_: owner)
    interrupt = Mock(return_value=True)
    monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    held, release, finished = (threading.Event() for _ in range(3))
    results, failures = [], []

    def hold_pool():
        with pool._lock:
            held.set()
            assert release.wait(5)

    def tick():
        try:
            results.append(closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time()))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    holder, ticker = threading.Thread(target=hold_pool), threading.Thread(target=tick)
    holder.start()
    assert held.wait(2)
    ticker.start()
    try:
        completed_while_held = finished.wait(1)
    finally:
        release.set()
        holder.join(3)
        ticker.join(3)
    assert not holder.is_alive() and not ticker.is_alive() and not failures
    assert completed_while_held, "closing waited for the owner pool lock"
    pool.peek.assert_not_called()
    assert results[0]["timed_out"] == 1
    assert read_json_file(ctx.path)["closing_deadline"]["approval_retirement"]["complete"] is False
    interrupt.assert_called_once()
