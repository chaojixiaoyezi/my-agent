"""Patch apply spec normalizer helper."""

from __future__ import annotations

from pathlib import Path


class PatchApplySpecNormalizer:
    """Normalize patch apply spec with write boundary enforcement.

    Used for backward compatibility when _patch_apply_service is not initialized.
    """

    @staticmethod
    def normalize(task, patch, workspace_root, build_unified_diff_func):
        """Normalize patch apply spec."""
        raw_path = str(patch.get("path") or "").strip()
        status = str(patch.get("status") or "").strip().lower()
        patch_type = str(
            patch.get("tool")
            or patch.get("type")
            or (
                "write_file"
                if any(key in patch for key in ("content", "new_content", "file_content", "after"))
                else ""
            )
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
            target = workspace_root / target
        target = target.resolve(strict=False)
        before_text = target.read_text(encoding="utf-8") if target.exists() else ""
        audit["diff_preview"] = diff_text or build_unified_diff_func(raw_path, before_text, content)

        return {
            "ok": True,
            "audit": audit,
            "content": content,
            "target": target,
            "patch_ref": patch,
        }