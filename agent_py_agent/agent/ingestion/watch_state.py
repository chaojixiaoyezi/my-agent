"""盯守状态:每路 watch 的游标+引擎+账目,进程内注册表 + owner 盘上快照。

审计文件用 .ndjson 后缀且落在 owner_home/watch_state/(不在 tasks/ 交付面):
对账脚本只扫"上报面",原始拉取/初筛账目绝不能混进去虚增误报。
"""

from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha1, sha256
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
    # source_id 是 owner 范围内长期稳定的数据源身份；watch_id 才是它在某次
    # Audit 中的运行绑定。说明和文档只保存引用，不复制成 Audit 专用知识库。
    source_id: str
    tuning: IngestTuning
    engine: StreamDigestEngine
    owner_id: str = ""
    source_profile_ref: str = ""
    document_refs: list[str] = field(default_factory=list)
    source_config_version: str = ""
    # External source progress is deliberately opaque to the host.  Legacy
    # cursor/file sources leave this empty; a prepare-learned adapter can keep
    # a time window, string token, page/offset/range or several parameters here.
    # The local integer cursor remains the monotonic record ordinal used by the
    # spool and source_ref ledger.
    source_checkpoint: dict[str, Any] = field(default_factory=dict)
    cursor: int = 0
    opened_at: float = 0.0
    watch_window_seconds: int = 0
    # LLM: A nonzero value proves the one final source read at the collection
    # deadline committed through the same cursor/spool transaction as normal reads.
    # 函数用途: 窗口到点先补拉最后一个轮询间隔；该事实随事务落盘，崩溃接管后不重复补拉。
    window_finalized_at: float = 0.0
    closed: bool = False
    closed_at: float = 0.0
    close_reason: str = ""
    close_pending_records: int = 0
    totals: dict[str, int] = field(
        default_factory=lambda: {
            "pulls": 0,
            "http_errors": 0,
            "gap_events": 0,
            "spool_candidates": 0,
            "source_duplicates": 0,
            "backpressure_skips": 0,
            "disk_backpressure_skips": 0,
            "page_limit_reductions": 0,
        }
    )
    last_pull_at: float = 0.0
    last_reached_end: bool = False
    last_error: str = ""
    # 最近一次来源失败的 typed code；与文本分开持久化，避免跨线程/跨进程后
    # 一律降级成 NETWORK_REQUEST_FAILED 或 UNKNOWN_ERROR。
    last_error_code: str = ""
    # Current accepted-ingest rate uses a short durable mechanical window.
    # Lifetime average is not operational health: after a finite feed stops it
    # stays positive for hours and falsely suggests that backlog is growing.
    ingest_window_started_at: float = 0.0
    ingest_window_start_records: int = 0
    ingest_records_per_second: float = 0.0
    ingest_rate_updated_at: float = 0.0
    # 后台收割 spool:已写入的候选批记录序号 + 轮转世代(harvester.py 单写者)。
    spool_seq: int = 0
    spool_generation: int = 0
    # LLM: The durable harvest commit id proves which fetch-to-spool transaction
    # reached the canonical state snapshot; cursor values alone are ambiguous.
    # 函数用途: 崩溃恢复只认结构化提交编号，不靠游标大小猜测半次收割是否已提交。
    harvest_commit_id: str = ""
    # 源信封只保存采集结构事实，例如记录列表键、游标键和记录边界；不保存设备、
    # 告警类型或怎样判断等业务语义。
    source_envelope: dict[str, Any] = field(default_factory=dict)
    # 源形态:""=cursor(HTTP 游标流,默认)/"poll"(快照接口定时查);file 源由
    # source_url 的 file:// 前缀判定,不占本字段。随 watch 持久化。
    source_mode: str = ""
    # file 源的行号游标(stream_pos=行号,1-based;字节偏移在 cursor);其余源恒 0。
    line_cursor: int = 0
    # file 源的物理身份与尾部未完成记录账。原始片段单独保存在 owner 范围内的
    # `<watch_id>.fragment.bin`；快照只存位置、字节数和哈希，避免把大段原文复制进状态 JSON。
    file_identity: str = ""
    file_fragment_start: int = 0
    file_fragment_bytes: int = 0
    file_fragment_sha256: str = ""
    # poll 源最近一次真的查询接口的时刻(节拍闸:距今不足 poll_query_seconds 不再查)。
    last_poll_at: float = 0.0
    # 普通 watch 可保存判读提示；/audit 的语义只来自当前命名任务目标，不使用这里。
    judgment_note: str = ""
    # 普通 watch 可保存筛选 spec；/audit 必须全量进入耐久队列，因此该模式恒为 None。
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
    # 准备探针属于准确命名的 Audit，但还不是正式保证档。单独保存这条 typed
    # scope，避免两个 Audit 检查同一 URL 时覆盖彼此的探针证据。
    prepare_root_task_id: str = ""
    # /audit 根任务身份只用于隔离同一 owner 对同一 URL 的多次独立审计、精确 clear 和对账。
    # 不能从 URL、名称或自然语言反推；主任务与所有后代共享同一个 typed request id。
    audit_root_task_id: str = ""
    # Host-owned run generation for one stable named Audit. Zero denotes the
    # pre-epoch legacy namespace; new activations are positive and isolated.
    audit_run_epoch: int = 0
    # 用户交给本次 /audit 的原始任务正文。它只是无历史判读模型的背景，不参与程序决策。
    audit_objective: str = ""
    # 当前 run 启动命令的原始正文。它可补充本轮执行意图，但不得覆盖
    # prepare 已发布的判据和传输绑定。
    audit_run_prompt: str = ""
    # 第一次打开本来源的专属子代理任务正文。它由协调代理针对现场刚学会的这一条
    # 来源编写，程序只原样保存并在同一逻辑岗位补岗时复用，不解析其中语义。
    source_task_goal: str = ""
    # 保证档启用时刻的引擎有损计数基线(suppressed/overflow/audit_throttled):新开即
    # 保证档时全 0;老 watch 升级时快照当前值——覆盖回执的 dropped 只算启用之后的增量
    # (启用后这些计数就不该再涨,涨了=违约,回执如实亮红)。
    audit_baseline: dict[str, int] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


