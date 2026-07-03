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
    totals: dict[str, int] = field(default_factory=lambda: {"pulls": 0, "http_errors": 0, "gap_events": 0, "spool_candidates": 0})
    last_pull_at: float = 0.0
    last_reached_end: bool = False
    last_error: str = ""
    # 岗位归属:最近一次 pull 这路流的 run(编队补岗的结构化事实来源)。
    last_puller_run_id: str = ""
    respawn_count: int = 0
    last_respawn_at: float = 0.0
    # 后台收割 spool:已写入的候选批记录序号 + 轮转世代(harvester.py 单写者)。
    spool_seq: int = 0
    spool_generation: int = 0
    # 源信封元数据(open 探针抓取的标量字段,如 schema_note/api 名):原样透传给模型,
    # 让它锚定【源自带的结果端判据说明】——真机实锤:puller 只取 items,信封被丢,
    # 有的盯守子代理自立判据把迷惑项当命中报(B 路 20+ 误报)。代码不解读内容,只搬运。
    source_envelope: dict[str, Any] = field(default_factory=dict)
    # per-源判据 spec(模型从样本学出、action=configure 灌入的结构化判据):每源一份、
    # 随 watch 持久化,重启/补岗自动回灌引擎;None=未学(引擎走通用兜底车道)。
    source_spec: dict[str, Any] | None = None
    # 最近一次 sample 的每字段取值分布(纯计数,configure 校验 target 频次用:真机实锤
    # 模型会把样本里的高频常态取值配成 target → 全误报;有样本证据时结构化拒配)。
    last_sample_digest: dict[str, Any] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


def watch_id_for(owner_home: Path, source_url: str) -> str:
    digest = sha1(f"{owner_home}|{source_url}".encode("utf-8")).hexdigest()
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
        "respawn_count": respawn_count,
        "last_respawn_at": max(state.last_respawn_at, float(disk.get("last_respawn_at") or 0.0)),
        "spool_seq": state.spool_seq,
        "spool_generation": state.spool_generation,
        "source_envelope": dict(state.source_envelope),
        "source_spec": dict(state.source_spec) if state.source_spec else None,
        "last_sample_digest": dict(state.last_sample_digest),
        "tuning": {k: getattr(state.tuning, k) for k in ("window_seconds", "bucket_seconds", "rare_threshold", "max_candidates_per_pull", "page_limit", "background_harvest", "harvester_idle_stop_seconds")},
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
    state.last_reached_end = bool(payload.get("last_reached_end"))
    state.closed = bool(state.closed or payload.get("closed"))
    state.spool_seq = max(state.spool_seq, int(payload.get("spool_seq") or 0))
    state.spool_generation = max(state.spool_generation, int(payload.get("spool_generation") or 0))
    state.respawn_count = max(state.respawn_count, int(payload.get("respawn_count") or 0))
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
    state.respawn_count = int(payload.get("respawn_count") or 0)
    state.last_respawn_at = float(payload.get("last_respawn_at") or 0.0)
    state.spool_seq = int(payload.get("spool_seq") or 0)
    state.spool_generation = int(payload.get("spool_generation") or 0)
    envelope = payload.get("source_envelope")
    state.source_envelope = dict(envelope) if isinstance(envelope, dict) else {}
    sample_digest = payload.get("last_sample_digest")
    state.last_sample_digest = dict(sample_digest) if isinstance(sample_digest, dict) else {}
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
        "respawn_count": int(payload.get("respawn_count") or 0),
        "totals": dict(payload.get("totals") or {}),
    }


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
    "state_dir",
    "watch_id_for",
]
