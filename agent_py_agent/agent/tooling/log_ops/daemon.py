
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
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...common import heartbeat, schema_version
from . import baseline
from .collector import collect_source
from .splitter import build_splitter
from .store import CollectMetrics, LogOpsStore, SourceSpec, SourceTick, compact_candidates
from .triage import DEFAULT_RULES, TriageInput, TriageRule, compile_profile_rules, triage_line
from ...ml_engine import rule_store as ml_rule_store
from ...ml_engine.detection import MetricBaseline, evaluate_rule

_HEARTBEAT_KEY = "heartbeat_at"
_DAEMON_SCHEMA_VERSION = 1  # daemon.json schema 版本
_CANDIDATES_COMPACT_EVERY = 200       # 每多少拍检查一次候选队列轮转
_CANDIDATES_COMPACT_THRESHOLD = 5000  # 已研判候选(cursor)超过此数就轮转归档
_DEFAULT_POLL_INTERVAL = 2.0
_MIN_POLL_INTERVAL = 0.05
_ML_EVAL_WINDOW = 20000  # ML 检测 pass 每拍评估最近多少条候选(确定性,零 LLM)
_BASELINE_FILE = "ml_metric_baseline.json"  # deviation 规则的 per-group 基线持久化(跨重启续学)


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
    now = time.time()
    if not acc.breaker_for(spec.source_id).allow(now=now):
        return  # 该源连续失败已熔断且冷却未到:本拍跳过,不反复失败烧资源;冷却到自动 half-open 试探
    prev_state = store.read_state(spec.source_id)
    splitter, active_rules = _profile_collect_config(store.read_profile(spec.source_id), rules)
    baseline_model = baseline.load_baseline(store, spec.source_id)
    result = collect_source(spec.kind, spec.locator, prev_state, splitter=splitter)
    new_lines = result.new_lines

    # ① 先全量落存档(不丢的根本保证 —— 即便后面 triage/状态写挂了,原始行也在档里)。
    archived = store.append_archive(spec.source_id, new_lines) if new_lines else 0

    # ② 逐行确定性初筛:正则命中 OR 统计异常(数据驱动)产候选。行号 = 该源存档累计行号(1-based,可回查)。
    base_line_no = _archive_base_line_no(store, spec.source_id, archived)
    ctx = _TriageCtx(spec, active_rules, baseline_model, acc.burst_tracker_for(spec.source_id))
    candidates = _triage_new_lines(ctx, new_lines, base_line_no)
    if candidates:
        store.append_candidates(candidates)
        urgent = [c for c in candidates if c.get("severity") == "high"]
        if urgent:
            store.append_urgent(urgent)  # 高危 → 紧急队列,供按需唤醒秒级拉研判,不等周期
    if new_lines:
        baseline_model.decay()  # 周期淘汰长期(默认 10 万条记录)未再现的值:适应正常漂移、让攻击污染可恢复
        baseline.save_baseline(store, spec.source_id, baseline_model)  # 持久化本拍学到的"正常",跨拍/重启续学

    # ③ 原子写该源断点状态(在存档之后,保证"宁可重不可丢")。
    if new_lines or result.state != prev_state:
        store.write_state(spec.source_id, result.state)

    # ④ 累加计数(供 status 做"已采集 vs 存档"对账)。
    acc.bump_source(
        spec.source_id,
        SourceTick(len(new_lines), archived, len(candidates), result.cursor_repr()),
    )
    # 记录采集成败到该源断路器(连续失败→熔断,下拍起冷却期跳过;成功→复位)。circuit 快照入 metrics 供 status 见。
    acc.note_source_health(spec.source_id, result.error, now)
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


@dataclass
class _TriageCtx:
    """逐行初筛的不变上下文:源、初筛规则、该源统计基线、速率突变追踪器(收成一个对象,降参数数)。"""

    spec: SourceSpec
    rules: list[TriageRule]
    baseline_model: baseline.SourceBaseline
    burst_tracker: Any  # resilience.BurstTracker:同实体滑窗高频检测