def _unique_tmp(path: Path) -> Path:
    """原子写的临时文件名带【进程+线程】唯一后缀:同一 watch 的快照会被多写者并发落盘
    (open/pull 工具线程与收割线程各自 persist;补丁写还可能来自别的进程)。固定 tmp 名
    会互相把对方刚写好的 tmp replace 走 → FileNotFoundError(真机月级长跑必现的竞态)。
    replace 本身仍原子,后写者赢,单调字段由读-合并保住。"""
    import os
    import threading

    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


# LLM: Watch identity is deterministic from owner/source plus an optional trusted audit root id, never model prose.
# 函数用途: 普通盯守复用历史编号，独立 Audit 即使地址相同也得到互不干扰的游标和账本。
def watch_id_for(owner_home: Path, source_url: str, audit_root_task_id: str = "") -> str:
    """Build a stable owner/source id, optionally scoped to one explicit audit task.

    Ordinary watches retain the historical id.  A named/detached audit gets an
    independent cursor and ledger even when an earlier audit used the same URL.
    """
    audit_scope = str(audit_root_task_id or "").strip()
    identity = f"{owner_home}|{source_url}"
    if audit_scope:
        identity = f"{identity}|audit:{audit_scope}"
    digest = sha1(identity.encode()).hexdigest()
    return f"ws-{digest[:10]}"


# LLM: A source id is derived only from the owner-scoped canonical transport
# address when the caller does not already have a durable id.
# 函数用途: 为尚未登记名字的数据源生成稳定默认编号，不从来源名称或日志语义推断。
def source_id_for(owner_home: Path, source_url: str) -> str:
    digest = sha256(f"{Path(owner_home)}\0{source_url}".encode()).hexdigest()
    return f"src-{digest[:16]}"


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


