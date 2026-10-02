# LLM: 能力包 v2 块 4：任务的输入原件清单。任务第一次改工作区前（与块 3 的基线同一时机、同一次扫描），把已启用、声明了核验的包的
#   声明（交付物与检查程序输入）匹配到的文件记成原件：path、sha256、size、匹配到的包 ID（就地修改检查只对其中声明了
#   input_policy=preserve_originals、且被本任务钉住的包生效；task_input 解析对所有包都从这里找）；不超过 MAX_ORIGINAL_COPY_BYTES
#   的文件另存一份副本到宿主核验目录（<规范任务根>/data/pack_verification，宿主托管、对模型只读、读照常）下的 originals/<sha256>，
#   权限 600。一个任务只记一次（清单是同目录的 originals.json），
#   后一回合写出的文件不会成为原件。副本用之前按原件摘要核对，对不上就当没有。只读结构化事实，不读模型文字。
#   副作用：读工作区文件、写清单和副本。改动同步 test_pack_verification_originals.py 与 docs/design/CAPABILITY_PACKS_V2.md 第 4 节。
# 模块用途: 记下并提供“任务开始前用户给的输入原件”，供收尾时发现就地修改、给检查程序交原件。

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .pack_verification_matching import WorkspaceScan, file_matches

INPUT_POLICY_PRESERVE_ORIGINALS = "preserve_originals"
ORIGINALS_MANIFEST = "originals.json"
ORIGINAL_COPIES_DIRECTORY = "originals"
# 一个任务最多记的原件个数（3a 审定）。
MAX_ORIGINAL_FILES_COUNT = 64
# 单个原件副本的字节上限（3a 审定 1 MB）；更大的只记摘要，不存副本。
MAX_ORIGINAL_COPY_BYTES = 1_048_576
# 一个任务全部原件副本合计的字节上限（3a 审定 16 MB）。
MAX_ORIGINAL_COPIES_TOTAL_BYTES = 16_777_216


# 类用途: 一个输入原件：工作区相对路径、任务开始时的内容摘要和大小、是否存了副本、匹配到的包 ID。
@dataclass(frozen=True)
class OriginalFile:
    path: str
    sha256: str
    size: int
    copied: bool = False
    packages: tuple[str, ...] = ()

    # 函数用途: 序列化为清单里的固定字段。
    def to_payload(self) -> dict:
        return {"path": self.path, "sha256": self.sha256, "size": self.size, "copied": self.copied,
                "packages": list(self.packages)}


# LLM: 清单只由宿主写；形状不对的条目跳过，不猜。
# 函数用途: 读出本任务的原件清单（没有清单时返回 None，表示还没记过）。
def load_task_originals(pack_root: Path) -> dict[str, OriginalFile] | None:
    try:
        payload = json.loads((Path(pack_root) / ORIGINALS_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    originals = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("path"), str) and isinstance(row.get("sha256"), str):
            packages = tuple(item for item in row.get("packages") or [] if isinstance(item, str))
            originals[row["path"]] = OriginalFile(row["path"], row["sha256"], int(row.get("size") or 0),
                                                  bool(row.get("copied")), packages)
    return originals


# LLM: scan 是块 3 本次基线扫描（模式已覆盖全部已启用、声明了核验的包）；packages 是 [(包 ID, 该包全部交付物与输入声明)]。
#   已有清单就什么都不做（一个任务只记一次），所以后一回合写出的文件不会成为原件；没有匹配文件时也写一份空清单。
#   按路径排序取前 MAX_ORIGINAL_FILES_COUNT 个，副本有单个和合计上限。
# 函数用途: 在任务第一次改工作区前记下输入原件清单和副本。
def capture_task_originals(roots: tuple[Path, Path], scan: WorkspaceScan, packages: list[tuple[str, list]]) -> None:
    pack_root, workspace_root = roots
    manifest = Path(pack_root) / ORIGINALS_MANIFEST
    if manifest.exists():
        return
    matched = [(relpath, ids) for relpath, ids in ((relpath, _matching_packages(packages, relpath, workspace_root))
                                                   for relpath, state in sorted(scan.files.items()) if state.sha256) if ids]
    originals, budget = [], MAX_ORIGINAL_COPIES_TOTAL_BYTES
    for relpath, ids in matched[:MAX_ORIGINAL_FILES_COUNT]:
        state = scan.files[relpath]
        copied = state.size <= min(MAX_ORIGINAL_COPY_BYTES, budget) and _store_copy(workspace_root / relpath, pack_root,
                                                                                    state.sha256)
        budget -= state.size if copied else 0
        originals.append(OriginalFile(relpath, state.sha256, state.size, copied, ids))
    _write_private(manifest, json.dumps({"files": [item.to_payload() for item in originals],
                                         "truncated": len(matched) > MAX_ORIGINAL_FILES_COUNT or scan.truncated},
                                        ensure_ascii=False, sort_keys=True).encode("utf-8"))


# 函数用途: 列出声明匹配这个文件的包 ID（按传入顺序）。
def _matching_packages(packages: list[tuple[str, list]], relpath: str, workspace_root: Path) -> tuple[str, ...]:
    return tuple(package_id for package_id, declarations in packages
                 if any(file_matches(item, relpath, workspace_root / relpath) for item in declarations))


# LLM: 副本可能被改动或删掉；只有内容摘要仍等于原件摘要时才返回路径。
# 函数用途: 返回一个原件可用的副本路径，没有或对不上时为 None。
def original_copy_path(pack_root: Path, original: OriginalFile) -> Path | None:
    if not original.copied:
        return None
    path = Path(pack_root) / ORIGINAL_COPIES_DIRECTORY / original.sha256
    try:
        return path if hashlib.sha256(path.read_bytes()).hexdigest() == original.sha256 else None
    except OSError:
        return None


# 函数用途: 按内容摘要存一份副本（同内容只存一次）；读写失败或内容和摘要对不上返回 False。
def _store_copy(source: Path, pack_root: Path, sha256: str) -> bool:
    target = Path(pack_root) / ORIGINAL_COPIES_DIRECTORY / sha256
    try:
        data = source.read_bytes()
    except OSError:
        return False
    if hashlib.sha256(data).hexdigest() != sha256:
        return False
    return target.exists() or _write_private(target, data)


# 函数用途: 以 600 权限原子写一个文件（目录 700）；失败返回 False。
def _write_private(path: Path, data: bytes) -> bool:
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except OSError:
        return False
    return True
