"""真实文件 I/O 并发竞态测试(不使用 MagicMock 模拟被测对象)。

背景:现有 test_stress_* 系列用 ThreadPoolExecutor + MagicMock,只验证了调度
逻辑本身,从未触及真实文件系统。atomic write(临时文件 + rename)和
scoped lock 的正确性只有在真实文件竞态下才能验证。本文件全部使用 tmp_path +
真实实现 + threading/ThreadPoolExecutor 构造竞态:

- 场景 a:多线程并发 save 同一个任务 → 验证 canonical_state.json 原子写,
  结束后文件必须是完整合法 JSON,且能被 manager.load 读回;
- 场景 b:多线程并发 append JSONL 账本(agent.io.jsonl.append_jsonl,内部为
  线程锁 + fcntl.flock 双重锁)→ 行数、行级 JSON 合法性、无交错;
- 场景 c:scoped lock(gateway_parts.scoped_locks)的进程级单例语义钉子:
  同进程线程重入=刷新心跳(必须成功),真实子进程抢锁必须失败、其 release
  不得误删持有者的锁。历史背景:曾有 strict xfail 钉子要求这把锁提供线程
  互斥,经全仓调用点排查(生产代码零调用,设计用途是 gateway 机器级身份
  单例)+ 长期助手 ProcessRegistry 对照后定性:线程互斥不是它的契约,线程
  临界区应使用 threading.Lock(参考 agent/io/jsonl.py 双层锁),故改写;
- 场景 d:一个线程快速 create_run + shutil.rmtree 任务目录,另一线程并发调
  list_runs_report / indexing.select_runs → 列表操作不得裸抛
  FileNotFoundError(容忍 load_errors 结构化记录);
- 场景 e:多线程各自 create_run + save 不同任务 → 全部可 load、互不污染。

所有断言基于确定性事实(计数、JSON 合法性、集合相等),不依赖 sleep 碰运气。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent_py_agent
from agent_py_agent.agent.concurrency.exceptions import ConcurrencyConflictError
from agent_py_agent.agent.gateway_parts.scoped_locks import (
    acquire_scoped_lock,
    release_scoped_lock,
)
from agent_py_agent.agent.io.jsonl import append_jsonl
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import CapabilityGrant, CapabilityRequest
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityGapParams,
    RecordCapabilityGrantParams,
    RecordCapabilityRequestParams,
)

# ---------------------------------------------------------------------------
# 共用小工具:真实线程编排(被测对象一律真实实现,这里只做线程管理)
# ---------------------------------------------------------------------------


def _run_threads(worker, count: int, *, join_timeout: float = 15.0) -> list[str]:
    """启动 count 个线程跑 worker(idx),返回各线程捕获的异常 traceback 列表。"""

    errors: list[str] = []
    errors_guard = threading.Lock()

    def _wrapped(idx: int) -> None:
        try:
            worker(idx)
        except BaseException:  # noqa: BLE001 - 测试需要完整记录线程内异常。
            with errors_guard:
                errors.append(f"[thread-{idx}]\n{traceback.format_exc()}")

    threads = [threading.Thread(target=_wrapped, args=(i,), daemon=True) for i in range(count)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + join_timeout
    for thread in threads:
        thread.join(timeout=max(0.1, deadline - time.monotonic()))
    alive = [t.name for t in threads if t.is_alive()]
    if alive:
        errors.append(f"threads still alive after {join_timeout}s: {alive}")
    return errors


def _fresh_manager(tmp_path: Path) -> SubAgentManager:
    return SubAgentManager(workspace=tmp_path / "ws")


# ---------------------------------------------------------------------------
# 场景 a:多线程并发 save 同一个任务 → canonical_state.json 原子写验证
# ---------------------------------------------------------------------------


def test_concurrent_save_same_task_keeps_canonical_state_atomic(tmp_path: Path) -> None:
    """12 线程并发 load 副本→改 latest_summary→save 同一任务,canonical 必须完整。"""

    manager = _fresh_manager(tmp_path)
    task = manager.create_run(goal="并发 save 原子性验证", thought="t", plan=["p1"])
    run_id = task.id
    canonical_path = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    assert canonical_path.exists()

    thread_count = 12
    rounds = 3
    # 较长的 summary 放大"半截文件"窗口:若写入不是原子的,更容易被读端撞见。
    payload_tail = "x" * 512
    expected_summaries = {
        f"writer-{idx:02d}-round-{r}-{payload_tail}" for idx in range(thread_count) for r in range(rounds)
    }
    barrier = threading.Barrier(thread_count)

    def worker(idx: int) -> None:
        for r in range(rounds):
            copy = manager.load(run_id)
            copy.latest_summary = f"writer-{idx:02d}-round-{r}-{payload_tail}"
            barrier.wait(timeout=10)
            manager.save(copy)

    errors = _run_threads(worker, thread_count)
    assert errors == [], "并发 save 不应抛出异常:\n" + "\n".join(errors)

    # 1) canonical_state.json 必须是完整合法 JSON(无半截文件、无 JSONDecodeError)。
    raw = canonical_path.read_text(encoding="utf-8")
    payload = json.loads(raw)  # 若原子写失效,这里会 JSONDecodeError。
    assert payload["id"] == run_id
    # 2) workspace 下的 locator task.json 同样必须完整合法。
    locator = json.loads((manager.workspace / run_id / "task.json").read_text(encoding="utf-8"))
    assert locator["run_id"] == run_id
    # 3) 能被 manager.load 读回,且 summary 是某个线程写入的完整值(最后写者胜)。
    reloaded = manager.load(run_id)
    assert reloaded.latest_summary in expected_summaries, (
        f"latest_summary 不是任何线程写入的完整值,疑似交错损坏: {reloaded.latest_summary[:120]!r}"
    )


def test_stale_capability_writer_cannot_reopen_settled_runner_attempt(tmp_path: Path) -> None:
    """迟到的能力授权副本只能合并授权账，不能把已收口 attempt 改回 RUNNING。"""

    manager = _fresh_manager(tmp_path)
    task = manager.create_run(goal="能力授权与 runner 收口竞态", thought="t", plan=["p1"])
    run_id = task.id

    # 两个并发写者都从 attempt 尚未收口的同一版 canonical state 起步。
    stale_capability_writer = manager.load(run_id)
    stale_capability_writer.status = "RUNNING"
    stale_capability_writer.runner_active_attempt_id = "attempt-race-1"
    stale_capability_writer.capability_requests = [
        CapabilityRequest(
            id="capreq-race-1",
            from_run_id=run_id,
            problem="需要在自己的任务目录写文件",
            needed_capability="filesystem",
            requested_tools=["write_file"],
            status="GRANTED",
        )
    ]
    stale_capability_writer.capability_grants = [
        CapabilityGrant(
            id="capgrant-race-1",
            request_id="capreq-race-1",
            grant_to_run_id=run_id,
            tools=["write_file"],
        )
    ]
    stale_capability_writer.allowed_tools = [*stale_capability_writer.allowed_tools, "write_file"]

    settled_runner = manager.load(run_id)
    settled_runner.status = "BLOCKED"
    settled_runner.turn_end_reason = "blocked"
    settled_runner.failure_type = "capability_request"
    settled_runner.result = "runner 已结构化收口，等待授权后续派"
    settled_runner.runner_attempts = 1
    settled_runner.runner_active_attempt_id = ""
    settled_runner.runner_last_attempt_at = 123.0
    settled_runner.capability_requests = [
        CapabilityRequest(
            id="capreq-race-1",
            from_run_id=run_id,
            problem="需要在自己的任务目录写文件",
            needed_capability="filesystem",
            requested_tools=["write_file"],
            status="OPEN",
        )
    ]
    manager.save(settled_runner)

    # 精确复现真机顺序：runner_result 先落盘，持有旧副本的授权写者后落盘。
    manager.save(stale_capability_writer)
    reloaded = manager.load(run_id)

    assert reloaded.status == "PENDING"
    assert reloaded.turn_end_reason == "blocked"
    assert reloaded.failure_type == ""
    assert reloaded.result == "runner 已结构化收口，等待授权后续派"
    assert reloaded.runner_attempts == 1
    assert reloaded.runner_active_attempt_id == ""
    assert reloaded.runner_last_attempt_at == 123.0
    assert reloaded.allowed_tools.count("write_file") == 1
    assert [item.id for item in reloaded.capability_grants] == ["capgrant-race-1"]
    assert reloaded.capability_requests[0].status == "GRANTED"


def test_runner_result_writer_keeps_capability_granted_after_its_load(tmp_path: Path) -> None:
    """runner 收尾若早已 load，晚于它落盘的授权仍必须保留在同一 canonical state。"""

    manager = _fresh_manager(tmp_path)
    task = manager.create_run(goal="runner 旧读与授权新写竞态", thought="t", plan=["p1"])
    run_id = task.id

    stale_runner_writer = manager.load(run_id)
    stale_runner_writer.status = "BLOCKED"
    stale_runner_writer.turn_end_reason = "blocked"
    stale_runner_writer.failure_type = "capability_request"
    stale_runner_writer.result = "runner 收口"
    stale_runner_writer.runner_attempts = 1
    stale_runner_writer.runner_active_attempt_id = ""
    stale_runner_writer.capability_requests = [
        CapabilityRequest(
            id="capreq-race-2",
            from_run_id=run_id,
            problem="需要写文件",
            needed_capability="filesystem",
            requested_tools=["write_file"],
            status="OPEN",
        )
    ]

    capability_writer = manager.load(run_id)
    capability_writer.capability_requests = [
        CapabilityRequest(
            id="capreq-race-2",
            from_run_id=run_id,
            problem="需要写文件",
            needed_capability="filesystem",
            requested_tools=["write_file"],
            status="GRANTED",
        )
    ]
    capability_writer.capability_grants = [
        CapabilityGrant(
            id="capgrant-race-2",
            request_id="capreq-race-2",
            grant_to_run_id=run_id,
            tools=["write_file"],
        )
    ]
    capability_writer.allowed_tools = [*capability_writer.allowed_tools, "write_file"]
    manager.save(capability_writer)

    # 反向时序：授权先提交，早已读取旧状态的 runner 收尾后提交。
    manager.save(stale_runner_writer)
    reloaded = manager.load(run_id)

    assert reloaded.status == "PENDING"
    assert reloaded.failure_type == ""
    assert reloaded.runner_attempts == 1
    assert reloaded.capability_requests[0].status == "GRANTED"
    assert [item.id for item in reloaded.capability_grants] == ["capgrant-race-2"]
    assert reloaded.allowed_tools.count("write_file") == 1


@pytest.mark.parametrize(
    ("canonical_status", "stale_status"),
    [("GRANTED", "GAP"), ("GAP", "GRANTED")],
)
def test_capability_terminal_decision_is_monotonic_and_grant_is_idempotent(
    tmp_path: Path,
    canonical_status: str,
    stale_status: str,
) -> None:
    """同一申请的首个终态不能被旧副本翻转，重复 grant 也只能保留一份。"""

    manager = _fresh_manager(tmp_path)
    task = manager.create_run(goal="能力终态冲突", thought="t", plan=["p1"])
    run_id = task.id
    stale = manager.load(run_id)
    canonical = manager.load(run_id)
    for snapshot, status, grant_id in (
        (canonical, canonical_status, "capgrant-canonical"),
        (stale, stale_status, "capgrant-stale"),
    ):
        snapshot.capability_requests = [
            CapabilityRequest(
                id="capreq-terminal-race",
                from_run_id=run_id,
                problem="需要写文件",
                needed_capability="filesystem",
                status=status,
            )
        ]
        snapshot.capability_grants = [
            CapabilityGrant(
                id=grant_id,
                request_id="capreq-terminal-race",
                grant_to_run_id=run_id,
                tools=["write_file"],
            )
        ]
    manager.save(canonical)
    manager.save(stale)

    reloaded = manager.load(run_id)
    assert reloaded.capability_requests[0].status == canonical_status
    assert [item.id for item in reloaded.capability_grants] == ["capgrant-canonical"]


def test_canonical_mutate_serializes_reducers_and_rejects_old_revision(
    tmp_path: Path,
) -> None:
    """typed mutate 必须保留每个并发增量，并让旧 expected revision 明确冲突。"""

    manager = _fresh_manager(tmp_path)
    task = manager.create_run(goal="原子 reducer", thought="t", plan=["p1"])
    initial_revision = task.state_revision

    def worker(index: int) -> None:
        def reducer(current) -> None:
            attrs = dict(current.attributes or {})
            values = list(attrs.get("mutation_values") or [])
            values.append(index)
            attrs["mutation_values"] = values
            current.attributes = attrs

        manager.mutate(task.id, reducer)

    errors = _run_threads(worker, 8)
    assert errors == []
    reloaded = manager.load(task.id)
    assert sorted(reloaded.attributes["mutation_values"]) == list(range(8))
    assert reloaded.state_revision == initial_revision + 8

    with pytest.raises(ConcurrencyConflictError, match="并发冲突"):
        manager.mutate(
            task.id,
            lambda current: setattr(current, "latest_summary", "不应提交"),
            expected_revision=initial_revision,
        )
    assert manager.load(task.id).latest_summary != "不应提交"


def test_concurrent_equivalent_capability_requests_create_one_open_record(
    tmp_path: Path,
) -> None:
    """并发提交同一结构化能力需求时，canonical 中只能有一条 OPEN 申请。"""

    manager = _fresh_manager(tmp_path)
    task = manager.create_run(goal="能力申请去重", thought="t", plan=["p1"])
    params = RecordCapabilityRequestParams(
        problem="需要写当前任务文件",
        needed_capability="filesystem",
        requested_tools=["write_file"],
        path_scope=[str(tmp_path)],
    )
    returned_ids: list[str] = []
    ids_guard = threading.Lock()

    def worker(_index: int) -> None:
        request = manager.lifecycle.record_capability_request(task.id, params)
        with ids_guard:
            returned_ids.append(request.id)

    errors = _run_threads(worker, 8)
    reloaded = manager.load(task.id)
    assert errors == []
    assert len(set(returned_ids)) == 1
    assert len(reloaded.capability_requests) == 1
    assert reloaded.capability_requests[0].status == "OPEN"


def test_capability_grant_and_gap_conflict_cannot_flip_first_terminal_decision(
    tmp_path: Path,
) -> None:
    """同一申请先 GRANTED 后到 GAP 时，迟到裁决必须失败且不能翻转权威终态。"""

    manager = _fresh_manager(tmp_path)
    task = manager.create_run(goal="能力裁决冲突", thought="t", plan=["p1"])
    request = manager.lifecycle.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="需要写文件",
            needed_capability="filesystem",
        ),
    )
    manager.lifecycle.record_capability_grant(
        task.id,
        RecordCapabilityGrantParams(
            request_id=request.id,
            tools=["write_file"],
        ),
    )

    with pytest.raises(ValueError, match="already_resolved:GRANTED"):
        manager.lifecycle.record_capability_gap(
            task.id,
            RecordCapabilityGapParams(
                request_id=request.id,
                missing_capability="filesystem",
                why_failed="迟到的冲突裁决",
            ),
        )

    reloaded = manager.load(task.id)
    assert reloaded.capability_requests[0].status == "GRANTED"
    assert len(reloaded.capability_grants) == 1
    assert reloaded.capability_gaps == []


def test_capability_gap_and_late_grant_cannot_flip_first_terminal_decision(
    tmp_path: Path,
) -> None:
    """同一申请先 GAP 后到授权时，迟到授权必须失败且不能扩大实际权限。"""

    manager = _fresh_manager(tmp_path)
    task = manager.create_run(goal="能力裁决反向冲突", thought="t", plan=["p1"])
    request = manager.lifecycle.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="需要写文件",
            needed_capability="filesystem",
        ),
    )
    manager.lifecycle.record_capability_gap(
        task.id,
        RecordCapabilityGapParams(
            request_id=request.id,
            missing_capability="filesystem",
            why_failed="当前边界不允许",
        ),
    )

    with pytest.raises(ValueError, match="already_resolved:GAP"):
        manager.lifecycle.record_capability_grant(
            task.id,
            RecordCapabilityGrantParams(
                request_id=request.id,
                tools=["special_writer"],
            ),
        )

    reloaded = manager.load(task.id)
    assert reloaded.capability_requests[0].status == "GAP"
    assert reloaded.capability_grants == []
    assert len(reloaded.capability_gaps) == 1
    assert "special_writer" not in reloaded.allowed_tools


# ---------------------------------------------------------------------------
# 场景 b:多线程并发写 + 读 JSONL 账本 → 行级完整性验证
# ---------------------------------------------------------------------------


def test_concurrent_jsonl_append_keeps_ledger_lines_intact(tmp_path: Path) -> None:
    """8 线程并发 append 各 50 条,行数==总写入数,每行都是合法 JSON,无交错。"""

    ledger = tmp_path / "ledger" / "events.jsonl"
    thread_count = 8
    per_thread = 50
    pad = "p" * 200  # 放大单条记录,提升交错损坏的可检出性。
    barrier = threading.Barrier(thread_count)

    def worker(idx: int) -> None:
        barrier.wait(timeout=10)
        for seq in range(per_thread):
            append_jsonl(ledger, {"thread": idx, "seq": seq, "pad": pad})

    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        futures = [pool.submit(worker, idx) for idx in range(thread_count)]
        errors = [repr(f.exception()) for f in futures if f.exception() is not None]
    assert errors == [], "并发 append 不应抛出异常:\n" + "\n".join(errors)

    content = ledger.read_text(encoding="utf-8")
    assert content.endswith("\n"), "账本最后一行没有换行符,存在半截写入"
    lines = content.splitlines()
    assert len(lines) == thread_count * per_thread, (
        f"行数 {len(lines)} != 总写入数 {thread_count * per_thread},存在丢行或交错"
    )
    seen: set[tuple[int, int]] = set()
    for line_no, line in enumerate(lines, start=1):
        record = json.loads(line)  # 任何一行损坏都会 JSONDecodeError。
        assert record["pad"] == pad, f"第 {line_no} 行 pad 字段被截断/交错"
        seen.add((record["thread"], record["seq"]))
    expected = {(idx, seq) for idx in range(thread_count) for seq in range(per_thread)}
    assert seen == expected, "写入记录集合与期望不一致,存在覆盖或交错"


# ---------------------------------------------------------------------------
# 场景 c:scoped lock 进程级单例语义 → 同进程线程重入刷新 + 真实跨进程互斥
# ---------------------------------------------------------------------------

# 子进程探针:在真实独立进程里抢同一把锁并尝试 release。
# 预期:抢不到(进程间互斥),release 也删不掉持有者(父进程)的锁文件。
_CHILD_LOCK_PROBE_CODE = """\
import json
import os
import sys