# LLM: Callers may supply only an already-derived stable watch id; this constructor does not infer audit lineage.
# 函数用途: 建立一条新盯守的内存状态，并沿用统一调参和持久化结构。
def new_state(
    owner_home: Path,
    source_url: str,
    params: dict[str, object],
    *,
    watch_id: str = "",
) -> WatchState:
    tuning = tuning_from_params(params)
    state = WatchState(
        watch_id=str(watch_id or watch_id_for(owner_home, source_url)),
        owner_home=Path(owner_home),
        source_url=source_url,
        source_id=source_id_for(owner_home, source_url),
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
    """快照原子写盘(tmp+rename);包含引擎画像/census,重启后从游标+温启动续。"""
    path = state_dir(state.owner_home) / f"{state.watch_id}.json"
    disk = _disk_merge_facts(path)
    _merge_monotonic_disk_state(state, disk)
    _write_state_payload(path, _state_payload(state, disk))


def persist_source_binding_revision(state: WatchState) -> None:
    """Persist one published same-source runtime revision.

    Normal snapshots treat source profile/config facts as disk-authoritative so
    a stale harvester cannot roll them back.  This is the single inverse
    transition used after the named Audit has committed a newer revision: it
    still merges every monotonic progress fact, but deliberately lets the
    caller's profile, documents and config version replace their old values.
    The caller holds ``state.lock`` and has already verified that source
    identity, URL, mode and committed record boundary did not change.
    """

    path = state_dir(state.owner_home) / f"{state.watch_id}.json"
    profile_ref = state.source_profile_ref
    document_refs = list(state.document_refs)
    config_version = state.source_config_version
    disk = _disk_merge_facts(path)
    for key in (
        "source_profile_ref",
        "document_refs",
        "source_config_version",
    ):
        disk.pop(key, None)
    _merge_monotonic_disk_state(state, disk)
    state.source_profile_ref = profile_ref
    state.document_refs = document_refs
    state.source_config_version = config_version
    _write_state_payload(path, _state_payload(state, disk))


def _merge_monotonic_disk_state(state: WatchState, disk: dict[str, Any]) -> None:
    """Preserve facts another process may have advanced before snapshot replace."""

    state.window_finalized_at = max(
        state.window_finalized_at,
        float(disk.get("window_finalized_at") or 0.0),
    )
    state.closed = bool(state.closed or disk.get("closed"))
    disk_closed_at = float(disk.get("closed_at") or 0.0)
    if disk_closed_at > 0:
        state.closed_at = disk_closed_at
        state.close_reason = str(disk.get("close_reason") or "")
        state.close_pending_records = max(
            0,
            int(disk.get("close_pending_records") or 0),
        )
    state.audit_root_task_id = state.audit_root_task_id or str(disk.get("audit_root_task_id") or "")
    state.prepare_root_task_id = state.prepare_root_task_id or str(
        disk.get("prepare_root_task_id") or ""
    )
    _merge_audit_run_fields(state, disk)
    disk_ingest_updated_at = float(disk.get("ingest_rate_updated_at") or 0.0)
    if disk_ingest_updated_at > state.ingest_rate_updated_at:
        state.ingest_window_started_at = float(disk.get("ingest_window_started_at") or 0.0)
        state.ingest_window_start_records = max(
            0,
            int(disk.get("ingest_window_start_records") or 0),
        )
        state.ingest_records_per_second = max(
            0.0,
            float(disk.get("ingest_records_per_second") or 0.0),
        )
        state.ingest_rate_updated_at = disk_ingest_updated_at
    if state.audit_guarantee or bool(disk.get("audit_guarantee")):
        state.owner_id = str(disk.get("owner_id") or state.owner_id)
        state.source_id = str(disk.get("source_id") or state.source_id)
        state.source_profile_ref = str(disk.get("source_profile_ref") or state.source_profile_ref)
        disk_document_refs = disk.get("document_refs")
        if isinstance(disk_document_refs, list) and disk_document_refs:
            state.document_refs = [str(item) for item in disk_document_refs if str(item).strip()]
        state.source_config_version = str(
            disk.get("source_config_version") or state.source_config_version
        )


def _merge_audit_run_fields(state: WatchState, disk: dict[str, Any]) -> None:
    """Merge one run's prose only with facts from that exact/newer epoch.

    Cursor and ledgers survive a named-Audit continuation, while the previous
    run's objective/prompt must not overwrite the newly activated run merely
    because those strings are non-empty on disk.
    """

    disk_epoch = max(0, int(disk.get("audit_run_epoch") or 0))
    state_epoch = max(0, int(state.audit_run_epoch or 0))
    if disk_epoch > state_epoch:
        state.audit_run_epoch = disk_epoch
        state.audit_objective = str(disk.get("audit_objective") or "")
        state.audit_run_prompt = str(disk.get("audit_run_prompt") or "")
        state.source_task_goal = str(disk.get("source_task_goal") or "")
        return
    if disk_epoch < state_epoch:
        return
    state.audit_objective = state.audit_objective or str(disk.get("audit_objective") or "")
    state.audit_run_prompt = state.audit_run_prompt or str(disk.get("audit_run_prompt") or "")
    state.source_task_goal = state.source_task_goal or str(disk.get("source_task_goal") or "")


def _state_payload(state: WatchState, disk: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "watch_id": state.watch_id,
        "source_url": state.source_url,
        "owner_id": state.owner_id,
        "source_id": state.source_id,
        "source_profile_ref": state.source_profile_ref,
        "document_refs": list(state.document_refs),
        "source_config_version": state.source_config_version,
        "source_checkpoint": deepcopy(state.source_checkpoint),
        "cursor": state.cursor,
        "opened_at": state.opened_at,
        "watch_window_seconds": state.watch_window_seconds,
        "window_finalized_at": state.window_finalized_at,
        "closed": state.closed,
        "closed_at": state.closed_at,
        "close_reason": state.close_reason,
        "close_pending_records": state.close_pending_records,
        "totals": dict(state.totals),
        "last_pull_at": state.last_pull_at,
        "last_reached_end": state.last_reached_end,
        "last_error": state.last_error,
        "last_error_code": state.last_error_code,
        "ingest_window_started_at": state.ingest_window_started_at,
        "ingest_window_start_records": state.ingest_window_start_records,
        "ingest_records_per_second": state.ingest_records_per_second,
        "ingest_rate_updated_at": state.ingest_rate_updated_at,
        "spool_seq": state.spool_seq,
        "spool_generation": state.spool_generation,
        "harvest_commit_id": state.harvest_commit_id,
        "source_mode": state.source_mode,
        "line_cursor": state.line_cursor,
        "file_identity": state.file_identity,
        "file_fragment_start": state.file_fragment_start,
        "file_fragment_bytes": state.file_fragment_bytes,
        "file_fragment_sha256": state.file_fragment_sha256,
        "last_poll_at": state.last_poll_at,
        "judgment_note": state.judgment_note,
        "source_envelope": dict(state.source_envelope),
        "source_spec": dict(state.source_spec) if state.source_spec else None,
        "last_sample_digest": dict(state.last_sample_digest),
        "feedback_offset": state.feedback_offset,
        # 保证档同 closed 一样单调合并:别的进程(open 升级)置的 True 不被收割线程整体覆写吃掉。
        "audit_guarantee": bool(state.audit_guarantee or disk.get("audit_guarantee")),
        "prepare_root_task_id": state.prepare_root_task_id,
        "audit_root_task_id": state.audit_root_task_id,
        "audit_run_epoch": state.audit_run_epoch,
        "audit_objective": state.audit_objective,
        "audit_run_prompt": state.audit_run_prompt,
        "source_task_goal": state.source_task_goal,
        "audit_baseline": dict(state.audit_baseline),
        "tuning": {
            k: getattr(state.tuning, k)
            for k in (
                "window_seconds",
                "bucket_seconds",
                "rare_threshold",
                "max_candidates_per_pull",
                "full_read_per_pull",
                "page_limit",
                "background_harvest",
                "harvester_idle_stop_seconds",
                "poll_query_seconds",
            )
        },
        "engine": state.engine.snapshot(time.time()),
        "saved_at": time.time(),
    }


def _write_state_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(path)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _disk_merge_facts(path: Path) -> dict[str, Any]:
    """读盘上须单调合并的事实，整体覆写前先取，防跨进程覆盖。"""
    report = read_json_object_report(path, context="watch_state.merge_read")
    if report.load_error is not None:
        return {}
    payload = report.payload
    return {
        "closed": payload.get("closed"),
        "window_finalized_at": payload.get("window_finalized_at"),
        "closed_at": payload.get("closed_at"),
        "close_reason": payload.get("close_reason"),
        "close_pending_records": payload.get("close_pending_records"),
        "audit_guarantee": payload.get("audit_guarantee"),
        "prepare_root_task_id": payload.get("prepare_root_task_id"),
        "audit_root_task_id": payload.get("audit_root_task_id"),
        "audit_run_epoch": payload.get("audit_run_epoch"),
        "audit_objective": payload.get("audit_objective"),
        "audit_run_prompt": payload.get("audit_run_prompt"),
        "source_task_goal": payload.get("source_task_goal"),
        "source_id": payload.get("source_id"),
        "owner_id": payload.get("owner_id"),
        "source_profile_ref": payload.get("source_profile_ref"),
        "document_refs": payload.get("document_refs"),
        "source_config_version": payload.get("source_config_version"),
        "ingest_window_started_at": payload.get("ingest_window_started_at"),
        "ingest_window_start_records": payload.get("ingest_window_start_records"),
        "ingest_records_per_second": payload.get("ingest_records_per_second"),
        "ingest_rate_updated_at": payload.get("ingest_rate_updated_at"),
    }


def refresh_scalars_from_disk(state: WatchState) -> None:
    """从盘上快照回灌覆盖类标量(游标/账目/closed/spool 序号)——供「收割者在别的进程」
    时的 pull 侧展示新鲜覆盖;不动引擎画像(引擎归收割者)。调用方自行持锁。"""
    path = state_dir(state.owner_home) / f"{state.watch_id}.json"
    report = read_json_object_report(path, context="watch_state.refresh")
    if report.load_error is not None:
        return
    payload = report.payload
    _refresh_source_position(state, payload)
    _refresh_durable_totals(state, payload)
    _refresh_audit_identity(state, payload)
    _refresh_ingest_rate(state, payload)
    _refresh_engine_totals(state, payload)


def _refresh_source_position(state: WatchState, payload: dict[str, Any]) -> None:
    checkpoint = payload.get("source_checkpoint")
    if isinstance(checkpoint, dict):
        state.source_checkpoint = deepcopy(checkpoint)
    state.cursor = max(state.cursor, int(payload.get("cursor") or 0))
    state.line_cursor = max(state.line_cursor, int(payload.get("line_cursor") or 0))
    state.window_finalized_at = max(
        state.window_finalized_at,
        float(payload.get("window_finalized_at") or 0.0),
    )
    if "file_identity" in payload:
        state.file_identity = str(payload.get("file_identity") or "")
    if "file_fragment_start" in payload:
        state.file_fragment_start = int(payload.get("file_fragment_start") or 0)
    if "file_fragment_bytes" in payload:
        state.file_fragment_bytes = int(payload.get("file_fragment_bytes") or 0)
    if "file_fragment_sha256" in payload:
        state.file_fragment_sha256 = str(payload.get("file_fragment_sha256") or "")
    state.last_reached_end = bool(payload.get("last_reached_end"))
    state.closed = bool(state.closed or payload.get("closed"))
    disk_closed_at = float(payload.get("closed_at") or 0.0)
    if disk_closed_at > 0:
        state.closed_at = disk_closed_at
        state.close_reason = str(payload.get("close_reason") or "")
        state.close_pending_records = max(
            0,
            int(payload.get("close_pending_records") or 0),
        )
    state.spool_seq = max(state.spool_seq, int(payload.get("spool_seq") or 0))
    state.spool_generation = max(state.spool_generation, int(payload.get("spool_generation") or 0))


def _refresh_durable_totals(state: WatchState, payload: dict[str, Any]) -> None:
    """Merge monotonic counters written by harvesters in another process."""

    disk_totals = payload.get("totals")
    if isinstance(disk_totals, dict):
        for key, raw_value in disk_totals.items():
            try:
                value = int(raw_value or 0)
            except (TypeError, ValueError):
                continue
            state.totals[str(key)] = max(
                int(state.totals.get(str(key), 0) or 0),
                value,
            )
    state.harvest_commit_id = str(payload.get("harvest_commit_id") or "") or state.harvest_commit_id


def _refresh_audit_identity(state: WatchState, payload: dict[str, Any]) -> None:
    state.audit_guarantee = bool(state.audit_guarantee or payload.get("audit_guarantee"))
    state.prepare_root_task_id = state.prepare_root_task_id or str(
        payload.get("prepare_root_task_id") or ""
    )
    state.audit_root_task_id = state.audit_root_task_id or str(
        payload.get("audit_root_task_id") or ""
    )
    _merge_audit_run_fields(state, payload)
    state.source_id = str(payload.get("source_id") or state.source_id)
    state.owner_id = str(payload.get("owner_id") or state.owner_id)
    state.source_profile_ref = str(payload.get("source_profile_ref") or state.source_profile_ref)
    disk_document_refs = payload.get("document_refs")
    if isinstance(disk_document_refs, list) and disk_document_refs:
        state.document_refs = [str(item) for item in disk_document_refs if str(item).strip()]
    state.source_config_version = str(
        payload.get("source_config_version") or state.source_config_version
    )
    state.last_error = str(payload.get("last_error") or "") or state.last_error
    state.last_error_code = str(payload.get("last_error_code") or "") or state.last_error_code


def _refresh_ingest_rate(state: WatchState, payload: dict[str, Any]) -> None:
    disk_ingest_updated_at = float(payload.get("ingest_rate_updated_at") or 0.0)
    if disk_ingest_updated_at > state.ingest_rate_updated_at:
        state.ingest_window_started_at = float(payload.get("ingest_window_started_at") or 0.0)
        state.ingest_window_start_records = max(
            0,
            int(payload.get("ingest_window_start_records") or 0),
        )
        state.ingest_records_per_second = max(
            0.0,
            float(payload.get("ingest_records_per_second") or 0.0),
        )
        state.ingest_rate_updated_at = disk_ingest_updated_at
    disk_spec = payload.get("source_spec")
    if isinstance(disk_spec, dict) and disk_spec and not state.source_spec:
        state.source_spec = dict(disk_spec)  # 展示用(引擎归收割者进程,这里不 apply)


def _refresh_engine_totals(state: WatchState, payload: dict[str, Any]) -> None:
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
    state = new_state(
        owner_home,
        source_url,
        dict(payload.get("tuning") or {}),
        watch_id=str(payload.get("watch_id") or watch_id),
    )
    state.cursor = int(payload.get("cursor") or 0)
    state.owner_id = str(payload.get("owner_id") or "")
    state.source_id = str(payload.get("source_id") or source_id_for(owner_home, source_url))
    state.source_profile_ref = str(payload.get("source_profile_ref") or "")
    raw_document_refs = payload.get("document_refs")
    state.document_refs = (
        [str(item) for item in raw_document_refs if str(item).strip()]
        if isinstance(raw_document_refs, list)
        else []
    )
    state.source_config_version = str(payload.get("source_config_version") or "")
    checkpoint = payload.get("source_checkpoint")
    state.source_checkpoint = deepcopy(checkpoint) if isinstance(checkpoint, dict) else {}
    state.opened_at = float(payload.get("opened_at") or time.time())
    state.watch_window_seconds = int(payload.get("watch_window_seconds") or 0)
    state.window_finalized_at = float(payload.get("window_finalized_at") or 0.0)
    state.closed = bool(payload.get("closed"))
    state.closed_at = float(payload.get("closed_at") or 0.0)
    state.close_reason = str(payload.get("close_reason") or "")
    state.close_pending_records = max(
        0,
        int(payload.get("close_pending_records") or 0),
    )
    state.last_pull_at = float(payload.get("last_pull_at") or 0.0)
    state.last_reached_end = bool(payload.get("last_reached_end"))
    state.last_error = str(payload.get("last_error") or "")
    state.last_error_code = str(payload.get("last_error_code") or "")
    state.ingest_window_started_at = float(payload.get("ingest_window_started_at") or 0.0)
    state.ingest_window_start_records = max(
        0,
        int(payload.get("ingest_window_start_records") or 0),
    )
    state.ingest_records_per_second = max(
        0.0,
        float(payload.get("ingest_records_per_second") or 0.0),
    )
    state.ingest_rate_updated_at = float(payload.get("ingest_rate_updated_at") or 0.0)
    state.spool_seq = int(payload.get("spool_seq") or 0)
    state.spool_generation = int(payload.get("spool_generation") or 0)
    state.harvest_commit_id = str(payload.get("harvest_commit_id") or "")
    state.source_mode = str(payload.get("source_mode") or "")
    state.line_cursor = int(payload.get("line_cursor") or 0)
    state.file_identity = str(payload.get("file_identity") or "")
    state.file_fragment_start = int(payload.get("file_fragment_start") or 0)
    state.file_fragment_bytes = int(payload.get("file_fragment_bytes") or 0)
    state.file_fragment_sha256 = str(payload.get("file_fragment_sha256") or "")
    state.last_poll_at = float(payload.get("last_poll_at") or 0.0)
    state.judgment_note = str(payload.get("judgment_note") or "")
    envelope = payload.get("source_envelope")
    state.source_envelope = dict(envelope) if isinstance(envelope, dict) else {}
    sample_digest = payload.get("last_sample_digest")
    state.last_sample_digest = dict(sample_digest) if isinstance(sample_digest, dict) else {}
    state.feedback_offset = int(payload.get("feedback_offset") or 0)
    state.audit_guarantee = bool(payload.get("audit_guarantee"))
    state.prepare_root_task_id = str(payload.get("prepare_root_task_id") or "")
    state.audit_root_task_id = str(payload.get("audit_root_task_id") or "")
    state.audit_run_epoch = max(0, int(payload.get("audit_run_epoch") or 0))
    state.audit_objective = str(payload.get("audit_objective") or "")
    state.audit_run_prompt = str(payload.get("audit_run_prompt") or "")
    state.source_task_goal = str(payload.get("source_task_goal") or "")
    baseline = payload.get("audit_baseline")
    state.audit_baseline = (
        {str(k): int(v) for k, v in baseline.items()} if isinstance(baseline, dict) else {}
    )
    if state.audit_guarantee:
        state.source_spec = None
        state.judgment_note = ""
        state.last_sample_digest = {}
        state.engine.apply_spec(None)
    else:
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
        if report.load_error is not None:
            continue
        watch_id = str(report.payload.get("watch_id") or "")
        if watch_id and path.name == f"{watch_id}.json":
            rows.append(_list_row(report.payload))
    return rows


def _list_row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "watch_id": str(payload.get("watch_id") or ""),
        "source_url": str(payload.get("source_url") or ""),
        "source_id": str(payload.get("source_id") or ""),
        "owner_id": str(payload.get("owner_id") or ""),
        "source_profile_ref": str(payload.get("source_profile_ref") or ""),
        "document_refs": list(payload.get("document_refs") or []),
        "source_config_version": str(payload.get("source_config_version") or ""),
        "cursor": int(payload.get("cursor") or 0),
        "opened_at": float(payload.get("opened_at") or 0.0),
        "watch_window_seconds": int(payload.get("watch_window_seconds") or 0),
        "window_finalized_at": float(payload.get("window_finalized_at") or 0.0),
        "closed": bool(payload.get("closed")),
        "closed_at": float(payload.get("closed_at") or 0.0),
        "close_reason": str(payload.get("close_reason") or ""),
        "close_pending_records": max(
            0,
            int(payload.get("close_pending_records") or 0),
        ),
        "last_pull_at": float(payload.get("last_pull_at") or 0.0),
        "last_reached_end": bool(payload.get("last_reached_end")),
        "audit_guarantee": bool(payload.get("audit_guarantee")),
        "prepare_root_task_id": str(payload.get("prepare_root_task_id") or ""),
        "audit_root_task_id": str(payload.get("audit_root_task_id") or ""),
        "audit_run_epoch": max(0, int(payload.get("audit_run_epoch") or 0)),
        "source_task_goal_bound": bool(str(payload.get("source_task_goal") or "").strip()),
        "totals": dict(payload.get("totals") or {}),
    }


