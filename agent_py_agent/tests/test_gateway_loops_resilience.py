"""网关后台线程循环的永不停机硬保障单测。

真机实锤(1.10):网关进程被 systemd 拉着 active,但干活的后台主循环线程死了再也不回来。
`_gateway_background_main_loop` 的 tick 编排缝隙(种子重扫/owner 池同步/提交/汇报)原先
不在任何保护内,一个异常就杀死整条线程。撤修复即 FAIL:异常直接传播出循环函数。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths


def test_background_main_loop_survives_tick_exceptions_and_reports_supply_error(monkeypatch, capsys) -> None:
    from agent_py_agent.cli import gateway_loops

    stop_event = threading.Event()
    ticks = {"count": 0}

    class _ExplodingSupervisor:
        poll_interval = 0.01

        def __init__(self, context) -> None:
            pass

        def tick(self) -> bool:
            ticks["count"] += 1
            if ticks["count"] >= 3:
                stop_event.set()
            raise ProviderTransientError('HTTP 429: rate_limit_error "已达到 Token Plan 用量上限"')

    monkeypatch.setattr(gateway_loops, "_BackgroundMainSupervisor", _ExplodingSupervisor)
    monkeypatch.setattr(gateway_loops, "_start_orphan_reconcile_loop", lambda _context, _stop: None)
    monkeypatch.setattr(gateway_loops, "_start_owner_maintenance_loop", lambda _context, _stop: None)
    monkeypatch.setattr(gateway_loops, "_start_scheduler_due_loop", lambda _context, _stop: None)

    gateway_loops._gateway_background_main_loop(SimpleNamespace(), stop_event)  # 撤修复:异常在这里炸出

    assert ticks["count"] >= 3  # 循环在异常后继续跑,只随 stop_event 退出
    err = capsys.readouterr().err
    assert "[gateway-loop-error]" in err
    assert '"category": "provider_transient"' in err  # 联动分类修复:429 不再误标 programmer_bug
    assert '"recoverable": true' in err
    assert "programmer_bug" not in err


def test_heartbeat_loop_survives_write_failure(monkeypatch) -> None:
    from agent_py_agent.cli import gateway_loops

    stop_event = threading.Event()
    writes = {"count": 0}

    def _failing_write(paths, agent, *, status, pid) -> None:
        writes["count"] += 1
        if writes["count"] >= 2:
            stop_event.set()
        raise OSError("disk full")

    monkeypatch.setattr(gateway_loops, "_write_gateway_heartbeat", _failing_write)
    context = SimpleNamespace(
        paths=SimpleNamespace(),
        agent=SimpleNamespace(config=SimpleNamespace(gateway_heartbeat_interval=0)),
        options=SimpleNamespace(),
    )

    gateway_loops._gateway_heartbeat_loop(context, stop_event)  # 撤修复:OSError 在这里炸出

    assert writes["count"] >= 2  # 写失败不杀心跳线程


def test_request_worker_initialization_error_is_terminalized(tmp_path, monkeypatch) -> None:
    """A failure before the normal request handler must not strand processing state."""
    from agent_py_agent.cli import gateway_loops

    root = tmp_path / "gateway"
    paths = GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "gateway_state.json",
        heartbeat=root / "gateway_heartbeat.json",
        stop_request=root / "gateway_stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "gateway_requests.jsonl",
    )
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    processing = paths.processing / "req-ast.json"
    processing.write_text(
        json.dumps(
            {
                "id": "req-ast",
                "kind": "ask",
                "created_at": 1.0,
                "attempts": 1,
                "status": "processing",
            }
        ),
        encoding="utf-8",
    )

    dispatcher = object.__new__(gateway_loops._RequestDispatcher)
    dispatcher.paths = paths

    def explode():
        raise SystemError("AST constructor recursion depth mismatch (before=48, after=53)")

    dispatcher._thread_agent = explode
    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gateway_loops.admission,
        "release",
        lambda user_key, *, conversation_key: released.append((user_key, conversation_key)),
    )

    dispatcher._execute(processing, "user-1", "conversation-1")

    response = json.loads((paths.responses / "req-ast.json").read_text(encoding="utf-8"))
    archived = json.loads((paths.failed / "req-ast.json").read_text(encoding="utf-8"))
    assert response["status"] == "failed"
    assert response["error_code"] == "GATEWAY_WORKER_UNHANDLED_ERROR"
    assert response["worker_error"]["category"] == "programmer_bug"
    assert archived["status"] == "failed"
    assert processing.exists() is False
    assert released == [("user-1", "conversation-1")]


class _ImmediateFuture:
    """Submit 即执行;done 恒 True(可被 _run_due_curators 清 in-flight)。"""

    def __init__(self, fn, args) -> None:
        self._value = None
        self._error = None
        try:
            self._value = fn(*args)
        except Exception as exc:  # noqa: BLE001 - 测试辅助
            self._error = exc

    def done(self) -> bool:
        return True

    def result(self):
        if self._error is not None:
            raise self._error
        return self._value


class _RunningFuture:
    def done(self) -> bool:
        return False  # 模拟 curator 提取仍在跑(LLM 调用中)


def _curator_supervisor(*, base_calls, scoped_calls, running=()) -> object:
    from agent_py_agent.cli import gateway_loops

    base_agent = SimpleNamespace(
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60),
        memory_curator=SimpleNamespace(run_if_due=lambda: base_calls.append(1)),
        owner_id="local",
    )
    scoped_agent = SimpleNamespace(
        memory_curator=SimpleNamespace(run_if_due=lambda: scoped_calls.append(1)),
        owner_id="owner-a",
    )
    pool = SimpleNamespace(active_agents=lambda: [scoped_agent])

    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = base_agent
    supervisor._owner_pool = pool
    supervisor._curator_inflight = {}
    supervisor._next_curator_run_at = 0.0
    supervisor._get_executor = lambda: SimpleNamespace(
        submit=lambda fn, *args: _ImmediateFuture(fn, args)
    )
    supervisor._curator_inflight.update({id(agent): _RunningFuture() for agent in running})
    return supervisor, base_agent, scoped_agent


def test_background_main_supervisor_wakes_base_and_scoped_curators_with_throttle() -> None:
    base_calls: list[int] = []
    scoped_calls: list[int] = []
    supervisor, _base, _scoped = _curator_supervisor(
        base_calls=base_calls, scoped_calls=scoped_calls
    )

    supervisor._run_due_curators()
    assert base_calls == [1] and scoped_calls == [1]  # base + 每个活跃 scoped owner 都被唤醒
    assert supervisor._next_curator_run_at > 0.0  # 节流已生效

    supervisor._run_due_curators()  # 节流窗口内再调 → 不重复唤醒
    assert base_calls == [1] and scoped_calls == [1]


def test_background_main_supervisor_curator_dedupes_and_isolates_failures(monkeypatch) -> None:
    from agent_py_agent.cli import gateway_loops

    base_calls: list[int] = []
    scoped_calls: list[int] = []
    supervisor, base_agent, scoped_agent = _curator_supervisor(
        base_calls=base_calls, scoped_calls=scoped_calls
    )

    # base 的 curator 抛异常 → _safe_run_curator 兜住,scoped 照常唤醒(单 owner 失败隔离)
    def _boom():
        raise RuntimeError("curator backend down")

    base_agent.memory_curator.run_if_due = _boom
    supervisor._run_due_curators()
    assert base_calls == [] and scoped_calls == [1]

    # in-flight 去重:某 owner 上一轮还没跑完 → 本轮不重复提交;其余照常
    supervisor._next_curator_run_at = 0.0  # 破节流
    supervisor._curator_inflight[id(scoped_agent)] = _RunningFuture()
    base_agent.memory_curator.run_if_due = lambda: base_calls.append(1)
    supervisor._run_due_curators()
    assert base_calls == [1] and scoped_calls == [1]  # base 被唤醒,scoped 仍在跑被跳过
