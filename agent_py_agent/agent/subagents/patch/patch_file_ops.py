
"""patch file write execution with rollback support.

Human version:
这个模块处理 patch 文件的实际写入和回滚操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import SubAgentTask


@dataclass(frozen=True)
class PatchFileApplyContext:
    patch_specs: list[dict]
    task: SubAgentTask
    applier: str
    note: str
    # 预像账本由调用方（PatchApplyExecutor.execute）在写者运行前持有；写到一半失败时调用方仍能据此完整回滚。
    preimages: dict[Path, dict] = field(default_factory=dict)


# LLM: 回滚结果是结构化事实：逐路径记录已恢复/已删除/失败，调用方据此如实上报，不能用“尝试过”冒充“已回滚”。
# 类用途: 汇总一次 patch 事务回滚的实际结果。
@dataclass(frozen=True)
class PatchRollbackOutcome:
    restored: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed


def do_apply_patches(ctx: PatchFileApplyContext) -> tuple[int, dict]:
    """Perform actual patch file writes and diff building.

    Returns (applied_count, touched_files).
    """

    import time as time_module

    applied_count = 0
    now = time_module.time()
    touched_files = ctx.preimages
    # 先为全部目标各捕获一次精确前像（字节+是否存在），任一目标读不出就在任何写入前失败；重复目标保留第一份前像。
    for spec in ctx.patch_specs:
        target = spec["target"]
        if target not in touched_files:
            touched_files[target] = _capture_preimage(target)

    for spec in ctx.patch_specs:
        _apply_patch_spec(spec, ctx=ctx, now=now)
        applied_count += 1

    return applied_count, touched_files


# LLM: 前像只认文件系统事实；读失败直接上抛（此时尚未写任何目标），不把读不出当作“原本不存在”。
# 函数用途: 记录目标写入前的精确字节与存在性。
def _capture_preimage(target: Path) -> dict:
    if not target.exists():
        return {"before_exists": False, "before_bytes": b""}
    return {"before_exists": True, "before_bytes": target.read_bytes()}


def _apply_patch_spec(spec: dict, *, ctx: PatchFileApplyContext, now: float) -> None:
    from .patch_renderer import build_unified_diff

    target = spec["target"]
    # 差异只用于展示：按当前盘上内容计算（重复目标对比上一份 patch 的结果），回滚依据始终是账本中的第一份前像。
    before_text = target.read_bytes().decode("utf-8", errors="replace") if target.exists() else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(spec["content"], encoding="utf-8")
    _mark_patch_applied(spec, applier=ctx.applier, now=now, note=ctx.note)
    spec["audit"]["apply_status"] = "APPLIED"
    spec["audit"]["actual_diff"] = build_unified_diff(spec["path"], before_text, spec["content"])
    spec["audit"]["message"] = "patch 已写入文件。"


def _mark_patch_applied(spec: dict, *, applier: str, now: float, note: str) -> None:
    spec["patch_ref"]["status"] = "applied"
    spec["patch_ref"]["apply_status"] = "APPLIED"
    spec["patch_ref"]["applied_by"] = applier
    spec["patch_ref"]["applied_at"] = now
    spec["patch_ref"]["review_status"] = "APPROVED"
    spec["patch_ref"]["reviewed_by"] = applier
    spec["patch_ref"]["reviewed_at"] = now
    if note:
        spec["patch_ref"]["apply_note"] = note
        spec["patch_ref"]["review_note"] = note


# LLM: 逐个目标独立恢复：原有文件按前像字节原样写回，事务新建的目标才删除；单个失败不跳过其余目标，并如实计入 failed。
# 函数用途: 按预像账本回滚一次 patch 事务并返回实际结果（写文件、删除事务新建文件）。
def rollback_patch_apply(touched_files: dict[Path, dict]) -> PatchRollbackOutcome:
    """Rollback patch apply by restoring original file contents."""

    restored: list[str] = []
    removed: list[str] = []
    failed: list[str] = []
    for path, snapshot in touched_files.items():
        try:
            if _rollback_touched_file(path, snapshot):
                removed.append(str(path))
            else:
                restored.append(str(path))
        except OSError:
            failed.append(str(path))
    return PatchRollbackOutcome(tuple(restored), tuple(removed), tuple(failed))


def _rollback_touched_file(path: Path, snapshot: dict) -> bool:
    if snapshot["before_exists"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(snapshot["before_bytes"])
        return False
    path.unlink(missing_ok=True)
    return True
