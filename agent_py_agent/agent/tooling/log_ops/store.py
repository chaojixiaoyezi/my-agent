
from __future__ import annotations

"""log_ops 落盘布局 + 原子状态 + 全量存档 + 候选队列 + 已读游标。

目录布局(全部在一个 root 下,默认 <workspace>/.log_ops/<monitor_id>):
    config.json            采集配置(sources 列表),monitor_start 写,daemon 读
    daemon.json            daemon PID/启动时间/心跳(原子写),start/stop/status 用它找进程
    state/<source_id>.json 每源断点状态(文件 offset / 文件夹已处理集合 / API cursor)
    archive/<source_id>.log 每源全量原始日志存档(append,一条不丢的根本保证)
    candidates.jsonl       候选告警队列(append,triage 命中即写,供 LLM poll)
    poll_cursor.json       log_alert_poll 已读游标(读到第几条,下次从这之后给,不重复)
    metrics.json           采集计数器(每源已采集行数/命中数,原子写,供状态对账)

所有"读-改-写"的小 JSON 都走 write_json_file_atomic(temp+replace+per-path 锁),崩溃不留半截。
archive / candidates 是 append-only,用 O_APPEND 单次 write,POSIX 下对 < PIPE_BUF 的写有原子性,
且 daemon 是单写者,不会撕行。
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any

from ...common.json_io import (
    read_json_object,
    write_json_file_atomic,
)
from ...common import schema_version
from ...common.resilience import BurstTracker, CircuitBreaker
from ...common.rotating_log import (
    RotatePolicy,
    append_with_rotation,
    iter_all_lines,
    total_line_count,
)

# monitor_id 只允许这些字符,避免路径穿越/怪字符进文件名。
_MONITOR_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
# source_id 由源类型+源定位算出来,稳定且文件名安全。
_SOURCE_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

def _env_int(name: str, default: int) -> int:
    """从环境变量读整数阈值,便于真机测试/调优临时覆盖(不改代码、不动生产默认);缺失/非法用默认。"""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# archive 轮转策略(可被 LOGOPS_ARCHIVE_* 环境变量覆盖):单段 64MB、在线 30 段、gzip、总预算 4GB、保留 30 天。
# 默认偏宽(存档是"一条不丢"的根本,优先全留);真超预算/超龄才删最老段,删量记 sidecar 不破对账。
_ARCHIVE_ROTATE = RotatePolicy(
    max_bytes=_env_int("LOGOPS_ARCHIVE_MAX_BYTES", 64 * 1024 * 1024),
    backup_count=_env_int("LOGOPS_ARCHIVE_BACKUP_COUNT", 30),
    compress=True,
    retention_seconds=_env_int("LOGOPS_ARCHIVE_RETENTION_SECONDS", 30 * 86400),
    max_total_bytes=_env_int("LOGOPS_ARCHIVE_MAX_TOTAL_BYTES", 4 * 1024 * 1024 * 1024),
)

# 源采集断路器(可被 LOGOPS_SOURCE_* 覆盖):连续失败 N 拍即熔断,冷却期跳过该源不烧资源,冷却到 half-open 试探。
_SOURCE_FAIL_THRESHOLD = _env_int("LOGOPS_SOURCE_FAIL_THRESHOLD", 3)
_SOURCE_COOLDOWN = float(_env_int("LOGOPS_SOURCE_COOLDOWN", 120))

# 速率突变检测(可被 LOGOPS_BURST_* 覆盖):同一 IP 在最近 _BURST_WINDOW 次出现里达 _BURST_THRESHOLD 次 = 突发。
_BURST_WINDOW = _env_int("LOGOPS_BURST_WINDOW", 200)
_BURST_THRESHOLD = _env_int("LOGOPS_BURST_THRESHOLD", 30)

# metrics.json schema 版本(改字段时 +1 并在 _METRICS_MIGRATIONS 挂 v→v+1 迁移函数)。
_METRICS_SCHEMA_VERSION = 1
_METRICS_MIGRATIONS: dict[int, schema_version.Migration] = {}


def sanitize_monitor_id(monitor_id: str) -> str:
    """把 monitor_id 收敛成文件名安全的形式;空则回退 default。"""
    cleaned = _MONITOR_ID_RE.sub("-", str(monitor_id or "").strip()).strip("-")
    return cleaned or "default"


@dataclass(frozen=True)
class SourceSpec:
    """一个被监控的源。kind ∈ {file, folder, api};locator 是路径或 URL。"""

    kind: str
    locator: str
    source_id: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "locator": self.locator, "source_id": self.source_id}


def source_id_for(kind: str, locator: str) -> str:
    """源唯一标识:类型 + 短哈希(定位串) + 可读尾巴。稳定 → 重启后状态/存档对得上。"""
    digest = hashlib.sha1(f"{kind}:{locator}".encode()).hexdigest()[:10]
    tail = _SOURCE_ID_SAFE_RE.sub("-", locator)[-40:].strip("-")
    return f"{kind}-{digest}-{tail}" if tail else f"{kind}-{digest}"


def classify_source(locator: str) -> str:
    """从 locator 字符串判定源类型:http(s):// → api;是已存在目录 → folder;否则 file。

    文件/文件夹可能尚未创建(daemon 起在源之前),所以:存在且是目录→folder,存在且是文件→file,
    不存在时按是否有文件后缀名/结尾斜杠粗判(结尾 / 视为文件夹,否则视为文件)。
    """
    text = str(locator or "").strip()
    low = text.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return "api"
    path = Path(text)
    if path.is_dir():
        return "folder"
    if path.is_file():
        return "file"
    if text.endswith(("/", os.sep)):
        return "folder"
    return "file"


def build_source_specs(sources: list[str]) -> list[SourceSpec]:
    """把原始 sources 字符串列表规范成 SourceSpec(去重,保序)。"""
    specs: list[SourceSpec] = []
    seen: set[str] = set()
    for raw in sources:
        locator = str(raw or "").strip()
        if not locator:
            continue
        kind = classify_source(locator)
        sid = source_id_for(kind, locator)
        if sid in seen:
            continue
        seen.add(sid)
        specs.append(SourceSpec(kind=kind, locator=locator, source_id=sid))
    return specs


class LogOpsStore:
    """log_ops 单个 monitor 的落盘门面。所有路径派生 + 原子状态读写 + 存档/候选 append 都走这里。"""

    def __init__(self, root: Path, monitor_id: str = "default"):
        self.monitor_id = sanitize_monitor_id(monitor_id)
        self.root = Path(root).resolve() / self.monitor_id
        self.state_dir = self.root / "state"
        self.archive_dir = self.root / "archive"
        self.config_path = self.root / "config.json"
        self.daemon_path = self.root / "daemon.json"
        self.watchdog_path = self.root / "watchdog.json"  # 主动看门狗进程的单例锁 + 心跳
        self.candidates_path = self.root / "candidates.jsonl"
        self.urgent_path = self.root / "urgent.jsonl"
        self.poll_cursor_path = self.root / "poll_cursor.json"
        self.metrics_path = self.root / "metrics.json"
        self.profiles_dir = self.root / "profiles"
        self.reports_path = self.root / "reports.jsonl"
        self.leads_path = self.root / "leads.jsonl"
        self.review_path = self.root / "review.json"  # 研判侧跨 run 游标(按需唤醒续接:研判到哪/上次研判时间)
        self.seen_iocs_path = self.root / "seen_iocs.json"  # 已见攻击者 IOC 集(新颖性检测:对抗"新攻击者被当已知"坍缩漏报)
        self.baseline_dir = self.root / "baselines"  # 每源统计基线(数据驱动异常检测:学正常→判偏离)

    # ---- 目录 ----
    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    # ---- config(sources) ----
    def write_config(self, specs: list[SourceSpec], *, poll_interval_seconds: float) -> None:
        self.ensure_dirs()
        payload = {
            "monitor_id": self.monitor_id,
            "poll_interval_seconds": poll_interval_seconds,
            "sources": [spec.to_dict() for spec in specs],
            "created_at": time.time(),
        }
        write_json_file_atomic(self.config_path, payload)

    def read_config(self) -> dict[str, Any]:
        return read_json_object(self.config_path)

    def update_config_field(self, key: str, value: Any) -> None:
        """原子 patch config 的单个字段(不动 sources;monitor 级设置如 report_floor 用)。"""
        config = self.read_config()
        config[str(key)] = value
        write_json_file_atomic(self.config_path, config)

    def source_specs(self) -> list[SourceSpec]:
        config = self.read_config()
        specs: list[SourceSpec] = []
        for item in config.get("sources", []) or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            locator = str(item.get("locator") or "")
            sid = str(item.get("source_id") or "") or source_id_for(kind, locator)
            specs.append(SourceSpec(kind=kind, locator=locator, source_id=sid))
        return specs

    # ---- 每源断点状态(原子) ----
    def state_path(self, source_id: str) -> Path:
        safe = _SOURCE_ID_SAFE_RE.sub("-", source_id)
        return self.state_dir / f"{safe}.json"

    def read_state(self, source_id: str) -> dict[str, Any]:
        return read_json_object(self.state_path(source_id))

    def write_state(self, source_id: str, state: dict[str, Any]) -> None:
        self.ensure_dirs()
        write_json_file_atomic(self.state_path(source_id), state)

    # ---- 每源监控方案 SourceProfile(原子;LLM 探查产出,daemon 读它切割+初筛) ----
    def profile_path(self, source_id: str) -> Path:
        safe = _SOURCE_ID_SAFE_RE.sub("-", source_id)
        return self.profiles_dir / f"{safe}.json"

    def read_profile(self, source_id: str) -> dict[str, Any]:
        """读某源 SourceProfile(splitter/rules/triage_hint/reporting/fields...);没有返回空 dict(走默认)。"""
        return read_json_object(self.profile_path(source_id))

    def write_profile(self, source_id: str, profile: dict[str, Any]) -> None:
        self.ensure_dirs()
        write_json_file_atomic(self.profile_path(source_id), profile)

    def all_profiles(self) -> dict[str, dict[str, Any]]:
        """读出所有已登记源的 profile(供 status/总览;无 profile 的源不在结果里)。"""
        out: dict[str, dict[str, Any]] = {}
        for spec in self.source_specs():
            profile = self.read_profile(spec.source_id)
            if profile:
                out[spec.source_id] = profile
        return out

    # ---- 分级汇报(append-only 审计全量;读时按级别过滤,只把够级别的给用户) ----
    def append_report(self, report: dict[str, Any]) -> None:
        """追加一条分级汇报到审计流(全量留存,不在写入层抑制;抑制只体现在"是否推送用户")。"""
        self.ensure_dirs()
        blob = json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n"
        _atomic_append(self.reports_path, blob)

    def read_reports(self, *, min_level: str = "") -> list[dict[str, Any]]:
        """读汇报审计流。min_level 非空时只返回 >= 该级别的(用户只看够级别的那些,不被噪声淹没)。"""
        if not self.reports_path.exists():
            return []
        floor = level_rank(min_level) if min_level else 0
        with self.reports_path.open("r", encoding="utf-8", errors="ignore") as handle:
            parsed = [_parse_jsonl_line(line) for line in handle]
        return [r for r in parsed if r is not None and _report_passes_floor(r, floor)]

    # ---- 关联线索池(子代理上报可疑 IOC 线索;主代理/关联代理读它跨源拼链。联合查询责任分工) ----
    def append_lead(self, lead: dict[str, Any]) -> None:
        """子代理把可疑 IOC 线索 append 线索池(它只盯单源不自己跨源查,跨源串联归上层)。"""
        self.ensure_dirs()
        blob = json.dumps(lead, ensure_ascii=False, sort_keys=True) + "\n"
        _atomic_append(self.leads_path, blob)

    def read_leads(self) -> list[dict[str, Any]]:
        if not self.leads_path.exists():
            return []
        with self.leads_path.open("r", encoding="utf-8", errors="ignore") as handle:
            return [lead for line in handle if (lead := _parse_jsonl_line(line)) is not None]

    # ---- 全量存档(append-only) ----
    def archive_path(self, source_id: str) -> Path:
        safe = _SOURCE_ID_SAFE_RE.sub("-", source_id)
        return self.archive_dir / f"{safe}.log"

    def append_archive(self, source_id: str, lines: list[str]) -> int:
        """把若干记录 append 到该源存档,返回写入记录数。每条记录压平成一行(内部换行→空格)再确保
        \\n 结尾,使 archive 严格一行一记录(count_archive_lines 对账才准,对多行切割的记录也成立)。"""
        if not lines:
            return 0
        blob = "".join(
            line.replace("\r\n", " ").replace("\n", " ").replace("\r", " ") + "\n" for line in lines
        )
        # 走通用轮转:超段大小→gzip 老段,超总预算/超龄→删最老段(删量入 sidecar,对账仍准)。
        append_with_rotation(
            self.archive_path(source_id), blob, _ARCHIVE_ROTATE, line_count=len(lines)
        )
        return len(lines)

    def count_archive_lines(self, source_id: str) -> int:
        """累计落盘行数(含已轮转/已删段)。轮转后不再等于当前单文件行数,但等于"曾经落盘的
        总量",no_loss 对账口径(collected==archived==此值)与候选全局行号都不变。"""
        return total_line_count(self.archive_path(source_id))

    def iter_archive_lines(self, source_id: str):
        """按时间顺序(最老→最新)遍历该源所有在线存档行(含历史段/.gz)。供 query 全量扫描。"""
        return iter_all_lines(self.archive_path(source_id))

    # ---- 候选告警队列(append-only JSONL) ----
    def append_candidates(self, candidates: list[dict[str, Any]]) -> int:
        if not candidates:
            return 0
        path = self.candidates_path
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in candidates)
        _atomic_append(path, blob)
        return len(candidates)

    def count_candidates(self) -> int:
        return _count_file_lines(self.candidates_path)

    # ---- 紧急信号队列(高危候选;故障即时上报,供按需唤醒秒级拉研判,不等周期) ----
    def append_urgent(self, candidates: list[dict[str, Any]]) -> int:
        """高危候选 append 紧急队列(append-only,与候选队列同结构)。返回写入数。"""
        if not candidates:
            return 0
        self.urgent_path.parent.mkdir(parents=True, exist_ok=True)
        blob = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in candidates)
        _atomic_append(self.urgent_path, blob)
        return len(candidates)

    def count_urgent(self) -> int:
        return _count_file_lines(self.urgent_path)

    def read_candidates(self, *, offset: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """从候选队列读 [offset, offset+limit) 行,解析成 dict 列表。坏行跳过。

        注意:offset/limit 按"行号"切片(候选队列每行一条且不删改,行号即条目序),坏行被
        跳过不计入 out 但仍占一个行号位 —— 与 count_candidates(数行数)同口径,游标对得上。
        """
        if not self.candidates_path.exists():
            return []
        stop = None if limit is None else offset + limit
        # errors="ignore":daemon 是并发单写者,poll 读到的可能是一条多字节字符刚写一半的
        # 行(O_APPEND 单次 write 对 >PIPE_BUF 的 blob 仍可能被读者看到截断尾)。严格 utf-8
        # 会抛 UnicodeDecodeError,无 error_code 逃逸成 UNKNOWN_ERROR(retryable=False)误导
        # 模型放弃值班(2 小时真机实锤);忽略坏字节只损这一行的尾巴,与"坏行跳过"同口径,
        # 下一拍 daemon 写完整后再 poll 即完整,不丢候选(count 同样按 \n 数,游标对得上)。
        with self.candidates_path.open("r", encoding="utf-8", errors="ignore") as handle:
            window = islice(handle, offset, stop)
            return [payload for line in window if (payload := _parse_jsonl_line(line)) is not None]

    # ---- log_alert_poll 已读游标(原子) ----
    def read_poll_cursor(self) -> int:
        data = read_json_object(self.poll_cursor_path)
        try:
            return max(0, int(data.get("cursor", 0)))
        except (TypeError, ValueError):
            return 0

    def write_poll_cursor(self, cursor: int) -> None:
        self.ensure_dirs()
        write_json_file_atomic(self.poll_cursor_path, {"cursor": int(cursor), "updated_at": time.time()})

    # ---- 采集计数器(原子,daemon 写,status 读做对账) ----
    def read_metrics(self) -> dict[str, Any]:
        return read_json_object(self.metrics_path)

    def write_metrics(self, metrics: dict[str, Any]) -> None:
        self.ensure_dirs()
        write_json_file_atomic(self.metrics_path, metrics)

    # ---- daemon 句柄(PID/心跳,原子) ----
    def read_daemon(self) -> dict[str, Any]:
        return read_json_object(self.daemon_path)

    def write_daemon(self, payload: dict[str, Any]) -> None:
        self.ensure_dirs()
        write_json_file_atomic(self.daemon_path, payload)

    def clear_daemon(self) -> None:
        try:
            self.daemon_path.unlink()
        except OSError:
            pass


@dataclass
class CollectMetrics:
    """一个 daemon 循环周期内/累计的采集计数,落 metrics.json。"""

    collected_lines: int = 0
    archived_lines: int = 0
    candidates: int = 0
    per_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    # per-source 断路器(内存态,不落盘;daemon 重启则重置为 closed,可接受)。
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict, repr=False, compare=False)
    # per-source 速率突变追踪器(内存态,不落盘)。
    burst_trackers: dict[str, BurstTracker] = field(default_factory=dict, repr=False, compare=False)

    def breaker_for(self, source_id: str) -> CircuitBreaker:
        return self.breakers.setdefault(
            source_id, CircuitBreaker(threshold=_SOURCE_FAIL_THRESHOLD, cooldown_seconds=_SOURCE_COOLDOWN)
        )

    def burst_tracker_for(self, source_id: str) -> BurstTracker:
        return self.burst_trackers.setdefault(
            source_id, BurstTracker(window=_BURST_WINDOW, threshold=_BURST_THRESHOLD)
        )

    def note_source_health(self, source_id: str, error: str, now: float) -> bool:
        """记录该源本拍采集成败到断路器,刷新 metrics 里的 circuit 快照。
        返回 True 当本拍【刚熔断】(连续失败到阈值),供调用方主动告警一次。"""
        breaker = self.breaker_for(source_id)
        tripped = breaker.on_failure(now=now) if error else False
        if not error:
            breaker.on_success()
        self.per_source.setdefault(source_id, {})["circuit"] = breaker.snapshot()
        return tripped

    def bump_source(self, source_id: str, tick: SourceTick) -> None:
        entry = self.per_source.setdefault(
            source_id, {"collected_lines": 0, "archived_lines": 0, "candidates": 0, "cursor": None}
        )
        entry["collected_lines"] += tick.collected
        entry["archived_lines"] += tick.archived
        entry["candidates"] += tick.candidates
        entry["cursor"] = tick.cursor
        self.collected_lines += tick.collected
        self.archived_lines += tick.archived
        self.candidates += tick.candidates

    def to_dict(self) -> dict[str, Any]:
        return schema_version.stamp(
            {
                "collected_lines": self.collected_lines,
                "archived_lines": self.archived_lines,
                "candidates": self.candidates,
                "per_source": self.per_source,
            },
            _METRICS_SCHEMA_VERSION,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectMetrics:
        data = schema_version.migrate(
            dict(data), current_version=_METRICS_SCHEMA_VERSION, migrations=_METRICS_MIGRATIONS
        )
        metrics = cls()
        metrics.collected_lines = int(data.get("collected_lines", 0) or 0)
        metrics.archived_lines = int(data.get("archived_lines", 0) or 0)
        metrics.candidates = int(data.get("candidates", 0) or 0)
        metrics.per_source = _coerce_per_source(data.get("per_source", {}))
        return metrics


@dataclass(frozen=True)
class SourceTick:
    """一拍里单个源的采集增量(收敛 bump_source 的参数,避免长参数列表)。"""

    collected: int
    archived: int
    candidates: int
    cursor: Any


def _coerce_per_source(per_source: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(per_source, dict):
        return {}
    return {str(sid): dict(entry) for sid, entry in per_source.items() if isinstance(entry, dict)}


# 分级汇报级别:P0 最高(紧急,如攻击得手/数据外泄),P3 最低(噪声)。数字越大越该立即推送用户。
_LEVEL_RANK = {"P0": 4, "P1": 3, "P2": 2, "P3": 1, "": 0}


def level_rank(level: str) -> int:
    """汇报级别 → 数字优先级(P0>P1>P2>P3);无法识别按 0。用于"只把够级别的给用户"。"""
    return _LEVEL_RANK.get(str(level or "").strip().upper(), 0)


def _report_passes_floor(report: dict[str, Any], floor: int) -> bool:
    """汇报是否达到阈值(floor==0 表示不过滤,全要)。"""
    return floor == 0 or level_rank(str(report.get("level") or "")) >= floor


def _atomic_append(path: Path, blob: str) -> None:
    """O_APPEND 单次 write 原子追加(POSIX 下对 <PIPE_BUF 的写不撕行;多写者也不交错撕行)。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, blob.encode("utf-8"))
    finally:
        os.close(fd)


def _parse_jsonl_line(line: str) -> dict[str, Any] | None:
    """解析一行 JSONL;空行/坏行/非对象返回 None(调用方跳过)。"""
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _count_file_lines(path: Path) -> int:
    """数文件行数(按 \\n)。不存在返回 0。大文件按块读,不整文件 splitlines 占内存。"""
    if not path.exists():
        return 0
    try:
        return _count_newlines(path)
    except OSError:
        return 0


def _count_newlines(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            count += chunk.count(b"\n")
    return count


__all__ = [
    "CollectMetrics",
    "LogOpsStore",
    "SourceSpec",
    "SourceTick",
    "build_source_specs",
    "classify_source",
    "level_rank",
    "sanitize_monitor_id",
    "source_id_for",
]
