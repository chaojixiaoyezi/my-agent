from __future__ import annotations

# LLM: Gateway 的 alive / starting / ready / failed 判据只有一份实现，就在本模块的
#   gateway_readiness() 与 wait_for_gateway_readiness()：readiness 的唯一来源是
#   _gateway_record_facts()（state/heartbeat 任一记录匹配本次代际且 status=running）。
#   CLI gateway start、CLI 等待、TUI preflight 都必须调用它，任何调用方都不允许再写
#   "PID 活就算就绪"。判据只读结构化字段（PID 记录代际锚点、state/heartbeat 的
#   pid/status/started_at），不读文案、不看文件 mtime。等待使用单调时钟有界预算，
#   墙钟跳变不能延长或缩短配置的时间上限。
# 模块用途: 提供 Gateway 存活、启动阶段与就绪的统一判据，以及状态展示；交互界面通过这里
#   判断是否可以开始发送请求，启动失败时靠结构化 state/reason 分型而不是靠文案。

"""Gateway status and liveness rendering helpers."""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..runtime_errors import runtime_error_report
from .daemon_control import (
    get_running_pid_report,
    read_pid_record_report,
)
from .io import gateway_request_counts, read_json_file_report
from .paths import GatewayPaths, gateway_chunk_path
from .process_control import is_pid_alive

if TYPE_CHECKING:
    from ...core import SimpleAgent


@dataclass(frozen=True)
class GatewayRunningReport:
    pid: int | None
    alive: bool
    load_error: dict | None = None


@dataclass(frozen=True)
class GatewayHeartbeatFacts:
    updated_at: float
    age_seconds: float


@dataclass(frozen=True)
class GatewayStatusRenderContext:
    paths: GatewayPaths
    running_report: GatewayRunningReport
    status: str
    heartbeat_at: float
    heartbeat_age_seconds: float


# LLM: 观测状态只有这四种，调用方按 state 分派，不允许再自己组合 PID/state 文件得到第五种说法。
GATEWAY_READINESS_READY = "ready"
GATEWAY_READINESS_STARTING = "starting"
GATEWAY_READINESS_FAILED = "failed"
GATEWAY_READINESS_STOPPED = "stopped"

# LLM: 有界等待的结局状态：ready / failed（真失败）/ timeout（预算耗尽，可能是仍在启动）。
GATEWAY_WAIT_READY = "ready"
GATEWAY_WAIT_FAILED = "failed"
GATEWAY_WAIT_TIMEOUT = "timeout"

# LLM: reason 是可直接展示和断言的稳定错误码，覆盖"等待超时 / 服务真失败 / 仍在启动 / 从未启动"
#   四种结局；调用方不得从文案或异常正文反推分型。
REASON_GATEWAY_READY = "GATEWAY_READY"
REASON_GATEWAY_STARTING = "GATEWAY_STARTING"
REASON_GATEWAY_START_FAILED = "GATEWAY_START_FAILED"
REASON_GATEWAY_PROCESS_EXITED = "GATEWAY_PROCESS_EXITED"
REASON_GATEWAY_START_TIMEOUT = "GATEWAY_START_TIMEOUT"
REASON_GATEWAY_NOT_READY = "GATEWAY_NOT_READY"

_GATEWAY_RUNNING_STATUS = "running"
_GATEWAY_FAILED_STATUSES = ("failed", "interrupted")
# 多个记录同时匹配时按这个顺序选出最能解释当前阶段的状态，避免 state/heartbeat 交叉时误报。
_GATEWAY_RECORD_STATUS_PRIORITY = (_GATEWAY_RUNNING_STATUS,) + _GATEWAY_FAILED_STATUSES
_GATEWAY_READY_POLL_SECONDS = 0.2


