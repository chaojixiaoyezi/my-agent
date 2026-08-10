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

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runtime_db.operations import sha256_of


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
