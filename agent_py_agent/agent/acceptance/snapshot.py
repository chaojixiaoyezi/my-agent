"""immutable artifact snapshot（3.txt H.9/I.9）。

validator 只验证内容寻址的 immutable artifact snapshot，不验证 live
workspace：输入源是 artifact_records 的 digest 记录，验证前重算实际
文件 digest 与记录比对 —— 不一致 = snapshot 无效（BLOCKED 语义），
绝不把 live workspace 的半成品当验收输入。

已发布文件在共享区本身不可变（H.10：源后来变化不改旧 snapshot）；
本模块同时支持把 snapshot 物化到独立目录（reflink 优先），供需要
隔离目录的 validator 使用。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runtime_db.operations import sha256_of

try:  # Linux reflink（CoW）；macOS 无 FICLONE → 回退全量复制
    import fcntl

    _FICLONE = 0x40049409
except ImportError:  # pragma: no cover - 非 Linux 平台
    fcntl = None  # type: ignore[assignment]
    _FICLONE = 0


def _reflink_or_copy(src: Path, dst: Path) -> None:
    """reflink（CoW）优先复制；平台不支持/失败 → 全量复制（shutil.copy2）。

    不用硬链接：live 文件同 inode 原地改写会穿透快照；reflink/copy
    都是新 inode，物化副本与 live 彻底解耦。
    """
    if fcntl is not None:
        try:
            with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                fcntl.ioctl(fdst.fileno(), _FICLONE, fsrc.fileno())
            return
        except OSError:
            pass
    shutil.copy2(src, dst)


@dataclass(frozen=True)
class ArtifactSnapshot:
    """内容寻址快照：记录 digest 与实际文件已验证一致。"""

    shared_root: Path
    files: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def path_for(self, rel_path: str) -> Path | None:
        for item in self.files:
            if item["rel_path"] == rel_path:
                return self.shared_root / rel_path
        return None

    def materialize(self, root: Path) -> "ArtifactSnapshot":
        """把已验证文件复制到独立目录（H.9：validator 不再读 live workspace）。

        物化发生在验证时点之后：live 后续再变（工具改写/并发发布）都不
        影响物化副本 —— 消除「验证后到执行前」的 TOCTOU。reflink 优先，
        回退全量复制；文件小、一次性执行，开销可忽略。
        """
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for item in self.files:
            rel = str(item["rel_path"])
            src = self.shared_root / rel
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _reflink_or_copy(src, dst)
        return ArtifactSnapshot(shared_root=root, files=self.files)


def load_artifact_snapshot(
    *,
    records: list[dict[str, Any]],
    shared_root: Path,
) -> ArtifactSnapshot | None:
    """从 artifact_records 记录构建已验证快照；任一 digest 不匹配 → None。

    返回 None = snapshot 无效（H.9：不得作为验收输入）。
    """
    shared = Path(shared_root)
    verified: list[dict[str, Any]] = []
    for record in records:
        rel = str(record.get("rel_path") or "")
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            return None  # rel_path 不合法（框架写入，理论上不会；防御）
        path = shared / rel
        try:
            actual = sha256_of(path)
        except OSError:
            return None
        if actual != str(record.get("digest") or ""):
            return None
        verified.append(
            {
                "rel_path": rel,
                "digest": actual,
                "size": path.stat().st_size,
            }
        )
    return ArtifactSnapshot(shared_root=shared, files=tuple(verified))
