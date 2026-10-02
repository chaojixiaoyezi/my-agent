# LLM: 能力包 v2 块 4 的输入保护：原件清单的记录时机、task_input 候选、收尾时的就地修改判定与返工提示。只看结构化事实：
#   原件清单里的摘要、本 run 基线里的摘要、文件现在的摘要、钉住包的 input_policy；不读模型文字，不管是哪个工具改的。
#   就地修改 = 原件在本回合开始时还是原样（或已不是原样但本回合又改了），现在内容和本回合开始时不同、也和原件不同（删除也算）。
#   只对本任务钉住、声明了 input_policy=preserve_originals、且清单里记着匹配到这个原件的包生效。
#   副作用：capture_originals_for_run 读工作区并写原件清单和副本。改动同步 test_pack_verification_inputs.py。
# 模块用途: 发现并提示“用户给的输入原件被就地改了”，并给检查程序交任务开始时的原件。

from __future__ import annotations

from pathlib import Path

from .pack_verification_matching import FileState, WorkspaceScan, file_state
from .pack_verification_originals import (
    INPUT_POLICY_PRESERVE_ORIGINALS,
    OriginalFile,
    capture_task_originals,
    original_copy_path,
)
from .pack_verification_scope import enabled_verification_declarations

INPUT_MODIFIED_IN_PLACE = "INPUT_MODIFIED_IN_PLACE"
# 收尾发现输入原件被就地改时最多给几次返工提示（3a 审定 1 次）。
MAX_INPUT_REWORK_COUNT = 1
# 返工提示里最多列几个被改的原件。
MAX_INPUT_REWORK_ITEMS_COUNT = 5


# LLM: roots = (宿主核验目录, 工作区根)；scan 是本 run 刚做的基线扫描。清单已存在时由 capture_task_originals 跳过。
# 函数用途: 在任务第一次改工作区前记下输入原件清单和副本。
def capture_originals_for_run(roots: tuple[Path, Path], owner: object, scan: WorkspaceScan) -> None:
    capture_task_originals(roots, scan, enabled_verification_declarations(owner))


# LLM: 原件现在还是原样就交工作区里的文件；被改过但有可用副本就交副本（副本在工作区外，检查程序只读）；都不行就不是候选。
# 函数用途: 列出 task_input 的候选 (工作区相对路径, 交给检查程序的路径)。
def task_input_candidates(originals: dict[str, OriginalFile] | None, pack_root: Path | None,
                          workspace_root: Path) -> list[tuple[str, Path]]:
    candidates = []
    for relpath, original in sorted((originals or {}).items()):
        path = workspace_root / relpath
        if _sha(file_state(path)) == original.sha256:
            candidates.append((relpath, path))
            continue
        copy = original_copy_path(pack_root, original) if pack_root is not None else None
        if copy is not None:
            candidates.append((relpath, copy))
    return candidates


# LLM: protected 是本任务钉住、声明了 preserve_originals 的包 ID；baseline 是本 run 开始改动前的扫描。本 run 开始时不在基线里的原件
#   （之前回合已删除或扫描截断）不判，免得把旧回合的事算到本回合头上。返回每个被改原件的结构化事实。
# 函数用途: 找出本回合被就地改过或删掉的输入原件。
def modified_originals(originals: dict[str, OriginalFile] | None, baseline: WorkspaceScan | None,
                       context: tuple[Path, Path | None, frozenset[str]]) -> list[dict]:
    workspace_root, pack_root, protected = context
    if not originals or baseline is None or not protected:
        return []
    items = []
    for relpath, original in sorted(originals.items()):
        start, now = baseline.files.get(relpath), file_state(workspace_root / relpath)
        if start is None or not protected.intersection(original.packages):
            continue
        if _sha(now) != start.sha256 and _sha(now) != original.sha256:
            copy = original_copy_path(pack_root, original) if pack_root is not None else None
            items.append({"code": INPUT_MODIFIED_IN_PLACE, "path": relpath, "original_sha256": original.sha256,
                          "current_sha256": _sha(now), "copy_path": str(copy) if copy is not None else ""})
    return items


# 函数用途: 从钉住的包里挑出声明了 preserve_originals 的包 ID。
def preserving_packages(packages: tuple) -> frozenset[str]:
    return frozenset(package.installation.manifest.plugin_id for package in packages
                     if getattr(package.verification, "input_policy", "") == INPUT_POLICY_PRESERVE_ORIGINALS)


# LLM: 只列结构化事实（路径、有没有副本、副本在哪），建议用 cp 恢复（字节必须完全一致，手抄做不到）；改动另存新文件。
# 函数用途: 生成一次输入原件被就地修改的返工提示。
def input_rework_text(items: list[dict]) -> str:
    lines = ["宿主发现本回合就地改了用户给的输入原件（钉住的能力包要求保留原件）："]
    for item in items[:MAX_INPUT_REWORK_ITEMS_COUNT]:
        if item["copy_path"]:
            lines.append(f"- {item['path']}：原件副本在 {item['copy_path']}，可用 cp \"{item['copy_path']}\" \"{item['path']}\" 恢复。")
        else:
            lines.append(f"- {item['path']}：原件太大或没能留副本，请按原样恢复，或在答复里说明。")
    if not any(item["current_sha256"] for item in items):
        lines.append("（删除也算修改。）")
    lines.append("请先把原件恢复成原样，再把你的改动写进一个新文件，并在答复里写明新文件路径。")
    return "\n".join(lines)


# 函数用途: 取文件状态里的摘要；文件不存在时为空串。
def _sha(state: FileState | None) -> str:
    return state.sha256 if state is not None else ""
