
from __future__ import annotations

"""确定性采集 daemon —— 编排 collector→archive→triage→候选队列→状态/计数,长跑零 LLM。

两个入口:
  run_collection_cycle(store, specs, rules)  跑一轮采集(可单测,不起进程):
      对每个源:采集新行 → 先全量 append archive(不丢根本保证)→ 逐行 triage 产候选 →
      候选 append 候选队列 → 原子写该源断点状态 → 累加 metrics。
      顺序关键:archive 在 state 之前落盘。崩溃在 archive 后 state 前 → 重启重采这批(已在
      archive 里,靠指纹去重,候选可能重复但 LLM poll 去重 + 不丢);崩溃在 archive 前 → 状态
      没动,重启重采,不丢。任一崩溃点都"宁可重不可丢"。

  main(argv) / serve(store, ...)  常驻进程主循环:
      读 config 拿 sources + 间隔,循环 run_collection_cycle,每轮刷新 daemon.json 心跳,
      响应 SIGTERM 优雅退出(写最后一次状态再退)。独立进程,父 agent 退出它照跑(撑数天数月)。
"""

import argparse
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .collector import collect_source
from .splitter import build_splitter
from .store import CollectMetrics, LogOpsStore, SourceSpec, SourceTick
from .triage import DEFAULT_RULES, TriageRule, compile_profile_rules, triage_line

_HEARTBEAT_KEY = "heartbeat_at"
_DEFAULT_POLL_INTERVAL = 2.0
_MIN_POLL_INTERVAL = 0.05


def run_collection_cycle(
    store: LogOpsStore,
    specs: list[SourceSpec],
    *,
    rules: list[TriageRule] | None = None,
    metrics: CollectMetrics | None = None,
) -> CollectMetrics:
    """跑一轮采集 + 初筛,就地累加到 metrics 并落盘。返回更新后的 metrics。

    这是 daemon 的"一拍",也是单测的主入口(不需要起进程就能验证不丢/初筛/续接)。
    """
    active_rules = rules if rules is not None else DEFAULT_RULES
    acc = metrics if metrics is not None else CollectMetrics.from_dict(store.read_metrics())
    store.ensure_dirs()
    for spec in specs:
        _collect_one_source(store, spec, active_rules, acc)
    store.write_metrics(acc.to_dict())
    return acc


def _collect_one_source(
    store: LogOpsStore,
    spec: SourceSpec,
    rules: list[TriageRule],
    acc: CollectMetrics,
) -> None:
    """采集单个源一拍:采集→落存档→初筛产候选→写断点状态→累加计数。顺序保证"宁可重不可丢"。

    按该源 profile 执行:profile.splitter 决定切割方式,profile.rules 决定初筛规则(没 profile 走默认)。
    """
    prev_state = store.read_state(spec.source_id)
    splitter, active_rules = _profile_collect_config(store.read_profile(spec.source_id), rules)
    result = collect_source(spec.kind, spec.locator, prev_state, splitter=splitter)
    new_lines = result.new_lines

    # ① 先全量落存档(不丢的根本保证 —— 即便后面 triage/状态写挂了,原始行也在档里)。
    archived = store.append_archive(spec.source_id, new_lines) if new_lines else 0

    # ② 逐行确定性初筛,命中产候选。行号 = 该源存档里的累计行号(1-based,可回查)。
    base_line_no = _archive_base_line_no(store, spec.source_id, archived)
    candidates = _triage_new_lines(spec, new_lines, base_line_no, active_rules)
    if candidates:
        store.append_candidates(candidates)

    # ③ 原子写该源断点状态(在存档之后,保证"宁可重不可丢")。
    if new_lines or result.state != prev_state:
        store.write_state(spec.source_id, result.state)

    # ④ 累加计数(供 status 做"已采集 vs 存档"对账)。
    acc.bump_source(
        spec.source_id,
        SourceTick(len(new_lines), archived, len(candidates), result.cursor_repr()),
    )
    if result.error:
        acc.per_source.setdefault(spec.source_id, {})["last_error"] = result.error


def _archive_base_line_no(store: LogOpsStore, source_id: str, archived: int) -> int:
    """这批新行在存档里的起始行号(1-based)。= 当前存档总行数 - 本批行数 + 1。

    存档已在本轮 append 过,所以 count 包含本批;减去本批得本批之前的行数,+1 即本批第一行号。
    """
    if archived <= 0:
        return store.count_archive_lines(source_id) + 1
    total = store.count_archive_lines(source_id)
    return max(1, total - archived + 1)


