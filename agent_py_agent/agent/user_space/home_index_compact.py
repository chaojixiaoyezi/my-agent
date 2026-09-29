"""global_index 只追加索引的**内部按 key 压缩**（纯投影，可重建）。

## 背景

`global_index/{owners,active_tasks,active_runs,active_agents}.jsonl` 是四份**只追加**的
查找投影：权威状态在各自的 owner home 里，这几份文件只是"按 key 取最新一行"的快捷方式
（见 ``home_indexes`` 模块头注释）。题主只增不减，生产上四份合计 0.93 GB，而原先只有手动
``home-index-rebuild --apply`` 才会压缩——那条路要重扫全部 owner 权威文件，实测分钟级，
挂进维护循环太重。

本模块**不读任何权威文件**，只在索引文件内部把"同一个 key 的旧行"丢掉，保留每个 key
最后一次出现的那一行。手动 rebuild 仍是权威修复工具，保持不变。

## 为什么"保留最后一行"不会改变任何查询结果

读取方 ``home_indexes._latest_unique_refs`` 的顺序是：``read_jsonl_objects_report`` 按文件
顺序拿到全部记录 → ``reversed`` → 遇到某 key 第一次出现就取它、之后的同类跳过。也就是
"**文件里最后出现的那个 key 赢**，返回顺序按 key 最后一次出现的先后倒序"。

所以只要压缩时**按原顺序、原字节**只把"不是该 key 最后一行"的行丢掉，读取侧自然会得到
逐条相同的结果和相同的顺序——这是构造保证，不是巧合。

## 两遍流式（内存只与"key 数量"同阶）

第一遍：只按 **LF** 切行扫一遍，记下每个 key 最后一次出现的行号（解析 key 需要 JSON，
但每条解析完立即丢弃，不积攒记录）。
第二遍：再扫一遍，按原顺序写出"行号 ∈ 最后一次出现集合"的那些行，**原字节照抄**、
不重新序列化。

这样峰值内存与文件大小无关（只与 key 数、行号集合有关），而不是把整个前缀读进来再解析成
dict（那正是第一版把 145 MB 文件吃出 952 MB 峰值的原因）。

## 两个必须守住的边界

- **只按 LF 切行。** ``str.splitlines()`` 会在 U+000B/U+000C/U+001C–U+001E/U+0085/U+2028/
  U+2029 处切开，而这些字符可以是合法 JSON 字符串的内容——一整条记录会被静默丢掉。
  仓库规矩见 ``json_io.jsonl_lines``。
- **每个文件用自己的 key_fields。** 四份索引的读取键各不相同（owners 是 owner_id；
  tasks 是 owner_id+task_id；runs 是 owner_id+run_id；agents 是 owner_id+agent_id）。
  用一套统一的 fields 会导致 runs 的两个 task_id 版本被当成两个 key，"旧状态复活"。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


# LLM: 临时文件写失败必须被清掉，别在索引目录里留垃圾。
# 函数用途: 尽力删除一个临时文件。
def _discard(tmp: Path) -> None:
    try:
        os.unlink(tmp)
    except OSError:
        pass

# LLM: 阈值与冷却都是**内部常量**，不新增用户参数（参数中心正在减量）；量级依据见下方注释。
# 函数用途: 低于这个大小不压缩，避免对小文件白跑一遍 IO。
COMPACT_MIN_BYTES = 64 * 1024 * 1024

# LLM: 冷却按小时算：压缩本身是 O(文件) 的流式扫描，压完文件只剩几 MB，
#   下一次触发靠"大小涨到上次的 2 倍"，所以冷却只是防止极端抖动下频繁重扫。
# 函数用途: 两次压缩之间的最小间隔。
COMPACT_COOLDOWN_SECONDS = 6 * 60 * 60

# LLM: 流式扫描时每这么多行让出一次 GIL，避免连续几秒拖慢请求线程（维护跑在 Gateway 里）。
# 函数用途: 让出执行权的行间隔。
_YIELD_EVERY_LINES = 20_000


@dataclass(frozen=True)
class IndexCompactResult:
    """一次压缩的结果（给 tick summary / 诊断用，不泄露正文）。"""

    path: Path
    compacted: bool
    reason: str
    before_bytes: int = 0
    after_bytes: int = 0
    kept_lines: int = 0
    dropped_lines: int = 0
    bad_lines: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "compacted": self.compacted,
            "reason": self.reason,
            "before_bytes": self.before_bytes,
            "after_bytes": self.after_bytes,
            "kept_lines": self.kept_lines,
            "dropped_lines": self.dropped_lines,
            "bad_lines": self.bad_lines,
        }


# LLM: 路径 → (上次压缩后大小, 上次压缩时间)，供下一轮的零扫描判据使用。
#   必须**持久化**：只记进程内的话，Gateway 每次重启后第一次 tick 都会不受冷却限制地压一遍。
#   放在索引同目录的一个小 sidecar 里（派生数据，丢了按"无记录"处理）。
_LAST_COMPACT_BYTES: dict[str, int] = {}
_LAST_COMPACT_AT: dict[str, float] = {}
_SIDECAR_NAME = "compact_state.json"


# LLM: sidecar 与索引同目录；只存路径→大小/时间，不含任何正文。
# 函数用途: 返回某份索引对应的 sidecar 路径。
def _sidecar_path(path: Path) -> Path:
    return path.parent / _SIDECAR_NAME


# LLM: 读失败一律当"无记录"（派生数据），绝不影响压缩本身。
# 函数用途: 把持久化的上次压缩状态载入内存。
def load_compact_state(path: str | Path) -> None:
    target = Path(path)
    sidecar = _sidecar_path(target)
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return
    key = str(target)
    record = entries.get(key)
    if not isinstance(record, dict):
        return
    try:
        _LAST_COMPACT_BYTES[key] = int(record.get("bytes") or 0)
        _LAST_COMPACT_AT[key] = float(record.get("at") or 0.0)
    except (TypeError, ValueError):
        return


# LLM: 写失败静默（派生数据，丢了按无记录处理），但尽量原子替换。
# 函数用途: 把当前内存里的上次压缩状态写回 sidecar。
def save_compact_state(path: str | Path) -> None:
    from ..common.json_io import write_text_file_atomic_unlocked

    target = Path(path)
    sidecar = _sidecar_path(target)
    key = str(target)
    if key not in _LAST_COMPACT_BYTES and key not in _LAST_COMPACT_AT:
        return
    entries: dict[str, dict[str, object]] = {}
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
            entries = dict(raw["entries"])
    except (OSError, json.JSONDecodeError):
        entries = {}
    entries[key] = {
        "bytes": int(_LAST_COMPACT_BYTES.get(key, 0)),
        "at": float(_LAST_COMPACT_AT.get(key, 0.0)),
    }
    try:
        write_text_file_atomic_unlocked(
            sidecar,
            json.dumps({"schema_version": "index-compact-state.v1", "entries": entries}, ensure_ascii=False),
        )
    except OSError:
        pass


# LLM: 读取键在四份文件里不同，用文件名查表；未知文件按"只用 owner_id"退回，绝不抛。
# 函数用途: 返回某份索引文件的 key_fields。
def key_fields_for(path: Path) -> tuple[str, ...]:
    # LLM: 键定义只有一处权威：home_indexes 的注册表。压缩与写入共用同一份，
    #   免得 spec 改了以后压缩把不同实体的行当成同一个 key 合并掉。
    # 函数用途: 按索引文件名取该文件记录的唯一键字段。
    from .home_indexes import key_fields_for_index_file

    return key_fields_for_index_file(path)


# LLM: 零扫描判据——不看内容、不估重复率（那要整读一遍，代价和压缩本身差不多）。
# 函数用途: 判断一份索引文件现在该不该压缩，并给出原因码。
def should_compact(path: str | Path, *, now: float | None = None) -> str:
    target = Path(path)
    key = str(target)
    if key not in _LAST_COMPACT_BYTES and key not in _LAST_COMPACT_AT:
        load_compact_state(target)  # 进程内没有记录时，先看 sidecar（重启后也能守冷却）
    try:
        size = target.stat().st_size
    except OSError:
        return "missing"
    if size < COMPACT_MIN_BYTES:
        return "below_min_bytes"
    last_bytes = _LAST_COMPACT_BYTES.get(key, 0)
    if size < max(COMPACT_MIN_BYTES, 2 * last_bytes):
        return "below_growth_ratio"
    last_at = _LAST_COMPACT_AT.get(key, 0.0)
    current = float(now if now is not None else time.time())
    if last_at and (current - last_at) < COMPACT_COOLDOWN_SECONDS:
        return "in_cooldown"
    return "due"


# LLM: 第一遍——只按 LF 切行，记下每个 key **最后一次出现**的行号。
#   每条解析完立即丢弃，不积攒记录；内存只与 key 数量同阶。
# 函数用途: 扫一遍前缀，返回 (key→最后行号, 总行数, 坏行数)。
def _scan_last_line_per_key(
    target: Path, prefix_len: int, key_fields: tuple[str, ...]
) -> tuple[dict[tuple[str, ...], int], int, int]:
    last_line_for_key: dict[tuple[str, ...], int] = {}
    line_count = 0
    bad_lines = 0
    with target.open("rb") as handle:
        offset = 0
        line_index = 0
        while offset < prefix_len:
            raw = handle.readline()
            if not raw:
                break
            offset += len(raw)
            line_count += 1
            if line_index and line_index % _YIELD_EVERY_LINES == 0:
                time.sleep(0)  # 让出 GIL，别连续几秒占着请求线程
            key = _record_key(raw, key_fields)
            if key is None:
                bad_lines += 1  # 坏行计数上报，不静默
            else:
                last_line_for_key[key] = line_index
            line_index += 1
    return last_line_for_key, line_count, bad_lines


# LLM: 一行原始字节 → key；不是合法 JSON 对象就返回 None（由调用方计数）。
# 函数用途: 解析一行并抽出该文件自己的 key。
def _record_key(raw: bytes, key_fields: tuple[str, ...]) -> tuple[str, ...] | None:
    stripped = raw.rstrip(b"\r\n")
    if not stripped:
        return None
    try:
        record = json.loads(stripped.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    return tuple(str(record.get(field) or "") for field in key_fields)


# LLM: 第二遍——按原顺序把"是某 key 最后一行"的行流式写进临时文件，原字节照抄。
#   刻意逐行 write，不把整份结果攒进内存：否则峰值又回到 O(文件大小)。
# 函数用途: 把保留下来的行写入临时文件，返回写入行数；失败返回 None。
def _write_kept_lines(target: Path, prefix_len: int, keep: set[int], tmp: Path) -> int | None:
    kept = 0
    try:
        with target.open("rb") as src, tmp.open("wb") as dst:
            offset = 0
            line_index = 0
            while offset < prefix_len:
                raw = src.readline()
                if not raw:
                    break
                offset += len(raw)
                if line_index in keep:
                    dst.write(raw)
                    kept += 1
                line_index += 1
                if line_index % _YIELD_EVERY_LINES == 0:
                    time.sleep(0)
    except OSError:
        return None
    return kept


# LLM: 压缩期间 `home-index-rebuild --apply` 可能整体换掉文件（inode 变）或截短它；
#   这两种情况下前缀快照已经不可信，必须放弃这次压缩而不是把陈旧内容写回去。
# 函数用途: 核对文件身份没变、长度没变短。
def _still_same_file(target: Path, stat0: object, prefix_len: int) -> bool:
    try:
        stat1 = target.stat()
    except OSError:
        return False
    return stat1.st_ino == stat0.st_ino and stat1.st_size >= prefix_len


# LLM: 第三遍（很轻）——**在锁内**再核一次身份，然后接上窗口期追加、原子替换。
#   锁外那次核对（compact_index_file 里的 _still_same_file）只能挡住"扫描期间"被换文件；
#   rebuild（home_indexes.py 的 replace_home_index_snapshots，同样在 locked_json_path 里原子替换）
#   恰好能落在「锁外核对通过 → 这里拿到锁」之间，把陈旧前缀盖到 rebuild 的新内容上。
#   所以拿到锁之后必须再核一次，这才是真正关严的那一道。
# 函数用途: 锁内核对身份后接上窗口期新追加的字节并替换目标文件；返回新大小或失败原因。
def _append_tail_and_replace(
    target: Path, tmp: Path, prefix_len: int, stat0: object
) -> tuple[int | None, str]:
    """把 prefix_len 之后的追加字节接到 tmp 尾部，再原子替换目标文件。"""
    from ..common.json_io import locked_json_path

    try:
        with locked_json_path(target):
            if not _still_same_file(target, stat0, prefix_len):
                return None, "identity_changed"
            _copy_tail(target, tmp, prefix_len)
            after_bytes = tmp.stat().st_size
            tmp.replace(target)
    except OSError:
        return None, "io_error"
    return after_bytes, ""


# LLM: 前缀之后的字节必须原样保留（那是压缩窗口期别的进程追加的行）。
# 函数用途: 把源文件 prefix_len 之后的字节分块追加到 tmp 尾部。
def _copy_tail(target: Path, tmp: Path, prefix_len: int) -> None:
    with target.open("rb") as src, tmp.open("ab") as dst:
        src.seek(prefix_len)
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                return
            dst.write(chunk)


# LLM: 两遍流式压缩，不重新序列化、不读权威源；见模块头的顺序/内存论证。
# 函数用途: 把一份索引文件压成"每个 key 只留最后一行"，保持读取结果逐条相同。
def compact_index_file(path: str | Path, *, now: float | None = None) -> IndexCompactResult:
    from ..common.json_io import locked_json_path

    target = Path(path)
    if not target.exists():
        return IndexCompactResult(target, False, "missing")
    key_fields = key_fields_for(target)

    # 锁内记下当前字节长度与 inode 身份，然后放锁做重活。
    with locked_json_path(target):
        stat0 = target.stat()
        prefix_len = stat0.st_size

    last_line_for_key, line_count, bad_lines = _scan_last_line_per_key(target, prefix_len, key_fields)

    tmp = target.with_name(f"{target.name}.{uuid4().hex}.compact")
    kept = _write_kept_lines(target, prefix_len, set(last_line_for_key.values()), tmp)
    if kept is None:
        _discard(tmp)
        return IndexCompactResult(target, False, "io_error", before_bytes=prefix_len, bad_lines=bad_lines)

    if not _still_same_file(target, stat0, prefix_len):
        _discard(tmp)
        return IndexCompactResult(target, False, "identity_changed", before_bytes=prefix_len, bad_lines=bad_lines)

    # 锁内会再核一次身份：rebuild 换文件能落在"上面那次核对通过 → 这里拿到锁"之间。
    after_bytes, failure = _append_tail_and_replace(target, tmp, prefix_len, stat0)
    if after_bytes is None:
        _discard(tmp)
        return IndexCompactResult(target, False, failure, before_bytes=prefix_len, bad_lines=bad_lines)

    dropped = line_count - kept - bad_lines

    current = float(now if now is not None else time.time())
    _LAST_COMPACT_BYTES[str(target)] = after_bytes
    _LAST_COMPACT_AT[str(target)] = current
    save_compact_state(target)
    return IndexCompactResult(
        target,
        compacted=True,
        reason="compacted",
        before_bytes=prefix_len,
        after_bytes=after_bytes,
        kept_lines=kept,
        dropped_lines=max(0, dropped),
        bad_lines=bad_lines,
    )


# LLM: 维护入口：只有 should_compact 说 due 才真压；异常不中断维护循环。
# 函数用途: 对一份索引按阈值/冷却决定是否压缩，并返回结果。
def compact_index_if_due(path: str | Path, *, now: float | None = None) -> IndexCompactResult:
    reason = should_compact(path, now=now)
    if reason != "due":
        return IndexCompactResult(Path(path), False, reason)
    try:
        return compact_index_file(path, now=now)
    except OSError:
        return IndexCompactResult(Path(path), False, "io_error")


# LLM: 维护循环只调这一个入口；它遍历四份 global_index，按各自的阈值/冷却决定是否压缩。
#   单份失败不中断其余（异常只记在结果里），绝不影响 retention 主流程。
# 函数用途: 对 owner home 的四份 global_index 做"该压就压"，返回结构化结果列表。
def compact_global_indexes_if_due(home: object, *, now: float | None = None) -> list[IndexCompactResult]:
    results: list[IndexCompactResult] = []
    for attr in (
        "global_index_owners_jsonl",
        "global_index_active_tasks_jsonl",
        "global_index_active_runs_jsonl",
        "global_index_active_agents_jsonl",
    ):
        target = getattr(home, attr, None)
        if target is None:
            continue
        try:
            results.append(compact_index_if_due(target, now=now))
        except Exception:
            results.append(IndexCompactResult(Path(target), False, "io_error"))
    return results


__all__ = [
    "COMPACT_COOLDOWN_SECONDS",
    "COMPACT_MIN_BYTES",
    "IndexCompactResult",
    "compact_index_file",
    "compact_index_if_due",
    "compact_global_indexes_if_due",
    "key_fields_for",
    "load_compact_state",
    "save_compact_state",
    "should_compact",
]
