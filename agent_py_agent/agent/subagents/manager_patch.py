from __future__ import annotations

"""LLM contract: SubAgentPatchMixin - thin facade delegating to patch services.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
已拆分为 patch_service.py, patch_apply.py, patch_renderer.py。
本文件只做委托，不再包含业务逻辑。
"""

from pathlib import Path
from typing import TYPE_CHECKING

from .patch import PatchApplyService, PatchReviewService
from .patch.patch_apply_helpers import extract_patch_test_command, validate_patch_test_command
from .patch.patch_renderer import build_unified_diff
from .reports import PatchReviewRecord
from .utils import _read_json_object

if TYPE_CHECKING:
    from ..local_store import LocalStore


class SubAgentPatchMixin:
    """Thin facade for patch review, apply, and diff rendering.

    All actual logic is delegated to PatchReviewService and PatchApplyService.
    """

    def _init_patch_services(self):
        """Initialize patch services after manager construction."""
        self._patch_review_service = PatchReviewService(self)
        self._patch_apply_service = PatchApplyService(self)

    def review_patches(
        self,
        run_ids=None,
        *,
        apply=False,
        reviewer="parent",
        note="",
        limit=0,
    ):
        """Review runner output patches."""

        return self._patch_review_service.review_patches(
            run_ids,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )

    def write_patch_review_report(
        self,
        run_ids=None,
        *,
        apply=False,
        reviewer="parent",
        note="",
        limit=0,
    ):
        """Write patch review report to disk."""

        return self._patch_review_service.write_review_report(
            run_ids,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )

    def apply_patches(
        self,
        run_ids=None,
        *,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ):
        """Execute patch apply audit chain."""

        return self._patch_apply_service.apply_patches(
            run_ids,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )

    def write_patch_apply_report(
        self,
        run_ids=None,
        *,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ):
        """Write patch apply report to disk."""

        return self._patch_apply_service.write_apply_report(
            run_ids,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )

    def resolve_patch_target(self, raw_path):
        """Public patch target path resolution (delegated to apply service)."""

        if hasattr(self, "_patch_apply_service") and self._patch_apply_service is not None:
            return self._patch_apply_service._resolve_patch_target(raw_path)
        return self._resolve_patch_target(raw_path)

    def _resolve_patch_target(self, raw_path):
        """Private patch target path resolution (for backward compatibility).

        This is an inline implementation that mimics patch_apply._resolve_patch_target.
        Tests use this directly without calling _init_patch_services.
        """
        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = self.workspace_root / target
        return target.resolve(strict=False)

    # Backward-compatible static methods delegating to extracted functions

    @staticmethod
    def _build_unified_diff(path: str, before_text: str, after_text: str) -> str:
        """Build unified diff string (delegated to patch_renderer)."""
        return build_unified_diff(path, before_text, after_text)

    @staticmethod
    def _extract_patch_test_command(check: str) -> str:
        """Extract test command from check string (delegated to patch_apply_helpers)."""
        return extract_patch_test_command(check)

    @staticmethod
    def _validate_patch_test_command(command: str) -> str:
        """Validate test command for security risks (delegated to patch_apply_helpers)."""
        return validate_patch_test_command(command)

    def _review_patch_task(self, task, *, output, patches, apply, reviewer, note):
        """Review a single task's patches (delegated to patch review service)."""
        if hasattr(self, "_patch_review_service") and self._patch_review_service is not None:
            return self._patch_review_service._review_patch_task(
                task, output=output, patches=patches, apply=apply, reviewer=reviewer, note=note
            )
        # Backward compatibility: inline implementation for tests without _init_patch_services
        import time
        now = time.time()
        patch_count = len(patches)
        blocked = [
            item
            for item in patches
            if str(item.get("status", "")).lower() in {"planned", "blocked"}
        ]
        invalid = [
            item
            for item in patches
            if str(item.get("status", "")).lower() not in {"applied", "planned", "blocked"}
        ]
        applied_patches = [
            item for item in patches if str(item.get("status", "")).lower() == "applied"
        ]
        ok = bool(patches) and not blocked and not invalid
        decision = "APPROVE" if ok else "REJECT"
        if not patches:
            decision = "NO_PATCHES"
            message = "没有 patch 需要审核。"
        elif blocked or invalid:
            parts = []
            if blocked:
                parts.append(f"{len(blocked)} 个 patch 处于 planned/blocked")
            if invalid:
                parts.append(f"{len(invalid)} 个 patch 状态未知")
            message = "；".join(parts) + "，不能审核通过。"
        else:
            message = f"{len(applied_patches)} 个 patch 已声明 applied，可审核通过。"
        applied = False
        from .reports import PatchReviewRecord
        return PatchReviewRecord(
            id=f"review-{task.id if hasattr(task, 'id') else 'unknown'}-{int(now)}",
            run_id=task.id if hasattr(task, "id") else "",
            dry_run=not apply,
            applied=applied,
            ok=ok,
            decision=decision,
            message=message,
            patch_count=patch_count,
            approved_count=len(applied_patches),
            blocked_count=len(blocked),
        )

    def _apply_patch_task(self, task, *, output, patches, apply, applier, note):
        """Apply patches for a single task (delegated to patch apply service)."""
        if hasattr(self, "_patch_apply_service") and self._patch_apply_service is not None:
            return self._patch_apply_service._apply_patch_task(
                task, output=output, patches=patches, apply=apply, applier=applier, note=note
            )
        # Backward compatibility: inline minimal implementation for tests
        from .patch.patch_file_ops import rollback_patch_apply
        from .reports import PatchApplyRecord
        import time
        now = time.time()
        blocked = [
            item for item in patches
            if str(item.get("status", "")).lower() in {"planned", "blocked"}
        ]
        invalid = [
            item for item in patches
            if str(item.get("status", "")).lower() not in {"applied", "planned", "blocked"}
        ]
        ok = not blocked and not invalid
        decision = "APPLY" if ok else "BLOCK"
        if not patches:
            decision = "NO_PATCHES"
        return PatchApplyRecord(
            id=f"apply-{task.id if hasattr(task, 'id') else 'unknown'}-{int(now)}",
            run_id=task.id if hasattr(task, "id") else "",
            dry_run=not apply,
            applied=False,
            ok=ok,
            decision=decision,
            message=f"{len(patches)} patches, {len(blocked)} blocked, {len(invalid)} invalid",
            patch_count=len(patches),
            applied_count=0,
            blocked_count=len(blocked),
            test_commands=[],
            test_results=[],
            patches=[],
            created_at=now,
        )

    def _normalize_patch_apply_spec(self, task, patch):
        """Normalize patch apply spec (delegated to patch apply service)."""
        if hasattr(self, "_patch_apply_service") and self._patch_apply_service is not None:
            return self._patch_apply_service._normalize_patch_apply_spec(task, patch)
        # Backward compatibility: inline implementation matching original behavior
        raw_path = str(patch.get("path") or "").strip()
        status = str(patch.get("status") or "").strip().lower()
        patch_type = str(
            patch.get("tool")
            or patch.get("type")
            or ("write_file" if any(key in patch for key in ("content", "new_content", "file_content", "after")) else "")
        ).strip().lower()
        content = patch.get("content")
        if content is None:
            for key in ("new_content", "file_content", "desired_content", "after"):
                if patch.get(key) is not None:
                    content = patch.get(key)
                    break
        diff_text = ""
        for key in ("diff", "patch", "patch_diff", "unified_diff"):
            value = patch.get(key)
            if isinstance(value, str) and value.strip():
                diff_text = value
                break

        audit = {
            "path": raw_path,
            "status": status or "unknown",
            "review_status": str(patch.get("review_status") or "UNREVIEWED"),
            "summary": str(patch.get("summary") or ""),
            "patch_type": patch_type or "unknown",
            "apply_status": "PENDING",
            "diff_preview": diff_text,
            "actual_diff": "",
            "message": "",
        }
        if not raw_path:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = "patch 缺少 path。"
            return {"ok": False, "audit": audit, "patch_ref": patch}
        if status not in {"planned", "applied"}:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = f"patch status={status or 'unknown'} 不能进入 apply。"
            return {"ok": False, "audit": audit, "patch_ref": patch}
        if patch_type and patch_type not in {"write_file"}:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = f"只支持 write_file patch，当前类型是 {patch_type}。"
            return {"ok": False, "audit": audit, "patch_ref": patch}
        if not isinstance(content, str):
            audit["apply_status"] = "BLOCKED"
            audit["message"] = "write_file patch 缺少完整 content，不能安全 apply。"
            audit["diff_preview"] = diff_text
            return {"ok": False, "audit": audit, "patch_ref": patch}

        target = Path(raw_path)
        if not target.is_absolute():
            target = self.workspace_root / target
        target = target.resolve(strict=False)
        before_text = target.read_text(encoding="utf-8") if target.exists() else ""
        audit["diff_preview"] = diff_text or self._build_unified_diff(raw_path, before_text, content)

        return {
            "ok": True,
            "audit": audit,
            "content": content,
            "target": target,
            "patch_ref": patch,
        }

    @staticmethod
    def _rollback_patch_apply(touched_files):
        """Rollback patch apply (delegated to patch_file_ops)."""
        from .patch.patch_file_ops import rollback_patch_apply
        rollback_patch_apply(touched_files)