from agent_py_agent.agent.gateway_parts.scoped_locks import (
    acquire_scoped_lock,
    release_scoped_lock,
)

scope, identity = sys.argv[1], sys.argv[2]
acquired, holder = acquire_scoped_lock(scope, identity)
release_scoped_lock(scope, identity)
print(json.dumps({
    "pid": os.getpid(),
    "acquired": bool(acquired),
    "holder_pid": (holder or {}).get("pid"),
}))
"""


def test_scoped_lock_process_singleton_reentrant_threads_and_cross_process_mutex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """钉死 scoped lock 的进程级单例契约:线程重入=刷新,跨进程互斥+释放保护。

    历史背景:此处曾是 strict xfail 钉子,要求这把锁提供进程内线程互斥
    (8 线程读-改-写计数),实测必然失败(期望 800 实得 ~15)。经全仓调用点
    排查(acquire/release 生产代码零调用;supervisor.py 的 import 是死引用已删;
    daemon_control.py 仅作公共 API 转口)与对照组核查(长期助手 ProcessRegistry:
    进程内并发一律 threading.Lock,pid+start_time 只做进程身份),定性为:
    线程互斥不是这把锁的契约——同进程任意线程 acquire=持有者重入刷新心跳,
    是 gateway 身份单例所依赖的特性。线程临界区应使用 threading.Lock
    (参考 agent/io/jsonl.py 的"线程锁+flock"双层模式),禁止复用这把锁。

    本钉子防两类回归:
    1) 同进程重入被破坏(例如有人给 _owns_lock 加 thread id)→ 会打断
       supervisor/gateway 的 heartbeat 刷新;
    2) 跨进程互斥或释放保护被破坏 → 两个 gateway 进程可同时持有同一身份。
    """

    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    scope, identity = "io-singleton-test", "gateway-identity"

    # 契约 1:本进程首次 acquire 成功,且没有前任持有者。
    ok_first, prior = acquire_scoped_lock(scope, identity, metadata={"round": 1})
    assert ok_first is True, "前置条件失败:首次 acquire 应当成功"
    assert prior is None, "首次 acquire 不应返回前任持有者记录"

    # 契约 2:同进程另一线程 acquire 同一把锁 = 持有者重入,返回 True 并
    # 把刷新前的旧记录作为 existing 返回(heartbeat 刷新语义)。
    thread_result: dict[str, object] = {}

    def reenter() -> None:
        ok, existing = acquire_scoped_lock(scope, identity, metadata={"round": 2})
        thread_result["ok"] = ok
        thread_result["existing"] = existing

    reenter_thread = threading.Thread(target=reenter, daemon=True)
    reenter_thread.start()
    reenter_thread.join(timeout=10)
    assert thread_result.get("ok") is True, (
        "同进程线程重入 acquire 应返回 True(进程级重入=刷新心跳);"
        "若此断言失败,说明锁归属被改成了线程维度,会破坏 supervisor 重入刷新语义"
    )
    reentered = thread_result.get("existing")
    assert isinstance(reentered, dict) and reentered.get("metadata") == {"round": 1}, (
        f"重入时应返回刷新前的上一份锁记录,实际: {reentered!r}"
    )

    # 契约 3:真实子进程抢同一把锁必须失败,且能读到持有者(本进程)pid;
    # 子进程的 release 不得删掉本进程仍持有的锁。
    repo_root = Path(agent_py_agent.__file__).resolve().parents[1]
    env = {**os.environ, "XDG_STATE_HOME": str(state_home)}
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_LOCK_PROBE_CODE, scope, identity],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, f"子进程探针异常退出:\n{proc.stderr}"
    probe = json.loads(proc.stdout.strip().splitlines()[-1])
    assert probe["acquired"] is False, "跨进程互斥被破坏:子进程抢到了本进程持有的锁"
    assert probe["holder_pid"] == os.getpid(), (
        f"子进程读到的持有者应是本进程 pid={os.getpid()},实际: {probe['holder_pid']!r}"
    )

    # 子进程 release 之后,锁必须仍归本进程:重入 acquire 仍成功且记录是自己的。
    ok_after, existing_after = acquire_scoped_lock(scope, identity, metadata={"round": 3})
    assert ok_after is True, "释放保护被破坏:子进程 release 删掉了本进程持有的锁"
    assert isinstance(existing_after, dict) and existing_after.get("pid") == os.getpid(), (
        f"子进程 release 后锁记录应仍属于本进程,实际: {existing_after!r}"
    )

    # 契约 4:本进程 release 后锁文件真正消失,新一轮 acquire 是全新持有。
    release_scoped_lock(scope, identity)
    ok_fresh, prior_fresh = acquire_scoped_lock(scope, identity)
    assert ok_fresh is True, "release 后重新 acquire 应当成功"
    assert prior_fresh is None, "release 后不应残留旧锁记录"
    release_scoped_lock(scope, identity)


# ---------------------------------------------------------------------------
# 场景 d:快速创建 + 删除任务目录 vs 并发列表扫描 → 不得裸抛 FileNotFoundError
# ---------------------------------------------------------------------------


def test_create_delete_task_dirs_with_concurrent_listing_does_not_raise(tmp_path: Path) -> None:
    """一个线程循环 create_run+rmtree,两个线程并发列表;列表只能记 load_errors,不能裸抛。"""

    manager = _fresh_manager(tmp_path)
    stop = threading.Event()
    writer_error: list[str] = []
    reader_errors: list[str] = []
    reader_errors_guard = threading.Lock()
    list_iterations = [0, 0]

    def writer() -> None:
        try:
            for i in range(25):
                task = manager.create_run(goal=f"churn-{i}", thought="t", plan=["p"])
                # 先删任务工作目录(canonical_state.json 所在),再删 workspace 下的
                # locator 目录,制造 glob 与 load 之间文件消失的竞态窗口。
                shutil.rmtree(task.task_dir, ignore_errors=True)
                shutil.rmtree(task.task_workspace_dir, ignore_errors=True)
                shutil.rmtree(manager.workspace / task.id, ignore_errors=True)
        except BaseException:  # noqa: BLE001
            writer_error.append(traceback.format_exc())
        finally:
            stop.set()

    def reader(slot: int) -> None:
        while not stop.is_set():
            try:
                report = manager.list_runs_report()
                assert isinstance(report.runs, list)
                assert isinstance(report.load_errors, list)  # 结构化记录是允许的。
                selected = manager.indexing.select_runs(None)
                assert isinstance(selected, list)
                list_iterations[slot] += 1
            except BaseException:  # noqa: BLE001 - 任何裸抛(含 FileNotFoundError)都算失败。
                with reader_errors_guard:
                    reader_errors.append(traceback.format_exc())
                return

    readers = [threading.Thread(target=reader, args=(slot,), daemon=True) for slot in range(2)]
    for thread in readers:
        thread.start()
    writer_thread = threading.Thread(target=writer, daemon=True)
    writer_thread.start()
    writer_thread.join(timeout=15)
    stop.set()
    for thread in readers:
        thread.join(timeout=15)

    assert writer_error == [], "创建/删除线程不应抛出异常:\n" + "\n".join(writer_error)
    assert reader_errors == [], (
        "列表操作在目录消失竞态下裸抛了异常(应转为 load_errors 结构化记录):\n"
        + "\n".join(reader_errors)
    )
    assert min(list_iterations) >= 1, "读线程没有真正跑起来,竞态未被覆盖"
    # 收尾确定性断言:全部任务已删除,列表应回到干净状态。
    final_report = manager.list_runs_report()
    assert final_report.runs == []
    assert final_report.load_errors == []


# ---------------------------------------------------------------------------
# 场景 e:多线程并发写不同任务 → 全部可 load、互不污染
# ---------------------------------------------------------------------------


def test_concurrent_distinct_task_saves_do_not_cross_pollute(tmp_path: Path) -> None:
    """8 线程各自 create_run+save 自己的任务,结束后逐一 load 校验字段归属。"""

    manager = _fresh_manager(tmp_path)
    thread_count = 8
    created: dict[int, str] = {}
    created_guard = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def worker(idx: int) -> None:
        barrier.wait(timeout=10)
        task = manager.create_run(goal=f"goal-{idx}", thought=f"thought-{idx}", plan=[f"step-{idx}"])
        task.latest_summary = f"summary-{idx}"
        manager.save(task)
        with created_guard:
            created[idx] = task.id

    errors = _run_threads(worker, thread_count)
    assert errors == [], "并发创建/保存不同任务不应抛出异常:\n" + "\n".join(errors)
    assert len(created) == thread_count
    assert len(set(created.values())) == thread_count, "run_id 出现重复,任务目录互相覆盖"

    for idx, run_id in created.items():
        loaded = manager.load(run_id)
        assert loaded.id == run_id
        assert loaded.goal == f"goal-{idx}", f"任务 {run_id} 的 goal 被其他线程污染: {loaded.goal!r}"
        assert loaded.thought == f"thought-{idx}"
        assert loaded.latest_summary == f"summary-{idx}", (
            f"任务 {run_id} 的 latest_summary 被污染: {loaded.latest_summary!r}"
        )
        canonical = Path(loaded.agent_run_workspace_dir) / "canonical_state.json"
        payload = json.loads(canonical.read_text(encoding="utf-8"))
        assert payload["id"] == run_id

    report = manager.list_runs_report()
    assert len(report.runs) == thread_count
    assert report.load_errors == []
