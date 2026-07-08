"""后台连续摄取(harvester):把「脚本级吞吐」和「LLM 级判断」在时间上解耦。

真机实锤(§8-1 加难回归):合计 900 条/秒 + 源端滚动缓冲 ~6 分钟淘汰,而 pull 只在
工具调用块内拉流——模型研判/写报告/compact 的几分钟里无人拉,缓冲淘汰=永久丢,
命中全集中在早期序号。本模块给每路 watch 起一个进程内 daemon 线程,按固定节拍
持续「drain→结构化引擎→候选批落盘 spool」,pull 工具改为消费 spool——模型思考
多久都不丢流,候选积压如实入账。

铁律(与 engine 同):本层零自然语言/关键词定性,只做结构化降维与搬运;
候选真假永远留给模型判。

并发契约:
- 引擎/游标/账目只被 harvester 线程(或无 harvester 时的 inline pull)在 state.lock
  下动;spool 是 append-only ndjson,读者无锁。
- 跨进程单收割者:''<watch_id>.harvester.json'' 租约(心跳新鲜=有人在收),别的进程
  只读 spool 不再起线程;租约过期即接管(从持久化游标续,不重不漏)。
- spool 世代轮转:积压清零且文件超限时换代重写,读者按 generation 对齐偏移,
  支撑数天数月长守不涨盘。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report
from .puller import DrainBudget, DrainResult
from .watch_payloads import build_audit_record, candidate_rows, frequent_hit_rows, group_rows
from .watch_state import WatchState, audit_append, persist_state, state_dir

_LOGGER = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 15
# 窗口走完后的收尾余量:让最后一批事件被拉完、盯守子代理来得及消费。
_WINDOW_GRACE_SECONDS = 120.0
# 源连续失败时的最大退避(不放弃:源恢复即续,缺口由游标+账目如实体现)。
_MAX_ERROR_BACKOFF_SECONDS = 10.0
# 跨进程租约新鲜窗:超过即视为收割者已死,可接管。
_LEASE_FRESH_SECONDS = 15.0
# spool 轮转阈值:积压清零且文件超过此大小时换代,防数月长守涨盘。
_SPOOL_ROTATE_BYTES = 32 * 1024 * 1024


@dataclass
class HarvesterHandle:
    watch_id: str
    stop_event: threading.Event
    thread: threading.Thread
    started_at: float = field(default_factory=time.time)


class _HarvesterRegistry:
    """进程内收割线程注册表(每 watch 至多一个;死线程自动清位)。"""

    def __init__(self) -> None:
        self._handles: dict[str, HarvesterHandle] = {}
        self._lock = threading.Lock()

    def get_live(self, watch_id: str) -> HarvesterHandle | None:
        with self._lock:
            handle = self._handles.get(watch_id)
            if handle is not None and handle.thread.is_alive():
                return handle
            self._handles.pop(watch_id, None)
            return None

    def put(self, handle: HarvesterHandle) -> None:
        with self._lock:
            self._handles[handle.watch_id] = handle

    def stop(self, watch_id: str) -> None:
        with self._lock:
            handle = self._handles.pop(watch_id, None)
        if handle is not None:
            handle.stop_event.set()


harvesters = _HarvesterRegistry()


def spool_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.spool.ndjson"


def _lease_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.harvester.json"


def _read_sidecar_path(state: WatchState, shard_index: int = 0, shard_count: int = 1) -> Path:
    """读游标 sidecar 路径。shard_count<=1(默认)= 旧单消费者路径(R5 逐字节等价);
    shard_count>1 = 每个判读分片一份独立游标({watch_id}.read.s{i}of{K}.json),互不干扰。"""
    if shard_count <= 1:
        return state_dir(state.owner_home) / f"{state.watch_id}.read.json"
    return state_dir(state.owner_home) / f"{state.watch_id}.read.s{shard_index}of{shard_count}.json"


def _shard_sidecar_glob(owner_home: Path, watch_id: str) -> list[Path]:
    try:
        return sorted(state_dir(owner_home).glob(f"{watch_id}.read.s*of*.json"))
    except OSError:
        return []


def consumed_and_acked_on_disk(owner_home: Path, watch_id: str) -> tuple[int, int]:
    """一路 watch 的【全分片合计】已交付 / 已确认判完候选数(纯盘上结构信号)。
    有分片游标(sharded 消费)→ 合计各分片;否则读单消费者基座游标。背压/过载/唤醒
    兜底的未判积压都按这把全局尺算(K 个判读工各推各的游标,积压是全局账)。"""
    shard_paths = _shard_sidecar_glob(owner_home, watch_id)
    if shard_paths:
        consumed = acked = 0
        for path in shard_paths:
            report = read_json_object_report(path, context="watch_spool.shard_cursor")
            if report.load_error is not None:
                continue
            consumed += int(report.payload.get("candidates_consumed") or 0)
            acked += acked_candidates(report.payload)
        return consumed, acked
    base = state_dir(owner_home) / f"{watch_id}.read.json"
    report = read_json_object_report(base, context="watch_spool.base_cursor")
    if report.load_error is not None:
        return 0, 0
    return int(report.payload.get("candidates_consumed") or 0), acked_candidates(report.payload)


def ensure_harvester(state: WatchState, fetch_json: Callable) -> dict[str, Any] | None:
    """确保这路 watch 有收割者:本进程有活线程→复用;别处租约新鲜→remote;否则起线程。

    返回 {"mode": "local"|"remote"} ;线程起不来且无 remote 时返回 None(调用方回落
    inline drain,行为与无 harvester 时完全一致)。
    """
    if harvesters.get_live(state.watch_id) is not None:
        return {"mode": "local"}
    lease = _read_lease(state)
    if lease is not None and not _lease_owned_by_me(lease):
        return {"mode": "remote", "lease": lease}
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_harvest_loop,
        name=f"watch-harvester-{state.watch_id}",
        args=(state, fetch_json, stop_event),
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        _LOGGER.warning("harvester thread start failed (watch=%s)", state.watch_id, exc_info=True)
        return None
    harvesters.put(HarvesterHandle(state.watch_id, stop_event, thread))
    return {"mode": "local"}


def stop_harvester(watch_id: str) -> None:
    harvesters.stop(watch_id)


def _harvest_loop(state: WatchState, fetch_json: Callable, stop_event: threading.Event) -> None:
    backoff = 0.0
    while not stop_event.is_set():
        outcome = _locked_harvest_step(state, fetch_json)
        if outcome == "stop":
            break
        backoff = min(_MAX_ERROR_BACKOFF_SECONDS, backoff + 1.0) if outcome == "error" else 0.0
        stop_event.wait(state.tuning.poll_interval_seconds + backoff)
    _clear_lease(state)


def _locked_harvest_step(state: WatchState, fetch_json: Callable) -> str:
    """跑一拍并续租约;返回 stop/ok/error。任何异常都吞成 error,线程绝不裸死。"""
    try:
        outcome = _harvest_once(state, fetch_json)
        if outcome != "stop":
            _write_lease(state)
        return outcome
    except Exception:
        _LOGGER.warning("harvest cycle failed (watch=%s)", state.watch_id, exc_info=True)
        return "error"


def _harvest_once(state: WatchState, fetch_json: Callable) -> str:
    """持 state.lock 跑一拍:先判停,再背压闸,再收割。"""
    with state.lock:
        if _should_stop(state):
            return "stop"
        if _backpressured(state):
            # 抬取快于判读:本拍不 drain(游标不动),只回 ok 让循环续租约心跳、等消费者
            # 推进读游标积压回落。绝不静默丢——被钳住的事件仍在源端,消费追上即续抬。
            state.totals["backpressure_skips"] = int(state.totals.get("backpressure_skips", 0) or 0) + 1
            return "ok"
        return "ok" if _harvest_cycle(state, fetch_json) else "error"


def _active_shard_count(state: WatchState) -> int:
    """当前在消费这路 spool 的判读工数(=分片 sidecar 数,单消费者时为 1)。抬取缓冲与背压
    上限随并发判读工数放大:K 个判读工能并行判 K×,缓冲太浅会把他们饿着等抬取。"""
    return max(1, len(_shard_sidecar_glob(state.owner_home, state.watch_id)))


def _effective_ceiling(state: WatchState) -> int:
    """随并发判读工数放大的抬取背压上限:单工=一份前瞻缓冲;K 工=K 份(不把并行工饿着)。"""
    base = backpressure_ceiling(state.tuning)
    return base * _active_shard_count(state) if base > 0 else 0


def _backpressured(state: WatchState) -> bool:
    """未读积压是否已到背压上限(纯计数)。只对内容型全量直通(易洪泛)生效——结构化源
    候选稀疏不洪泛,不背压;上限 0=关闭背压,行为回到旧的无界堆积。上限随判读工数放大。"""
    if not is_content_mode(state):
        return False
    ceiling = _effective_ceiling(state)
    return ceiling > 0 and spool_unread(state) >= ceiling


def _should_stop(state: WatchState) -> bool:
    if state.closed:
        return True
    window = int(state.watch_window_seconds or 0)
    elapsed = time.time() - float(state.opened_at or 0.0)
    if window > 0:
        return elapsed > window + _WINDOW_GRACE_SECONDS
    # 无窗长守:长时间没人 pull 消费(盯守方消失)即自停,防孤儿线程白烧;
    # 下一次 pull 会立即重新拉起并从游标续。消费信号认本进程 last_pull_at 与
    # 跨进程读游标 sidecar 的较新者(消费者可能在别的进程)。
    idle_cap = float(state.tuning.harvester_idle_stop_seconds)
    if idle_cap <= 0:
        return False
    cursor = read_spool_cursor(state)
    last_consume = max(
        float(state.last_pull_at or 0.0),
        float(state.opened_at or 0.0),
        float(cursor.get("updated_at") or 0.0),
    )
    return (time.time() - last_consume) > idle_cap


def _harvest_cycle(state: WatchState, fetch_json: Callable) -> bool:
    """一拍:drain→分片喂引擎→候选落 spool→账目/快照。返回源是否健康(False=本拍拉流失败)。

    分片喂:冷启动/断点追赶一次 drain 可达上万条,整批一次 process 会让稀有候选挤爆
    "每批候选上限"落 overflow(模型看不见);按 harvest_chunk_events 切片,候选位随
    积压量线性扩。稳态每拍只有几百条=单片,行为不变。
    """
    from .sources import drain_watch_source
    from .watch_feedback import consume_feedback_inbox

    # 反馈收件箱先消费(B3):模型上一批确认的真目标特征即刻入库,本拍就能抬同类。
    consume_feedback_inbox(state, time.time())
    content_mode = is_content_mode(state)
    budget = DrainBudget(
        max_events=_cycle_drain_events(state, content_mode),
        page_limit=state.tuning.page_limit,
        deadline=time.time() + _HTTP_TIMEOUT_SECONDS,
    )
    drain = drain_watch_source(state, fetch_json, budget)
    state.totals["pulls"] += 1
    if drain.error:
        state.totals["http_errors"] += 1
        state.last_error = drain.error
        persist_state(state)
        return False
    state.last_error = ""
    state.cursor = drain.cursor
    state.line_cursor = int(getattr(drain, "aux_cursor", 0) or 0)
    if float(getattr(drain, "fetched_at", 0.0) or 0.0) > 0:
        state.last_poll_at = float(drain.fetched_at)
    state.last_reached_end = drain.reached_end
    state.totals["gap_events"] += drain.gap_events
    # content_mode(冷启动/passthrough 全量直通)记录尺寸钳到一批可精读量(反 rubber-stamp);
    # 结构化源沿用 harvest_chunk_events(稀有候选挤出保护)。
    chunk_size = content_batch_size(state.tuning) if content_mode else int(state.tuning.harvest_chunk_events or 0)
    chunks = _event_chunks(drain.events, chunk_size)
    headroom = judge_headroom(state)
    # 冷启动:本源还没 configure 出 spec 时,判据没学出来,存量不能靠结构规则筛
    # (根因2)——整批 full_read 无条件生效(宁滥勿漏)。configure 后转 spec 驱动
    # (passthrough spec 自带无条件直通,普通 spec 走降维分诊)。
    cold_start = state.source_spec is None
    for index, chunk in enumerate(chunks):
        chunk_view = _chunk_drain_view(drain, chunk, first=(index == 0))
        digest = state.engine.process(chunk, time.time(), judge_headroom=headroom, cold_start=cold_start)
        # 本片实抬的候选(真车道+抽检)即时扣减余量:同拍后续片共享同一份判读余量。
        headroom = max(0, headroom - len(digest.candidates))
        if digest.candidates:
            _spool_append(state, chunk_view, digest)
        audit_append(state, build_audit_record(chunk_view, digest))
    persist_state(state)
    return True


def judge_headroom(state: WatchState) -> int:
    """判读吞吐反压余量(真机实锤:audit 抽检 4000+/用户淹没主代理判力,逐条报出
    156→18):余量 = 每 pull 判读口粮 - spool 未读积压。
    消费者(主代理逐条重判后继续 pull)推进读游标 → 积压回落 → 余量自动回升;
    判得慢积压高 → 余量归零 → 抽检自动停抬。纯结构计数,自适应任意模型判读速度,
    不需要估算速率、没有新参数。真信号车道不受此限(见 engine.process)。
    口粮尺与消费口粮同源(judge_quota):直通开着时口粮=直通批量级,否则一批直通
    落 spool 就把余量吃穿、直通永久自锁在关闭态。
    积压按【全分片合计】算:sharded 消费下 K 个判读工各推各的读游标,余量看整路的已消费。"""
    written = int(state.totals.get("spool_candidates", 0))
    consumed, _acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    backlog = max(0, written - consumed)
    return max(0, judge_quota(state.tuning) - backlog)


def judge_quota(tuning) -> int:
    """每轮 pull 的判读口粮(消费侧取数上限与反压余量共用同一把尺):
    max(每批候选上限, 正常量直通上限)。直通关闭(=0)时与旧口径一致。"""
    return max(int(tuning.max_candidates_per_pull), int(tuning.full_read_per_pull or 0))


def spool_unread(state: WatchState) -> int:
    """spool 里已写入而消费者【尚未取走】的候选数(纯计数:引擎累计写入 − 读游标已交付)。
    背压看这把尺:未读堆着=消费者没跟上,抬取该等一等。sharded 消费下按全分片合计已交付。"""
    written = int(state.totals.get("spool_candidates", 0) or 0)
    if written <= 0:
        return 0
    consumed, _acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    return max(0, written - consumed)


def recommended_judge_workers(state: WatchState) -> int:
    """按未判积压结构信号动态推荐的判读工数(横向扩:别一个判读工串行扛,按积压加工/并行判)。
    = ceil(未判积压 / 每工一轮判读口粮),钳到 [1, max_judge_workers]。积压越深、推荐工越多;
    积压清零回落到 1。纯计数,不写死源数/工数——由具体积压决定该派几个判读工。"""
    max_workers = max(1, int(getattr(state.tuning, "max_judge_workers", 1) or 1))
    if max_workers <= 1:
        return 1
    written = int(state.totals.get("spool_candidates", 0) or 0)
    _consumed, acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    unjudged = max(0, written - acked)
    quota = max(1, judge_quota(state.tuning))
    workers = (unjudged + quota - 1) // quota
    return max(1, min(max_workers, workers))


def backpressure_ceiling(tuning) -> int:
    """抬取背压上限:未读积压达到 factor×judge_quota 即本拍停抬(0=关闭背压)。
    下限至少一个 quota——ceiling 比一批口粮还小会把正常一批直通也误判成过载。"""
    factor = int(getattr(tuning, "spool_backpressure_factor", 0) or 0)
    if factor <= 0:
        return 0
    return max(judge_quota(tuning), factor * judge_quota(tuning))


def overload_threshold(tuning) -> int:
    """过载线:未读积压 ≥ 一个 judge_quota(明显落后一整批)即视为判读跟不上抬取——
    触发如实标注(overload 块)。纯计数,不决定候选真假。"""
    return max(1, judge_quota(tuning))


def content_batch_size(tuning) -> int:
    """content_mode(passthrough/冷启动全量直通)下每条 spool 记录 = 模型每 pull 批量的
    候选上限:一批正常量直通(full_read_per_pull),回退每批候选上限。真机实锤:passthrough
    洪泛时一次 drain 500 条正常流全落进【一条】spool 记录,而记录整条读取——模型每 pull 被
    怼 500 条正常流当命中整车 rubber-stamp(60 条同微秒批量乱报)。把 content_mode 的记录
    钳到"一批可精读"的量,过载时模型至多面对一批而非一片。结构化源(已配非 passthrough
    判据)不走此路:记录本就稀疏,沿用 harvest_chunk_events 的稀有挤出保护。"""
    return max(1, int(tuning.full_read_per_pull or tuning.max_candidates_per_pull))


def is_content_mode(state: WatchState) -> bool:
    """本源当前是否内容型全量直通(冷启动未学 spec / spec.passthrough)且直通确实开着:
    此时候选≈事件,抬取无稀有筛、易洪泛,记录尺寸与抬取速率都要按背压钳住。
    直通关闭(full_read_per_pull=0)时即便冷启动也走结构化降维分诊(候选稀疏不洪泛),
    不套用背压(否则 judge_quota 掉到 max_candidates 级、ceiling 过小误钳正常盯守)。
    已配非 passthrough 判据的结构化源同样不算。"""
    if int(state.tuning.full_read_per_pull or 0) <= 0:
        return False
    spec = state.engine.spec
    return spec is None or bool(getattr(spec, "passthrough", False))


def _cycle_drain_events(state: WatchState, content_mode: bool) -> int:
    """本拍抓取事件上限:结构化源/背压关时照旧(大预算追赶);content_mode + 背压开时
    钳到"距未读上限还差多少(room)",一拍不把整条存量倒进 spool(真机 since=0 一次
    5000 条埋掉真事)。至少抓一批口粮,room 再小也有进度。"""
    base = int(state.tuning.max_events_per_pull)
    ceiling = _effective_ceiling(state)
    if ceiling <= 0 or not content_mode:
        return base
    room = max(0, ceiling - spool_unread(state))
    return max(content_batch_size(state.tuning), min(base, room))


def _event_chunks(events: list, chunk_size: int) -> list[list]:
    if not events:
        return []
    if chunk_size <= 0 or len(events) <= chunk_size:
        return [events]
    return [events[i : i + chunk_size] for i in range(0, len(events), chunk_size)]


def _chunk_drain_view(drain: Any, chunk: list, *, first: bool) -> DrainResult:
    """片级账目视图:seen/cursor_to 记本片,gap 只记在首片(缺口发生在片切分之前)。"""
    view = DrainResult(events=chunk)
    view.cursor = (chunk[-1][0] + 1) if chunk else drain.cursor
    view.pages = drain.pages if first else 0
    view.reached_end = drain.reached_end and view.cursor >= drain.cursor
    view.gap_events = drain.gap_events if first else 0
    return view


def _spool_append(state: WatchState, drain: Any, digest: Any) -> None:
    # 先写盘、成功才冒泡计数:计数即"记录已可读"(消费方以计数判积压,写失败不留幽灵积压)。
    next_seq = state.spool_seq + 1
    record = {
        "spool_seq": next_seq,
        "generation": state.spool_generation,
        "t": round(time.time(), 3),
        "candidates": candidate_rows(digest),
        "suppressed_groups": group_rows(digest),
        "suppressed_groups_total": digest.groups_total,
        "suppressed_events": digest.suppressed_total,
        "overflow_count": len(digest.overflow),
        "cursor_to": drain.cursor,
    }
    # 高频命中类调查告警随批落 spool(消费侧合并渲染,与 inline pull 同契约);
    # 内容规则命中数一并落盘(消费侧汇总成本批减负账,零静默)。
    if digest.frequent_hits:
        record["frequent_hits"] = frequent_hit_rows(digest)
    if digest.normal_rule_hits:
        record["normal_rule_hits"] = digest.normal_rule_hits
    path = spool_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    state.spool_seq = next_seq
    state.totals["spool_candidates"] = state.totals.get("spool_candidates", 0) + len(digest.candidates)
    if read_spool_cursor(state).get("read_seq", 0) >= next_seq - 1:
        _maybe_rotate_spool(state, path)


def _maybe_rotate_spool(state: WatchState, path: Path) -> None:
    """积压已清(读者追平上一条)且文件超限 → 换代重写,读者按 generation 重置偏移。
    有未确认的在途批时不轮转:轮转只留最后一条记录,会吃掉接管重投的依据
    (在途在消费者空轮询 ack 后清空,轮转窗口照常出现,长守不涨盘)。"""
    try:
        if path.stat().st_size < _SPOOL_ROTATE_BYTES:
            return
    except OSError:
        return
    cursor = read_spool_cursor(state)
    if cursor.get("read_seq", 0) < state.spool_seq - 1:
        return
    if isinstance(cursor.get("inflight"), dict):
        return
    try:
        _rewrite_spool_keeping_last(state, path)
    except OSError:
        _LOGGER.warning("spool rotation failed (watch=%s)", state.watch_id, exc_info=True)


def _rewrite_spool_keeping_last(state: WatchState, path: Path) -> None:
    """换代重写:只保留最后一条完整记录(读者可能还没读它),世代号 +1。"""
    last_line = ""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            last_line = line if line.endswith("\n") else last_line
    state.spool_generation += 1
    tmp = path.with_suffix(".ndjson.tmp")
    tmp.write_text(last_line, encoding="utf-8")
    tmp.replace(path)


def read_spool_records(
    state: WatchState, *, max_candidates: int, consumer: str = "", shard_index: int = 0, shard_count: int = 1
) -> tuple[list[dict], dict[str, Any]]:
    """读取未消费的 spool 记录(至少 1 条、候选数够 max_candidates 即停),并推进读游标。

    交付是 at-least-once:交付出去的一批先挂"在途"(inflight),**同一消费者下一次来取
    才算确认判完(ack)**——契约与 PULL_GUIDANCE 一致(逐批判完才继续 pull)。消费者
    换人(接管/补岗)时,前任没 ack 的在途批**原样重投给继任者**,不推进交付游标——
    真机实锤:接管只续游标时,前任拉走还没判完就死的候选成了永久孤儿(已抬升的真事
    躺在 spool 里没人判、没上报)。consumer 传拉取方 run_id(solo 主代理恒为空串,
    同样构成稳定身份;身份变化才触发重投)。

    判读并发/横向扩(P1):shard_count>1 时本消费者只认领 spool_seq % shard_count ==
    shard_index 的记录(不重不漏),各分片一份独立读游标——K 个判读工并行判同一路 spool、
    墙钟 ≈ 1/K。shard_count<=1(默认)= 单消费者旧路,与本函数 R5 语义逐字节等价。
    每个分片的在途/换人重投/ack 都在自己的分片游标上独立发生。

    返回 (records, backlog_info);重投批的 backlog_info 带 redelivered_candidates>0。
    """
    cursor = read_spool_cursor(state, shard_index, shard_count)
    inflight = cursor.get("inflight") if isinstance(cursor.get("inflight"), dict) else None
    acked_this_call = False
    if inflight is not None and str(inflight.get("consumer") or "") != str(consumer or ""):
        redelivered = _redeliver_inflight(state, cursor, inflight, consumer, shard_index, shard_count)
        if redelivered:
            info = _backlog_info(state, read_spool_cursor(state, shard_index, shard_count))
            info["redelivered_candidates"] = int(inflight.get("count") or 0)
            return redelivered, info
        # 在途批已不可恢复(spool 文件缺失/记录不在了):按缺口如实入账后清掉,
        # 别让一条坏在途卡死整路消费(缺口计数随游标持久化,零静默)。
        cursor = _acked_cursor(cursor, inflight, gap=True)
        inflight = None
        acked_this_call = True
    if inflight is not None:
        # 同一消费者回来取下一批 = 上一批已判完(ack):确认计数推进、在途清空。
        cursor = _acked_cursor(cursor, inflight)
        acked_this_call = True
    try:
        records, offset, taken, start_offset = _scan_spool(state, cursor, max_candidates, shard_index, shard_count)
    except OSError:
        records, offset, taken, start_offset = [], 0, 0, 0
    if records:
        payload = dict(cursor)
        payload["inflight"] = {
            "from_seq": int(cursor.get("read_seq") or 0) if int(cursor.get("generation") or 0) == state.spool_generation else 0,
            "to_seq": int(records[-1].get("spool_seq") or 0),
            "from_offset": start_offset,
            "count": taken,
            "consumer": str(consumer or ""),
            "generation": state.spool_generation,
            "delivered_at": time.time(),
        }
        payload.update(
            {
                "read_seq": int(records[-1].get("spool_seq") or 0),
                "offset": offset,
                "generation": state.spool_generation,
                "candidates_consumed": int(cursor.get("candidates_consumed") or 0) + taken,
                # acked 必须显式落值:没这个键的游标会被当"旧 sidecar"按已交付数回落,
                # 本批在途一旦丢失就不会体现在未判账上(等于白改)。
                "candidates_acked": acked_candidates(cursor),
                "updated_at": time.time(),
            }
        )
        _write_spool_cursor(state, payload, shard_index, shard_count)
    elif acked_this_call:
        # 没有新记录但发生了 ack(在途被确认/按缺口清掉):确认必须落盘,否则下次
        # 还会把已判完的批当在途重投。空轮询(无 ack 无新批)不写盘,别刷 IO。
        _write_spool_cursor(state, {**cursor, "updated_at": time.time()}, shard_index, shard_count)
    return records, _backlog_info(state, read_spool_cursor(state, shard_index, shard_count))


def _acked_cursor(cursor: dict, inflight: dict, *, gap: bool = False) -> dict:
    """确认在途批:acked 计数推进、在途清空;gap=True 记不可恢复缺口(结构化计数)。"""
    payload = dict(cursor)
    payload["candidates_acked"] = acked_candidates(cursor) + int(inflight.get("count") or 0)
    payload.pop("inflight", None)
    if gap:
        payload["redelivery_gap_candidates"] = int(payload.get("redelivery_gap_candidates") or 0) + int(
            inflight.get("count") or 0
        )
    return payload


def acked_candidates(cursor: dict[str, Any]) -> int:
    """已确认判完的候选累计数。旧 sidecar 没有 acked 字段:按已交付数起底
    (历史批无法追认,如实沿用旧口径,不追溯重投)。"""
    value = cursor.get("candidates_acked", cursor.get("candidates_consumed"))
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _in_shard(seq: int, shard_index: int, shard_count: int) -> bool:
    """记录 seq 是否归本分片:shard_count<=1 全归(单消费者);否则按 spool_seq 取模分片
    (记录级不重不漏划分——一条真事只落一条记录 → 只归一个分片,不双判也不漏判)。"""
    if shard_count <= 1:
        return True
    return (seq % shard_count) == shard_index


def _redeliver_inflight(
    state: WatchState, cursor: dict, inflight: dict, consumer: str, shard_index: int = 0, shard_count: int = 1
) -> list[dict]:
    """把前任消费者的在途批原样重投给新消费者:只换在途归属,不动交付游标/确认计数。
    重投可能造成重复判读(前任可能判完了没来得及 ack)——重复上报无害,漏判才是丢。"""
    if int(inflight.get("generation") or 0) != state.spool_generation:
        return []  # 轮转已换代,在途记录不复存在(rotation 有在途不轮转,此为防御残留)
    try:
        records = _scan_spool_range(state, inflight, shard_index, shard_count)
    except OSError:
        return []
    if not records:
        return []
    updated = dict(inflight)
    updated["consumer"] = str(consumer or "")
    updated["delivered_at"] = time.time()
    _write_spool_cursor(state, {**cursor, "inflight": updated, "updated_at": time.time()}, shard_index, shard_count)
    return records


def _scan_spool_range(state: WatchState, inflight: dict, shard_index: int = 0, shard_count: int = 1) -> list[dict]:
    """按在途标记重读 (from_seq, to_seq] 区间【本分片】的记录(从 from_offset 起顺扫)。"""
    from_seq = int(inflight.get("from_seq") or 0)
    to_seq = int(inflight.get("to_seq") or 0)
    offset = max(0, int(inflight.get("from_offset") or 0))
    records: list[dict] = []
    with spool_path(state).open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        while True:
            row, offset, complete = _next_spool_row(handle, offset)
            if not complete:
                break
            if row is None:
                continue
            seq = int(row.get("spool_seq") or 0)
            if seq <= from_seq:
                continue
            if seq > to_seq:
                break
            if _in_shard(seq, shard_index, shard_count):
                records.append(row)
    return records


def _scan_spool(
    state: WatchState, cursor: dict, max_candidates: int, shard_index: int = 0, shard_count: int = 1
) -> tuple[list[dict], int, int, int]:
    """从读游标偏移顺扫 spool,收集本分片未读记录;世代不符则从头扫(轮转后偏移作废)。
    返回 (records, 扫后偏移, 候选数, 起扫偏移)——起扫偏移供在途标记记录重投起点。"""
    offset = int(cursor.get("offset") or 0)
    if int(cursor.get("generation") or 0) != state.spool_generation:
        offset = 0
    start_offset = offset
    read_seq = int(cursor.get("read_seq") or 0)
    with spool_path(state).open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        records, end_offset, taken = _collect_spool_rows(
            handle, offset, read_seq, max_candidates, shard_index, shard_count
        )
    return records, end_offset, taken, start_offset


def _collect_spool_rows(
    handle, offset: int, read_seq: int, max_candidates: int, shard_index: int = 0, shard_count: int = 1
) -> tuple[list[dict], int, int]:
    """顺扫已打开的 spool 句柄:跳过已读/坏行/非本分片记录,候选凑够额度即停。"""
    records: list[dict] = []
    taken = 0
    while taken < max_candidates:
        row, offset, complete = _next_spool_row(handle, offset)
        if not complete:
            break
        if row is None:
            continue
        seq = int(row.get("spool_seq") or 0)
        if seq <= read_seq or not _in_shard(seq, shard_index, shard_count):
            continue
        records.append(row)
        taken += len(row.get("candidates") or [])
    return records, offset, taken


def _next_spool_row(handle, offset: int) -> tuple[dict | None, int, bool]:
    """读下一条完整记录行;尾部半行(写入中)视为流末,offset 停在半行前。"""
    line = handle.readline()
    if not line or not line.endswith("\n"):
        return None, offset, False
    return _parse_spool_line(line), handle.tell(), True


def _parse_spool_line(line: str) -> dict | None:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def _backlog_info(state: WatchState, cursor: dict) -> dict[str, Any]:
    written = int(state.totals.get("spool_candidates", 0) or 0)
    # 未读/未判按【全分片合计】算:sharded 消费下 written 是全局的,单看本分片游标会把
    # 别的分片已消费的也算成积压(过载信号虚高)。单消费者(shard_count=1)下合计=基座游标,
    # 与旧口径逐值等价。
    consumed, acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    info = {
        "records_unread": max(0, state.spool_seq - int(cursor.get("read_seq") or 0)),
        "candidates_unread": max(0, written - consumed),
        # 未判完口径(ack):未读 + 在途(交付出去还没被下一次 pull 确认)。唤醒兜底/
        # 收口守卫用这把尺——交付≠判完,消费者死在判读中途的批不能从账上消失。
        "candidates_unjudged": max(0, written - acked),
    }
    gap = int(cursor.get("redelivery_gap_candidates") or 0)
    if gap > 0:
        info["redelivery_gap_candidates"] = gap
    return info


def read_spool_cursor(state: WatchState, shard_index: int = 0, shard_count: int = 1) -> dict[str, Any]:
    report = read_json_object_report(
        _read_sidecar_path(state, shard_index, shard_count), context="watch_spool.read_cursor"
    )
    return {} if report.load_error is not None else dict(report.payload)


def _write_spool_cursor(state: WatchState, payload: dict[str, Any], shard_index: int = 0, shard_count: int = 1) -> None:
    path = _read_sidecar_path(state, shard_index, shard_count)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _LOGGER.warning("spool read-cursor write failed (watch=%s)", state.watch_id, exc_info=True)


def _lease_owner() -> str:
    return f"pid:{os.getpid()}"


def _read_lease(state: WatchState) -> dict[str, Any] | None:
    report = read_json_object_report(_lease_path(state), context="watch_harvester.lease")
    if report.load_error is not None:
        return None
    lease = dict(report.payload)
    try:
        fresh = (time.time() - float(lease.get("heartbeat_at") or 0.0)) <= _LEASE_FRESH_SECONDS
    except (TypeError, ValueError):
        return None
    return lease if fresh else None


def _lease_owned_by_me(lease: dict[str, Any]) -> bool:
    return str(lease.get("owner") or "") == _lease_owner()


def _write_lease(state: WatchState) -> None:
    path = _lease_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"owner": _lease_owner(), "watch_id": state.watch_id, "heartbeat_at": time.time()}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _LOGGER.debug("harvester lease write failed (watch=%s)", state.watch_id, exc_info=True)


def _clear_lease(state: WatchState) -> None:
    try:
        _lease_path(state).unlink(missing_ok=True)
    except OSError:
        pass


def harvester_block(state: WatchState) -> dict[str, Any]:
    """给 pull/status 载荷的收割者健康块(模型据此如实上报源健康/积压)。"""
    handle = harvesters.get_live(state.watch_id)
    lease = _read_lease(state)
    block = {
        "running": handle is not None or lease is not None,
        "mode": "local" if handle is not None else ("remote" if lease is not None else "off"),
        "spool_seq": state.spool_seq,
        "last_error": state.last_error or "",
    }
    # 背压观测口(纯计数,验收/排障可见"抬取在等判读"):被钳过几拍、当前是否触顶。
    skips = int(state.totals.get("backpressure_skips", 0) or 0)
    if skips > 0:
        block["backpressure_skips"] = skips
    if _backpressured(state):
        block["backpressure_active"] = True
    return block


__all__ = [
    "acked_candidates",
    "backpressure_ceiling",
    "consumed_and_acked_on_disk",
    "content_batch_size",
    "ensure_harvester",
    "harvester_block",
    "harvesters",
    "is_content_mode",
    "judge_headroom",
    "judge_quota",
    "overload_threshold",
    "read_spool_cursor",
    "read_spool_records",
    "recommended_judge_workers",
    "spool_path",
    "spool_unread",
    "stop_harvester",
]