# LLM: 一次就绪观测的结构化结果，是 CLI/TUI 唯一可以据此分支的事实；ready 只可能来自本模块判据。
# 类用途: 描述某个 PID 在当前代次下的存活、阶段、就绪与否以及代际记录来源，供启动等待分型。
@dataclass(frozen=True)
class GatewayReadiness:
    state: str
    reason: str
    pid: int | None
    process_alive: bool
    ready: bool
    status: str
    source: str
    started_at: float
    observed_at: float
    load_errors: dict[str, dict] = field(default_factory=dict)


# LLM: 有界等待的结构化结局；last 保留最后一次观测，timeout 时用 last.state 区分"仍在启动"与
#   "从未启动"，失败时用 reason 区分"进程消退"与"服务自身失败"。
# 类用途: 描述一次启动等待的结局、耗时、最后观察到的 PID 和最后一次真实观测。
@dataclass(frozen=True)
class GatewayReadinessWait:
    state: str
    reason: str
    ready: bool
    pid: int | None
    elapsed_seconds: float
    polls: int
    observed_alive: bool
    last: GatewayReadiness


# LLM: 记录层事实是本模块内部唯一允许解读 state/heartbeat 的地方；ready 的唯一定义就是
#   status == "running"，其他函数只能读它，不能另写比较。
@dataclass(frozen=True)
class _GatewayRecordFacts:
    status: str = ""
    source: str = ""
    started_at: float = 0.0
    ready: bool = False
    load_errors: dict[str, dict] = field(default_factory=dict)


_GATEWAY_LOG_SAMPLE_MAX_BYTES = 64 * 1024
_EXCEPTION_SIGNATURE_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_.]*\.)?([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Interrupt)):\s*"
)


# LLM: 这是 CLI、HTTP 和模型只读工具可共同消费的 Gateway 运行事实投影；健康结论只读
#   PID/state/heartbeat/queue，日志仅作观测诊断，绝不能反向改变运行状态或任务终态。
# 函数用途: 从唯一 Gateway 状态目录生成结构化健康、端点、配置与本生命周期日志摘要。
def gateway_runtime_snapshot(
    agent: SimpleAgent,
    paths: GatewayPaths,
    *,
    include_log_diagnostics: bool = False,
) -> dict[str, object]:
    running = gateway_running_report(paths)
    state_report = read_json_file_report(paths.state, context="gateway.runtime_snapshot.state.read")
    heartbeat_report = read_json_file_report(
        paths.heartbeat,
        context="gateway.runtime_snapshot.heartbeat.read",
    )
    state = state_report.payload
    heartbeat = heartbeat_report.payload
    heartbeat_at = _float_value(heartbeat.get("updated_at"))
    heartbeat_facts = GatewayHeartbeatFacts(
        heartbeat_at,
        max(0.0, time.time() - heartbeat_at) if heartbeat_at else 0.0,
    )
    status = _gateway_status(agent, running, state, heartbeat_facts)
    snapshot = _gateway_runtime_base_snapshot(
        agent,
        paths,
        running=running,
        state=state,
        heartbeat=heartbeat,
        heartbeat_facts=heartbeat_facts,
        status=status,
    )
    pid_record = read_pid_record_report(paths.pid)
    identity = snapshot.get("identity")
    if isinstance(identity, dict):
        identity["process_start_time"] = (
            (pid_record.payload or {}).get("start_time") if pid_record.payload else None
        )
    load_errors = {
        name: report
        for name, report in (
            ("pid", running.load_error or pid_record.load_error),
            ("state", state_report.load_error),
            ("heartbeat", heartbeat_report.load_error),
        )
        if report is not None
    }
    if load_errors:
        snapshot["load_errors"] = load_errors
    if include_log_diagnostics:
        snapshot["log_diagnostics"] = gateway_log_diagnostics(
            paths.log,
            start_offset_bytes=_int_value(state.get("log_start_offset_bytes")),
        )
    return snapshot


