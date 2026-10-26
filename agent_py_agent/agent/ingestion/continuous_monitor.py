# LLM: 连续证据共用折叠算法；文件评估以有界头摘要守护增量游标，不写证据、不认证并发写者。
# 模块用途: 监督生产 watch 并评估连续证明，不生成模拟证据。
"""生产 watch 长守监督与连续证据，不生成模拟事件、不读取专项 harness answer-key。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_py_agent.agent.ingestion import watch_state
from agent_py_agent.agent.ingestion.harvester import ensure_harvester
from agent_py_agent.agent.ingestion.source_http import SourceHttpRequest
from agent_py_agent.agent.ingestion.watch_state import (
    list_states,
    load_state,
    refresh_scalars_from_disk,
)
from agent_py_agent.agent.ingestion.watch_tool import WatchStreamTool

from ..common.json_io import jsonl_lines
from ..common.nofollow_fs import open_readonly_file_beneath

_HEAD_DIGEST_BYTES = 4096


@dataclass(frozen=True)
class ContinuousProofPolicy:
    minimum_seconds: int = 86_400
    minimum_sources: int = 3
    minimum_signatures: int = 2
    maximum_sample_gap_seconds: int = 120
    maximum_source_staleness_seconds: int = 900


@dataclass(frozen=True)
class ProofMetrics:
    duration: int = 0
    healthy_sources: int = 0
    signatures: int = 0
    maximum_gap: float = 0.0


# LLM: 全量和流式评估共用唯一连续区间算法，只保留七个标量，不驻留历史样本或watches。
# 类用途: 以O(1)空间维护最新连续合格证据区间。
@dataclass
class _ProofAccumulator:
    has_rows: bool = False
    previous_at: float | None = None
    latest_at: float = 0.0
    segment_start: float | None = None
    segment_maximum_gap: float = 0.0
    latest_healthy: int = 0
    latest_signatures: int = 0

    # LLM: 样本按单调非递减时间折叠；不合格、负间隙和超大间隙保持原全量重置规则。
    # 函数用途: 更新区间起点、最大间隙及最新源计数。
    def push(self, observed_at: float, healthy: int, signatures: int, policy: ContinuousProofPolicy) -> None:
        self.has_rows = True
        self.latest_at = observed_at
        self.latest_healthy = healthy
        self.latest_signatures = signatures
        gap = observed_at - self.previous_at if self.previous_at is not None else 0.0
        qualifies = healthy >= policy.minimum_sources and signatures >= policy.minimum_signatures
        if not qualifies or gap < 0 or gap > policy.maximum_sample_gap_seconds:
            self.segment_start = observed_at if qualifies else None
            self.segment_maximum_gap = 0.0
        elif self.segment_start is None:
            self.segment_start = observed_at
        else:
            self.segment_maximum_gap = max(self.segment_maximum_gap, gap)
        self.previous_at = observed_at

    # LLM: 导出不可变统计，不修改折叠状态，持续时长的截断及负值处理与原实现一致。
    # 函数用途: 计算当前连续合格秒数与统计结果。
    def metrics(self) -> ProofMetrics:
        duration = max(0, int(self.latest_at - self.segment_start)) if self.segment_start is not None else 0
        return ProofMetrics(duration, self.latest_healthy, self.latest_signatures, self.segment_maximum_gap)


# LLM: 单调追加只解析新增LF字节，以最多4KiB头摘要探测增长覆写；乱序临时保留标量投影。
# 类用途: 长期以固定常驻状态增量评估一个证据文件。
class ContinuousProofFileEvaluator:
    # LLM: 每个实例独占自己的游标和累积器，构造时不访问文件、不写入或启动服务。
    # 函数用途: 创建证据文件评估器并初始化固定大小状态。
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._reset()

    # LLM: stat变化时摘要未知/不匹配均重读；跨4KiB补齐摘要，数据读取异常清缓存、语义异常继续传播。
    # 函数用途: 从新增证据或安全重算结果生成本轮公开报告。
    def evaluate(self, policy: ContinuousProofPolicy | None = None) -> dict[str, Any]:
        cfg = policy or ContinuousProofPolicy()
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            reset = self._needs_reset(identity, stamp, cfg)
            if not reset and self._committed_offset > 0 and stamp != self._last_stat_stamp:
                reset = not self._head_digest or _file_head_digest(self.path, self._committed_offset) != self._head_digest
            if reset:
                self._reset()
                self._file_identity, self._policy = identity, cfg
            previous_offset = self._committed_offset
            effective_acc = self._read_incremental(cfg)
            if effective_acc is None:
                effective_acc = self._evaluate_non_monotonic_stream(cfg)
            if self._committed_offset > 0 and (
                not self._head_digest
                or previous_offset < _HEAD_DIGEST_BYTES and self._committed_offset != previous_offset
            ):
                # 旧前缀未满4KiB时，追加跨越上限也必须将摘要覆盖范围补齐。
                self._head_digest = _file_head_digest(self.path, self._committed_offset)
        except OSError:
            self._reset()
            return _proof("no_evidence", ProofMetrics())
        except Exception:
            self._reset()
            raise
        self._last_stat_stamp = stamp
        if not effective_acc.has_rows:
            return _proof("no_evidence", ProofMetrics())
        metrics = effective_acc.metrics()
        return _proof(_proof_reason(metrics, cfg), metrics)

    # LLM: 原路径不变，清空旧文件/policy/游标/证明及字符串摘要，避免轮转后复用陈旧成功。
    # 函数用途: 初始化或丢弃文件评估缓存。
    def _reset(self) -> None:
        self._file_identity: tuple[int, int] | None = None
        self._committed_offset: int = 0
        self._last_stat_stamp: tuple[int, int, int] | None = None
        self._head_digest: str = ""
        self._policy: ContinuousProofPolicy | None = None
        self._accumulator = _ProofAccumulator()
        self._non_monotonic = False

    # LLM: 比较上次完整文件大小，补足未提交尾行存在时的同尺寸覆写/截断；正常增长不回扫历史。
    # 函数用途: 判断当前文件与策略能否继续复用提交前缀。
    def _needs_reset(self, identity, stamp, cfg: ContinuousProofPolicy) -> bool:
        changed = self._last_stat_stamp is not None and stamp != self._last_stat_stamp
        rewritten = changed and (stamp[0] == self._committed_offset or stamp[0] <= self._last_stat_stamp[0])
        return (
            identity != self._file_identity or self._policy != cfg
            or stamp[0] < self._committed_offset or rewritten or self._non_monotonic
        )

    # LLM: LF才推进游标；无LF尾行在浅副本折叠后立即结束本轮，避免同时追加把尾行续片误当新行。
    # 函数用途: 流式消费新行并返回本轮有效折叠状态。
    def _read_incremental(self, cfg: ContinuousProofPolicy) -> _ProofAccumulator | None:
        effective_acc = self._accumulator
        with self.path.open("rb") as handle:
            handle.seek(self._committed_offset)
            while raw_line := handle.readline():
                committed = raw_line.endswith(b"\n")
                if committed:
                    self._committed_offset += len(raw_line)
                row = _evidence_row(raw_line)
                if row is None and not committed:
                    return effective_acc
                if row is None:
                    continue
                observed_at = float(row.get("observed_at") or 0.0)
                if self._accumulator.previous_at is not None and observed_at < self._accumulator.previous_at:
                    self._non_monotonic = True
                    return None
                if not committed:
                    effective_acc = copy.copy(self._accumulator)
                healthy, signatures = _snapshot_health(row, cfg)
                effective_acc.push(observed_at, healthy, signatures, cfg)
                if not committed:
                    return effective_acc
        return effective_acc

    # LLM: 乱序仅在本轮保存时间/索引/两个计数，稳定排序保持相等时间的文件顺序；不驻留原始watches。
    # 函数用途: 流式重读并重算不满足单调追加假设的证据。
    def _evaluate_non_monotonic_stream(self, cfg: ContinuousProofPolicy) -> _ProofAccumulator:
        points: list[tuple[float, int, int, int]] = []
        with self.path.open("rb") as handle:
            for index, raw_line in enumerate(iter(handle.readline, b"")):
                row = _evidence_row(raw_line)
                if row is not None:
                    healthy, signatures = _snapshot_health(row, cfg)
                    points.append((float(row.get("observed_at") or 0.0), index, healthy, signatures))
        points.sort(key=lambda point: (point[0], point[1]))
        accumulator = _ProofAccumulator()
        for observed_at, _index, healthy, signatures in points:
            accumulator.push(observed_at, healthy, signatures, cfg)
        return accumulator


# LLM: 只按调用方给出的物理LF行解析；坏JSON/UTF8及非dict跳过，合法无LF尾行同样可用。
# 函数用途: 将一条证据字节行解码为快照对象。
def _evidence_row(raw_line: bytes) -> dict[str, Any] | None:
    try:
        text = (raw_line[:-1] if raw_line.endswith(b"\n") else raw_line).rstrip(b"\r").decode("utf-8")
        row = json.loads(text)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


# LLM: 复用严格nofollow只读FD，每次实际read最多4KiB；关闭自有FD，OSError/空前缀返回空串。
# 函数用途: 为已提交文件头计算固定大小SHA-256字符串，不读取整份历史。
def _file_head_digest(path: Path, limit: int) -> str:
    if limit <= 0:
        return ""
    try:
        descriptor = open_readonly_file_beneath(path.parent, (path.name,))
        try:
            head_bytes = os.read(descriptor, min(limit, _HEAD_DIGEST_BYTES))
        finally:
            os.close(descriptor)
    except OSError:
        return ""
    return hashlib.sha256(head_bytes).hexdigest() if head_bytes else ""


def discover_owner_homes(my_agent_home: Path) -> list[Path]:
    """只走规范 owner 层级，禁止 ``**`` 递归进 task/artifact 大树。"""
    root = Path(my_agent_home)
    patterns = (
        "owners/local/main",
        "owners/providers/*/users/*",
        "owners/providers/*/groups/*",
    )
    candidates = chain.from_iterable(root.glob(pattern) for pattern in patterns)
    return sorted({owner_home for owner_home in candidates if _owner_has_watch(owner_home)})


def _owner_has_watch(owner_home: Path) -> bool:
    return any((owner_home / "watch_state").glob("ws-*.json"))


# LLM: 恢复起的收割线程必须持有 registry 里的同一个状态对象：之后 pull 经 registry 取对象，ensure_harvester 只按
#   watch_id 复用活线程，若这里另从盘上读一份，同一 watch 就有两份状态各自落盘。取出后按工具路径惯例先在锁内从盘上刷新。
# 函数用途: Gateway 恢复循环为仍在运行的命名 Audit 数据源重新拉起收割线程，父任务不活跃时不拉起。
def recover_active_audit_harvesters(agent: object) -> int:
    """Recover collectors only for durably active named Audit sources.

    Ordinary ``watch_stream`` calls are turn-scoped and restart on the next
    explicit pull.  They have no parent lifecycle that authorizes a host
    restart to resume network traffic.  Named Audit sources do: their exact
    conversation task link is checked before a collector is reacquired.  An
    unavailable or inactive parent therefore fails closed.
    """
    owner_home_text = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    if not owner_home_text:
        return 0
    owner_home = Path(owner_home_text)
    started = 0
    tool = WatchStreamTool(object())
    for row in list_states(owner_home):
        if row.get("closed") or not bool(row.get("audit_guarantee")):
            continue
        state = watch_state.registry.get_or_load(owner_home, str(row.get("watch_id") or ""))
        if state is None:
            continue
        with state.lock:
            refresh_scalars_from_disk(state)
        from agent_py_agent.agent.ingestion.source_worker import (
            audit_parent_reconcile_state,
        )

        parent_active, _parent_state = audit_parent_reconcile_state(
            agent,
            state.audit_root_task_id,
        )
        if not parent_active:
            continue

        def fetch(request: SourceHttpRequest, *, home: Path = owner_home):
            return tool._fetch_json_pinned(
                request,
                (),
                None,
            )

        from agent_py_agent.agent.ingestion.source_worker import (
            settle_audit_source_worker,
            wake_audit_source_worker,
        )

        if ensure_harvester(
            state,
            fetch,
            on_records_ready=lambda ready_state: wake_audit_source_worker(
                agent,
                ready_state,
            ),
            on_window_finalized=lambda finalized_state: settle_audit_source_worker(
                agent,
                finalized_state,
            ),
        ) is not None:
            started += 1
    return started


def build_snapshot(owner_homes: list[Path], *, observed_at: float | None = None) -> dict[str, Any]:
    now = float(observed_at or time.time())
    rows: list[dict[str, Any]] = []
    for owner_home in owner_homes:
        rows.extend(_owner_snapshot_rows(owner_home))
    return {"schema_version": "continuous-watch-evidence.v1", "observed_at": now, "watches": rows}


def _owner_snapshot_rows(owner_home: Path) -> list[dict[str, Any]]:
    states = [load_state(owner_home, str(row.get("watch_id") or "")) for row in list_states(owner_home)]
    return [_state_snapshot(owner_home, state) for state in states if state is not None and not state.closed]


def _state_snapshot(owner_home: Path, state: Any) -> dict[str, Any]:
    return {
        "owner_ref": hashlib.sha256(str(owner_home).encode()).hexdigest()[:16],
        "watch_id": state.watch_id,
        "source": _source_fact(state.source_url),
        "source_mode": state.source_mode or "cursor",
        "envelope_keys": sorted(str(key) for key in state.source_envelope)[:32],
        "audit_guarantee": state.audit_guarantee,
        "opened_at": state.opened_at,
        "last_pull_at": state.last_pull_at,
        "last_source_at": max(float(state.last_pull_at or 0.0), float(state.last_poll_at or 0.0)),
        "cursor": state.cursor,
        "totals": dict(state.totals),
        "last_error_code": state.last_error.split(":", 1)[0][:80] if state.last_error else "",
    }


def _source_fact(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    origin = f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}"
    return {
        "scheme": parts.scheme.lower(),
        "origin_hash": hashlib.sha256(origin.encode()).hexdigest()[:16],
        "path_shape": hashlib.sha256(parts.path.encode()).hexdigest()[:12],
    }


def append_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")


def read_snapshots(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
        lines = jsonl_lines(path.read_text(encoding="utf-8"))
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def evaluate_continuous_proof(
    snapshots: list[dict[str, Any]],
    policy: ContinuousProofPolicy | None = None,
) -> dict[str, Any]:
    cfg = policy or ContinuousProofPolicy()
    ordered = sorted(snapshots, key=lambda row: float(row.get("observed_at") or 0))
    if not ordered:
        return _proof("no_evidence", ProofMetrics())
    metrics = _proof_metrics(ordered, cfg)
    reason = _proof_reason(metrics, cfg)
    return _proof(reason, metrics)


# LLM: 原全量排序后的样本只投影到共用折叠器，避免与增量路径出现两套重置/间隙算法。
# 函数用途: 计算已排序样本的最后连续合格区间统计。
def _proof_metrics(ordered: list[dict[str, Any]], policy: ContinuousProofPolicy) -> ProofMetrics:
    accumulator = _ProofAccumulator()
    for snapshot in ordered:
        observed_at = float(snapshot.get("observed_at") or 0.0)
        healthy, signatures = _snapshot_health(snapshot, policy)
        accumulator.push(observed_at, healthy, signatures, policy)
    return accumulator.metrics()


def _snapshot_health(snapshot: dict[str, Any], policy: ContinuousProofPolicy) -> tuple[int, int]:
    """Return healthy guarantee sources and signatures for one evidence instant.

    A transient pull error is observable but does not immediately break the
    guarantee while the last successful source fact remains fresh.  Once that
    fact exceeds the configured staleness window, this snapshot stops
    qualifying and the continuous segment resets.
    """

    observed_at = float(snapshot.get("observed_at") or 0.0)
    watches = [row for row in snapshot.get("watches", []) if isinstance(row, dict)]
    healthy = [
        row
        for row in watches
        if row.get("audit_guarantee")
        and int((row.get("totals") or {}).get("pulls") or 0) > 0
        and float(row.get("last_source_at") or 0.0) > 0
        and 0
        <= observed_at - float(row.get("last_source_at") or 0.0)
        <= policy.maximum_source_staleness_seconds
    ]
    signatures = {
        (
            str((row.get("source") or {}).get("scheme") or ""),
            str((row.get("source") or {}).get("origin_hash") or ""),
            str(row.get("source_mode") or ""),
            tuple(row.get("envelope_keys") or ()),
        )
        for row in healthy
    }
    return len(healthy), len(signatures)


def _proof_reason(metrics: ProofMetrics, policy: ContinuousProofPolicy) -> str:
    if metrics.healthy_sources < policy.minimum_sources:
        return "insufficient_healthy_guarantee_sources"
    if metrics.signatures < policy.minimum_signatures:
        return "sources_not_heterogeneous"
    if metrics.duration < policy.minimum_seconds:
        return "duration_too_short"
    if metrics.maximum_gap > policy.maximum_sample_gap_seconds:
        return "evidence_gap"
    return "ok"


def _proof(reason: str, metrics: ProofMetrics) -> dict[str, Any]:
    return {
        "proven": reason == "ok",
        "reason": reason,
        "continuous_seconds": metrics.duration,
        "healthy_guarantee_sources": metrics.healthy_sources,
        "heterogeneous_signatures": metrics.signatures,
    }


__all__ = [
    "ContinuousProofFileEvaluator",
    "ContinuousProofPolicy",
    "append_snapshot",
    "build_snapshot",
    "discover_owner_homes",
    "evaluate_continuous_proof",
    "read_snapshots",
    "recover_active_audit_harvesters",
]
