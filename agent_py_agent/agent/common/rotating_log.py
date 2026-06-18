
from __future__ import annotations

"""通用 append-only 文件轮转 + retention,防长跑磁盘撑爆。

对照五项目最优解:
- 长期助手 RotatingFileHandler(按大小轮转 + backup_count) —— 轮转骨架
- 通道运行时 enforceSessionDiskBudget + pruneAfter(磁盘预算 + TTL) —— retention 维度
- 工具运行时 `git gc --prune=7.days` —— 老段按龄清理

设计要点(为"一条不丢"的存档量身):
- 轮转不立即丢:主文件超 max_bytes → 改名 .1,旧 .1→.2 ...(可 gzip),只在超 retention
  (段数上限 / TTL / 总字节预算)时才真删最老段。
- 对账友好:真删段时把删掉的行数累加进 sidecar(<file>.rotmeta.json)的 pruned_lines。
  total_line_count() = 在线所有段行数 + pruned_lines —— 轮转/gzip/删段都不破坏"累计落盘"
  口径,调用方据此做 no_loss 对账(不再依赖"当前单文件行数")。
- 单写者:沿用 log_ops daemon 的单写者模型;轮转用 os.replace 原子改名,读者(poll/query)
  并发读时要么读到旧路径要么新路径,不撕裂。
"""

import gzip
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .json_io import read_json_object, write_json_file_atomic


@dataclass(frozen=True)
class RotatePolicy:
    """轮转 + retention 策略。max_bytes<=0 时整体退化为纯 append(不轮转)。"""

    max_bytes: int = 0              # 主文件超过则轮转;<=0 关闭轮转
    backup_count: int = 5           # 在线保留几个历史段(超出删最老)
    compress: bool = False          # 轮转出的历史段 gzip(.gz)省空间
    retention_seconds: float = 0.0  # 段 mtime 超龄则删;<=0 不按龄删
    max_total_bytes: int = 0        # 历史段总字节预算超则删最老;<=0 不限

    @property
    def enabled(self) -> bool:
        return self.max_bytes > 0


@dataclass
class RotateResult:
    rotated: bool = False
    pruned_segments: int = 0
    pruned_lines: int = 0


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".rotmeta.json")


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _update_meta(path: Path, *, appended: int = 0, pruned: int = 0) -> None:
    """累加 sidecar 计数(appended=本次写入行数, pruned=本次删段行数),一次原子写。"""
    meta = read_json_object(_meta_path(path))
    meta["appended_lines"] = _safe_int(meta.get("appended_lines")) + appended
    meta["pruned_lines"] = _safe_int(meta.get("pruned_lines")) + pruned
    write_json_file_atomic(_meta_path(path), meta)


def _count_lines_any(p: Path) -> int:
    """数一个段的行数,透明支持 .gz。坏文件按 0 计(不抛)。"""
    opener = gzip.open if p.suffix == ".gz" else open
    try:
        with opener(p, "rt", encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _segments(path: Path) -> list[Path]:
    """所有历史段(path.1 / path.1.gz / path.2 ...),按序号升序(1=最新历史,末尾=最老)。"""
    found: list[tuple[int, Path]] = []
    prefix = path.name + "."
    for p in path.parent.glob(path.name + ".*"):
        if p.name.endswith(".rotmeta.json"):
            continue
        suffix = p.name[len(prefix):]
        idx_str = suffix[:-3] if suffix.endswith(".gz") else suffix
        if idx_str.isdigit():
            found.append((int(idx_str), p))
    found.sort(key=lambda t: t[0])
    return [p for _, p in found]


def _rollover(path: Path, policy: RotatePolicy) -> None:
    """主文件 → .1,旧 .N → .N+1(从高序号往下挪,避免覆盖)。"""
    for p in reversed(_segments(path)):
        suffix = p.name[len(path.name) + 1:]
        gz = suffix.endswith(".gz")
        idx = int(suffix[:-3] if gz else suffix)
        os.replace(p, path.with_name(f"{path.name}.{idx + 1}" + (".gz" if gz else "")))
    if policy.compress:
        target = path.with_name(path.name + ".1.gz")
        with path.open("rb") as src, gzip.open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
    else:
        os.replace(path, path.with_name(path.name + ".1"))


def _enforce_retention(path: Path, policy: RotatePolicy) -> tuple[int, int]:
    """删超额最老段(段数>backup_count 或 超龄 或 超总预算),返回(删段数, 删行数)。"""
    pruned_segs = pruned_lines = 0
    now = time.time()
    while True:
        segs = _segments(path)
        if not segs:
            break
        oldest = segs[-1]
        over_count = len(segs) > policy.backup_count
        over_age = policy.retention_seconds > 0 and (now - oldest.stat().st_mtime) > policy.retention_seconds
        over_total = policy.max_total_bytes > 0 and sum(s.stat().st_size for s in segs) > policy.max_total_bytes
        if not (over_count or over_age or over_total):
            break
        pruned_lines += _count_lines_any(oldest)
        oldest.unlink()
        pruned_segs += 1
    return pruned_segs, pruned_lines


def append_with_rotation(
    path: Path, blob: str, policy: RotatePolicy, *, line_count: int | None = None
) -> RotateResult:
    """把 blob append 到 path,按 policy 轮转 + retention。

    line_count:本次 blob 的逻辑行数(调用方知道则传,用于 O(1) 维护累计 appended_lines);
    None 则数 blob 的换行数。累计写进 sidecar,供 total_line_count() O(1) 读出做对账。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 接管已有旧档(有内容却无 sidecar):先把现有行数纳入 appended 基线,避免对账少计。
    if path.exists() and "appended_lines" not in read_json_object(_meta_path(path)):
        _update_meta(path, appended=online_line_count(path))
    result = RotateResult()
    data = blob.encode("utf-8")
    if policy.enabled and path.exists() and path.stat().st_size + len(data) > policy.max_bytes:
        _rollover(path, policy)
        result.rotated = True
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    if policy.enabled:
        result.pruned_segments, result.pruned_lines = _enforce_retention(path, policy)
    added = blob.count("\n") if line_count is None else line_count
    _update_meta(path, appended=added, pruned=result.pruned_lines)
    return result


def online_line_count(path: Path) -> int:
    """当前在线所有段(主 + 历史)的行数之和(含 .gz 段)。"""
    total = _count_lines_any(path) if path.exists() else 0
    for seg in _segments(path):
        total += _count_lines_any(seg)
    return total


def total_line_count(path: Path) -> int:
    """累计落盘行数(O(1))。优先读 sidecar 维护的 appended_lines(权威累计,轮转/删段都不减);
    从未经本模块写过的旧档(无 sidecar)回退为数在线段。用于"一条不丢"对账。"""
    meta = read_json_object(_meta_path(path))
    if "appended_lines" in meta:
        return _safe_int(meta.get("appended_lines"))
    return online_line_count(path)


def _iter_one_segment(seg: Path):
    opener = gzip.open if seg.suffix == ".gz" else open
    try:
        with opener(seg, "rt", encoding="utf-8", errors="ignore") as fh:
            yield from fh
    except OSError:
        return


def iter_all_lines(path: Path):
    """按时间顺序(最老→最新)产出所有在线行:先最老历史段,...,最后主文件。query 读全量用。"""
    for seg in reversed(_segments(path)):  # 序号大=老,先出
        yield from _iter_one_segment(seg)
    if path.exists():
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            yield from fh
