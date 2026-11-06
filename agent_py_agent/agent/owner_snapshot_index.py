# LLM: 本地 stat 索引只用于避免重复摘要，内容损坏即丢弃；远端清单仍是快照权威。
# 模块用途: 记录保守的文件签名，并从独立暂存副本计算上传摘要。
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic_unlocked

# 文件时间戳粒度保护秒数：mtime 距记录时刻小于该值的条目视为可能未落定，下次必须重新计算摘要。
OWNER_SNAPSHOT_RACY_MTIME_SECONDS = 2.0


# LLM: 签名包含设备、inode、完整 mode 和 ctime；近期 mtime 的条目不提供摘要命中。
# 类用途: 保存单个相对路径的文件签名和摘要。
@dataclass(frozen=True)
class StatEntry:
    relative_path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    sha256: str
    racy: bool


# LLM: 调用方持有 owner slot 和租约；索引不授予提交权，丢失索引只增加校验成本。
# 类用途: 读写温缓存文件签名并保守判断可复用摘要。
class OwnerStatIndex:
    # LLM: 空索引没有持久副作用，由 checkout 的清单校对填充。
    # 函数用途: 建立空的文件签名表。
    def __init__(self) -> None:
        self._entries: dict[str, StatEntry] = {}

    # LLM: 内容或读取失败一律返回空索引，不接受部分损坏的缓存作为事实。
    # 函数用途: 从已知版本的 JSON 恢复文件签名。
    @classmethod
    def load(cls, path: Path) -> OwnerStatIndex:
        index = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                return index
            entries = data.get("entries")
            if not isinstance(entries, dict):
                return index
            for relative, values in entries.items():
                if (not isinstance(relative, str) or not relative or not isinstance(values, list)
                        or len(values) != 8 or any(type(value) is not int for value in values[:6])
                        or not isinstance(values[6], str) or type(values[7]) is not bool):
                    return cls()
                index._entries[relative] = StatEntry(relative, *values)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return cls()
        return index

    # LLM: 私有原子 writer 同步文件及父目录；调用方必须串行访问同一个温缓存。
    # 函数用途: 耐久保存版本化文件签名表。
    def save(self, path: Path) -> None:
        entries = {
            rel: [e.dev, e.ino, e.size, e.mtime_ns, e.ctime_ns, e.mode, e.sha256, e.racy]
            for rel, e in self._entries.items()
        }
        write_private_json_file_atomic_unlocked(path, {"version": 1, "entries": entries})

    # LLM: 只有六项签名完全一致且非 racy 才返回摘要，不以缓存版本推断文件内容。
    # 函数用途: 查询给定文件状态能否复用已存摘要。
    def lookup(self, relative_path: str, st: os.stat_result) -> str | None:
        entry = self._entries.get(relative_path)
        if entry is None or entry.racy:
            return None
        actual = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)
        expected = (entry.dev, entry.ino, entry.size, entry.mtime_ns, entry.ctime_ns, entry.mode)
        return entry.sha256 if actual == expected else None

    # LLM: written_at 仅用于本地时间戳粒度防护，不参与数据库租约判定。
    # 函数用途: 为校验过的文件记录签名和近期写入标志。
    def record(self, relative_path: str, st: os.stat_result, sha256: str, written_at: float) -> None:
        racy = abs(st.st_mtime_ns / 1e9 - written_at) < OWNER_SNAPSHOT_RACY_MTIME_SECONDS
        self._entries[relative_path] = StatEntry(
            relative_path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_mode, sha256, racy,
        )

    # LLM: 删除索引项不删除文件，也不修改远端清单。
    # 函数用途: 忘记已不在缓存中的相对路径。
    def forget(self, relative_path: str) -> None:
        self._entries.pop(relative_path, None)

    # LLM: 返回独立路径集合，允许调用方在迭代时删除过期项。
    # 函数用途: 列出本地签名表中的路径。
    def paths(self) -> set[str]:
        return set(self._entries)


# LLM: 上传者只能使用返回的暂存副本；原文件在复制期间变化则删除副本并拒绝快照。
# 函数用途: 复制文件到唯一暂存路径并计算该副本的摘要与大小。
def stage_and_hash(source: Path, staging_dir: Path) -> tuple[Path, str, int]:
    before = source.lstat()
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / f"{uuid4().hex}.stage"
    try:
        shutil.copyfile(source, staged)
        after = source.lstat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"owner 文件在快照时仍被写入: {source}")
        with staged.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        return staged, digest, staged.stat().st_size
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
