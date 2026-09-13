from __future__ import annotations

"""GW-01 残留回归：state/heartbeat 交叉时的代际优先级（唯一权威判据 gateway_readiness）。

先做的可达性验证结论固化在这里，避免以后再靠假设争论：

1. 同一 PID 下 state=failed 与 heartbeat=running 会在真实写入链路里并存一个窗口：
   state 的失败记录由 gateway_process 在服务循环结束后写入（`_record_gateway_service_termination`
   或 `_record_gateway_run_failed`），而心跳线程要到 `_cmd_gateway_run_cleanup` 才停止并改写心跳，
   所以这段时间磁盘上就是 {state: failed, heartbeat: running}，且进程仍然存活。
   旧判据把 running 排在 failed 前面，于是附着式调用方（gateway ask / chat --gateway / TUI preflight）
   被误判 ready。本文件用真实写入方（`_write_gateway_heartbeat` / `_record_gateway_run_failed`）
   与真实判据把"不得误 ready"固定下来，不注入判据替身。
2. HTTP 服务线程崩溃时 state=http_server_error，而服务循环只等 stop request（不检查 HTTP 服务），
   进程与心跳线程继续存活 → 旧判据会**永久** ready。
3. heartbeat 载荷现在带 started_at：写入函数从"同 pid 的 state 记录"取本代锚点（state 的 running
   记录写的就是 context.process_started_at，两者同源），终止清理时 state 已是不含 started_at 的
   终止载荷，则继承"同 pid 的既有心跳"里已经写下的同一个值。heartbeat 分支因此也能做代际校验；
   无 started_at 的旧格式仍按"无法证明陈旧"兼容接受，否则升级期间一个健康的旧版网关会被新判据判死。

只用 tmp 目录里的真实文件，不启动真 Gateway、不碰用户状态目录、不发信号。
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts.daemon_metadata import _get_process_start_time
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.agent.gateway_parts.status_rendering import (
    _record_replaces,
    gateway_readiness,
    wait_for_gateway_readiness,
    wait_for_gateway_running,
)


# LLM: 判据文件必须与生产一致（state.json / gateway_heartbeat.json 同名同位置），
#   这样测试写的就是判据真正会读的那两个文件，不需要任何路径替身。
# 函数用途: 构造测试专用 Gateway 路径合同，全部落在 tmp 目录内。
def _readiness_paths(tmp_path: Path) -> GatewayPaths:
    return GatewayPaths(
        root=tmp_path / "gateway",
        pid=tmp_path / "gateway" / "gateway.pid",
        adapter_pid=tmp_path / "gateway" / "adapter.pid",
        state=tmp_path / "gateway" / "state.json",
        heartbeat=tmp_path / "gateway" / "heartbeat.json",
        stop_request=tmp_path / "gateway" / "stop.request",
        log=tmp_path / "gateway" / "gateway.log",
        inbox=tmp_path / "gateway" / "requests" / "pending",
        processing=tmp_path / "gateway" / "requests" / "processing",
        done=tmp_path / "gateway" / "requests" / "done",
        failed=tmp_path / "gateway" / "requests" / "failed",
        responses=tmp_path / "gateway" / "responses",
        history=tmp_path / "gateway" / "gateway_requests.jsonl",
    )


# LLM: 心跳写入方只依赖 agent.subagents.workspace；事件日志走 agent.local_store.log_record。
#   测试提供最小替身，让真实写入方可以离线跑，而不是把写入方逻辑抄一份进测试。
# 类用途: 记录 Gateway 事件的空实现，测试里不落任何账。
class _StubLocalStore:
    # 函数用途: 吞掉事件写入，测试只关心状态文件。
    def log_record(self, **_kwargs) -> None:
        return None


# LLM: 真实写入方需要的 agent 形状（心跳、失败记录、心跳循环）。替身只补齐被读到的字段，
#   被读到的字段名就是写入方的真实契约，改写入方时要一起改这里。
# 函数用途: 构造最小 agent 替身，供真实写入方在 tmp 目录里运行。
def _stub_agent(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        subagents=SimpleNamespace(workspace=str(tmp_path / "sub")),
        local_store=_StubLocalStore(),
        config=SimpleNamespace(gateway_heartbeat_interval=1.0),
    )


# LLM: 代际锚点来自 PID 记录的 updated_at（status_rendering.gateway_generation_anchor）；
#   start_time 必须是真实进程出生时间，否则 get_running_pid_report 会按 PID 复用把记录当陈旧。
# 函数用途: 写一份带指定代际锚点的 PID 记录。
def _write_pid_record(paths: GatewayPaths, pid: int, *, anchor_at: float | None = None) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.pid.write_text(
        json.dumps(
            {
                "pid": int(pid),
                "kind": "my-agent-gateway",
                "start_time": _get_process_start_time(int(pid)),
                "updated_at": datetime.fromtimestamp(
                    anchor_at if anchor_at is not None else time.time(),
                    timezone.utc,
                ).isoformat(),
            }
        ),
        encoding="utf-8",
    )


# LLM: 用于造"可证明是上一代"的旧记录（带显式 started_at）；本代的 state/heartbeat 走真实写入方。
# 函数用途: 直接写一份带 pid/status/started_at 的状态记录。
def _write_record(
    paths: GatewayPaths,
    *,
    pid: int,
    status: str,
    started_at: float,
    heartbeat: bool = False,
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    target = paths.heartbeat if heartbeat else paths.state
    target.write_text(
        json.dumps({"pid": int(pid), "status": status, "started_at": started_at}),
        encoding="utf-8",
    )


# LLM: 读回真实文件，证明判据当时确实同时看到了两条记录（而不是只有一条）。
# 函数用途: 读取一份 JSON 记录，缺失时返回空字典。
def _read_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# LLM: spawn 方的存活事实来自 poll()（gateway_process._wait_for_gateway_start_ready 的输入）。
# 类用途: 模拟 Popen 形状的子进程。
class _FakeSpawnedProcess:
    # 函数用途: 记录伪造 PID 与是否已退出。
    def __init__(self, pid: int, *, alive: bool = True) -> None:
        self.pid = int(pid)
        self._alive = alive

    # 函数用途: 与 Popen.poll() 同语义，None 表示仍在运行。
    def poll(self):
        return None if self._alive else 1


# ---------------------------------------------------------------------------
# 修复一：心跳载荷带代际锚点 started_at
# ---------------------------------------------------------------------------


# LLM: 心跳线程是就绪发布方之一，它写出去的 started_at 必须与 state 的 started_at 同源
#   （state 的 running 记录写的就是 context.process_started_at），否则 heartbeat 分支无法做代际校验。
#   心跳循环的调用签名保持不变（锚点在写入函数内部解析），所以这里跑的是真实循环。
# 函数用途: 验证心跳循环写出的载荷带本代 started_at（取自 state 记录）。
def test_heartbeat_loop_writes_state_generation_started_at(tmp_path: Path) -> None:
    from agent_py_agent.cli.gateway_loops import _gateway_heartbeat_loop

    paths = _readiness_paths(tmp_path)
    started_at = time.time() - 5.0
    # 生命周期所有者先发布本代记录（gateway_process 的 setup/发布顺序）
    _write_record(paths, pid=os.getpid(), status="running", started_at=started_at)
    stop_event = threading.Event()
    context = SimpleNamespace(paths=paths, agent=_stub_agent(tmp_path))

    thread = threading.Thread(
        target=_gateway_heartbeat_loop,
        args=(context, stop_event),
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.time() + 5.0
        while not paths.heartbeat.exists() and time.time() < deadline:
            time.sleep(0.01)
    finally:
        stop_event.set()
        thread.join(timeout=5)

    heartbeat = _read_record(paths.heartbeat)
    assert heartbeat["status"] == "running"
    assert heartbeat["pid"] == os.getpid()
    assert heartbeat["started_at"] == started_at


# LLM: 终止清理心跳（gateway_process 的调用点）不传锚点，且此时 state 已被改写成不含 started_at 的
#   终止记录，写入函数必须从"同 pid 的既有心跳"继承本代锚点；pid 不同时绝不继承。
# 函数用途: 验证同 pid 继承、异 pid 不继承。
def test_cleanup_heartbeat_inherits_same_pid_generation_anchor(tmp_path: Path) -> None:
    from agent_py_agent.cli.gateway_loops import _write_gateway_heartbeat

    paths = _readiness_paths(tmp_path)
    agent = _stub_agent(tmp_path)
    started_at = time.time() - 30.0

    # 运行期：锚点来自 state 的本代记录
    _write_record(paths, pid=os.getpid(), status="running", started_at=started_at)
    _write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
    assert _read_record(paths.heartbeat)["started_at"] == started_at

    # 终止清理：state 被终止载荷覆盖（不含 started_at）→ 继承本代既有心跳的锚点
    _write_record(paths, pid=os.getpid(), status="failed", started_at=0.0)
    _write_gateway_heartbeat(paths, agent, status="failed", pid=os.getpid())
    assert _read_record(paths.heartbeat)["started_at"] == started_at

    # 换了 pid（上一代残留）→ 两个来源的 pid 都不匹配 → 0.0，不抄别的进程代的锚点
    _write_gateway_heartbeat(paths, agent, status="failed", pid=os.getpid() + 1)
    assert _read_record(paths.heartbeat)["started_at"] == 0.0


# ---------------------------------------------------------------------------
# 修复二：state 越过 starting 后不被 heartbeat 的 running 掩盖
# ---------------------------------------------------------------------------


# LLM: 这是 GW-01 残留的核心组合（第一步实测：真实链路里 5/5 出现，窗口约 0.5s）：
#   state 已由真实失败写入方写成 failed，心跳文件仍是本代最后一次 running（心跳线程还没被 stop）。
#   判据必须报 failed/GATEWAY_START_FAILED，附着式等待也必须 alive=False。
# 函数用途: 验证 state=failed 不被 running heartbeat 掩盖。
def test_state_failed_after_running_heartbeat_is_not_ready(tmp_path: Path) -> None:
    from agent_py_agent.cli.gateway_loops import _write_gateway_heartbeat
    from agent_py_agent.cli.gateway_process import _record_gateway_run_failed

    paths = _readiness_paths(tmp_path)
    agent = _stub_agent(tmp_path)
    pid = os.getpid()
    anchor = time.time()
    _write_pid_record(paths, pid, anchor_at=anchor)

    # 真实发布顺序：生命周期所有者先写 state=running（gateway_process:409），
    # 心跳线程随后写 running（gateway_loops:1567），锚点与 state 同源。
    _write_record(paths, pid=pid, status="running", started_at=anchor)
    _write_gateway_heartbeat(paths, agent, status="running", pid=pid)
    # 真实失败写入方：state=failed（心跳线程此时仍活着，文件里还是 running）。
    _record_gateway_run_failed(paths, agent, pid, RuntimeError("controlled service loop failure"))

    state_record = _read_record(paths.state)
    heartbeat_record = _read_record(paths.heartbeat)
    assert state_record["status"] == "failed"
    assert heartbeat_record["status"] == "running"

    readiness = gateway_readiness(paths, pid=pid, not_before=anchor, process_alive=True)
    assert (readiness.state, readiness.reason) == ("failed", "GATEWAY_START_FAILED")
    assert readiness.ready is False
    assert readiness.source == "state"
    assert readiness.status == "failed"

    # 附着式调用方（gateway ask / chat --gateway 的 wait_for_gateway_running）不得被放行。
    _, alive = wait_for_gateway_running(paths, 0.3)
    assert alive is False

    # 等待结局必须是"服务失败"而不是"超时/仍在启动"：调用方能据此分型。
    outcome = wait_for_gateway_readiness(paths, 0.3)
    assert (outcome.state, outcome.reason) == ("failed", "GATEWAY_START_FAILED")


# LLM: HTTP 服务线程崩溃后 state=http_server_error，进程与心跳线程仍活着，服务循环只等 stop request，
#   所以这是**永久**态：判据必须按失败暴露，不能让 heartbeat 的周期 running 无限期掩盖。
# 函数用途: 验证 state=http_server_error 不被 running heartbeat 掩盖。
def test_http_server_error_is_not_masked_by_running_heartbeat(tmp_path: Path) -> None:
    from agent_py_agent.cli.gateway_loops import _write_gateway_heartbeat

    paths = _readiness_paths(tmp_path)
    agent = _stub_agent(tmp_path)
    pid = os.getpid()
    anchor = time.time()
    _write_pid_record(paths, pid, anchor_at=anchor)
    _write_record(paths, pid=pid, status="running", started_at=anchor)
    _write_gateway_heartbeat(paths, agent, status="running", pid=pid)
    # http_service._record_serve_error 写出的载荷形状（在既有 state 上更新 status）。
    _write_record(paths, pid=pid, status="http_server_error", started_at=anchor)

    readiness = gateway_readiness(paths, pid=pid, not_before=anchor, process_alive=True)

    assert readiness.status == "http_server_error"
    assert (readiness.state, readiness.reason) == ("failed", "GATEWAY_START_FAILED")
    assert readiness.ready is False
    _, alive = wait_for_gateway_running(paths, 0.3)
    assert alive is False


# LLM: 对照组：发布顺序是 HTTP bind → heartbeat running → state running，所以 state 还停在 starting
#   而本代 heartbeat 已 running 时仍必须就绪，不能把发布窗口里的网关判成未启动。
# 函数用途: 验证 starting + 本代 running heartbeat 保持 ready。
def test_state_starting_with_current_generation_heartbeat_is_ready(tmp_path: Path) -> None:
    from agent_py_agent.cli.gateway_loops import _write_gateway_heartbeat

    paths = _readiness_paths(tmp_path)
    agent = _stub_agent(tmp_path)
    pid = os.getpid()
    anchor = time.time()
    _write_pid_record(paths, pid, anchor_at=anchor)
    _write_record(paths, pid=pid, status="starting", started_at=anchor)
    _write_gateway_heartbeat(paths, agent, status="running", pid=pid)

    readiness = gateway_readiness(paths, pid=pid, not_before=anchor, process_alive=True)

    assert readiness.ready is True
    assert readiness.source == "heartbeat"
    assert (readiness.state, readiness.reason) == ("ready", "GATEWAY_READY")
    _, alive = wait_for_gateway_running(paths, 0.3)
    assert alive is True


# ---------------------------------------------------------------------------
# 代际校验：可证明的旧 heartbeat 必须被拒绝
# ---------------------------------------------------------------------------


# LLM: heartbeat 现在带 started_at，因此"可证明是上一代"的 running 心跳必须被代际过滤拒绝：
#   state 还停在 starting 时不得被它放行；state 已是本代 running 时以 state 为准（来源必须是 state）。
# 函数用途: 验证旧代 heartbeat 被代际过滤，state 决定结论。
def test_stale_heartbeat_generation_is_rejected_and_state_decides(tmp_path: Path) -> None:
    paths = _readiness_paths(tmp_path)
    pid = os.getpid()
    anchor = time.time()
    previous_generation = anchor - 3600.0
    _write_pid_record(paths, pid, anchor_at=anchor)

    # state=starting（本代）+ 旧代 running heartbeat → 未就绪，不能靠旧心跳放行
    _write_record(paths, pid=pid, status="starting", started_at=anchor)
    _write_record(paths, pid=pid, status="running", started_at=previous_generation, heartbeat=True)
    starting = gateway_readiness(paths, pid=pid, not_before=anchor, process_alive=True)
    assert (starting.state, starting.reason) == ("starting", "GATEWAY_STARTING")
    assert starting.ready is False and starting.source == "state"

    # state=running（本代）+ 旧代 running heartbeat → 就绪，且结论来源是本代 state
    _write_record(paths, pid=pid, status="running", started_at=anchor)
    ready = gateway_readiness(paths, pid=pid, not_before=anchor, process_alive=True)
    assert ready.ready is True and ready.source == "state"

    # 只有旧代 heartbeat（没有 state）→ 也不能算就绪
    paths.state.unlink()
    only_stale = gateway_readiness(paths, pid=pid, not_before=anchor, process_alive=True)
    assert only_stale.ready is False
    assert (only_stale.state, only_stale.reason) == ("starting", "GATEWAY_STARTING")


# LLM: 兼容边界必须显式固定：没有 started_at 的心跳（升级前旧版本写入方 / 人工文件）无法被证明陈旧，
#   判据按既有语义接受它。若这里改成拒绝，升级期间一个健康运行的旧版网关会被新判据判死。
# 函数用途: 验证无 started_at 的 heartbeat 保持向后兼容，且仍然受 pid 约束。
def test_heartbeat_without_started_at_keeps_legacy_compatibility(tmp_path: Path) -> None:
    paths = _readiness_paths(tmp_path)
    pid = os.getpid()
    anchor = time.time()
    _write_pid_record(paths, pid, anchor_at=anchor)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.heartbeat.write_text(
        json.dumps({"pid": pid, "status": "running", "updated_at": anchor}),
        encoding="utf-8",
    )

    # 仅旧格式 heartbeat running（无 state）→ 仍是发布信号
    assert gateway_readiness(paths, pid=pid, not_before=anchor, process_alive=True).ready is True

    # 但 heartbeat 必须仍受 pid 约束：别的 pid 的 running 不能放行
    paths.heartbeat.write_text(
        json.dumps({"pid": pid + 1, "status": "running", "updated_at": anchor}),
        encoding="utf-8",
    )
    foreign = gateway_readiness(paths, pid=pid, not_before=anchor, process_alive=True)
    assert foreign.ready is False


# LLM: 优先级规则不许依赖读取顺序或状态枚举顺序：同一条组合无论谁先被读到都要得到同一结论。
# 函数用途: 直接断言 _record_replaces 的档位规则。
def test_record_precedence_is_order_independent() -> None:
    # 发布窗口：state 只是 starting 占位时，heartbeat 的 running 必须能胜出
    assert _record_replaces("heartbeat", "running", "state", "starting") is True
    # GW-01 残留：state 已越过 starting（failed/http_server_error）时，running heartbeat 不许覆盖
    assert _record_replaces("heartbeat", "running", "state", "failed") is False
    assert _record_replaces("heartbeat", "running", "state", "http_server_error") is False
    assert _record_replaces("heartbeat", "running", "state", "interrupted") is False
    # 本代 state=running 不被旧代/失败的 heartbeat 改写成失败
    assert _record_replaces("heartbeat", "failed", "state", "running") is False
    # 同级（占位 vs 失败心跳）保留生命周期所有者的记录：两个方向存活下来的都是 state
    assert _record_replaces("heartbeat", "failed", "state", "starting") is False
    assert _record_replaces("state", "starting", "heartbeat", "failed") is True
    # 空/未知 state 状态不构成权威结论，running heartbeat 仍可放行
    assert _record_replaces("heartbeat", "running", "state", "") is True


# LLM: spawn 侧（cmd_gateway_start）在"子进程还活着但已发布 failed"时必须快速失败并带上失败分型，
#   既不能返回成功（退出码语义不变仍是 2），也不能退化成超时文案。
# 函数用途: 验证 _wait_for_gateway_start_ready 对已发布 failed 的网关不做就绪判定。
def test_spawn_wait_does_not_report_ready_for_failed_gateway(tmp_path: Path) -> None:
    from agent_py_agent.cli.gateway_process import _wait_for_gateway_start_ready

    paths = _readiness_paths(tmp_path)
    agent = _stub_agent(tmp_path)
    pid = os.getpid()
    started_at = time.time()
    _write_pid_record(paths, pid, anchor_at=started_at)
    from agent_py_agent.cli.gateway_loops import _write_gateway_heartbeat
    from agent_py_agent.cli.gateway_process import _record_gateway_run_failed

    _write_record(paths, pid=pid, status="running", started_at=started_at)
    _write_gateway_heartbeat(paths, agent, status="running", pid=pid)
    _record_gateway_run_failed(paths, agent, pid, RuntimeError("controlled service loop failure"))

    began = time.monotonic()
    ready = _wait_for_gateway_start_ready(paths, _FakeSpawnedProcess(pid), timeout=5.0)
    elapsed = time.monotonic() - began

    assert ready is False
    # 失败分型来自 state=failed，不是"等满预算再超时"
    assert elapsed < 1.0
