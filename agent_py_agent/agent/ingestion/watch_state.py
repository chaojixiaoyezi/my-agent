"""盯守状态:每路 watch 的游标+引擎+账目,进程内注册表 + owner 盘上快照(补岗/重启可续)。

审计文件用 .ndjson 后缀且落在 owner_home/watch_state/(不在 tasks/ 交付面):
对账脚本只扫"上报面",原始拉取/初筛账目绝不能混进去虚增误报。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from hashlib import sha1
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report
from .config import IngestTuning, tuning_from_params
from .engine import StreamDigestEngine

_SCHEMA_VERSION = "watch-stream-state.v1"
_DIR_NAME = "watch_state"


@dataclass
class WatchState:
    watch_id: str
    owner_home: Path
    source_url: str
    tuning: IngestTuning
    engine: StreamDigestEngine
    cursor: int = 0
    opened_at: float = 0.0
    watch_window_seconds: int = 0
    closed: bool = False
    totals: dict[str, int] = field(default_factory=lambda: {"pulls": 0, "http_errors": 0, "gap_events": 0, "spool_candidates": 0, "backpressure_skips": 0, "disk_backpressure_skips": 0})
    last_pull_at: float = 0.0
    last_reached_end: bool = False
    last_error: str = ""
    # 岗位归属:最近一次 pull 这路流的 run(编队补岗的结构化事实来源)。
    last_puller_run_id: str = ""
    # 最近一次 open 这路 watch 的 run(扇出结构探针:同一个 run 开了 2+ 路 watch = "一个
    # 子代理独扛多源"的反模式,open 回执给出"一源一子代理"扇出提示。纯结构计数,不判内容)。
    opened_by_run: str = ""
    respawn_count: int = 0
    last_respawn_at: float = 0.0
    # 后台收割 spool:已写入的候选批记录序号 + 轮转世代(harvester.py 单写者)。
    spool_seq: int = 0
    spool_generation: int = 0
    # 源信封元数据(open 探针抓取的标量字段,如 schema_note/api 名):原样透传给模型,
    # 让它锚定【源自带的结果端判据说明】——真机实锤:puller 只取 items,信封被丢,
    # 有的盯守子代理自立判据把迷惑项当命中报(B 路 20+ 误报)。代码不解读内容,只搬运。
    source_envelope: dict[str, Any] = field(default_factory=dict)
    # 源形态:""=cursor(HTTP 游标流,默认)/"poll"(快照接口定时查);file 源由
    # source_url 的 file:// 前缀判定,不占本字段。随 watch 持久化。
    source_mode: str = ""
    # file 源的行号游标(stream_pos=行号,1-based;字节偏移在 cursor);其余源恒 0。
    line_cursor: int = 0
    # poll 源最近一次真的查询接口的时刻(节拍闸:距今不足 poll_query_seconds 不再查)。
    last_poll_at: float = 0.0
    # 轻量记忆(教一次别重教):用户教的"这个来源/这类事怎么看"(样品说明/判据描述原文)
    # 经 configure 的 judgment_note 存在这里,随 watch 持久化——重启/补岗/换人接手时在
    # open/sample/pull 载荷里原样带回,同一来源不用重教。代码只搬运不解读。
    judgment_note: str = ""
    # per-源判据 spec(模型从样本学出、action=configure 灌入的结构化判据):每源一份、
    # 随 watch 持久化,重启/补岗自动回灌引擎;None=未学(引擎走通用兜底车道)。
    source_spec: dict[str, Any] | None = None
    # 最近一次 sample 的每字段取值分布(纯计数,configure 校验 target 频次用:真机实锤
    # 模型会把样本里的高频常态取值配成 target → 全误报;有样本证据时结构化拒配)。
    last_sample_digest: dict[str, Any] = field(default_factory=dict)
    # 反馈确认收件箱({watch_id}.feedback.ndjson)的消费偏移(引擎属主单消费者推进)。
    feedback_offset: int = 0
    # /audit 保证档(用户可点的逐条保证判读模式;与抽检车道 audit_sample 无关):
    # True = 本路契约变为【每条都判 · 一条不漏 · 判完才签收 · 给覆盖回执】——引擎关有损
    # 筛(normal 规则命中也逐条入队)、抬取不按判读积压背压(只按磁盘水位)、消费换
    # ack-on-judge(结论交齐才推游标)。单调置位:一旦开启不因后续 open 不带参数而降级
    # (保证是用户级契约,不许被转写/换人静默摘掉)。随 watch 持久化。
    audit_guarantee: bool = False
    # 保证档启用时刻的引擎有损计数基线(suppressed/overflow/audit_throttled):新开即
    # 保证档时全 0;老 watch 升级时快照当前值——覆盖回执的 dropped 只算启用之后的增量
    # (启用后这些计数就不该再涨,涨了=违约,回执如实亮红)。
    audit_baseline: dict[str, int] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


def watch_id_for(owner_home: Path, source_url: str) -> str:
    digest = sha1(f"{owner_home}|{source_url}".encode()).hexdigest()
    return f"ws-{digest[:10]}"


def state_dir(owner_home: Path) -> Path:
    return Path(owner_home) / _DIR_NAME


class WatchRegistry:
    """进程内 watch 注册表;open 幂等(同 owner+url 同 id),miss 时从盘上快照复活。"""

    def __init__(self) -> None:
        self._states: dict[str, WatchState] = {}
        self._lock = threading.RLock()

    def get(self, watch_id: str) -> WatchState | None:
        with self._lock:
            return self._states.get(watch_id)

    def put(self, state: WatchState) -> None:
        with self._lock:
            self._states[state.watch_id] = state

    def drop(self, watch_id: str) -> None:
        with self._lock:
            self._states.pop(watch_id, None)

    def get_or_load(self, owner_home: Path, watch_id: str) -> WatchState | None:
        with self._lock:
            state = self._states.get(watch_id)
            if state is not None:
                return state
            state = load_state(owner_home, watch_id)
            if state is not None:
                self._states[watch_id] = state
            return state


registry = WatchRegistry()


def new_state(owner_home: Path, source_url: str, params: dict[str, object]) -> WatchState:
    tuning = tuning_from_params(params)
    state = WatchState(
        watch_id=watch_id_for(owner_home, source_url),
        owner_home=Path(owner_home),
        source_url=source_url,
        tuning=tuning,
        engine=StreamDigestEngine(tuning),
        opened_at=time.time(),
        watch_window_seconds=_window_seconds(params),
    )
    return state


def _window_seconds(params: dict[str, object]) -> int:
    try:
        return max(0, int(str(params.get("watch_window_seconds") or 0).strip() or 0))
    except (TypeError, ValueError):
        return 0


def persist_state(state: WatchState) -> None:
    """快照原子写盘(tmp+rename);包含引擎画像/census,补岗或重启后从游标+温启动续。"""
    path = state_dir(state.owner_home) / f"{state.watch_id}.json"
    # 补岗计数由另一方(编队扫描)直接补丁文件,而拉流方按内存态整体覆写——两者并发会把
    # respawn_count 覆写回旧值。respawn 单调,取盘上与内存的较大值,拉流覆写不抹掉补岗记账。
    # closed 同理:关闭可能来自另一进程的 close 工具调用,收割线程整体覆写不得把它翻回去。
    disk = _disk_merge_facts(path)
    respawn_count = max(state.respawn_count, int(disk.get("respawn_count") or 0))
    state.closed = bool(state.closed or disk.get("closed"))
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "watch_id": state.watch_id,
        "source_url": state.source_url,
        "cursor": state.cursor,
        "opened_at": state.opened_at,
        "watch_window_seconds": state.watch_window_seconds,
        "closed": state.closed,
        "totals": dict(state.totals),
        "last_pull_at": state.last_pull_at,
        "last_reached_end": state.last_reached_end,
        "last_error": state.last_error,
        "last_puller_run_id": state.last_puller_run_id,
        "opened_by_run": state.opened_by_run,
        "respawn_count": respawn_count,
        "last_respawn_at": max(state.last_respawn_at, float(disk.get("last_respawn_at") or 0.0)),
        "spool_seq": state.spool_seq,
        "spool_generation": state.spool_generation,
        "source_mode": state.source_mode,
        "line_cursor": state.line_cursor,
        "last_poll_at": state.last_poll_at,
        "judgment_note": state.judgment_note,
        "source_envelope": dict(state.source_envelope),
        "source_spec": dict(state.source_spec) if state.source_spec else None,
        "last_sample_digest": dict(state.last_sample_digest),
        "feedback_offset": state.feedback_offset,
        # 保证档同 closed 一样单调合并:别的进程(open 升级)置的 True 不被收割线程整体覆写吃掉。
        "audit_guarantee": bool(state.audit_guarantee or disk.get("audit_guarantee")),
        "audit_baseline": dict(state.audit_baseline),
        "tuning": {k: getattr(state.tuning, k) for k in ("window_seconds", "bucket_seconds", "rare_threshold", "max_candidates_per_pull", "full_read_per_pull", "page_limit", "background_harvest", "harvester_idle_stop_seconds", "poll_query_seconds")},
        "engine": state.engine.snapshot(time.time()),
        "saved_at": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _disk_merge_facts(path: Path) -> dict[str, Any]:
    """读盘上须单调合并的事实(补岗计数/closed),整体覆写前先取,防跨方覆盖记账。"""
    report = read_json_object_report(path, context="watch_state.merge_read")
    if report.load_error is not None:
        return {}
    payload = report.payload
    return {
        "respawn_count": payload.get("respawn_count"),
        "last_respawn_at": payload.get("last_respawn_at"),
        "closed": payload.get("closed"),
        "audit_guarantee": payload.get("audit_guarantee"),
    }


def refresh_scalars_from_disk(state: WatchState) -> None:
    """从盘上快照回灌覆盖类标量(游标/账目/closed/spool 序号)——供「收割者在别的进程」
    时的 pull 侧展示新鲜覆盖;不动引擎画像(引擎归收割者)。调用方自行持锁。"""
    path = state_dir(state.owner_home) / f"{state.watch_id}.json"
    report = read_json_object_report(path, context="watch_state.refresh")
    if report.load_error is not None:
        return
    payload = report.payload
    state.cursor = max(state.cursor, int(payload.get("cursor") or 0))
    state.line_cursor = max(state.line_cursor, int(payload.get("line_cursor") or 0))
    state.last_reached_end = bool(payload.get("last_reached_end"))
    state.closed = bool(state.closed or payload.get("closed"))
    state.spool_seq = max(state.spool_seq, int(payload.get("spool_seq") or 0))
    state.spool_generation = max(state.spool_generation, int(payload.get("spool_generation") or 0))
    state.respawn_count = max(state.respawn_count, int(payload.get("respawn_count") or 0))
    state.audit_guarantee = bool(state.audit_guarantee or payload.get("audit_guarantee"))
    state.last_error = str(payload.get("last_error") or "") or state.last_error
    disk_spec = payload.get("source_spec")
    if isinstance(disk_spec, dict) and disk_spec and not state.source_spec:
        state.source_spec = dict(disk_spec)  # 展示用(引擎归收割者进程,这里不 apply)
    for key, value in dict(payload.get("totals") or {}).items():
        if key in state.totals:
            state.totals[key] = max(state.totals[key], int(value))
    engine_totals = dict((payload.get("engine") or {}).get("totals") or {})
    for key, value in engine_totals.items():
        if key in state.engine.totals:
            state.engine.totals[key] = max(state.engine.totals[key], int(value))


def load_state(owner_home: Path, watch_id: str) -> WatchState | None:
    path = state_dir(owner_home) / f"{watch_id}.json"
    report = read_json_object_report(path, context="watch_state.read")
    if report.load_error is not None:
        return None
    payload = report.payload
    source_url = str(payload.get("source_url") or "")
    if not source_url:
        return None
    state = new_state(owner_home, source_url, dict(payload.get("tuning") or {}))
    state.cursor = int(payload.get("cursor") or 0)
    state.opened_at = float(payload.get("opened_at") or time.time())
    state.watch_window_seconds = int(payload.get("watch_window_seconds") or 0)
    state.closed = bool(payload.get("closed"))
    state.last_pull_at = float(payload.get("last_pull_at") or 0.0)
    state.last_reached_end = bool(payload.get("last_reached_end"))
    state.last_error = str(payload.get("last_error") or "")
    state.last_puller_run_id = str(payload.get("last_puller_run_id") or "")
    state.opened_by_run = str(payload.get("opened_by_run") or "")
    state.respawn_count = int(payload.get("respawn_count") or 0)
    state.last_respawn_at = float(payload.get("last_respawn_at") or 0.0)
    state.spool_seq = int(payload.get("spool_seq") or 0)
    state.spool_generation = int(payload.get("spool_generation") or 0)
    state.source_mode = str(payload.get("source_mode") or "")
    state.line_cursor = int(payload.get("line_cursor") or 0)
    state.last_poll_at = float(payload.get("last_poll_at") or 0.0)
    state.judgment_note = str(payload.get("judgment_note") or "")
    envelope = payload.get("source_envelope")
    state.source_envelope = dict(envelope) if isinstance(envelope, dict) else {}
    sample_digest = payload.get("last_sample_digest")
    state.last_sample_digest = dict(sample_digest) if isinstance(sample_digest, dict) else {}
    state.feedback_offset = int(payload.get("feedback_offset") or 0)
    state.audit_guarantee = bool(payload.get("audit_guarantee"))
    baseline = payload.get("audit_baseline")
    state.audit_baseline = {str(k): int(v) for k, v in baseline.items()} if isinstance(baseline, dict) else {}
    _restore_spec(state, payload.get("source_spec"))
    for key, value in dict(payload.get("totals") or {}).items():
        if key in state.totals:
            state.totals[key] = int(value)
    state.engine.restore(dict(payload.get("engine") or {}), time.time())
    return state


def _restore_spec(state: WatchState, raw_spec: object) -> None:
    """盘上快照里的判据 spec 回灌引擎(在 engine.restore 之前:apply 会重置计数器,
    随后 restore 再回灌画像/滑窗——重启后判据与统计都续上)。解析失败按未配处理。"""
    if not isinstance(raw_spec, dict) or not raw_spec:
        return
    from .source_spec import parse_source_spec

    try:
        state.engine.apply_spec(parse_source_spec(raw_spec))
    except ValueError:
        return
    state.source_spec = dict(raw_spec)


def list_states(owner_home: Path) -> list[dict[str, Any]]:
    """盘上全部 watch 快照的轻量视图(不复活引擎)。"""
    rows: list[dict[str, Any]] = []
    directory = state_dir(owner_home)
    try:
        paths = sorted(directory.glob("ws-*.json"))
    except OSError:
        return rows
    for path in paths:
        report = read_json_object_report(path, context="watch_state.list")
        if report.load_error is None:
            rows.append(_list_row(report.payload))
    return rows


def _list_row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "watch_id": str(payload.get("watch_id") or ""),
        "source_url": str(payload.get("source_url") or ""),
        "cursor": int(payload.get("cursor") or 0),
        "opened_at": float(payload.get("opened_at") or 0.0),
        "watch_window_seconds": int(payload.get("watch_window_seconds") or 0),
        "closed": bool(payload.get("closed")),
        "last_pull_at": float(payload.get("last_pull_at") or 0.0),
        "last_reached_end": bool(payload.get("last_reached_end")),
        "last_puller_run_id": str(payload.get("last_puller_run_id") or ""),
        "opened_by_run": str(payload.get("opened_by_run") or ""),
        "respawn_count": int(payload.get("respawn_count") or 0),
        "audit_guarantee": bool(payload.get("audit_guarantee")),
        "totals": dict(payload.get("totals") or {}),
    }


def reopen_on_disk(state: WatchState) -> None:
    """用户显式 re-open:把盘上快照的 closed 翻回 False(直接补丁,与 record_respawn 同款)。
    persist_state 的单调合并(盘上 closed=True 不被覆写翻回)只该防【收割线程整体覆写】
    吃掉别进程的 close;显式 open 是用户意图,必须能重开——真机实锤:close 过的源重启后
    再 open,内存态刚置 False 就被盘上旧 True 合并回去,收割线程按 closed 自停,盯守空转。
    绝不抛异常。"""
    path = state_dir(state.owner_home) / f"{state.watch_id}.json"
    report = read_json_object_report(path, context="watch_state.reopen")
    if report.load_error is not None:
        return
    payload = report.payload
    if not payload.get("closed"):
        return
    payload["closed"] = False
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def record_respawn(owner_home: Path, watch_id: str, takeover_run_id: str) -> None:
    """补岗记账:直接补丁快照文件(观测用,幂等语义由 takeover 服务保证)。绝不抛异常。"""
    path = state_dir(owner_home) / f"{watch_id}.json"
    report = read_json_object_report(path, context="watch_state.respawn")
    if report.load_error is not None:
        return
    payload = report.payload
    payload["respawn_count"] = int(payload.get("respawn_count") or 0) + 1
    payload["last_respawn_at"] = time.time()
    payload["last_respawn_takeover_run_id"] = str(takeover_run_id or "")
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def audit_append(state: WatchState, record: dict[str, Any]) -> None:
    """初筛账目(.ndjson,非上报面):每次 drain 的覆盖区间/候选/溢出/组计数,漏报可归因。"""
    path = state_dir(state.owner_home) / f"{state.watch_id}.audit.ndjson"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


__all__ = [
    "WatchRegistry",
    "WatchState",
    "audit_append",
    "list_states",
    "load_state",
    "new_state",
    "persist_state",
    "record_respawn",
    "refresh_scalars_from_disk",
    "registry",
    "reopen_on_disk",
    "state_dir",
    "watch_id_for",
]