# LLM: This is the single watch close lifecycle. It snapshots the exact
# unjudged backlog before atomically persisting cancellation facts, then stops
# only ephemeral harvest/lease state and leaves raw evidence intact.
# 函数用途: 关闭一条 watch 时统一记录时间、原因和未完成数量，并停采集、撤租约，但不删除原始记录。
def close_watch_state(
    state: WatchState,
    *,
    reason: str,
) -> dict[str, object]:
    selected_reason = str(reason or "").strip()
    if selected_reason not in {
        "named_audit_clear",
        "watch_tool_close",
        "audit_parent_inactive",
        "audit_prepare_published",
        "audit_run_superseded",
        "audit_source_membership_removed",
        "audit_window_settled",
        "audit_window_incomplete",
    }:
        raise ValueError(f"unknown watch close reason: {selected_reason}")
    from .harvester import spool_backlog_facts, stop_harvester
    from .source_worker import clear_source_worker_lease

    with state.lock:
        refresh_scalars_from_disk(state)
        if not state.closed or state.closed_at <= 0:
            backlog = spool_backlog_facts(state)
            state.closed = True
            state.closed_at = time.time()
            state.close_reason = selected_reason
            state.close_pending_records = max(
                0,
                int(backlog.get("candidates_unjudged") or 0),
            )
            persist_state(state)
    stop_harvester(state.watch_id)
    clear_source_worker_lease(state.owner_home, state.watch_id)
    registry.drop(state.watch_id)
    return {
        "watch_id": state.watch_id,
        "closed_at": state.closed_at,
        "close_reason": state.close_reason,
        "pending_records": state.close_pending_records,
    }


