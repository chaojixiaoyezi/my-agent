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
        raw_path, status, patch_type, content, diff_text = _extract_patch_fields(patch)
        audit = _build_initial_audit(raw_path, status, patch_type, patch, diff_text)

        blocked = _validate_patch_spec(raw_path, status, patch_type, content, audit, patch)
        if blocked:
            return blocked

        target, before_text = _resolve_target_path(raw_path, workspace_root, audit)
        audit["diff_preview"] = diff_text or build_unified_diff_func(raw_path, before_text, content)

        return {
            "ok": True,
            "audit": audit,
            "content": content,
            "target": target,
            "patch_ref": patch,
        }


def _extract_patch_fields(patch):
    """Extract and normalize patch fields.

    Args:
        patch: raw patch dict

    Returns:
        Tuple of (raw_path, status, patch_type, content, diff_text)
    """
    raw_path = str(patch.get("path") or "").strip()
    status = str(patch.get("status") or "").strip().lower()
    patch_type = _extract_patch_type(patch)

    content = _extract_patch_content(patch)
    diff_text = ""
    for key in ("diff", "patch", "patch_diff", "unified_diff"):
        value = patch.get(key)
        if isinstance(value, str) and value.strip():
            diff_text = value
            break

    return raw_path, status, patch_type, content, diff_text


def _extract_patch_content(patch):
    content = patch.get("content")
    if content is not None:
        return content
    return next(
        (
            patch.get(key)
            for key in ("new_content", "file_content", "desired_content", "after")
            if patch.get(key) is not None
        ),
        None,
    )


def _extract_patch_type(patch) -> str:
    patch_type = patch.get("tool") or patch.get("type")
    if patch_type:
        return str(patch_type).strip().lower()
    if any(key in patch for key in ("content", "new_content", "file_content", "after")):
        return "write_file"
    return ""


def _build_initial_audit(raw_path, status, patch_type, patch, diff_text):
    """Build initial audit dict from extracted fields.

    Args:
        raw_path: normalized file path
        status: normalized status
        patch_type: normalized patch type
        patch: original patch dict (for review_status, summary)
        diff_text: extracted diff text

    Returns:
        Initial audit dict
    """
    return {
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


def _validate_patch_spec(raw_path, status, patch_type, content, audit, patch):
    """Validate patch spec for early-return blockers.

    Args:
        raw_path: normalized file path
        status: normalized status
        patch_type: normalized patch type
        content: extracted content (may be None)
        audit: audit dict to update
        patch: original patch dict (for error return)

    Returns:
        Error dict with {"ok": False, "audit": ..., "patch_ref": ...} if blocked, else None
    """
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
        audit["diff_preview"] = ""
        return {"ok": False, "audit": audit, "patch_ref": patch}
    return None


def _resolve_target_path(raw_path, workspace_root, audit):
    """Resolve target path and read existing content.

    Args:
        raw_path: normalized file path string
        workspace_root: workspace root Path
        audit: audit dict (not modified)

    Returns:
        Tuple of (resolved Path, before_text string)
    """
    path_obj = Path(raw_path)
    if not path_obj.is_absolute():
        path_obj = workspace_root / path_obj
    path_obj = path_obj.resolve(strict=False)
    before_text = path_obj.read_text(encoding="utf-8") if path_obj.exists() else ""
    return path_obj, before_text
