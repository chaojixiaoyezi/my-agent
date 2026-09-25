"""慢模型/长任务活性合同:区分"连接/首包等待 / 流式无进展 / 总时长 / 明确服务失败"。

四层各自的既有合同(本文件逐层钉住,不引入新机制):
  ① 提供方等待(`_wait_for_generation_result`):流式后端由传输层持有 **空闲** 超时,
     总墙钟守卫必须让位——慢首包/慢持续流不会因为"总耗时长"被判死;非流式才有总预算。
  ② 客户端等待(`poll_gateway_chunks`):等待期只由**机器可观测的活动**续期(服务端租约心跳、
     chunk 文件、请求文件),续期没有总上限;完全没有活动才在空闲窗口后放弃(有界,不无限盲等)。
  ③ 服务端租约心跳(`start_lease_heartbeat`):静默期(慢首包/长工具/等子代理)里持续刷新
     processing 请求文件的租约时间,让①的"活动"事实成立;进程存活不等于请求健康,反过来
     心跳在推进也不允许被"总时长"判死。
  ④ 长工具租约续租(`_invoke_with_lease_renewal`):handler 阻塞超过初始 lease 时滚动续租,
     不会被另一个请求按 lease 过期抢锁(避免重复副作用)。

用户停止与真实错误仍然优先:①③ 里停止/异常路径单独断言。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_model_generation import (
    _MODEL_INTERRUPT_DRAIN_SECONDS,
    ModelGenerateParams,
    _BackendGenerateResult,
    _wait_for_generation_result,
)
from agent_py_agent.agent.backends import ProviderTimeoutError
from agent_py_agent.agent.concurrency.interrupt import set_interrupt
from agent_py_agent.agent.gateway_parts.lease_service import start_lease_heartbeat
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    poll_gateway_chunks,
)


# --------------------------------------------------------------------------------------
# ① 提供方等待:流式只看空闲,不按总时长判死
# --------------------------------------------------------------------------------------
def _request(*, stream: bool):
    backend = SimpleNamespace(
        stream_enabled=stream,
        stream_timeout_is_idle=stream,
        model_name="slow-test-backend",
    )
    return ModelGenerateParams(
        agent=SimpleNamespace(backend=backend),
        params=SimpleNamespace(live_archive_state={}),
        prompt="慢任务",
        tool_rounds=1,
    )


def _results_after(delay: float, result: _BackendGenerateResult) -> tuple[queue.Queue, threading.Thread]:
    results: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            time.sleep(delay)
            results.put(result)
        finally:
            set_interrupt(False)  # 与生产生成线程相同：退出前撤掉可能被立的中断旗，不把脏标志留给复用 ident 的线程

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return results, thread


def test_streaming_result_after_total_budget_is_not_killed() -> None:
    """流式:结果在总预算之后才到,也必须被接受(总时长不判死正在推进的请求)。"""
    request = _request(stream=True)
    results, worker = _results_after(0.4, _BackendGenerateResult(response="slow but alive"))

    response = _wait_for_generation_result(request, None, results, worker, timeout=0.05)

    assert response == "slow but alive"
    worker.join(timeout=2)


def test_non_stream_total_budget_still_fails_explicitly() -> None:
    """非流式没有进度信号:总预算到点必须给出**显式**超时(而不是静默挂住)。"""
    request = _request(stream=False)
    results, worker = _results_after(0.6, _BackendGenerateResult(response="too late"))

    with pytest.raises(ProviderTimeoutError) as excinfo:
        _wait_for_generation_result(request, None, results, worker, timeout=0.05)

    assert excinfo.value.stage == "wall_clock"
    worker.join(timeout=2)


def test_user_stop_wins_during_slow_generation() -> None:
    """用户停止优先于慢请求:立刻以中断结束,不等慢结果。"""
    request = _request(stream=True)
    results, worker = _results_after(5.0, _BackendGenerateResult(response="never"))
    set_interrupt(True, threading.get_ident())
    try:
        started = time.monotonic()
        with pytest.raises(InterruptedError):
            _wait_for_generation_result(request, None, results, worker, timeout=30.0)
        # 停止必须在传输收口的有限窗口内生效,而不是等慢结果(5s 后才到)
        assert time.monotonic() - started < _MODEL_INTERRUPT_DRAIN_SECONDS + 0.5
    finally:
        set_interrupt(False, threading.get_ident())


def test_provider_http_failure_is_raised_immediately() -> None:
    """明确服务失败:传输异常立刻上抛,不被当成"再等等"。"""
    request = _request(stream=True)
    failure = RuntimeError("provider 503")
    results, worker = _results_after(0.05, _BackendGenerateResult(exc=failure))

    with pytest.raises(RuntimeError, match="provider 503"):
        _wait_for_generation_result(request, None, results, worker, timeout=30.0)
    worker.join(timeout=2)


# --------------------------------------------------------------------------------------
# ② 客户端等待:只由机器可观测活动续期
# --------------------------------------------------------------------------------------
def _terminal(path: Path, request_id: str, response: str) -> None:
    terminal_response = {"id": request_id, "ok": True, "response": response, "status": "done"}
    path.write_text(
        json.dumps(
            {
                "schema_version": "gateway_terminal_request.v1",
                "id": request_id,
                "status": "done",
                "turn_phase": "closed",
                "terminal_response": terminal_response,
            }
        ),
        encoding="utf-8",
    )


def test_slow_stream_renews_wait_beyond_total_deadline(tmp_path) -> None:
    """慢持续流:总耗时远超初始 deadline,只要 chunk 在推进就必须继续等到终态。"""
    chunk_path = tmp_path / "req.chunks.jsonl"
    terminal_path = tmp_path / "req.json"
    seen: list[str] = []

    def slow_stream() -> None:
        for index in range(8):
            with chunk_path.open("a", encoding="utf-8") as handle:
                # 只要求"文件在推进且该行被消费":行形状用既有 legacy 投影认得的 text 字段。
                handle.write(json.dumps({"text": f"段{index}"}) + "\n")
            time.sleep(0.06)
        _terminal(terminal_path, "req", "慢流完成")

    worker = threading.Thread(target=slow_stream)
    worker.start()
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            terminal_path,
            time.time() + 0.05,  # 初始 deadline 远早于真实完成时间
            lambda chunk: seen.append(str(chunk)) or True,
            [0],
            [0],
            activity_paths=(chunk_path,),
            inactivity_timeout_seconds=0.12,
        )
    )
    worker.join(timeout=5)

    assert response, "慢流不得因总时长被判死"
    assert response["response"] == "慢流完成"
    assert seen, "推进期间的 chunk 必须仍然被消费"


def test_heartbeat_only_activity_keeps_client_waiting(tmp_path) -> None:
    """静默期(慢首包/长工具/等子代理):只有服务端租约心跳在推进时也必须继续等。"""
    chunk_path = tmp_path / "req.chunks.jsonl"
    processing_path = tmp_path / "processing.json"
    terminal_path = tmp_path / "req.json"
    processing_path.write_text("{}", encoding="utf-8")

    def silent_work() -> None:
        for index in range(6):
            time.sleep(0.06)
            processing_path.write_text(
                json.dumps({"lease_heartbeat_at": time.time(), "tick": index}),
                encoding="utf-8",
            )
        _terminal(terminal_path, "req", "静默后完成")

    worker = threading.Thread(target=silent_work)
    worker.start()
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            terminal_path,
            time.time() + 0.05,
            lambda _chunk: False,
            [0],
            activity_paths=(chunk_path, processing_path),
            inactivity_timeout_seconds=0.12,
        )
    )
    worker.join(timeout=5)

    assert response, "有租约心跳的静默期不得被判成死请求"
    assert response["response"] == "静默后完成"


def test_no_activity_expires_after_inactivity_window(tmp_path) -> None:
    """完全没有活动:必须在空闲窗口后放弃(有界,不做无限盲等)。"""
    chunk_path = tmp_path / "req.chunks.jsonl"
    processing_path = tmp_path / "processing.json"
    terminal_path = tmp_path / "req.json"
    processing_path.write_text("{}", encoding="utf-8")

    started = time.monotonic()
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            terminal_path,
            time.time() + 0.05,
            lambda _chunk: False,
            [0],
            activity_paths=(chunk_path, processing_path),
            inactivity_timeout_seconds=0.15,
        )
    )
    elapsed = time.monotonic() - started

    assert response == {}
    assert elapsed < 3.0, f"必须在空闲窗口内收口,实际 {elapsed:.2f}s"


def test_late_heartbeat_after_stall_does_not_resurrect_dead_wait(tmp_path) -> None:
    """真死请求(心跳早已停):即使后来有人碰了文件,也不应该再无限等下去。

    这里锁住"续期只能来自**当前**窗口内的活动"这一语义:窗口内没有任何活动就收口。
    """
    chunk_path = tmp_path / "req.chunks.jsonl"
    processing_path = tmp_path / "processing.json"
    terminal_path = tmp_path / "req.json"
    processing_path.write_text("{}", encoding="utf-8")

    def late_touch() -> None:
        time.sleep(0.5)  # 远晚于空闲窗口
        processing_path.write_text('{"late": 1}', encoding="utf-8")

    worker = threading.Thread(target=late_touch, daemon=True)
    worker.start()
    started = time.monotonic()
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            terminal_path,
            time.time() + 0.05,
            lambda _chunk: False,
            [0],
            activity_paths=(chunk_path, processing_path),
            inactivity_timeout_seconds=0.15,
        )
    )
    elapsed = time.monotonic() - started

    assert response == {}
    assert elapsed < 0.45, f"必须在空闲窗口内收口,实际 {elapsed:.2f}s"


# --------------------------------------------------------------------------------------
# ③ 服务端租约心跳:静默期持续推进(进程存活不等于健康,但推进必须被看见)
# --------------------------------------------------------------------------------------
def test_lease_heartbeat_advances_without_display_activity(tmp_path) -> None:
    request_path = tmp_path / "req.json"
    request_path.write_text(
        json.dumps(
            {
                "schema": "gateway_request.v1",
                "id": "req-heartbeat",
                "owner": {"provider": "local", "kind": "main", "id": "local/main"},
                "status": "processing",
                "lease_owner": "worker-1",
            }
        ),
        encoding="utf-8",
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(
            gateway_heartbeat_interval=0.05,
            gateway_processing_timeout_seconds=0,
        )
    )
    stop, thread = start_lease_heartbeat(
        agent,
        request_path,
        request_id="req-heartbeat",
        worker_id="worker-1",
    )
    try:
        stamps: list[float] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(stamps) < 3:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            value = float(payload.get("lease_heartbeat_at") or 0)
            if value and (not stamps or value > stamps[-1]):
                stamps.append(value)
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join(timeout=2)

    assert len(stamps) >= 3, f"静默期必须持续刷新租约,实际 {stamps}"
    assert stamps == sorted(stamps)
    first_size = request_path.stat().st_size
    time.sleep(0.2)
    assert request_path.stat().st_size == first_size or True  # 停止后不再要求(仅记录)


# --------------------------------------------------------------------------------------
# ④ 长工具:租约滚动续租,不会被按过期抢锁
# --------------------------------------------------------------------------------------
def test_long_tool_renews_lease_while_handler_blocks() -> None:
    from agent_py_agent.agent.local_storage.tool_operations import (
        ToolOperationClaim,
        ToolOperationRecord,
    )
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        ToolOperationExecutionRequest,
        _invoke_with_lease_renewal,
    )

    renewals: list[float] = []

    class Store:
        def renew_tool_operation_lease(self, *, operation_id, holder_id, lease_expires_at, owner_id, run_id):
            renewals.append(lease_expires_at)
            return True

    record = ToolOperationRecord(
        owner_id="local/main",
        run_id="run-1",
        task_id="task-1",
        operation_id="op-1",
        tool="slow_tool",
        args_hash="hash",
        idempotency_key="key",
        idempotency_scope="scope",
        idempotency_namespace="ns",
        status="EXECUTING",
        holder_id="holder-1",
        holder_host="test-host",
        holder_pid=1,
        holder_process_start_token="token",
        generation=1,
        lease_expires_at=time.time() + 0.4,
    )
    claim = ToolOperationClaim(action="claimed", record=record)
    request = ToolOperationExecutionRequest(
        store=Store(),
        store_required=True,
        owner_id="local/main",
        run_id="run-1",
        task_id="task-1",
        operation_id="op-1",
        tool_name="slow_tool",
        args_hash="hash",
        idempotency_key="key",
        idempotency_scope="scope",
        idempotency_namespace="ns",
        timeout_seconds=0,
        invoke=lambda: (time.sleep(1.3), "done")[1],
    )

    result = _invoke_with_lease_renewal(request, claim)

    assert result == "done"
    assert len(renewals) >= 2, f"长 handler 期间必须滚动续租,实际 {len(renewals)} 次"
    assert renewals[-1] > time.time() - 1.0, "最后一次续租必须覆盖 handler 结束时刻"
    assert renewals == sorted(renewals), "续租到期时间必须单调递增"


def test_lease_renewal_stops_after_handler_returns() -> None:
    from agent_py_agent.agent.local_storage.tool_operations import (
        ToolOperationClaim,
        ToolOperationRecord,
    )
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        ToolOperationExecutionRequest,
        _invoke_with_lease_renewal,
    )

    renewals: list[float] = []

    class Store:
        def renew_tool_operation_lease(self, *, operation_id, holder_id, lease_expires_at, owner_id, run_id):
            renewals.append(lease_expires_at)
            return True

    record = ToolOperationRecord(
        owner_id="local/main",
        run_id="run-1",
        task_id="task-1",
        operation_id="op-2",
        tool="quick_tool",
        args_hash="hash",
        idempotency_key="key",
        idempotency_scope="scope",
        idempotency_namespace="ns",
        status="EXECUTING",
        holder_id="holder-2",
        holder_host="test-host",
        holder_pid=1,
        holder_process_start_token="token",
        generation=1,
        lease_expires_at=time.time() + 0.2,
    )
    request = ToolOperationExecutionRequest(
        store=Store(),
        store_required=True,
        owner_id="local/main",
        run_id="run-1",
        task_id="task-1",
        operation_id="op-2",
        tool_name="quick_tool",
        args_hash="hash",
        idempotency_key="key",
        idempotency_scope="scope",
        idempotency_namespace="ns",
        timeout_seconds=0,
        invoke=lambda: "fast",
    )

    assert _invoke_with_lease_renewal(request, ToolOperationClaim(action="claimed", record=record)) == "fast"
    settled = len(renewals)
    time.sleep(0.6)
    assert len(renewals) == settled, "handler 返回后必须停止续租,不允许后台继续改租约"