# LLM: Named audit cancellation selects watches only by the exact typed root
# task id and delegates each selected watch to the canonical close lifecycle.
# 函数用途: 停止一个命名 Audit 时只关闭它自己的数据流，并保留所有原始记录供以后审计。
def close_audit_watches_for_task(
    owner_home: Path,
    task_id: str,
    *,
    reason: str = "named_audit_clear",
) -> tuple[str, ...]:
    selected_task = str(task_id or "").strip()
    if not selected_task:
        return ()
    closed: list[str] = []
    for row in list_states(owner_home):
        if str(row.get("audit_root_task_id") or "") != selected_task or bool(row.get("closed")):
            continue
        watch_id = str(row.get("watch_id") or "")
        state = registry.get_or_load(owner_home, watch_id)
        if state is None:
            continue
        if state.audit_root_task_id != selected_task:
            continue
        close_watch_state(state, reason=reason)
        closed.append(watch_id)
    return tuple(closed)


# LLM: Prepare probes are transient transport checks. Once their exact facts
# have been published into the named Audit, they must stop harvesting while
# their raw evidence remains on disk for later inspection.
# 函数用途: 按可信 prepare_root_task_id 回收一个命名 Audit 已发布的临时探针，避免正式
# Audit 启动后同一来源同时存在探针和来源工作者两条采集链。
def close_prepare_watches_for_task(
    owner_home: Path,
    task_id: str,
) -> tuple[str, ...]:
    selected_task = str(task_id or "").strip()
    if not selected_task:
        return ()
    closed: list[str] = []
    for row in list_states(owner_home):
        if (
            str(row.get("prepare_root_task_id") or "") != selected_task
            or bool(row.get("audit_guarantee"))
            or bool(row.get("closed"))
        ):
            continue
        watch_id = str(row.get("watch_id") or "")
        state = registry.get_or_load(owner_home, watch_id)
        if state is None:
            continue
        if state.prepare_root_task_id != selected_task or state.audit_guarantee:
            continue
        close_watch_state(state, reason="audit_prepare_published")
        closed.append(watch_id)
    return tuple(closed)


