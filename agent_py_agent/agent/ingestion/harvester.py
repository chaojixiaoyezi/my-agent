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
from .watch_state import WatchState, _unique_tmp, audit_append, persist_state, state_dir

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


def _read_sidecar_path(state: WatchState) -> Path:
    """读游标 sidecar 路径(单消费者=一源一判读工;见 config 判读并发说明:已退役分片)。"""
    return state_dir(state.owner_home) / f"{state.watch_id}.read.json"


def consumed_and_acked_on_disk(owner_home: Path, watch_id: str) -> tuple[int, int]:
    """一路 watch 的已交付 / 已确认判完候选数(纯盘上结构信号,单消费者)。
    背压/过载/唤醒兜底的未判积压都按这把尺算。"""
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
            # 保证档只会因磁盘水位走到这里,单独记账(回执要能区分"判读慢"与"盘满")。
            key = "disk_backpressure_skips" if state.audit_guarantee else "backpressure_skips"
            state.totals[key] = int(state.totals.get(key, 0) or 0) + 1
            return "ok"
        return "ok" if _harvest_cycle(state, fetch_json) else "error"


def _effective_ceiling(state: WatchState) -> int:
    """抬取背压上限(单消费者=一源一判读工:一份前瞻缓冲)。0=关闭背压。"""
    return backpressure_ceiling(state.tuning)


def _backpressured(state: WatchState) -> bool:
    """未读积压是否已到背压上限(纯计数)。只对内容型全量直通(易洪泛)生效——结构化源
    候选稀疏不洪泛,不背压;上限 0=关闭背压,行为回到旧的无界堆积。上限随判读工数放大。

    /audit 保证档不走这条:按判读积压停抬会让滚动缓冲源在源端淘汰(=真丢),违反
    "一条不漏"。保证档判读慢只体现为 spool(磁盘队列)涨,唯一停抬边界是磁盘水位。"""
    if state.audit_guarantee:
        return _disk_backpressured(state)
    if not is_content_mode(state):
        return False
    ceiling = _effective_ceiling(state)
    return ceiling > 0 and spool_unread(state) >= ceiling


def _disk_backpressured(state: WatchState) -> bool:
    """保证档的磁盘水位闸(丢弃恒 0 的最后边界):spool 文件超上限或盘上剩余空间不足
    → 本拍停抬,积压留在【源端】而不是丢已入队的;恢复(判完归档/腾出空间)即续抬。
    纯字节计数;0=不设限。触发即记 disk_backpressure_skips(回执/健康块可见)。"""
    max_bytes = int(getattr(state.tuning, "guarantee_spool_max_mb", 0) or 0) * 1024 * 1024
    if max_bytes > 0:
        try:
            if spool_path(state).stat().st_size >= max_bytes:
                return True
        except OSError:
            pass
    min_free = int(getattr(state.tuning, "guarantee_min_disk_free_mb", 0) or 0) * 1024 * 1024
    if min_free > 0:
        import shutil

        try:
            if shutil.disk_usage(state_dir(state.owner_home)).free <= min_free:
                return True
        except OSError:
            pass
    return False


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
    last_consume = max(
        float(state.last_pull_at or 0.0),
        float(state.opened_at or 0.0),
        _latest_cursor_update(state),
    )
    return (time.time() - last_consume) > idle_cap