def _triage_new_lines(ctx: _TriageCtx, new_lines: list[str], base_line_no: int) -> list[dict[str, Any]]:
    """逐行初筛:正则命中 或 统计异常(新实体/罕见值/速率突变)都产候选。先用当前基线判异常(旧基线),
    再只用"正常"行更新基线——威胁/异常行不学,避免攻击数据污染基线、把攻击者实体洗成"已知"。"""
    candidates: list[dict[str, Any]] = []
    for idx, raw_line in enumerate(new_lines):
        score, reasons = baseline.score_anomaly(ctx.baseline_model, raw_line)
        burst_ip = _burst_entity(ctx.burst_tracker, raw_line)  # 速率突变:同 IP 滑窗内高频(暴力破解/扫描)
        if burst_ip:
            score = max(score, 0.7)  # 突发并入统计异常信号(高分),即便基线判它"已知"也要浮出
            reasons = [*reasons, f"速率突变({burst_ip} 短时高频)"]
        candidate = triage_line(ctx.spec, TriageInput(base_line_no + idx, raw_line, (score, reasons)), rules=ctx.rules)
        if candidate is None:
            ctx.baseline_model.observe_line(raw_line)  # 只学正常行,威胁不污染基线
        else:
            candidates.append(candidate)
    return candidates


def _burst_entity(tracker: Any, raw_line: str) -> str | None:
    """提取首个 IP 喂给速率突变追踪器,返回该 IP 当它在滑动窗口内达突发阈值(否则 None)。"""
    ip = baseline.extract_entities(raw_line).get("ip")
    return ip if (ip and tracker.observe(ip)) else None


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
    """daemon 主循环的运行态(pid/启动指纹/间隔/已跑拍数),收敛成一个对象传给心跳记录函数。"""

    store: LogOpsStore
    interval: float
    pid: int
    start_time: str | None = None  # 本进程启动指纹(serve 启动时取一次),防 PID 复用误判
    cycles: int = 0


def _maybe_compact_candidates(store: LogOpsStore) -> None:
    """周期检查:已研判候选(cursor)积累多了就轮转归档,防 candidates 队列无限增长(对称 archive)。"""
    if store.read_poll_cursor() >= _CANDIDATES_COMPACT_THRESHOLD:
        compact_candidates(store)


def _report_ml_hit_once(store: LogOpsStore, hit: Any, seen: set[str]) -> None:
    """同一(规则,分组,时窗)命中只报一次(去重,防每拍重复报告淹没);区分绝对命中与偏离突增。"""
    signature = f"{hit.rule_id}|{hit.group_key}|{int(hit.window_start // 60)}"
    if signature in seen:
        return
    seen.add(signature)
    if hit.deviation > 0:
        title = f"[ML突增] 规则「{hit.rule_name}」{hit.group_key} 偏离基线 {hit.deviation}×"
        detail = f"当前聚合值 {hit.agg_value},超自己基线 {hit.deviation} 倍"
    else:
        title = f"[ML检测] 规则「{hit.rule_name}」命中 {hit.group_key}"
        detail = f"聚合值 {hit.agg_value} 超阈值 {hit.threshold}"
    store.append_report({
        "level": "P2", "title": title, "detail": detail,
        "evidence": list(hit.evidence_refs), "sources": [], "pushed": False, "ml_detection": True,
    })


def _run_ml_detection_pass(store: LogOpsStore, seen: set[str], baseline: MetricBaseline) -> None:
    """daemon 每拍对最近候选跑 agent 现编的声明式检测规则(绝对阈值 + 偏离基线**统一评估**),新命中写报告。
    deviation 规则用 per-group 基线(EWMA 续学,报一次后适应);关时根本不进这里,不碰采集/不丢流程。"""
    rules = ml_rule_store.read_rules(store.root)
    if not rules:
        return
    total = store.count_candidates()
    candidates = store.read_candidates(offset=max(0, total - _ML_EVAL_WINDOW), limit=_ML_EVAL_WINDOW)
    for rule in rules:
        for hit in evaluate_rule(rule, candidates, baseline):
            _report_ml_hit_once(store, hit, seen)


def _load_metric_baseline(store: LogOpsStore) -> MetricBaseline:
    """读 deviation 规则的 per-group 基线(没有则空,从头学)。"""
    path = store.root / _BASELINE_FILE
    if not path.exists():
        return MetricBaseline()
    try:
        return MetricBaseline.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return MetricBaseline()