def reopen_on_disk(state: WatchState) -> None:
    """用户显式 re-open:把盘上快照的 closed 翻回 False。
    persist_state 的单调合并(盘上 closed=True 不被覆写翻回)只该防【收割线程整体覆写】
    吃掉别进程的 close;显式 open 是用户意图,必须能重开——真机实锤:close 过的源重启后
    再 open,内存态刚置 False 就被盘上旧 True 合并回去,收割线程按 closed 自停,盯守空转。
    绝不抛异常。"""
    path = state_dir(state.owner_home) / f"{state.watch_id}.json"
    report = read_json_object_report(path, context="watch_state.reopen")
    if report.load_error is not None:
        return
    payload = report.payload
    disk_opened_at = float(payload.get("opened_at") or 0.0)
    reset_window = (
        float(state.opened_at or 0.0) > disk_opened_at
        and float(state.window_finalized_at or 0.0) <= 0
    )
    if not payload.get("closed") and not reset_window:
        return
    payload["closed"] = False
    payload["closed_at"] = 0.0
    payload["close_reason"] = ""
    payload["close_pending_records"] = 0
    if reset_window:
        # LLM: Explicitly reopening an expired window is the only operation
        # allowed to clear the otherwise monotonic boundary-commit marker.
        # 函数用途: 新一场窗口不能继承上一场“已最终补拉”事实，否则到点会少拉最后一拍。
        payload["window_finalized_at"] = 0.0
    try:
        tmp = _unique_tmp(path)
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        tmp.replace(path)
    except OSError:
        return


def audit_append(state: WatchState, record: dict[str, Any]) -> bool:
    """初筛账目(.ndjson,非上报面):每次 drain 的覆盖区间/候选/溢出/组计数,漏报可归因。

    返回值属于持久化契约：调用方只有看到 True 才能提交对应来源游标。
    """
    path = state_dir(state.owner_home) / f"{state.watch_id}.audit.ndjson"
    payload = dict(record)
    if state.audit_guarantee:
        payload.update(
            {
                "owner_id": state.owner_id,
                "audit_id": state.audit_root_task_id,
                "source_id": state.source_id,
                "watch_id": state.watch_id,
                "audit_run_epoch": max(0, int(state.audit_run_epoch or 0)),
            }
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return False
    return True


__all__ = [
    "WatchRegistry",
    "WatchState",
    "audit_append",
    "close_audit_watches_for_task",
    "close_prepare_watches_for_task",
    "close_watch_state",
    "list_states",
    "load_state",
    "new_state",
    "persist_source_binding_revision",
    "persist_state",
    "refresh_scalars_from_disk",
    "registry",
    "reopen_on_disk",
    "state_dir",
    "source_id_for",
    "watch_id_for",
]