def _latest_cursor_update(state: WatchState) -> float:
    """读游标最近一次推进时刻(单消费者:一路一个基座游标)——消费活跃度信号,供 idle 自停判定。"""
    return float(read_spool_cursor(state).get("updated_at") or 0.0)


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
        digest = state.engine.process(
            chunk, time.time(), judge_headroom=headroom, cold_start=cold_start,
            guarantee=state.audit_guarantee,
        )
        # 本片实抬的候选(真车道+抽检)即时扣减余量:同拍后续片共享同一份判读余量。
        headroom = max(0, headroom - len(digest.candidates))
        if digest.candidates:
            _spool_append(state, chunk_view, digest)
        audit_append(state, build_audit_record(chunk_view, digest))
    # cursor 是本批已完整提交的结构化水位，不是“网络已经读到哪里”的预告。
    # 必须在 engine/spool/audit 全部成功后发布；否则并发读者会提前判断追平，
    # 后续步骤一旦抛错，下一拍还会从新 cursor 续读并永久跳过未提交事件。
    state.cursor = drain.cursor
    state.line_cursor = int(getattr(drain, "aux_cursor", 0) or 0)
    if float(getattr(drain, "fetched_at", 0.0) or 0.0) > 0:
        state.last_poll_at = float(drain.fetched_at)
    state.last_reached_end = drain.reached_end
    state.totals["gap_events"] += drain.gap_events
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
    积压按读游标已消费数算(单消费者:一源一判读工)。"""
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
    背压看这把尺:未读堆着=消费者没跟上,抬取该等一等。"""
    written = int(state.totals.get("spool_candidates", 0) or 0)
    if written <= 0:
        return 0
    consumed, _acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    return max(0, written - consumed)


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
    5000 条埋掉真事)。至少抓一批口粮,room 再小也有进度。
    保证档不按判读积压钳抓取(全速收进 durable 队列,源端滚动缓冲淘汰才是真丢);
    盘满边界由 _disk_backpressured 在拍首整拍拦。"""
    base = int(state.tuning.max_events_per_pull)
    ceiling = _effective_ceiling(state)
    if state.audit_guarantee or ceiling <= 0 or not content_mode:
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


def candidate_ack_id(spool_seq: int, index: int) -> str:
    """候选的签收令牌(ack-on-judge 的对账键):spool 记录序号:记录内下标。全由盘上事实
    构成——spool_seq 跨轮转单调、下标落盘即定,重投/重启/换人重建出的令牌逐字节相同。"""
    return f"{spool_seq}:{index}"


def ensure_ack_ids(record: dict[str, Any]) -> list[dict[str, Any]]:
    """取记录的候选行并保证每行带 ack_id(升级前落盘的旧记录现场按同一规则补齐)。"""
    seq = int(record.get("spool_seq") or 0)
    rows = list(record.get("candidates") or [])
    for index, row in enumerate(rows):
        if isinstance(row, dict) and not row.get("ack_id"):
            row["ack_id"] = candidate_ack_id(seq, index)
    return rows


def _spool_append(state: WatchState, drain: Any, digest: Any) -> None:
    # 先写盘、成功才冒泡计数:计数即"记录已可读"(消费方以计数判积压,写失败不留幽灵积压)。
    next_seq = state.spool_seq + 1
    record = {
        "spool_seq": next_seq,
        "generation": state.spool_generation,
        "t": round(time.time(), 3),
        "candidates": [
            {**row, "ack_id": candidate_ack_id(next_seq, index)}
            for index, row in enumerate(candidate_rows(digest))
        ],
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
    # 轮转判据交给 _maybe_rotate_spool 自查(首查即文件尺寸,未超限即返回)。
    _maybe_rotate_spool(state, path)


def _maybe_rotate_spool(state: WatchState, path: Path) -> None:
    """积压已清(读游标追平倒数第二条,最后一条照旧留给读者)且无在途批、文件超限 →
    换代重写,读者按 generation 重置偏移。有在途不轮转:轮转会吃掉接管重投的依据
    (在途在消费者确认后清空,轮转窗口照常出现,长守不涨盘)。"""
    try:
        if path.stat().st_size < _SPOOL_ROTATE_BYTES:
            return
    except OSError:
        return
    cursor = read_spool_cursor(state)
    if isinstance(cursor.get("inflight"), dict):
        return
    if int(cursor.get("read_seq") or 0) < state.spool_seq - 1:
        return
    try:
        _rewrite_spool_keeping_last(state, path)
    except OSError:
        _LOGGER.warning("spool rotation failed (watch=%s)", state.watch_id, exc_info=True)


def _rewrite_spool_keeping_last(state: WatchState, path: Path) -> None:
    """换代重写:只保留最后一条完整记录(读者可能还没读它),世代号 +1。
    保证档(/audit)契约是【判完归档、永不删】:被切走的行(此刻全部已判完签收)
    先追加进 {watch_id}.archive.ndjson 留痕,再重写——活跃工作集有界,月级长跑
    不涨盘也不销毁任何已判记录;非保证档沿用旧行为(切走即弃,省盘)。"""
    lines: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.endswith("\n"):
                lines.append(line)
    last_line = lines[-1] if lines else ""
    if state.audit_guarantee and len(lines) > 1:
        archive = state_dir(state.owner_home) / f"{state.watch_id}.archive.ndjson"
        with archive.open("a", encoding="utf-8") as handle:
            handle.writelines(lines[:-1])
    state.spool_generation += 1
    tmp = path.with_suffix(".ndjson.tmp")
    tmp.write_text(last_line, encoding="utf-8")
    tmp.replace(path)


def read_spool_records(
    state: WatchState, *, max_candidates: int, consumer: str = ""
) -> tuple[list[dict], dict[str, Any]]:
    """读取未消费的 spool 记录(至少 1 条、候选数够 max_candidates 即停),并推进读游标。

    单消费者=一源一判读工:整路 spool 一份读游标,全量记录都归本消费者(无分片过滤)。
    交付是 at-least-once:交付出去的一批先挂"在途"(inflight),**同一消费者下一次来取
    才算确认判完(ack)**——契约与 PULL_GUIDANCE 一致(逐批判完才继续 pull)。消费者
    换人(接管/补岗)时,前任没 ack 的在途批**原样重投给继任者**,不推进交付游标——
    真机实锤:接管只续游标时,前任拉走还没判完就死的候选成了永久孤儿(已抬升的真事
    躺在 spool 里没人判、没上报)。consumer 传拉取方 run_id(solo 主代理恒为空串,
    同样构成稳定身份;身份变化才触发重投)。

    返回 (records, backlog_info);重投批的 backlog_info 带 redelivered_candidates>0。
    """
    cursor = read_spool_cursor(state)
    inflight = cursor.get("inflight") if isinstance(cursor.get("inflight"), dict) else None
    acked_this_call = False
    if inflight is not None and state.audit_guarantee and isinstance(inflight.get("pending_acks"), list):
        outcome = _redeliver_unacked_audit(state, (cursor, inflight), consumer)
        if isinstance(outcome, tuple):
            return outcome
        cursor = outcome  # 在途不可恢复按缺口清,或防御摘除空欠账在途
        inflight = None
        acked_this_call = True
    if inflight is not None and str(inflight.get("consumer") or "") != str(consumer or ""):
        redelivered = _redeliver_inflight(state, cursor, inflight, consumer)
        if redelivered:
            info = _backlog_info(state, read_spool_cursor(state))
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
        records, offset, taken, start_offset = _scan_spool(state, cursor, max_candidates)
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
        if state.audit_guarantee:
            # 保证档交付即立欠账:批内每条候选一个签收令牌,submit_verdicts 逐条销账,
            # 销齐才算这批判完(ack-on-judge)。旧记录缺 ack_id 的现场按同规则补齐。
            payload["inflight"]["pending_acks"] = [
                str(row.get("ack_id") or "") for record in records for row in ensure_ack_ids(record)
            ]
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
        _write_spool_cursor(state, payload)
    elif acked_this_call:
        # 没有新记录但发生了 ack(在途被确认/按缺口清掉):确认必须落盘,否则下次
        # 还会把已判完的批当在途重投。空轮询(无 ack 无新批)不写盘,别刷 IO。
        _write_spool_cursor(state, {**cursor, "updated_at": time.time()})
    return records, _backlog_info(state, read_spool_cursor(state))


def _redeliver_unacked_audit(
    state: WatchState, cursor_inflight: tuple[dict, dict], consumer: str
) -> tuple[list[dict], dict] | dict:
    """/audit 保证档 ack-on-judge(上一轮崩的直接根因:判读工空转,再 pull 一次就把没判
    的在途批"确认"掉=静默吃):在途批还有候选没交逐条结论(submit_verdicts)时,【同人
    换人一律重投同批、绝不 ack、绝不发新批】——空 pull 推不动游标,领了活不判的工只会
    反复拿到同一批和欠账清单;结论交齐时在途已被 verdict 动作当场清掉,走不到这里。
    返回 (records, info) 即重投;返回 dict = 调整后的游标(在途不可恢复按缺口如实入账
    ——这笔账让覆盖回执 dropped>0 亮红,丢弃恒 0 是硬约束,亮红=有 bug,绝不粉饰;
    或防御摘除欠账已空的在途——不再推 acked 计数,逐条结论入账时已逐条 +1,批级再加双计)。"""
    cursor, inflight = cursor_inflight
    pending = [str(x) for x in inflight.get("pending_acks") or []]
    if not pending:
        return {k: v for k, v in cursor.items() if k != "inflight"}
    redelivered = _redeliver_inflight(state, cursor, inflight, consumer)
    if not redelivered:
        return _acked_cursor(cursor, inflight, gap=True)
    info = _backlog_info(state, read_spool_cursor(state))
    info["redelivered_candidates"] = int(inflight.get("count") or 0)
    info["pending_verdicts"] = len(pending)
    info["pending_ack_ids"] = pending[:64]
    return redelivered, info


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


# 保证档逐条结论的合法取值:hit=确认命中(照常另走 record_finding 上报)、clear=明确判
# 无事、unsure=查证后仍拿不准(如实存疑也是逐条真结论——比盖章/硬判/不交都诚实)。
_VERDICT_KINDS = ("hit", "clear", "unsure")


def _verdict_ledger_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.verdicts.ndjson"


def _verdict_ledger_append(state: WatchState, rows: list[dict[str, Any]]) -> None:
    """逐条结论台账(append-only,.ndjson 非上报面):每条候选的最终结论都留痕——
    覆盖回执"已判 Y"的可复核凭证,测试方可逐条对回 spool 记录。写失败不阻断签收
    (计数已随游标持久化),只降级留痕。"""
    if not rows:
        return
    path = _verdict_ledger_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        _LOGGER.warning("verdict ledger append failed (watch=%s)", state.watch_id, exc_info=True)


def submit_verdicts(
    state: WatchState, *, consumer: str, verdicts: list[dict[str, Any]],
) -> dict[str, Any]:
    """/audit 保证档的逐条结论签收(ack-on-judge 的推进入口,与 pull 同线程序列化):
    把判读工交来的 [{ack_id, verdict, note?}] 与在途欠账(inflight.pending_acks)逐条对账
    ——对上的:结论进台账、acked 逐条 +1、欠账销一条;全销完才摘在途(下一次 pull 才发
    新批)。对不上的(ack_id 不在任何在途欠账里)原样退回 unknown,绝不凭空入账。
    游标/ACK 只在这里因"真判完+记了结论"推进——空转的判读工推不动任何账。"""
    parsed: dict[str, tuple[str, str]] = {}
    for row in verdicts or []:
        if not isinstance(row, dict):
            continue
        ack_id = str(row.get("ack_id") or "").strip()
        kind = str(row.get("verdict") or "").strip().lower()
        if ack_id and kind in _VERDICT_KINDS:
            parsed[ack_id] = (kind, str(row.get("note") or "")[:400])
    malformed = max(0, len(verdicts or [])) - len(parsed)
    if not parsed:
        return {
            "ok": False, "acked_now": 0, "malformed": malformed,
            "error": "没有可入账的结论:verdicts 每项需 {ack_id, verdict∈hit/clear/unsure}",
        }
    remaining = dict(parsed)
    counts = {"hit": 0, "clear": 0, "unsure": 0}
    acked_now = 0
    pending_remaining = 0
    settled = _settle_verdicts_on_cursor(state, remaining, consumer)
    if settled is not None:
        acked_now = settled[0]
        pending_remaining = settled[1]
        for kind, n in settled[2].items():
            counts[kind] += n
    return {
        "ok": True,
        "acked_now": acked_now,
        "verdicts_hit": counts["hit"],
        "verdicts_clear": counts["clear"],
        "verdicts_unsure": counts["unsure"],
        "pending_remaining": pending_remaining,
        "unknown_ack_ids": sorted(remaining),
        "malformed": malformed,
    }


def _settle_verdicts_on_cursor(
    state: WatchState, remaining: dict[str, tuple[str, str]], consumer: str
) -> tuple[int, int, dict[str, int]] | None:
    """在读游标的在途欠账上销账(remaining 原地消耗,销掉的条目从中移除):
    返回 (销账数, 本批剩余欠账数, 各结论计数);无在途/无交集返回 None(没参与)。
    销账 = 结论进台账 + acked 逐条推进 + 欠账收缩;欠账清零才摘在途(发新批的闸)。"""
    cursor = read_spool_cursor(state)
    inflight = cursor.get("inflight") if isinstance(cursor.get("inflight"), dict) else None
    if inflight is None or not isinstance(inflight.get("pending_acks"), list):
        return None
    pending = [str(x) for x in inflight.get("pending_acks") or []]
    matched = [aid for aid in pending if aid in remaining]
    if not matched:
        return None
    now = time.time()
    ledger_rows = []
    row_counts = {"hit": 0, "clear": 0, "unsure": 0}
    for aid in matched:
        kind, note = remaining.pop(aid)
        row_counts[kind] += 1
        entry = {"ack_id": aid, "verdict": kind, "by": str(consumer or ""), "t": round(now, 3)}
        if note:
            entry["note"] = note
        ledger_rows.append(entry)
    left = [aid for aid in pending if aid not in set(matched)]
    updated = dict(cursor)
    updated["candidates_acked"] = acked_candidates(cursor) + len(matched)
    for kind, n in row_counts.items():
        if n:
            updated[f"verdicts_{kind}"] = int(cursor.get(f"verdicts_{kind}") or 0) + n
    if left:
        updated["inflight"] = {**inflight, "pending_acks": left}
    else:
        updated.pop("inflight", None)
    updated["updated_at"] = now
    _write_spool_cursor(state, updated)
    _verdict_ledger_append(state, ledger_rows)
    return len(matched), len(left), row_counts


def audit_receipt_facts(state: WatchState) -> dict[str, Any]:
    """覆盖回执(用户敢不重审的唯一凭证,任意时刻可查):入队 X · 已判 Y · 待判 M ·
    丢弃 0。纯盘上/内存结构计数拼装,零推断:
    - enqueued=引擎累计入队候选;judged=全消费面逐条销账合计;pending=差值(还在判,
      不是漏)。dropped=已入队后未判即消失的(重投缺口)+ 保证档启用后引擎有损计数的
      增量(启用后就不该再涨)——恒 0 是硬约束,>0 即 bug 亮红,绝不粉饰。
    - source_gap_events:源端滚动缓冲淘汰造成的拉取缺口(丢在源端,不是队列里),如实单列。"""
    written = int(state.totals.get("spool_candidates", 0) or 0)
    _consumed, acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    cursor = read_spool_cursor(state)
    gap = int(cursor.get("redelivery_gap_candidates") or 0)
    verdict_counts = {
        kind: int(cursor.get(f"verdicts_{kind}") or 0) for kind in ("hit", "clear", "unsure")
    }
    lossy_now = sum(
        int(state.engine.totals.get(k, 0) or 0) for k in ("suppressed", "overflow", "audit_throttled")
    )
    lossy_baseline = sum(int(state.audit_baseline.get(k, 0) or 0) for k in ("suppressed", "overflow", "audit_throttled"))
    receipt = {
        "mode": "audit_guarantee",
        "enqueued": written,
        "judged": min(acked, written) if written else acked,
        "pending": max(0, written - acked),
        "dropped": gap + max(0, lossy_now - lossy_baseline),
        "verdicts": verdict_counts,
        "source_gap_events": int(state.totals.get("gap_events", 0) or 0),
    }
    skips = int(state.totals.get("disk_backpressure_skips", 0) or 0)
    if skips > 0:
        receipt["disk_backpressure_skips"] = skips
    return receipt


def _redeliver_inflight(
    state: WatchState, cursor: dict, inflight: dict, consumer: str
) -> list[dict]:
    """把前任消费者的在途批原样重投给新消费者:只换在途归属,不动交付游标/确认计数。
    重投可能造成重复判读(前任可能判完了没来得及 ack)——重复上报无害,漏判才是丢。"""
    if int(inflight.get("generation") or 0) != state.spool_generation:
        return []  # 轮转已换代,在途记录不复存在(rotation 有在途不轮转,此为防御残留)
    try:
        records = _scan_spool_range(state, inflight)
    except OSError:
        return []
    if not records:
        return []
    updated = dict(inflight)
    updated["consumer"] = str(consumer or "")
    updated["delivered_at"] = time.time()
    _write_spool_cursor(state, {**cursor, "inflight": updated, "updated_at": time.time()})
    return records


def _scan_spool_range(state: WatchState, inflight: dict) -> list[dict]:
    """按在途标记重读 (from_seq, to_seq] 区间的记录(从 from_offset 起顺扫)。"""
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
            records.append(row)
    return records


def _scan_spool(
    state: WatchState, cursor: dict, max_candidates: int
) -> tuple[list[dict], int, int, int]:
    """从读游标偏移顺扫 spool,收集未读记录;世代不符则从头扫(轮转后偏移作废)。
    返回 (records, 扫后偏移, 候选数, 起扫偏移)——起扫偏移供在途标记记录重投起点。"""
    offset = int(cursor.get("offset") or 0)
    if int(cursor.get("generation") or 0) != state.spool_generation:
        offset = 0
    start_offset = offset
    read_seq = int(cursor.get("read_seq") or 0)
    with spool_path(state).open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        records, end_offset, taken = _collect_spool_rows(
            handle, offset, read_seq, max_candidates
        )
    return records, end_offset, taken, start_offset


def _collect_spool_rows(
    handle, offset: int, read_seq: int, max_candidates: int
) -> tuple[list[dict], int, int]:
    """顺扫已打开的 spool 句柄:跳过已读/坏行,候选凑够额度即停。"""
    records: list[dict] = []
    taken = 0
    while taken < max_candidates:
        row, offset, complete = _next_spool_row(handle, offset)
        if not complete:
            break
        if row is None:
            continue
        seq = int(row.get("spool_seq") or 0)
        if seq <= read_seq:
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


def read_spool_cursor(state: WatchState) -> dict[str, Any]:
    report = read_json_object_report(
        _read_sidecar_path(state), context="watch_spool.read_cursor"
    )
    return {} if report.load_error is not None else dict(report.payload)


def _write_spool_cursor(state: WatchState, payload: dict[str, Any]) -> None:
    path = _read_sidecar_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _unique_tmp(path)
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
        tmp = _unique_tmp(path)
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
    disk_skips = int(state.totals.get("disk_backpressure_skips", 0) or 0)
    if disk_skips > 0:
        block["disk_backpressure_skips"] = disk_skips
    if _backpressured(state):
        block["backpressure_active"] = True
    return block


__all__ = [
    "acked_candidates",
    "audit_receipt_facts",
    "backpressure_ceiling",
    "candidate_ack_id",
    "consumed_and_acked_on_disk",
    "content_batch_size",
    "ensure_ack_ids",
    "ensure_harvester",
    "harvester_block",
    "harvesters",
    "is_content_mode",
    "judge_headroom",
    "judge_quota",
    "overload_threshold",
    "read_spool_cursor",
    "read_spool_records",
    "spool_path",
    "spool_unread",
    "stop_harvester",
    "submit_verdicts",
]