# LLM: v2 将进程身份与启动默认模型分开；Gateway 服务多个 owner/会话，启动配置绝不能充当当前调用模型。
# 函数用途: 组装网关健康事实；默认模型只标记为部署信息，不新增 I/O，也不猜当前会话使用哪个模型。
def _gateway_runtime_base_snapshot(
    agent: SimpleAgent,
    paths: GatewayPaths,
    *,
    running: GatewayRunningReport,
    state: dict[str, object],
    heartbeat: dict[str, object],
    heartbeat_facts: GatewayHeartbeatFacts,
    status: str,
) -> dict[str, object]:
    config = getattr(agent, "config", None)
    bind_host = str(
        state.get("http_bind_host")
        or getattr(config, "gateway_bind_host", "127.0.0.1")
        or "127.0.0.1"
    ).strip()
    port = _int_value(state.get("http_port") or getattr(config, "gateway_port", 0))
    started_at = _float_value(state.get("started_at"))
    queue_counts = gateway_request_counts(paths, include_archives=False)
    return {
        "schema": "gateway_runtime_snapshot.v2",
        "identity": {
            "pid": running.pid,
            "process_start_time": None,
            "config_path": str(
                state.get("config_path") or getattr(config, "config_path", "") or ""
            ),
        },
        "deployment_defaults": {
            "model_name": str(state.get("model_name") or ""),
            "scope": "gateway_startup_only",
            "is_current_request_model": False,
        },
        "status": status,
        "alive": running.alive,
        "uptime_seconds": round(max(0.0, time.time() - started_at), 3) if started_at else None,
        "heartbeat": {
            "updated_at": heartbeat_facts.updated_at or None,
            "age_seconds": round(heartbeat_facts.age_seconds, 3),
            "stale_after_seconds": max(
                0,
                int(getattr(config, "gateway_stale_seconds", 0) or 0),
            ),
        },
        "http": {
            "bind_host": bind_host,
            "port": port,
            "base_url": f"http://{bind_host}:{port}" if bind_host and port else "",
            "status_path": "/status",
            "metrics_path": "/metrics",
        },
        "queue": {
            **queue_counts,
            "oldest_pending_age_seconds": _float_value(
                (heartbeat.get("queue_ages") or {}).get("oldest_pending_age_seconds")
                if isinstance(heartbeat.get("queue_ages"), dict)
                else 0
            ),
            "oldest_processing_age_seconds": _float_value(
                (heartbeat.get("queue_ages") or {}).get("oldest_processing_age_seconds")
                if isinstance(heartbeat.get("queue_ages"), dict)
                else 0
            ),
            "inflight": (
                dict(heartbeat.get("inflight") or {})
                if isinstance(heartbeat.get("inflight"), dict)
                else {}
            ),
        },
        "runtime": {
            "gateway_workspace": str(paths.root),
            "log_path": str(paths.log),
        },
        "authority": {
            "process": "validated_gateway_pid_record",
            "health": "gateway_state_and_heartbeat",
            "endpoint": "gateway_process_state_with_config_fallback",
            "log_diagnostics": "observational_only",
        },
    }