def _save_metric_baseline(store: LogOpsStore, baseline: MetricBaseline) -> None:
    """落盘基线(daemon 退出时持久化,跨重启续学)。"""
    (store.root / _BASELINE_FILE).write_text(
        json.dumps(baseline.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


@dataclass
class _MlPassState:
    """daemon ML 旁路 pass 的跨拍状态:命中去重集 + deviation 规则的 per-group 基线。"""

    seen: set[str] = field(default_factory=set)
    baseline: MetricBaseline = field(default_factory=MetricBaseline)


def _safe_ml_pass(store: LogOpsStore, state: _MlPassState) -> None:
    """ML 旁路 pass(声明式检测规则,含偏离基线)包一层吞异常:绝不拖垮采集主循环(daemon 永不停机)。"""
    try:
        _run_ml_detection_pass(store, state.seen, state.baseline)
    except Exception:  # noqa: BLE001 — ML 是旁路增强,任何异常不影响确定性采集
        pass


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
    pid = os.getpid()
    run = _DaemonRunState(store=store, interval=interval, pid=pid, start_time=heartbeat.process_start_time(pid))
    _write_daemon_record(run, "running")
    ml_state = _MlPassState(baseline=_load_metric_baseline(store))

    while not flag.stop:
        error = _run_one_cycle(run, metrics, rules)
        if bool(store.read_config().get("ml_engine_enabled", False)):  # 每拍读:默认关零影响,支持运行中开关
            _safe_ml_pass(store, ml_state)
        run.cycles += 1
        if run.cycles % _CANDIDATES_COMPACT_EVERY == 0:
            _maybe_compact_candidates(store)  # 周期轮转候选队列,防无限增长
        _write_daemon_record(run, "running", last_error=error)
        if max_cycles is not None and run.cycles >= max_cycles:
            break
        _interruptible_sleep(interval, flag)

    _save_metric_baseline(store, ml_state.baseline)  # 退出前持久化基线,跨重启续学
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
    prev = run.store.read_daemon()
    payload: dict[str, Any] = {
        "status": status,
        "pid": run.pid,
        # 启动指纹用本进程自己的(serve 启动时取一次存 run.start_time),绝不继承 daemon.json 里的旧值——
        # 否则重启后新 daemon 顶着死进程的指纹,liveness 拿新进程真实指纹一比对就误判 PID 复用、反复自愈重启。
        "start_time": run.start_time,
        "poll_interval_seconds": run.interval,
        "cycles": run.cycles,
        _HEARTBEAT_KEY: time.time(),
        "started_at": prev.get("started_at", time.time()),
    }
    if last_error:
        payload["last_error"] = last_error
    run.store.write_daemon(schema_version.stamp(payload, _DAEMON_SCHEMA_VERSION))


def main(argv: list[str] | None = None) -> int:
    """python -m agent_py_agent.agent.tooling.log_ops.daemon <root> --monitor-id <id> 入口。

    被 manager 用后台进程方式拉起。root 是 .log_ops 根目录,monitor_id 指定哪个 monitor。
    sources/间隔从该 monitor 的 config.json 读(manager 在拉起前已写好 config)。
    """
    parser = argparse.ArgumentParser(prog="log_ops.daemon")
    parser.add_argument("root", help="`.log_ops` 根目录")
    parser.add_argument("--monitor-id", default="default")
    parser.add_argument("--max-cycles", type=int, default=None, help="跑几拍后退出(测试用,默认无限)")
    parser.add_argument("--watchdog", action="store_true", help="主动看门狗模式:周期检测 daemon 心跳,异常死亡则重拉")
    args = parser.parse_args(argv)

    store = LogOpsStore(Path(args.root), args.monitor_id)
    if args.watchdog:
        from .manager import watchdog_serve  # 函数内 import 避免与 manager 循环依赖
        watchdog_serve(store, max_cycles=args.max_cycles)
        return 0
    config = store.read_config()
    interval = float(config.get("poll_interval_seconds", _DEFAULT_POLL_INTERVAL) or _DEFAULT_POLL_INTERVAL)
    serve(store, poll_interval_seconds=interval, max_cycles=args.max_cycles)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "run_collection_cycle", "serve"]
