
from __future__ import annotations

import re
from dataclasses import dataclass

from .recovery_actions import RecoveryAction


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
        recommended_action=RecoveryAction.FIX_PATH.value,
        recovery_hint="修正路径，优先读取结构化 refs，不要从自然语言摘要里猜路径。",
    ),
    "PATH_OUTSIDE_WORKSPACE": ErrorContract(
        code="PATH_OUTSIDE_WORKSPACE",
        category="path",
        retryable=False,
        recommended_action=RecoveryAction.FIX_PATH_WITHIN_ALLOWED_ROOTS.value,
        recovery_hint="修正路径到允许工作区内；如果确实需要新目录，走能力/权限申请链路。",
    ),
    "PATH_NOT_FOUND": ErrorContract(
        code="PATH_NOT_FOUND",
        category="path",
        retryable=True,
        recommended_action=RecoveryAction.RETRY.value,
        recovery_hint="目标路径不存在；优先使用工具返回的 candidate_paths，或者用 list_files/search_text 重新定位。",
    ),
    "WRITE_FORBIDDEN": ErrorContract(
        code="WRITE_FORBIDDEN",
        category="permission",
        retryable=False,
        recommended_action=RecoveryAction.REQUEST_PERMISSION.value,
        recovery_hint="写入被禁止；改写到 allowed_write_roots，或上报需要授权。",
    ),
    "APPROVAL_REQUIRED": ErrorContract(
        code="APPROVAL_REQUIRED",
        category="permission",
        retryable=True,
        recommended_action=RecoveryAction.REQUEST_APPROVAL.value,
        recovery_hint="当前动作需要审批；先走审批链路，或者改成不需要高危权限的安全动作。",
    ),
    "TOOL_UNAVAILABLE": ErrorContract(
        code="TOOL_UNAVAILABLE",
        category="tool",
        retryable=False,
        recommended_action=RecoveryAction.REQUEST_CAPABILITY.value,
        recovery_hint="工具不可用；查看 ToolManifest，换可执行工具或申请能力。",
    ),
    "TOOL_INVALID_ARGUMENTS": ErrorContract(
        code="TOOL_INVALID_ARGUMENTS",
        category="tool",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
        recovery_hint="工具参数不合法；按工具 schema 修参数后可重试。",
    ),
    "TOOL_TIMEOUT": ErrorContract(
        code="TOOL_TIMEOUT",
        category="tool",
        retryable=True,
        recommended_action=RecoveryAction.RETRY.value,
        recovery_hint="工具超时；缩小读取/搜索范围，或使用更合适的超时配置。",
    ),
    "COMMAND_FAILED": ErrorContract(
        code="COMMAND_FAILED",
        category="tool",
        retryable=True,
        recommended_action=RecoveryAction.CHANGE_STRATEGY.value,
        recovery_hint="shell 命令返回非零状态；读取 stdout/stderr，修正命令或换成更可靠的专用工具。",
    ),
    "RATE_LIMITED": ErrorContract(
        code="RATE_LIMITED",
        category="model",
        retryable=True,
        recommended_action=RecoveryAction.RETRY_AFTER_BACKOFF.value,
        recovery_hint="请求触发速率限制；退避后重试，或切换可用后端。",
    ),
    "QUOTA_EXCEEDED": ErrorContract(
        code="QUOTA_EXCEEDED",
        category="model",
        retryable=False,
        recommended_action=RecoveryAction.SWITCH_BACKEND.value,
        recovery_hint="配额耗尽；切换可用模型/账号，或请求补充配额。",
    ),
    "MAINTENANCE": ErrorContract(
        code="MAINTENANCE",
        category="model",
        retryable=True,
        recommended_action=RecoveryAction.WAIT.value,
        recovery_hint="上游处于维护窗口；等待恢复或切换后端。",
    ),
    "NETWORK_FILE_URL_BLOCKED": ErrorContract(
        code="NETWORK_FILE_URL_BLOCKED",
        category="network",
        retryable=False,
        recommended_action=RecoveryAction.USE_HTTP_URL.value,
        recovery_hint="网络工具拒绝 file:// URL；改用可审计的 http/https 公网地址或本地文件工具。",
    ),
    "NETWORK_HOST_REQUIRED": ErrorContract(
        code="NETWORK_HOST_REQUIRED",
        category="network",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_URL_HOST.value,
        recovery_hint="URL 缺少可解析主机；补齐结构化 url 字段后再调用网络工具。",
    ),
    "NETWORK_ALWAYS_BLOCKED_HOST": ErrorContract(
        code="NETWORK_ALWAYS_BLOCKED_HOST",
        category="network",
        retryable=False,
        recommended_action=RecoveryAction.REPORT_BLOCKER.value,
        recovery_hint="目标是云 metadata 或凭证端点主机；无论私网授权如何都不能访问。",
    ),
    "NETWORK_ALWAYS_BLOCKED_IP": ErrorContract(
        code="NETWORK_ALWAYS_BLOCKED_IP",
        category="network",
        retryable=False,
        recommended_action=RecoveryAction.REPORT_BLOCKER.value,
        recovery_hint="目标解析到 metadata/link-local 凭证端点；无论私网授权如何都不能访问。",
    ),
    "NETWORK_PRIVATE_HOST_BLOCKED": ErrorContract(
        code="NETWORK_PRIVATE_HOST_BLOCKED",
        category="network",
        retryable=False,
        recommended_action=RecoveryAction.CHOOSE_PUBLIC_URL.value,
        recovery_hint="目标是本机/私网/特殊地址；换公网来源，或通过结构化 allowlist 明确授权。",
    ),
    "NETWORK_PRIVATE_IP_BLOCKED": ErrorContract(
        code="NETWORK_PRIVATE_IP_BLOCKED",
        category="network",
        retryable=False,
        recommended_action=RecoveryAction.CHOOSE_PUBLIC_URL.value,
        recovery_hint="域名解析到私网/特殊地址；不要继续请求，换公网来源或走结构化授权。",
    ),
    "NETWORK_DNS_REBINDING_BLOCKED": ErrorContract(
        code="NETWORK_DNS_REBINDING_BLOCKED",
        category="network",
        retryable=False,
        recommended_action=RecoveryAction.REPORT_BLOCKER.value,
        recovery_hint="DNS 复查发现重绑定到私网；停止请求并记录诊断，不能自动重试。",
    ),
    "NETWORK_HOST_RESOLUTION_FAILED": ErrorContract(
        code="NETWORK_HOST_RESOLUTION_FAILED",
        category="network",
        retryable=True,
        recommended_action=RecoveryAction.RETRY.value,
        recovery_hint="主机解析失败；可稍后重试，或换成可解析的公开来源。",
    ),
    "NETWORK_RESOLVED_IP_INVALID": ErrorContract(
        code="NETWORK_RESOLVED_IP_INVALID",
        category="network",
        retryable=False,
        recommended_action=RecoveryAction.CHOOSE_VALID_PUBLIC_URL.value,
        recovery_hint="DNS 返回值不是有效 IP；换可信公开 URL，不要继续请求。",
    ),
    "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED": ErrorContract(
        code="TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED",
        category="tool",
        retryable=True,
        recommended_action=RecoveryAction.CHANGE_STRATEGY.value,
        recovery_hint="同一工具同一参数同类失败已经重复过多次；不要原样重试，先改参数、换工具、换来源或记录明确阻塞原因。",
    ),
    "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED": ErrorContract(
        code="TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED",
        category="tool",
        retryable=True,
        recommended_action=RecoveryAction.CHANGE_STRATEGY.value,
        recovery_hint="同一只读工具同一参数连续返回相同结果；不要继续原样读取，先写 checkpoint、换参数或记录阻塞原因。",
    ),
    "SYSTEM_LEDGER_WRITE_BLOCKED": ErrorContract(
        code="SYSTEM_LEDGER_WRITE_BLOCKED",
        category="tool",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
        recovery_hint="系统运行账本不能用普通文件写入工具覆盖；改用对应专用工具更新，例如 task_progress。",
    ),
    "MODEL_UPSTREAM_FAILED": ErrorContract(
        code="MODEL_UPSTREAM_FAILED",
        category="model",
        retryable=True,
        recommended_action=RecoveryAction.RETRY.value,
        recovery_hint="模型上游失败；可重试，连续失败时切换模型或暂停等待。",
    ),
    "ARTIFACT_MISSING": ErrorContract(
        code="ARTIFACT_MISSING",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.READ_ARTIFACT_REF.value,
        recovery_hint="产物引用缺失；先按 refs 查找，找不到再重建产物。",
    ),
    "STAGED_ARTIFACT_MISSING": ErrorContract(
        code="STAGED_ARTIFACT_MISSING",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.MATERIALIZE_CHECKPOINT.value,
        recovery_hint="阶段产物缺失；先真实写出 checkpoint，再继续后续 builder 或最终产物。",
    ),
    "STAGED_ARTIFACT_EMPTY": ErrorContract(
        code="STAGED_ARTIFACT_EMPTY",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.REWRITE_CHECKPOINT.value,
        recovery_hint="阶段产物为空；补齐最小有效内容后，再继续后续阶段。",
    ),
    "STAGED_JSON_INVALID": ErrorContract(
        code="STAGED_JSON_INVALID",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value,
        recovery_hint="阶段 JSON 无法解析或已截断；先修成完整可解析 JSON，再继续后续阶段。",
    ),
    "STAGED_JSON_NO_ROWS": ErrorContract(
        code="STAGED_JSON_NO_ROWS",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.WRITE_NON_EMPTY_STRUCTURED_ROWS.value,
        recovery_hint="阶段 JSON 没有有效 rows/sheets 数据；先补齐非空结构化数据，再继续 builder。",
    ),
    "STAGED_JSON_DUPLICATE_SHEET_NAMES": ErrorContract(
        code="STAGED_JSON_DUPLICATE_SHEET_NAMES",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value,
        recovery_hint="阶段 JSON 的表格 sheet 身份重复；修正为唯一 sheet 后再继续 builder。",
    ),
    "STAGED_JSON_TABLE_SHAPE_INVALID": ErrorContract(
        code="STAGED_JSON_TABLE_SHAPE_INVALID",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value,
        recovery_hint="阶段 JSON 的表格结构不一致；修正 sheets、columns、rows 后再继续 builder。",
    ),
    "STAGED_JSON_REQUIRED_COLUMNS_MISSING": ErrorContract(
        code="STAGED_JSON_REQUIRED_COLUMNS_MISSING",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value,
        recovery_hint="阶段 JSON 缺少合同声明的必需列；补齐 required_columns 后再继续 builder。",
    ),
    "STAGED_JSON_REQUIRED_COLUMN_EMPTY_VALUES": ErrorContract(
        code="STAGED_JSON_REQUIRED_COLUMN_EMPTY_VALUES",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value,
        recovery_hint="阶段 JSON 的必填列存在空值；补齐 required_columns 的非空值后再继续 builder。",
    ),
    "API_JSON_EMPTY_EVIDENCE_FIELD": ErrorContract(
        code="API_JSON_EMPTY_EVIDENCE_FIELD",
        category="tool",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
        recovery_hint="API 映射出的证据字段为空；为该字段补 default/default_template，或改成非空来源路径后重试。",
    ),
    "STAGED_JSON_TOO_FEW_SHEETS": ErrorContract(
        code="STAGED_JSON_TOO_FEW_SHEETS",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value,
        recovery_hint="阶段 JSON 的 sheet 数少于合同要求；补齐 sheets 后再继续 builder。",
    ),
    "EVIDENCE_SOURCE_UNREADABLE": ErrorContract(
        code="EVIDENCE_SOURCE_UNREADABLE",
        category="evidence",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_EVIDENCE_REFS.value,
        recovery_hint="证据来源缺少可审计的 source_id、uri 或 artifact_ref；补齐结构化 source_refs 后再继续。",
    ),
    "EVIDENCE_CLAIM_UNSOURCED": ErrorContract(
        code="EVIDENCE_CLAIM_UNSOURCED",
        category="evidence",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_EVIDENCE_REFS.value,
        recovery_hint="证据 claim 没有关联 source_ids；把关键字段 claim 绑定到结构化来源后再继续。",
    ),
    "EVIDENCE_SOURCE_MISSING": ErrorContract(
        code="EVIDENCE_SOURCE_MISSING",
        category="evidence",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_EVIDENCE_REFS.value,
        recovery_hint="证据 claim 引用了不存在的 source_id；修正 source_refs 和 claim.source_ids 后再继续。",
    ),
    "EVIDENCE_CLAIM_UNVERIFIED": ErrorContract(
        code="EVIDENCE_CLAIM_UNVERIFIED",
        category="evidence",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_EVIDENCE_REFS.value,
        recovery_hint="关键 claim 尚未标记 VERIFIED；完成可审计验证并更新 verification_status 后再继续。",
    ),
    "EVIDENCE_REQUIRED_FIELD_MISSING": ErrorContract(
        code="EVIDENCE_REQUIRED_FIELD_MISSING",
        category="evidence",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_EVIDENCE_REFS.value,
        recovery_hint="必需字段缺少结构化 claim；为 required_fields 补齐 claims/source_ids 后再继续。",
    ),
    "TARGET_PENDING_WRITE": ErrorContract(
        code="TARGET_PENDING_WRITE",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.WRITE_TARGET_ARTIFACT.value,
        recovery_hint="目标文件尚未写出；用 write_file 或 apply_patch 写出目标文件后再读取或验收。",
    ),
    "ACCEPTANCE_FAILED": ErrorContract(
        code="ACCEPTANCE_FAILED",
        category="acceptance",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_AGAINST_ACCEPTANCE_FINDINGS.value,
        recovery_hint="验收未通过；按 findings 修复后重新测试和验收。",
    ),
    "COMPACT_REF_MISSING": ErrorContract(
        code="COMPACT_REF_MISSING",
        category="compact",
        retryable=False,
        recommended_action=RecoveryAction.RECOVER_FROM_CHECKPOINT.value,
        recovery_hint="compact 引用缺失；读取 checkpoint、summary、raw archive 做恢复，不要继续自动执行。",
    ),
    "CONTEXT_COMPACT_DEFERRED": ErrorContract(
        code="CONTEXT_COMPACT_DEFERRED",
        category="compact",
        retryable=True,
        recommended_action=RecoveryAction.RECOVER_FROM_CHECKPOINT.value,
        recovery_hint="当前上下文需要先 compact/resume；这个工具调用已经登记为未执行，恢复后再从同一目标继续。",
    ),
    "NO_PROGRESS": ErrorContract(
        code="NO_PROGRESS",
        category="orchestration",
        retryable=True,
        recommended_action=RecoveryAction.CHANGE_STRATEGY.value,
        recovery_hint="连续多轮没有新进展；不要原样重复，改策略、缩小范围、换工具，或者明确阻塞后停下。",
    ),
    "SPREADSHEET_SOURCE_MISSING": ErrorContract(
        code="SPREADSHEET_SOURCE_MISSING",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.WRITE_STRUCTURED_SOURCE_DATA.value,
        recovery_hint="表格源数据缺失；写出任意声明的机器可读 JSON，或直接传 sheets，再重新生成 workbook。",
    ),
    "SPREADSHEET_SOURCE_INVALID": ErrorContract(
        code="SPREADSHEET_SOURCE_INVALID",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_STRUCTURED_SOURCE_JSON.value,
        recovery_hint="表格源 JSON 无法解析；修复 JSON 结构后重新生成 workbook。",
    ),
    "SPREADSHEET_SOURCE_NO_ROWS": ErrorContract(
        code="SPREADSHEET_SOURCE_NO_ROWS",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.COLLECT_NON_EMPTY_ROWS.value,
        recovery_hint="表格源数据没有非空行；先补齐 rows/sheets 数据，再用通用代码生成 workbook。",
    ),
    "MARKDOWN_SOURCE_MISSING": ErrorContract(
        code="MARKDOWN_SOURCE_MISSING",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.WRITE_MARKDOWN_SOURCE.value,
        recovery_hint="Markdown 源文档缺失；先写出源文档，再用通用代码生成目标文档。",
    ),
    "MARKDOWN_SOURCE_EMPTY": ErrorContract(
        code="MARKDOWN_SOURCE_EMPTY",
        category="artifact",
        retryable=True,
        recommended_action=RecoveryAction.WRITE_NON_EMPTY_MARKDOWN_SOURCE.value,
        recovery_hint="Markdown 源文档为空；补齐正文后再用通用代码生成目标文档。",
    ),
    "UNKNOWN_ERROR": ErrorContract(
        code="UNKNOWN_ERROR",
        category="unknown",
        retryable=False,
        recommended_action=RecoveryAction.REPORT_BLOCKER.value,
        recovery_hint="未知失败；记录诊断信息，避免盲目复读工具调用。",
    ),
}


def error_contract(code: str) -> ErrorContract:
    return ERROR_CONTRACTS.get(str(code or "").upper(), ERROR_CONTRACTS["UNKNOWN_ERROR"])


def classify_error(message: str) -> ErrorContract:
    matches = _explicit_error_code_matches(str(message or ""), ERROR_CONTRACTS.keys())
    if matches:
        _, code = sorted(matches, key=lambda item: (-item[0], item[1]))[0]
        return error_contract(code)
    return error_contract("UNKNOWN_ERROR")


def _explicit_error_code_matches(text: str, contract_codes) -> list[tuple[int, str]]:
    lowered = text.lower()
    matches: list[tuple[int, str]] = []
    for code in contract_codes:
        variants = (code.lower(), code.lower().replace("_", "-"))
        if any(re.search(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", lowered) for variant in variants):
            matches.append((1000, code))
    return matches


def tool_failure_taxonomy() -> list[str]:
    return sorted(ERROR_CONTRACTS)


__all__ = [
    "ERROR_CONTRACTS",
    "ErrorContract",
    "classify_error",
    "error_contract",
    "tool_failure_taxonomy",
]
