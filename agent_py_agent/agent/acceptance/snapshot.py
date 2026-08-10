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


class SnapshotMaterializeError(RuntimeError):
    """物化失败：副本内容与记录 digest 不一致（验证后 live 被篡改/复制损坏）。

    调用方必须 fail-closed：绝不让 validator 读未经内容寻址确认的副本。
    """

    def __init__(self, rel_path: str, detail: str) -> None:
        super().__init__(f"snapshot materialize failed for {rel_path}: {detail}")
        self.rel_path = rel_path
        self.detail = detail


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

        G2：复制完成后对每个物化副本重算 digest 与记录比对。load 的
        校验（T1）与物化（T2）之间存在窗口——live 若在 T1 后被篡改，
        复制进来的就是篡改内容；只信 T1 的 digest 而不核副本会把这个
        洞带进 validator 输入。副本 digest 与记录不一致 → 抛
        SnapshotMaterializeError（fail-closed），绝不交付未确认副本。
        """
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for item in self.files:
            rel = str(item["rel_path"])
            # G2：优先从内容寻址 store 复制（发布时冻结，live 再变不影响）；
            # 旧记录（无 store 路径）回退 live 共享区。
            content_path = str(item.get("content_path") or "").strip()
            src = Path(content_path) if content_path else (self.shared_root / rel)
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _reflink_or_copy(src, dst)
            try:
                actual = sha256_of(dst)
            except OSError as exc:
                raise SnapshotMaterializeError(rel, f"物化副本不可读: {exc}") from exc
            if actual != str(item.get("digest") or ""):
                raise SnapshotMaterializeError(
                    rel,
                    "物化副本 digest 与记录不一致（验证后 live 被篡改或复制损坏）",
                )
        return ArtifactSnapshot(shared_root=root, files=self.files)


def load_artifact_snapshot(
    *,
    records: list[dict[str, Any]],
    shared_root: Path,
) -> ArtifactSnapshot | None:
    """从 artifact_records 记录构建已验证快照；任一 digest 不匹配 → None。

    返回 None = snapshot 无效（H.9：不得作为验收输入）。

    G2 补：优先从内容寻址 store（record.content_path）读——那是发布时冻结
    的 immutable 对象，live 共享区后来怎么改都不影响验收输入；旧记录
    （迁移前，content_path 空）回退 live 共享区（digest 校验仍生效）。
    """
    shared = Path(shared_root)
    verified: list[dict[str, Any]] = []
    for record in records:
        rel = str(record.get("rel_path") or "")
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            return None  # rel_path 不合法（框架写入，理论上不会；防御）
        content_path = str(record.get("content_path") or "").strip()
        if content_path:
            path = Path(content_path)
        else:
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
                "content_path": content_path,
            }
        )
    return ArtifactSnapshot(shared_root=shared, files=tuple(verified))