def _triage_new_lines(
    spec: SourceSpec,
    new_lines: list[str],
    base_line_no: int,
    rules: list[TriageRule],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for idx, raw_line in enumerate(new_lines):
        candidate = triage_line(spec, line_no=base_line_no + idx, raw_line=raw_line, rules=rules)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _profile_collect_config(
    profile: dict[str, Any], default_rules: list[TriageRule]
) -> tuple[Any, list[TriageRule]]:
    """从该源 profile 取(切割器, 初筛规则)。

    profile 有 splitter → 按它切;没有 → None(collector 用默认按行)。
    profile 有有效 rules → 编译+安全校验后用 profile 的;没有/全被安全校验拦掉 → 用 default_rules。
    """
    if not profile:
        return None, default_rules
    splitter = build_splitter(profile.get("splitter"))
    rule_defs = profile.get("rules")
    if isinstance(rule_defs, list) and rule_defs:
        compiled = compile_profile_rules(rule_defs)
        if compiled:
            return splitter, compiled
    return splitter, default_rules


class _StopFlag:
    """SIGTERM/SIGINT 信号 → 置位,主循环看到就优雅退出。"""

    def __init__(self) -> None:
        self.stop = False

    def request(self, _signum: int, _frame: object) -> None:
        self.stop = True


@dataclass
class _DaemonRunState:
    """daemon 主循环的运行态(pid/间隔/已跑拍数),收敛成一个对象传给心跳记录函数。"""

    store: LogOpsStore
    interval: float
    pid: int
    cycles: int = 0


def serve(
    store: LogOpsStore,
    *,
    poll_interval_seconds: float,
    max_cycles: int | None = None,
    rules: list[TriageRule] | None = None,
) -> int:
    """daemon 常驻主循环。返回完成的循环数(测试可传 max_cycles 限定跑几拍后退出)。

    每拍:读 config 拿最新 sources(支持运行中改源)→ run_collection_cycle → 刷心跳 → sleep。
    收到 SIGTERM/SIGINT 立即跳出,写一次最终 daemon 状态(stopped)。
    """
    flag = _StopFlag()
    _install_signal_handlers(flag)
    interval = max(_MIN_POLL_INTERVAL, float(poll_interval_seconds or _DEFAULT_POLL_INTERVAL))
    metrics = CollectMetrics.from_dict(store.read_metrics())
    run = _DaemonRunState(store=store, interval=interval, pid=os.getpid())
    _write_daemon_record(run, "running")

    while not flag.stop:
        error = _run_one_cycle(run, metrics, rules)
        run.cycles += 1
        _write_daemon_record(run, "running", last_error=error)
        if max_cycles is not None and run.cycles >= max_cycles:
            break
        _interruptible_sleep(interval, flag)

    _write_daemon_record(run, "stopped")
    return run.cycles


def _run_one_cycle(run: _DaemonRunState, metrics: CollectMetrics, rules: list[TriageRule] | None) -> str:
    """跑一拍采集;吞掉单拍异常(daemon 永不停机),返回错误串(无错则空)。"""
    try:
        run_collection_cycle(run.store, run.store.source_specs(), rules=rules, metrics=metrics)
    except Exception as exc:
        return str(exc)
    return ""


def _interruptible_sleep(interval: float, flag: _StopFlag) -> None:
    """可被信号打断的 sleep:小步轮询 stop 标志,SIGTERM 后最迟 ~0.1s 退出,不傻等满 interval。"""
    deadline = time.monotonic() + interval
    while not flag.stop and time.monotonic() < deadline:
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def _install_signal_handlers(flag: _StopFlag) -> None:
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, flag.request)
        except (ValueError, OSError):
            # 非主线程或不支持的平台:忽略(测试里 serve 可能在子线程跑)。
            pass


def _write_daemon_record(run: _DaemonRunState, status: str, *, last_error: str = "") -> None:
    payload: dict[str, Any] = {
        "status": status,
        "pid": run.pid,
        "poll_interval_seconds": run.interval,
        "cycles": run.cycles,
        _HEARTBEAT_KEY: time.time(),
        "started_at": run.store.read_daemon().get("started_at", time.time()),
    }
    if last_error:
        payload["last_error"] = last_error
    run.store.write_daemon(payload)


def main(argv: list[str] | None = None) -> int:
    """python -m agent_py_agent.agent.tooling.log_ops.daemon <root> --monitor-id <id> 入口。

    被 manager 用后台进程方式拉起。root 是 .log_ops 根目录,monitor_id 指定哪个 monitor。
    sources/间隔从该 monitor 的 config.json 读(manager 在拉起前已写好 config)。
    """
    parser = argparse.ArgumentParser(prog="log_ops.daemon")
    parser.add_argument("root", help="`.log_ops` 根目录")
    parser.add_argument("--monitor-id", default="default")
    parser.add_argument("--max-cycles", type=int, default=None, help="跑几拍后退出(测试用,默认无限)")
    args = parser.parse_args(argv)

    store = LogOpsStore(Path(args.root), args.monitor_id)
    config = store.read_config()
    interval = float(config.get("poll_interval_seconds", _DEFAULT_POLL_INTERVAL) or _DEFAULT_POLL_INTERVAL)
    serve(store, poll_interval_seconds=interval, max_cycles=args.max_cycles)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "run_collection_cycle", "serve"]