# LLM: 日志扫描必须从状态文件记录的本生命周期字节偏移开始，且只返回错误签名计数；
#   不能把原始日志、请求正文、密钥或用户消息复制进模型上下文。
# 函数用途: 有界统计当前 Gateway 生命周期新增日志中的异常类型和噪声规模。
def gateway_log_diagnostics(
    path: Path,
    *,
    start_offset_bytes: int = 0,
    sample_max_bytes: int = _GATEWAY_LOG_SAMPLE_MAX_BYTES,
) -> dict[str, object]:
    offset = max(0, int(start_offset_bytes or 0))
    limit = max(1, int(sample_max_bytes or _GATEWAY_LOG_SAMPLE_MAX_BYTES))
    try:
        size = max(0, int(path.stat().st_size))
        rotated = offset > size
        lifecycle_start = 0 if rotated else offset
        sample_start = max(lifecycle_start, size - limit)
        with path.open("rb") as stream:
            stream.seek(sample_start)
            raw = stream.read(limit)
    except OSError as exc:
        return {
            "status": "unavailable",
            "path": str(path),
            "load_error": runtime_error_report(
                exc,
                context="gateway.runtime_snapshot.log.read",
            ),
        }
    if sample_start > lifecycle_start:
        newline = raw.find(b"\n")
        raw = raw[newline + 1 :] if newline >= 0 else b""
    lines = [
        line.strip()
        for line in raw.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    exception_counts: dict[str, int] = {}
    traceback_count = 0
    loop_error_count = 0
    for line in lines:
        if line.startswith("Traceback (most recent call last)"):
            traceback_count += 1
        match = _EXCEPTION_SIGNATURE_RE.match(line)
        if match:
            key = match.group(1)
            exception_counts[key] = exception_counts.get(key, 0) + 1
        if line.startswith("[gateway-loop-error]"):
            loop_error_count += 1
    signature_total = sum(exception_counts.values()) + loop_error_count
    lifecycle_bytes = max(0, size - lifecycle_start)
    if signature_total or traceback_count:
        status = "noise_observed"
    elif lifecycle_bytes == 0:
        status = "no_new_log_bytes"
    else:
        status = "quiet"
    return {
        "status": status,
        "evidence_scope": "current_lifecycle_bounded_file_tail",
        "path": str(path),
        "lifecycle_start_offset_bytes": lifecycle_start,
        "current_size_bytes": size,
        "current_lifecycle_bytes": lifecycle_bytes,
        "sampled_bytes": len(raw),
        "sample_truncated": sample_start > lifecycle_start,
        "rotation_detected": rotated,
        "nonempty_line_count": len(lines),
        "traceback_count": traceback_count,
        "gateway_loop_error_count": loop_error_count,
        "exception_counts": dict(sorted(exception_counts.items())),
        "raw_log_included": False,
    }


# LLM: PID 记录里的结构化时间字段是"本次启动的那一代"的锚点：TUI/CLI 等待方没有 spawn 时刻时，
#   靠它能证明 state/heartbeat 是否上一代遗留。只认数值时间字段或 ISO updated_at，绝不用文件
#   mtime 猜；两者都缺就返回 0.0（无法证明陈旧，保持向后兼容）。
# 函数用途: 读取 PID 记录的代际锚点时间（epoch 秒），供就绪判据排除陈旧记录。
def gateway_generation_anchor(paths: GatewayPaths) -> float:
    record = read_pid_record_report(paths.pid).payload or {}
    for key in ("started_at", "spawned_at"):
        value = _float_value(record.get(key))
        if value > 0:
            return value
    return _iso_timestamp(record.get("updated_at"))


# LLM: ISO 字段可能来自旧版本或本机时钟；解析失败返回 0.0 而不是抛异常，无时区信息按 UTC 解释，
#   保证跨平台、跨时区结果稳定且可测试。
# 函数用途: 把 PID 记录里的 ISO 时间字段转成 epoch 秒，解析不了返回 0.0。
def _iso_timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# LLM: 判定只读结构化时间字段；缺字段的旧格式按"无法证明是旧代"处理（保持向后兼容，不用自然语言或
# 文件 mtime 猜）。容差覆盖父子进程写盘时差，避免把刚发布的这一代误判成陈旧。
# 函数用途: 判断一条 running 记录是否由本次启动之后的那一代进程写出。
def _record_is_current_generation(payload: dict, not_before: float, *, tolerance: float = 2.0) -> bool:
    started_at = payload.get("started_at")
    try:
        started = float(started_at or 0.0)
    except (TypeError, ValueError):
        return True
    if started <= 0.0:
        return True
    return started >= not_before - max(0.0, tolerance)


# LLM: 只读 state/heartbeat 的结构化字段（pid/status/started_at）：任一记录匹配该 PID 且属于本代
#   且 status=running 即 ready，这是 ready 的唯一定义。heartbeat 没有 started_at 时按"无法证明陈旧"
#   接受，与既有代际容差一致；本函数不写文件、不改发布顺序。
# 函数用途: 汇总某个 PID 在当前代次下的 Gateway 状态记录事实，供就绪判据与启动诊断复用。
def _gateway_record_facts(paths: GatewayPaths, pid: int, not_before: float) -> _GatewayRecordFacts:
    facts = _GatewayRecordFacts()
    for source, path, context in (
        ("state", paths.state, "gateway.readiness.state.read"),
        ("heartbeat", paths.heartbeat, "gateway.readiness.heartbeat.read"),
    ):
        report = read_json_file_report(path, context=context)
        if report.load_error:
            facts.load_errors[source] = report.load_error
        payload = report.payload
        if pid <= 0 or _int_value(payload.get("pid")) != pid:
            continue
        if not_before > 0 and not _record_is_current_generation(payload, not_before):
            continue
        status = str(payload.get("status") or "")
        if facts.source and _status_priority(status) >= _status_priority(facts.status):
            continue
        facts = _GatewayRecordFacts(
            status=status,
            source=source,
            started_at=_float_value(payload.get("started_at")),
            ready=status == _GATEWAY_RUNNING_STATUS,
            load_errors=facts.load_errors,
        )
    return facts


# LLM: 记录状态排序只服务展示与分型，不改变 ready 定义；未知状态排最后，仍保留为可观测事实。
# 函数用途: 返回状态在代际记录优先级中的位置，数字越小越能代表当前阶段。
def _status_priority(status: str) -> int:
    try:
        return _GATEWAY_RECORD_STATUS_PRIORITY.index(status)
    except ValueError:
        return len(_GATEWAY_RECORD_STATUS_PRIORITY)


# LLM: 兼容投影：只回答"本代是否已发布 running 记录"，进程存活由调用方（spawn 方 poll()）提供，
#   因此这里不做存活探测。gateway_process 从本模块导入同名函数，禁止再写第二套代际判定。
# 函数用途: 判断某个 PID 的记录层是否已就绪（cmd_gateway_start 的历史入口）。
def _gateway_ready_for_pid(paths: GatewayPaths, pid: int, *, not_before: float = 0.0) -> bool:
    return _gateway_record_facts(paths, int(pid), float(not_before or 0.0)).ready


# LLM: process_alive 是调用方提供的存活事实（spawn 方用 Popen.poll()，比 is_pid_alive 更早看到僵尸
#   子进程）；不传时才回落到 PID 记录 + start_time 校验，绝不把"看不见"当成"已死"。
# 函数用途: 解析本次判据要观察的 PID 与进程存活事实。
def _resolve_readiness_process(
    paths: GatewayPaths,
    pid: int | None,
    process_alive: bool | None,
) -> tuple[int | None, bool]:
    if process_alive is not None:
        return (int(pid) if pid else None), bool(process_alive)
    if pid is not None:
        target = int(pid)
        return target, is_pid_alive(target)
    report = get_running_pid_report(paths.pid)
    return report.pid, bool(report.pid)


# LLM: 代际锚点取"调用方 spawn 时刻"和"PID 记录锚点"的较大值：前者用于 gateway start，后者用于
#   只做附着观察的 TUI/CLI；任一存在即可证明记录是否本代。
# 函数用途: 计算本次判据使用的代际下界时间。
def _generation_anchor(paths: GatewayPaths, not_before: float) -> float:
    return max(float(not_before or 0.0), gateway_generation_anchor(paths))


# LLM: 只做状态归类，不改写记录事实；failed 记录在进程还活着时也按 failed 暴露，避免把服务自身
#   失败当成"仍在启动"骗用户继续等。
# 函数用途: 把进程存活事实与记录事实归成四态之一及对应结构化 reason。
def _readiness_state(alive: bool, facts: _GatewayRecordFacts) -> tuple[str, str]:
    if alive and facts.ready:
        return GATEWAY_READINESS_READY, REASON_GATEWAY_READY
    if facts.status in _GATEWAY_FAILED_STATUSES:
        return GATEWAY_READINESS_FAILED, REASON_GATEWAY_START_FAILED
    if alive:
        return GATEWAY_READINESS_STARTING, REASON_GATEWAY_STARTING
    return GATEWAY_READINESS_STOPPED, REASON_GATEWAY_NOT_READY


# LLM: 这是 alive / starting / ready / failed 的唯一权威判据。ready 的唯一定义在
#   _gateway_record_facts（本代记录 status=running）；本函数只负责把它与进程存活事实拼成结构化
#   四态。调用方不得再自行比较 PID 存活或解读 state 文件。
# 函数用途: 观测一次 Gateway 状态：进程是否存活、是否仍在启动、是否已就绪、是否已明确失败。
def gateway_readiness(
    paths: GatewayPaths,
    *,
    pid: int | None = None,
    not_before: float = 0.0,
    process_alive: bool | None = None,
) -> GatewayReadiness:
    resolved_pid, alive = _resolve_readiness_process(paths, pid, process_alive)
    facts = _gateway_record_facts(paths, resolved_pid or 0, _generation_anchor(paths, not_before))
    state, reason = _readiness_state(alive, facts)
    return GatewayReadiness(
        state=state,
        reason=reason,
        pid=resolved_pid,
        process_alive=alive,
        ready=state == GATEWAY_READINESS_READY,
        status=facts.status,
        source=facts.source,
        started_at=facts.started_at,
        observed_at=time.time(),
        load_errors=dict(facts.load_errors),
    )


# LLM: spawn 方传入 process 时，子进程存活事实来自 poll()；没有 process 时只有"曾经观察到存活、
#   之后又消失"才算进程消退，避免把"还没起来"误判成"起来了又死"。
# 函数用途: 判断本次观测是否已经可以定性为进程消失的真失败。
def _process_gone(process: object | None, last: GatewayReadiness, observed_alive: bool) -> bool:
    if process is not None:
        return not last.process_alive
    return observed_alive and not last.process_alive


# LLM: 存活探测只在 spawn 方提供 process 时存在；形状不合法（测试替身/旧调用）时返回 None，
#   让判据回落到 PID 记录，而不是猜一个存活结论。
# 函数用途: 读取调用方子进程对象当前的存活事实，无法判断时返回 None。
def _process_alive_probe(process: object | None) -> bool | None:
    poll = getattr(process, "poll", None)
    if process is None or not callable(poll):
        return None
    return poll() is None


# LLM: 有界等待必须复用同一个判据；结局用结构化 state+reason 分型（timeout 可能是"仍在启动"也可能是
#   "从未启动"，failed 区分"进程消退"与"服务自身失败"），调用方不得靠文案判断。预算用单调时钟，
#   超时后不再做无界重试。
# 函数用途: 在给定预算内轮询统一就绪判据，返回带分型的等待结局与最后一次观测。
def wait_for_gateway_readiness(
    paths: GatewayPaths,
    timeout: float = 3.0,
    *,
    process: object | None = None,
    not_before: float = 0.0,
    on_observation: Callable[[GatewayReadiness], None] | None = None,
    poll_interval: float = _GATEWAY_READY_POLL_SECONDS,
) -> GatewayReadinessWait:
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    interval = max(0.01, float(poll_interval or _GATEWAY_READY_POLL_SECONDS))
    watch_pid = _int_value(getattr(process, "pid", 0)) if process is not None else 0
    observed_pid: int | None = None
    observed_alive = False
    polls = 0

    # LLM: 每次观测都必须重新读取 spawn 方的存活事实（poll 会随子进程状态变化），否则首次观测会
    #   回落到本进程的 PID 记录并把已退出的子进程误判成就绪。
    # 函数用途: 用同一判据和当前存活事实观测一次 Gateway 状态。
    def _observe() -> GatewayReadiness:
        return gateway_readiness(
            paths,
            pid=watch_pid or None,
            not_before=not_before,
            process_alive=_process_alive_probe(process),
        )

    last = _observe()

    # LLM: 结局只在循环内构造，闭包读取当前观测值，避免把中间态复制成第二份事实源。
    # 函数用途: 汇总当前观测为结构化等待结局。
    def _outcome(state: str, reason: str) -> GatewayReadinessWait:
        return GatewayReadinessWait(
            state=state,
            reason=reason,
            ready=state == GATEWAY_WAIT_READY,
            pid=observed_pid,
            elapsed_seconds=round(time.monotonic() - started, 3),
            polls=polls,
            observed_alive=observed_alive,
            last=last,
        )

    while True:
        polls += 1
        observed_alive = observed_alive or last.process_alive
        observed_pid = last.pid or observed_pid
        if on_observation is not None:
            on_observation(last)
        if last.ready:
            return _outcome(GATEWAY_WAIT_READY, last.reason)
        if _process_gone(process, last, observed_alive):
            return _outcome(GATEWAY_WAIT_FAILED, REASON_GATEWAY_PROCESS_EXITED)
        if last.state == GATEWAY_READINESS_FAILED:
            return _outcome(GATEWAY_WAIT_FAILED, last.reason)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        last = _observe()
    reason = (
        REASON_GATEWAY_START_TIMEOUT
        if last.state == GATEWAY_READINESS_STARTING
        else REASON_GATEWAY_NOT_READY
    )
    return _outcome(GATEWAY_WAIT_TIMEOUT, reason)


# LLM: 兼容投影：(pid, alive)。alive 现在来自统一判据里的进程存活事实（含 PID 记录 start_time
#   校验），本函数不再自己算 bool(pid)；需要"已就绪"的调用方必须用 gateway_readiness。
# 函数用途: 返回当前 Gateway 进程 PID 与存活状态，供 status/supervisor 等诊断展示复用。
def gateway_running(paths: GatewayPaths) -> tuple[int, bool]:
    readiness = gateway_readiness(paths)
    return readiness.pid, readiness.process_alive


# LLM: 这是 CLI 普通聊天、gateway ask、TUI preflight 共用的有界等待；布尔值语义已从"PID 活"改为
#   "已就绪"，因为旧语义会在 HTTP 未 bind 时放行请求。就绪判定仍只有一处实现。
# 函数用途: 在给定秒数内等待 Gateway 真正就绪，返回最后观察到的 PID 和是否已就绪。
def wait_for_gateway_running(paths: GatewayPaths, timeout: float = 3.0) -> tuple[int, bool]:
    outcome = wait_for_gateway_readiness(paths, timeout)
    if outcome.ready:
        return outcome.pid, True
    return outcome.pid or 0, False


# LLM: PID 记录的存活投影给展示层用（含 load_error），不参与 ready 判定；ready 只走 gateway_readiness。
# 函数用途: 返回 PID 记录解析出的运行报告，供状态渲染显示诊断信息。
def gateway_running_report(paths: GatewayPaths) -> GatewayRunningReport:
    pid_report = get_running_pid_report(paths.pid)
    return GatewayRunningReport(pid_report.pid, bool(pid_report.pid), pid_report.load_error)


def render_gateway_status(agent: SimpleAgent, paths: GatewayPaths) -> list[str]:
    running_report = gateway_running_report(paths)
    state_report = read_json_file_report(paths.state, context="gateway.status.state.read")
    heartbeat_report = read_json_file_report(paths.heartbeat, context="gateway.status.heartbeat.read")
    heartbeat = heartbeat_report.payload
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    heartbeat_facts = GatewayHeartbeatFacts(heartbeat_at, time.time() - heartbeat_at if heartbeat_at else 0)
    status = _gateway_status(agent, running_report, state_report.payload, heartbeat_facts)
    lines = _base_status_lines(
        GatewayStatusRenderContext(
            paths,
            running_report,
            status,
            heartbeat_facts.updated_at,
            heartbeat_facts.age_seconds,
        )
    )
    _append_status_load_errors(lines, running_report, state_report.load_error, heartbeat_report.load_error)
    return lines


def _gateway_status(
    agent: SimpleAgent,
    running_report: GatewayRunningReport,
    state: dict,
    heartbeat: GatewayHeartbeatFacts,
) -> str:
    status = "running" if running_report.alive else state.get("status", "stopped")
    if running_report.alive and heartbeat.updated_at and heartbeat.age_seconds > agent.config.gateway_stale_seconds:
        return "stale"
    return status


def _base_status_lines(context: GatewayStatusRenderContext) -> list[str]:
    now = time.time()
    lines = [
        f"gateway status={context.status} "
        f"pid={context.running_report.pid if context.running_report.pid else '-'} "
        f"alive={context.running_report.alive}",
        "gateway requests="
        + json.dumps(gateway_request_counts(context.paths), ensure_ascii=False, sort_keys=True),
    ]
    if context.heartbeat_at:
        lines.append(f"gateway heartbeat_age_seconds={context.heartbeat_age_seconds:.1f}")
    _append_processing_request_lines(lines, context.paths, now)
    return lines


def _append_processing_request_lines(lines: list[str], paths: GatewayPaths, now: float) -> None:
    active_requests, load_errors, omitted_count = _processing_request_facts(paths, now)
    if active_requests:
        lines.append("gateway active_requests=" + _json_list(active_requests))
    if omitted_count:
        lines.append(f"gateway active_requests_omitted={omitted_count}")
    for load_error in load_errors:
        lines.append("gateway processing_load_error=" + _json(load_error))


def _processing_request_facts(paths: GatewayPaths, now: float) -> tuple[list[dict], list[dict], int]:
    active_requests: list[dict] = []
    load_errors: list[dict] = []
    request_paths = sorted(paths.processing.glob("*.json"))
    for request_path in request_paths[:5]:
        report = read_json_file_report(request_path, context="gateway.status.processing.read")
        if report.load_error:
            load_errors.append(report.load_error)
            continue
        active_requests.append(_processing_request_row(paths, request_path.stem, report.payload, now))
    return active_requests, load_errors, max(0, len(request_paths) - 5)


def _processing_request_row(
    paths: GatewayPaths,
    request_id: str,
    payload: dict,
    now: float,
) -> dict:
    resolved_id = str(payload.get("id") or request_id)
    row = {
        "id": resolved_id,
        "status": str(payload.get("status") or ""),
        "lease_owner": str(payload.get("lease_owner") or ""),
        "attempts": _int_value(payload.get("attempts")),
    }
    _add_age(row, "lease_age_seconds", payload.get("lease_started_at"), now)
    _add_age(row, "lease_heartbeat_age_seconds", payload.get("lease_heartbeat_at"), now)
    _add_age(row, "updated_age_seconds", payload.get("updated_at"), now)
    chunk_path = gateway_chunk_path(paths, resolved_id)
    if chunk_path.exists():
        row["chunk_stream_path"] = str(chunk_path)
    return row


def _add_age(row: dict, key: str, timestamp: object, now: float) -> None:
    timestamp_value = _float_value(timestamp)
    if timestamp_value:
        row[key] = round(max(0.0, now - timestamp_value), 1)


def _float_value(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _append_status_load_errors(
    lines: list[str],
    running_report: GatewayRunningReport,
    state_load_error: dict | None,
    heartbeat_load_error: dict | None,
) -> None:
    if state_load_error:
        lines.append("gateway state_load_error=" + _json(state_load_error))
    if heartbeat_load_error:
        lines.append("gateway heartbeat_load_error=" + _json(heartbeat_load_error))
    if running_report.load_error:
        lines.append("gateway pid_load_error=" + _json(running_report.load_error))


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_list(payload: list[dict]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
