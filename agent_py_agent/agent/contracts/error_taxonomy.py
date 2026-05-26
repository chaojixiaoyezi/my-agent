# LLM: Error taxonomy gives tools, compact, and orchestration one language for recoverable failures.
# 模块用途: 定义稳定错误类型和恢复建议，避免路径错、权限错、模型失败等都混成“失败了”。

from __future__ import annotations

from dataclasses import dataclass

from .error_classification_rules import matched_error_codes


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
    "APPROVAL_REQUIRED": ErrorContract(
        code="APPROVAL_REQUIRED",
        category="permission",
        retryable=True,
        recommended_action="request_approval_or_choose_safe_action",
        recovery_hint="当前动作需要审批；先走审批链路，或者改成不需要高危权限的安全动作。",
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
    "RATE_LIMITED": ErrorContract(
        code="RATE_LIMITED",
        category="model",
        retryable=True,
        recommended_action="retry_after_backoff_or_switch_backend",
        recovery_hint="请求触发速率限制；退避后重试，或切换可用后端。",
    ),
    "QUOTA_EXCEEDED": ErrorContract(
        code="QUOTA_EXCEEDED",
        category="model",
        retryable=False,
        recommended_action="switch_backend_or_request_quota",
        recovery_hint="配额耗尽；切换可用模型/账号，或请求补充配额。",
    ),
    "MAINTENANCE": ErrorContract(
        code="MAINTENANCE",
        category="model",
        retryable=True,
        recommended_action="wait_or_switch_backend",
        recovery_hint="上游处于维护窗口；等待恢复或切换后端。",
    ),
    "NETWORK_FILE_URL_BLOCKED": ErrorContract(
        code="NETWORK_FILE_URL_BLOCKED",
        category="network",
        retryable=False,
        recommended_action="use_http_or_https_public_url",
        recovery_hint="网络工具拒绝 file:// URL；改用可审计的 http/https 公网地址或本地文件工具。",
    ),
    "NETWORK_HOST_REQUIRED": ErrorContract(
        code="NETWORK_HOST_REQUIRED",
        category="network",
        retryable=True,
        recommended_action="repair_url_host",
        recovery_hint="URL 缺少可解析主机；补齐结构化 url 字段后再调用网络工具。",
    ),
    "NETWORK_ALWAYS_BLOCKED_HOST": ErrorContract(
        code="NETWORK_ALWAYS_BLOCKED_HOST",
        category="network",
        retryable=False,
        recommended_action="stop_and_report_blocked_metadata_endpoint",
        recovery_hint="目标是云 metadata 或凭证端点主机；无论私网授权如何都不能访问。",
    ),
    "NETWORK_ALWAYS_BLOCKED_IP": ErrorContract(
        code="NETWORK_ALWAYS_BLOCKED_IP",
        category="network",
        retryable=False,
        recommended_action="stop_and_report_blocked_metadata_endpoint",
        recovery_hint="目标解析到 metadata/link-local 凭证端点；无论私网授权如何都不能访问。",
    ),
    "NETWORK_PRIVATE_HOST_BLOCKED": ErrorContract(
        code="NETWORK_PRIVATE_HOST_BLOCKED",
        category="network",
        retryable=False,
        recommended_action="choose_public_url_or_request_private_host_grant",
        recovery_hint="目标是本机/私网/特殊地址；换公网来源，或通过结构化 allowlist 明确授权。",
    ),
    "NETWORK_PRIVATE_IP_BLOCKED": ErrorContract(
        code="NETWORK_PRIVATE_IP_BLOCKED",
        category="network",
        retryable=False,
        recommended_action="choose_public_url_or_request_private_host_grant",
        recovery_hint="域名解析到私网/特殊地址；不要继续请求，换公网来源或走结构化授权。",
    ),
    "NETWORK_DNS_REBINDING_BLOCKED": ErrorContract(
        code="NETWORK_DNS_REBINDING_BLOCKED",
        category="network",
        retryable=False,
        recommended_action="stop_and_record_network_safety_diagnostic",
        recovery_hint="DNS 复查发现重绑定到私网；停止请求并记录诊断，不能自动重试。",
    ),
    "NETWORK_HOST_RESOLUTION_FAILED": ErrorContract(
        code="NETWORK_HOST_RESOLUTION_FAILED",
        category="network",
        retryable=True,
        recommended_action="retry_or_choose_resolvable_public_url",
        recovery_hint="主机解析失败；可稍后重试，或换成可解析的公开来源。",
    ),
    "NETWORK_RESOLVED_IP_INVALID": ErrorContract(
        code="NETWORK_RESOLVED_IP_INVALID",
        category="network",
        retryable=False,
        recommended_action="choose_valid_public_url",
        recovery_hint="DNS 返回值不是有效 IP；换可信公开 URL，不要继续请求。",
    ),
    "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED": ErrorContract(
        code="TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED",
        category="tool",
        retryable=True,
        recommended_action="change_strategy",
        recovery_hint="同一工具同一参数同类失败已经重复过多次；不要原样重试，先改参数、换工具、换来源或记录明确阻塞原因。",
    ),
    "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED": ErrorContract(
        code="TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED",
        category="tool",
        retryable=True,
        recommended_action="change_strategy_or_materialize_progress",
        recovery_hint="同一只读工具同一参数连续返回相同结果；不要继续原样读取，先写 checkpoint、换参数或记录阻塞原因。",
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
    "STAGED_ARTIFACT_MISSING": ErrorContract(
        code="STAGED_ARTIFACT_MISSING",
        category="artifact",
        retryable=True,
        recommended_action="materialize_checkpoint",
        recovery_hint="阶段产物缺失；先真实写出 checkpoint，再继续后续 builder 或最终产物。",
    ),
    "STAGED_ARTIFACT_EMPTY": ErrorContract(
        code="STAGED_ARTIFACT_EMPTY",
        category="artifact",
        retryable=True,
        recommended_action="rewrite_checkpoint",
        recovery_hint="阶段产物为空；补齐最小有效内容后，再继续后续阶段。",
    ),
    "STAGED_JSON_INVALID": ErrorContract(
        code="STAGED_JSON_INVALID",
        category="artifact",
        retryable=True,
        recommended_action="repair_structured_checkpoint_json",
        recovery_hint="阶段 JSON 无法解析或已截断；先修成完整可解析 JSON，再继续后续阶段。",
    ),
    "STAGED_JSON_NO_ROWS": ErrorContract(
        code="STAGED_JSON_NO_ROWS",
        category="artifact",
        retryable=True,
        recommended_action="write_non_empty_structured_rows",
        recovery_hint="阶段 JSON 没有有效 rows/sheets 数据；先补齐非空结构化数据，再继续 builder。",
    ),
    "STAGED_JSON_DUPLICATE_SHEET_NAMES": ErrorContract(
        code="STAGED_JSON_DUPLICATE_SHEET_NAMES",
        category="artifact",
        retryable=True,
        recommended_action="repair_structured_checkpoint_json",
        recovery_hint="阶段 JSON 的表格 sheet 身份重复；修正为唯一 sheet 后再继续 builder。",
    ),
    "STAGED_JSON_TABLE_SHAPE_INVALID": ErrorContract(
        code="STAGED_JSON_TABLE_SHAPE_INVALID",
        category="artifact",
        retryable=True,
        recommended_action="repair_structured_checkpoint_json",
        recovery_hint="阶段 JSON 的表格结构不一致；修正 sheets、columns、rows 后再继续 builder。",
    ),
    "STAGED_JSON_REQUIRED_COLUMNS_MISSING": ErrorContract(
        code="STAGED_JSON_REQUIRED_COLUMNS_MISSING",
        category="artifact",
        retryable=True,
        recommended_action="repair_structured_checkpoint_json",
        recovery_hint="阶段 JSON 缺少合同声明的必需列；补齐 required_columns 后再继续 builder。",
    ),
    "STAGED_JSON_REQUIRED_COLUMN_EMPTY_VALUES": ErrorContract(
        code="STAGED_JSON_REQUIRED_COLUMN_EMPTY_VALUES",
        category="artifact",
        retryable=True,
        recommended_action="repair_structured_checkpoint_json",
        recovery_hint="阶段 JSON 的必填列存在空值；补齐 required_columns 的非空值后再继续 builder。",
    ),
    "API_JSON_EMPTY_EVIDENCE_FIELD": ErrorContract(
        code="API_JSON_EMPTY_EVIDENCE_FIELD",
        category="tool",
        retryable=True,
        recommended_action="repair_tool_arguments",
        recovery_hint="API 映射出的证据字段为空；为该字段补 default/default_template，或改成非空来源路径后重试。",
    ),
    "STAGED_JSON_TOO_FEW_SHEETS": ErrorContract(
        code="STAGED_JSON_TOO_FEW_SHEETS",
        category="artifact",
        retryable=True,
        recommended_action="repair_structured_checkpoint_json",
        recovery_hint="阶段 JSON 的 sheet 数少于合同要求；补齐 sheets 后再继续 builder。",
    ),
    "EVIDENCE_SOURCE_UNREADABLE": ErrorContract(
        code="EVIDENCE_SOURCE_UNREADABLE",
        category="evidence",
        retryable=True,
        recommended_action="repair_evidence_refs",
        recovery_hint="证据来源缺少可审计的 source_id、uri 或 artifact_ref；补齐结构化 source_refs 后再继续。",
    ),
    "EVIDENCE_CLAIM_UNSOURCED": ErrorContract(
        code="EVIDENCE_CLAIM_UNSOURCED",
        category="evidence",
        retryable=True,
        recommended_action="repair_evidence_refs",
        recovery_hint="证据 claim 没有关联 source_ids；把关键字段 claim 绑定到结构化来源后再继续。",
    ),
    "EVIDENCE_SOURCE_MISSING": ErrorContract(
        code="EVIDENCE_SOURCE_MISSING",
        category="evidence",
        retryable=True,
        recommended_action="repair_evidence_refs",
        recovery_hint="证据 claim 引用了不存在的 source_id；修正 source_refs 和 claim.source_ids 后再继续。",
    ),
    "EVIDENCE_CLAIM_UNVERIFIED": ErrorContract(
        code="EVIDENCE_CLAIM_UNVERIFIED",
        category="evidence",
        retryable=True,
        recommended_action="repair_evidence_refs",
        recovery_hint="关键 claim 尚未标记 VERIFIED；完成可审计验证并更新 verification_status 后再继续。",
    ),
    "EVIDENCE_REQUIRED_FIELD_MISSING": ErrorContract(
        code="EVIDENCE_REQUIRED_FIELD_MISSING",
        category="evidence",
        retryable=True,
        recommended_action="repair_evidence_refs",
        recovery_hint="必需字段缺少结构化 claim；为 required_fields 补齐 claims/source_ids 后再继续。",
    ),
    "TARGET_PENDING_FILE_WRITE_SESSION": ErrorContract(
        code="TARGET_PENDING_FILE_WRITE_SESSION",
        category="artifact",
        retryable=True,
        recommended_action="continue_pending_file_write_session",
        recovery_hint="目标文件尚未 materialize；继续推荐的 file_write_session 并 finish 后再读取最终文件。",
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
    "NO_PROGRESS": ErrorContract(
        code="NO_PROGRESS",
        category="orchestration",
        retryable=True,
        recommended_action="change_strategy_or_stop",
        recovery_hint="连续多轮没有新进展；不要原样重复，改策略、缩小范围、换工具，或者明确阻塞后停下。",
    ),
    "SPREADSHEET_SOURCE_MISSING": ErrorContract(
        code="SPREADSHEET_SOURCE_MISSING",
        category="artifact",
        retryable=True,
        recommended_action="write_or_fix_structured_source_data",
        recovery_hint="表格源数据缺失；写出任意声明的机器可读 JSON，或直接传 sheets，再重新生成 workbook。",
    ),
    "SPREADSHEET_SOURCE_INVALID": ErrorContract(
        code="SPREADSHEET_SOURCE_INVALID",
        category="artifact",
        retryable=True,
        recommended_action="repair_structured_source_json",
        recovery_hint="表格源 JSON 无法解析；修复 JSON 结构后重新生成 workbook。",
    ),
    "SPREADSHEET_SOURCE_NO_ROWS": ErrorContract(
        code="SPREADSHEET_SOURCE_NO_ROWS",
        category="artifact",
        retryable=True,
        recommended_action="collect_non_empty_rows_before_workbook",
        recovery_hint="表格源数据没有非空行；先补齐 rows/sheets 数据，再调用 data_to_workbook。",
    ),
    "MARKDOWN_SOURCE_MISSING": ErrorContract(
        code="MARKDOWN_SOURCE_MISSING",
        category="artifact",
        retryable=True,
        recommended_action="write_or_fix_markdown_source",
        recovery_hint="Markdown 源文档缺失；先写出 source_markdown_path，再调用 markdown_to_pdf。",
    ),
    "MARKDOWN_SOURCE_EMPTY": ErrorContract(
        code="MARKDOWN_SOURCE_EMPTY",
        category="artifact",
        retryable=True,
        recommended_action="write_non_empty_markdown_source",
        recovery_hint="Markdown 源文档为空；补齐正文后再调用 markdown_to_pdf。",
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
    matches = matched_error_codes(str(message or ""), ERROR_CONTRACTS.keys())
    if matches:
        _, code = sorted(matches, key=lambda item: (-item[0], item[1]))[0]
        return error_contract(code)
    return error_contract("UNKNOWN_ERROR")


# LLM: tool_failure_taxonomy exposes stable codes for ToolManifest without duplicating constants.
# 函数用途: 返回工具清单要展示的错误分类代码列表，供 context bundle 和工具网关复用。
def tool_failure_taxonomy() -> list[str]:
    return sorted(ERROR_CONTRACTS)


__all__ = [
    "ERROR_CONTRACTS",
    "ErrorContract",
    "classify_error",
    "error_contract",
    "tool_failure_taxonomy",
]
