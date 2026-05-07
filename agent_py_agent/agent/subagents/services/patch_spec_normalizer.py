# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Patch apply spec normalizer helper."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# LLM: PatchSpecFields 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存补丁spec字段字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class PatchSpecFields:
    """Extracted patch fields used by apply validation."""

    raw_path: str
    status: str
    patch_type: str
    content: object
    diff_text: str


# LLM: PatchApplySpecNormalizer 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装补丁应用spec归一化器相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class PatchApplySpecNormalizer:
    """Normalize patch apply spec with write boundary enforcement.

    Used for backward compatibility when _patch_apply_service is not initialized.
    """

    # LLM: normalize 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 解析并归一化归一化的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def normalize(task, patch, workspace_root, build_unified_diff_func):
        """Normalize patch apply spec."""
        fields = _extract_patch_fields(patch)
        audit = _build_initial_audit(fields, patch)

        blocked = _validate_patch_spec(fields, audit, patch)
        if blocked:
            return blocked

        target, before_text = _resolve_target_path(fields.raw_path, workspace_root, audit)
        audit["diff_preview"] = fields.diff_text or build_unified_diff_func(fields.raw_path, before_text, fields.content)

        return {
            "ok": True,
            "audit": audit,
            "content": fields.content,
            "target": target,
            "patch_ref": patch,
        }


# LLM: _extract_patch_fields 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理extract补丁字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _extract_patch_fields(patch) -> PatchSpecFields:
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

    return PatchSpecFields(raw_path, status, patch_type, content, diff_text)


# LLM: _extract_patch_content 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理extract补丁内容相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
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


# LLM: _extract_patch_type 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理extract补丁type相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _extract_patch_type(patch) -> str:
    patch_type = patch.get("tool") or patch.get("type")
    if patch_type:
        return str(patch_type).strip().lower()
    if any(key in patch for key in ("content", "new_content", "file_content", "after")):
        return "write_file"
    return ""


# LLM: _build_initial_audit 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 构建initialaudit所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_initial_audit(fields: PatchSpecFields, patch):
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
        "path": fields.raw_path,
        "status": fields.status or "unknown",
        "review_status": str(patch.get("review_status") or "UNREVIEWED"),
        "summary": str(patch.get("summary") or ""),
        "patch_type": fields.patch_type or "unknown",
        "apply_status": "PENDING",
        "diff_preview": fields.diff_text,
        "actual_diff": "",
        "message": "",
    }


# LLM: _validate_patch_spec 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验补丁spec需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _validate_patch_spec(fields: PatchSpecFields, audit, patch):
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
    if not fields.raw_path:
        audit["apply_status"] = "BLOCKED"
        audit["message"] = "patch 缺少 path。"
        return {"ok": False, "audit": audit, "patch_ref": patch}
    if fields.status not in {"planned", "applied"}:
        audit["apply_status"] = "BLOCKED"
        audit["message"] = f"patch status={fields.status or 'unknown'} 不能进入 apply。"
        return {"ok": False, "audit": audit, "patch_ref": patch}
    if fields.patch_type and fields.patch_type not in {"write_file"}:
        audit["apply_status"] = "BLOCKED"
        audit["message"] = f"只支持 write_file patch，当前类型是 {fields.patch_type}。"
        return {"ok": False, "audit": audit, "patch_ref": patch}
    if not isinstance(fields.content, str):
        audit["apply_status"] = "BLOCKED"
        audit["message"] = "write_file patch 缺少完整 content，不能安全 apply。"
        audit["diff_preview"] = ""
        return {"ok": False, "audit": audit, "patch_ref": patch}
    return None


# LLM: _resolve_target_path 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 读取或查询target路径需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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
