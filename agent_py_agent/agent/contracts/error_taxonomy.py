# LLM: Error taxonomy gives tools, compact, and orchestration one language for recoverable failures.
# 模块用途: 定义稳定错误类型和恢复建议，避免路径错、权限错、模型失败等都混成“失败了”。

from __future__ import annotations

from dataclasses import dataclass


# LLM: ErrorContract is the machine-readable description of one failure type.
# 类用途: 保存错误代码、分类、是否可重试和推荐恢复动作，供工具/状态机/上下文包复用。
@dataclass(frozen=True)
class ErrorContract:
    code: str
    category: str
    retryable: bool
    recommended_action: str
    recovery_hint: str


ERROR_CONTRACTS: dict[str, ErrorContract] = {
    "PATH_INVALID": ErrorContract(
        code="PATH_INVALID",
        category="path",
        retryable=False,
        recommended_action="fix_path_or_read_refs",
        recovery_hint="修正路径，优先读取结构化 refs，不要从自然语言摘要里猜路径。",
    ),
    "PATH_OUTSIDE_WORKSPACE": ErrorContract(
        code="PATH_OUTSIDE_WORKSPACE",
        category="path",
        retryable=False,
        recommended_action="fix_path_within_allowed_roots",
        recovery_hint="修正路径到允许工作区内；如果确实需要新目录，走能力/权限申请链路。",
    ),
    "WRITE_FORBIDDEN": ErrorContract(
        code="WRITE_FORBIDDEN",
        category="permission",
        retryable=False,
        recommended_action="request_permission_or_choose_allowed_root",
        recovery_hint="写入被禁止；改写到 allowed_write_roots，或上报需要授权。",
    ),
    "TOOL_UNAVAILABLE": ErrorContract(
        code="TOOL_UNAVAILABLE",
        category="tool",
        retryable=False,
        recommended_action="request_capability_or_choose_available_tool",
        recovery_hint="工具不可用；查看 ToolManifest，换可执行工具或申请能力。",
    ),
    "TOOL_INVALID_ARGUMENTS": ErrorContract(
        code="TOOL_INVALID_ARGUMENTS",
        category="tool",
        retryable=True,
        recommended_action="repair_tool_arguments",
        recovery_hint="工具参数不合法；按工具 schema 修参数后可重试。",
    ),
    "TOOL_TIMEOUT": ErrorContract(
        code="TOOL_TIMEOUT",
        category="tool",
        retryable=True,
        recommended_action="retry_with_smaller_scope_or_longer_timeout",
        recovery_hint="工具超时；缩小读取/搜索范围，或使用更合适的超时配置。",
    ),
    "MODEL_UPSTREAM_FAILED": ErrorContract(
        code="MODEL_UPSTREAM_FAILED",
        category="model",
        retryable=True,
        recommended_action="retry_or_switch_model_backend",
        recovery_hint="模型上游失败；可重试，连续失败时切换模型或暂停等待。",
    ),
    "ARTIFACT_MISSING": ErrorContract(
        code="ARTIFACT_MISSING",
        category="artifact",
        retryable=True,
        recommended_action="read_or_rebuild_artifact_ref",
        recovery_hint="产物引用缺失；先按 refs 查找，找不到再重建产物。",
    ),
    "ACCEPTANCE_FAILED": ErrorContract(
        code="ACCEPTANCE_FAILED",
        category="acceptance",
        retryable=True,
        recommended_action="repair_against_acceptance_findings",
        recovery_hint="验收未通过；按 findings 修复后重新测试和验收。",
    ),
    "COMPACT_REF_MISSING": ErrorContract(
        code="COMPACT_REF_MISSING",
        category="compact",
        retryable=False,
        recommended_action="fallback_to_checkpoint_or_summary",
        recovery_hint="compact 引用缺失；降级读 checkpoint、summary、raw archive，不要继续自动执行。",
    ),
    "UNKNOWN_ERROR": ErrorContract(
        code="UNKNOWN_ERROR",
        category="unknown",
        retryable=False,
        recommended_action="stop_and_record_diagnostic",
        recovery_hint="未知失败；记录诊断信息，避免盲目复读工具调用。",
    ),
}


# LLM: error_contract returns a stable contract even for unknown legacy codes.
# 函数用途: 按错误代码读取合同；未知代码降级为 UNKNOWN_ERROR，保证调用方不崩。
def error_contract(code: str) -> ErrorContract:
    return ERROR_CONTRACTS.get(str(code or "").upper(), ERROR_CONTRACTS["UNKNOWN_ERROR"])


# LLM: classify_error maps common raw failure text to a stable error contract.
# 函数用途: 把工具/模型/compact 的失败文本归类为稳定错误类型，给后续恢复策略使用。
def classify_error(message: str) -> ErrorContract:
    text = str(message or "").lower()
    if "outside workspace" in text or "path_outside_workspace" in text:
        return error_contract("PATH_OUTSIDE_WORKSPACE")
    if "permission" in text or "forbidden" in text or "denied" in text or "write_forbidden" in text:
        return error_contract("WRITE_FORBIDDEN")
    if "invalid argument" in text or "invalid_parameters" in text or "schema" in text:
        return error_contract("TOOL_INVALID_ARGUMENTS")
    if "tool unavailable" in text or "unknown tool" in text or "not found tool" in text:
        return error_contract("TOOL_UNAVAILABLE")
    if "artifact" in text and ("missing" in text or "not found" in text):
        return error_contract("ARTIFACT_MISSING")
    if "acceptance" in text and ("failed" in text or "not passed" in text):
        return error_contract("ACCEPTANCE_FAILED")
    if "compact" in text and ("missing" in text or "ref" in text):
        return error_contract("COMPACT_REF_MISSING")
    if "provider" in text or "upstream" in text or "anthropic" in text or "model" in text:
        return error_contract("MODEL_UPSTREAM_FAILED")
    if "timeout" in text or "timed out" in text:
        return error_contract("TOOL_TIMEOUT")
    if "path" in text and ("invalid" in text or "missing" in text):
        return error_contract("PATH_INVALID")
    if "文件不存在" in text or "路径不存在" in text or "目标不是文件" in text:
        return error_contract("PATH_INVALID")
    return error_contract("UNKNOWN_ERROR")


# LLM: tool_failure_taxonomy exposes stable codes for ToolManifest without duplicating constants.
# 函数用途: 返回工具清单要展示的错误分类代码列表，供 context bundle 和工具网关复用。
def tool_failure_taxonomy() -> list[str]:
    return [
        "PATH_INVALID",
        "PATH_OUTSIDE_WORKSPACE",
        "WRITE_FORBIDDEN",
        "TOOL_UNAVAILABLE",
        "TOOL_INVALID_ARGUMENTS",
        "TOOL_TIMEOUT",
        "MODEL_UPSTREAM_FAILED",
        "ARTIFACT_MISSING",
        "ACCEPTANCE_FAILED",
        "COMPACT_REF_MISSING",
        "UNKNOWN_ERROR",
    ]


__all__ = ["ERROR_CONTRACTS", "ErrorContract", "classify_error", "error_contract", "tool_failure_taxonomy"]
