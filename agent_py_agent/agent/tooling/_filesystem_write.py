# LLM: canonical 原子写入共用候选 fd；诊断收集故障独立降级，不改变来源合同、权限或二进制门，联测三写入口。
# 模块用途: 安全写入文本、二进制或精确来源，反馈有限语法信息，诊断普通异常不能把已写入报成失败。

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.cancellation import ToolCancelled
from ..common.encoding_detect import encode_like_original
from ..common.file_version import StaleFileVersionError, check_file_version, file_version
from ..contracts.artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from ..contracts.recovery import RecoveryAction
from ..contracts.tool_input_schema import EXCLUSIVE_ARGUMENT_GROUPS_KEY
from ..run_intent import reference_write_feedback
from ..user_space.owner_quota import OwnerQuotaChange, OwnerQuotaExceeded, OwnerQuotaUnavailable
from ._filesystem_display import (
    build_text_diff_display,
    build_write_display,
    existing_utf8_text_for_display,
)
from ._filesystem_helpers import _MAX_WRITE_TEXT_CHARS, _required_path, _text_param
from ._filesystem_read import (
    FileSystemAccessOptions,
    FileSystemTool,
    WriteScopeError,
    owner_quota_error_result,
)
from ._persona_write_guard import (
    _PERSONA_FILE_NAMES,
    _persona_approval_write_error,
    _persona_injection_write_error,
)
from .artifact_integrity import (
    artifact_integrity_payload,
    check_web_project_post_write,
    html_post_write_note,
    web_project_post_write_note,
)
from .content_transport_policy import (
    FileSourceContent,
    FileSourceUnavailableError,
    InlineContentPolicyRequest,
    check_inline_write_content,
    inline_write_content_limit,
    long_content_avoidance_rule,
    write_file_content_parameter_detail,
)
from .file_syntax_diagnostics import (
    FileSyntaxDiagnostics,
    FileSyntaxObservation,
    attach_syntax_diagnostics,
    unavailable_observation,
    update_syntax_diagnostics,
)
from .models import (
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

_WRITE_FILE_USE_CASES = [
    "新建代码文件、配置文件、文档或二进制产物",
    "已经明确要重写某个文件的完整内容",
    "长报告可以先覆盖写入标题，再用 mode=append 分段追加后续章节",
]
_WRITE_FILE_PARAMETERS = {
    "path": "要写入的文件路径",
    "content": "完整文本内容；和 data_base64 二选一",
    "data_base64": "完整二进制内容的 base64；和 content 二选一",
    "mode": "可选。overwrite 覆盖写入（默认）或 append 追加到文件末尾",
}
_WRITE_FILE_EXAMPLES = [
    '{"tool": "write_file", "path": "src/demo.py", "content": "print(\\"hello\\")\\n"}',
    '{"tool": "write_file", "path": "output/report.md", "mode": "append", "content": "\\n## 下一节\\n..."}',
    '{"tool": "write_file", "path": "output/report.pdf", "data_base64": "JVBERi0xLjQK..."}',
]


# LLM: source_resolver 与 source_ref_schema 只能由宿主成对装配；来源语义归宿主，工具层不导入具体来源模块或增加权限。
# 类用途: 配置正文预算、访问范围和可选资源读取合同，避免模型声明与解析器要求脱节。
@dataclass(frozen=True)
class WriteFileToolOptions:
    max_inline_content_chars: int | None = None
    access_options: FileSystemAccessOptions | None = None
    runtime_fact_roots: list[Path] = field(default_factory=list)
    source_resolver: Callable[[object], FileSourceContent] | None = None
    source_ref_schema: dict[str, Any] | None = None


# LLM: 缺少 resolver 或 schema 是宿主装配错误，不能退回宽泛 object；深拷贝后仍由原 ToolModelSpec 验证并固定整份 schema。
# 函数用途: 为可用来源准备隔离的模型输入声明，不读取来源或写文件；未启用来源时保持原工具格式。
def _source_ref_input_properties(options: WriteFileToolOptions) -> dict[str, Any]:
    if (options.source_resolver is None) != (options.source_ref_schema is None):
        raise ValueError("文件来源解析器与引用 schema 必须同时配置")
    if options.source_resolver is None:
        return {}
    if not callable(options.source_resolver):
        raise ValueError("文件来源解析器必须可调用")
    schema = options.source_ref_schema
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("文件来源引用 schema 必须声明 object 类型")
    return {"source_ref": deepcopy(schema)}


# LLM: source_content 是当前调用已解析的原字节及完整来源，只进入本次写入与原回执，不另建持久状态。
# 类用途: 固定写入前校验过的目标、内容、版本和可选来源。
@dataclass(frozen=True)
class WriteRequest:
    raw_path: str
    content: str | None
    data: bytes
    mode: str
    target: Path
    content_policy: Any | None
    observed_version: str | None = None
    source_content: FileSourceContent | None = None


@dataclass(frozen=True)
class WriteOutputRequest:
    display_path: str
    target: Path
    content: str | None
    content_policy: Any | None
    mode: str


# LLM: 三种内容输入共用 mutating/operation 合同；可选语法观察不绕审批、版本、owner 墙或改变写入成功事实。
# 类用途: 在原文件链完成可追踪的原子写入，并反馈本次候选的有限语法信息。
class WriteFileTool(FileSystemTool):
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(parameter_names=("path",)),
        promotes_task=True,
        mutates_workspace=True,
    )

    # LLM: 来源 resolver/schema 成对注入；未启用时保持原模型合同，已启用时原参数门消费准确字段声明，联查 core/registry 和写工具测试。
    # 函数用途: 初始化访问策略与隔离的来源声明，区分新正文和原样复制；装配错误在工具可用前明确失败。
    def __init__(
        self,
        workspace_root: Path,
        workspace_roots: list[Path] | None = None,
        options: WriteFileToolOptions | None = None,
    ):
        options = options or WriteFileToolOptions()
        source_properties = _source_ref_input_properties(options)
        super().__init__(
            workspace_root,
            workspace_roots,
            options.access_options,
        )
        self.max_inline_content_chars = inline_write_content_limit(options.max_inline_content_chars)
        self.source_resolver = options.source_resolver
        self.runtime_fact_roots = [
            Path(root).expanduser().resolve(strict=False) for root in options.runtime_fact_roots
        ]
        self.model_spec = ToolModelSpec(
            name="write_file",
            description=("原子写入或覆盖完整文件；支持文本 content 或二进制 data_base64，缺失父目录会自动创建。"
                         + ("复制已有脚本、模板或资源时优先原样传入 skill_search 返回的完整 source_ref，不经 content 手抄；三种内容输入互斥，复制不执行。" if self.source_resolver else "")),
            input_schema={
                "type": "object",
                "properties": {
                    **source_properties,
                    "expected_version": {"type": "string", "description": "基于 read_file 内容修改时，填它返回的 file_version；过期会拒绝覆盖，需重新读取合并。"},
                    "path": {
                        "type": "string",
                        "description": "相对工作区的目标文件路径；缺失父目录会自动创建。",
                    },
                    "content": {
                        "type": "string",
                        "description": write_file_content_parameter_detail(
                            self.max_inline_content_chars
                        ),
                    },
                    "data_base64": {
                        "type": "string",
                        "description": "可选。用于 PDF、XLSX、图片、压缩包等二进制文件；传入后按原始字节写入。",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "可选，精确值 overwrite 或 append。省略时始终覆盖；只有显式 append 才会原子地保留已有内容并追加到文件末尾。不接受 completed/continue 等别名。",
                    },
                },
                "required": ["path"],
                EXCLUSIVE_ARGUMENT_GROUPS_KEY: [
                    ["content", "data_base64", *source_properties]
                ],
                "additionalProperties": False,
            },
            hints=ToolModelHints(
                category="filesystem",
                use_cases=tuple(_WRITE_FILE_USE_CASES) + (("按工具返回的 source_ref 原样复制已有脚本、模板或资源",) if self.source_resolver else ()),
                avoid_when=(
                    "只想局部改已有文件时优先用 apply_patch",
                    long_content_avoidance_rule(),
                ),
                keywords=(
                    "写文件",
                    "生成代码",
                    "创建文件",
                    "覆盖",
                    "save file",
                    "write",
                    "binary",
                    "base64",
                ),
                examples=tuple(_WRITE_FILE_EXAMPLES),
            ),
        )

    # LLM: 来源错误仍在写前拒绝；语法观察属于本次调用，发布成功才附回执，诊断失败不得改写原 effect。
    # 函数用途: 共用原路径与产物检查完成写入，返回版本、字节数、来源和可选语法反馈。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            request = _write_request(self, params)
        except StaleFileVersionError as exc:
            return ToolHandlerOutcome("write_file", False, str(exc), error_code="STALE_VERSION", effect_outcome="not_started", retryable=True)
        except OSError as exc:
            return ToolHandlerOutcome("write_file", False, f"读取原文件失败: {exc}", error_code="TOOL_EXECUTION_FAILED", effect_outcome="not_started")
        except FileSourceUnavailableError as exc:
            return ToolHandlerOutcome("write_file", False, f"文件来源不可用: {exc}", error_code="TOOL_UNAVAILABLE", effect_outcome="not_started")
        except WriteScopeError as exc:
            return ToolHandlerOutcome(
                "write_file",
                False,
                str(exc),
                error_code="WRITE_FORBIDDEN",
                effect_outcome="not_started",
            )
        except ValueError as exc:
            return ToolHandlerOutcome(
                "write_file",
                False,
                str(exc),
                error_code="TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                retryable=True,
                recommended_action=RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
            )
        if request.content_policy and not request.content_policy.allowed:
            return ToolHandlerOutcome(
                "write_file",
                False,
                request.content_policy.message,
                error_code="ARTIFACT_VALIDATION_FAILED",
            )
        ledger_error = _system_ledger_write_error(request.target)
        if ledger_error:
            return _system_ledger_write_blocked_result(ledger_error)
        approval_error = _persona_approval_write_error(request.target, self.protected_persona_root)
        if approval_error:
            return ToolHandlerOutcome(
                "write_file",
                False,
                approval_error,
                error_code="PERSONA_WRITE_REQUIRES_TOOL",
            )
        persona_error = _persona_injection_write_error(request.target, _persona_content(request))
        if persona_error:
            return ToolHandlerOutcome(
                "write_file", False, persona_error, error_code="PERSONA_INJECTION_BLOCKED"
            )
        before_content = (
            existing_utf8_text_for_display(request.target)
            if request.mode == "overwrite" and request.content is not None
            else None
        )
        target = _prepare_write_target(self, request.target)
        diagnostics = FileSyntaxDiagnostics() if self.enable_file_syntax_diagnostics else None
        write_error = _atomic_write_or_error(self, target, request, diagnostics)
        if write_error is not None:
            return write_error
        web_decision = check_web_project_post_write(target, self.workspace_root)
        output = _write_output(
            WriteOutputRequest(
                display_path=self.display_path(target),
                target=target,
                content=request.content,
                content_policy=request.content_policy,
                mode=request.mode,
            )
        )
        output, feedback = _attach_reference_write_feedback(
            self.workspace_root, target, output, self.runtime_fact_roots
        )
        result = _write_result("write_file", target, output, web_decision)
        result.result_envelope["bytes_written"] = len(request.data)
        result.result_envelope["file_version"] = file_version(target)
        if request.source_content is not None:
            result.result_envelope["source_ref"] = dict(request.source_content.source_ref)
            result.result_envelope["content_sha256"] = hashlib.sha256(request.data).hexdigest()
        if before_content is not None and request.content is not None:
            result.result_envelope["display"] = build_text_diff_display(
                self.display_path(target),
                before_content,
                request.content,
            )
        else:
            result.result_envelope["display"] = build_write_display(
                path=self.display_path(target),
                content=request.content,
                mode=request.mode,
                bytes_written=len(request.data),
            )
        if feedback:
            result.result_envelope["soft_feedback"] = feedback
        return attach_syntax_diagnostics(result, diagnostics)


# LLM: 发布成功后经统一诊断边界登记；普通收集异常不进入产物失败分类，取消继续传播，联查三入口和 source_ref。
# 函数用途: 执行原子写入并保留原错误回执，成功后安全地汇集可选语法信息。
def _atomic_write_or_error(
    tool: WriteFileTool,
    target: Path,
    request: WriteRequest,
    diagnostics: FileSyntaxDiagnostics | None = None,
) -> ToolHandlerOutcome | None:
    """执行原子写入；失败返回 ToolHandlerOutcome，成功返回 None。从 execute 抽出以控行数。"""
    try:
        change = OwnerQuotaChange(
            target,
            len(request.data),
            append=request.mode == "append",
        )
        with tool.quota_changes([change]):
            observation = _atomic_write_bytes(target, request.data, mode=request.mode,
                                              expected_version=request.observed_version, diagnostics=diagnostics)
    except StaleFileVersionError as exc:
        return ToolHandlerOutcome("write_file", False, str(exc), error_code="STALE_VERSION", effect_outcome="not_started", retryable=True)
    except (OwnerQuotaExceeded, OwnerQuotaUnavailable) as exc:
        return owner_quota_error_result("write_file", exc)
    except ValueError as exc:
        return ToolHandlerOutcome(
            "write_file",
            False,
            str(exc),
            error_code="ARTIFACT_VALIDATION_FAILED",
            retryable=True,
            recommended_action=RecoveryAction.REWRITE_ARTIFACT_BYTES.value,
        )
    except OSError as exc:
        return ToolHandlerOutcome(
            "write_file",
            False,
            f"写入失败: {exc}",
            error_code="TOOL_EXECUTION_FAILED",
        )
    update_syntax_diagnostics(diagnostics, observation)
    return None


# LLM: 原格式探测前固定 observed version；精确来源字节不做目标编码迁移，解析器只在路径/版本许可后读取并固定原任务引用。
# 函数用途: 检查目标和写入参数，读取三种输入之一；此阶段不写目标文件。
def _write_request(tool: WriteFileTool, params: dict[str, Any]) -> WriteRequest:
    raw_path = _required_path(params.get("path"))
    write_mode = _write_mode(params)
    target = tool.resolve_write_path(raw_path)
    observed_version = check_file_version(target, params.get("expected_version"))
    source = _write_source(tool, params, write_mode)
    content, data = (None, source.data) if source is not None else _write_payload(params)
    if content is not None:
        data = _preserve_existing_encoding(target, content, write_mode, data)
    content_policy = _content_policy(raw_path, content, tool.max_inline_content_chars)
    return WriteRequest(
        raw_path=raw_path,
        content=content,
        data=data,
        mode=write_mode,
        target=target,
        content_policy=content_policy,
        observed_version=observed_version,
        source_content=source,
    )


# LLM: resolver 是宿主可信闭包；输入来源互斥已由公共 schema 门统一拒绝，这里只处理来源装配、仅覆盖约束和身份替换。
# 函数用途: 解析已通过公共参数校验的精确文件来源，保持字节不经过模型或文本编码转换。
def _write_source(tool: WriteFileTool, params: dict[str, Any], mode: str) -> FileSourceContent | None:
    if "source_ref" not in params:
        return None
    if tool.source_resolver is None:
        raise FileSourceUnavailableError("当前未装配文件来源读取能力")
    if mode != "overwrite":
        raise ValueError("source_ref 仅支持 overwrite")
    source = tool.source_resolver(params["source_ref"])
    if not isinstance(source, FileSourceContent) or dict(source.source_ref) != params["source_ref"]:
        raise ValueError("FILE_SOURCE_REFERENCE_MISMATCH")
    return source


# LLM: 精确来源仍经过原人格正文守卫；解码只供安全扫描，绝不能重新编码或替换待写字节。
# 函数用途: 为人格文件提取可扫描文本，普通资源维持原二进制展示与写入。
def _persona_content(request: WriteRequest) -> str | None:
    if request.source_content is not None and request.target.name in _PERSONA_FILE_NAMES:
        return request.data.decode("utf-8", errors="replace")
    return request.content


# LLM: 追加与覆盖都使用同一原格式编码器；读取或编码失败必须先报错，不允许默认 UTF-8 覆盖未知字节。
# 函数用途: 写文件前保留原编码、BOM、端序和换行；新文件继续使用 UTF-8。
def _preserve_existing_encoding(
    target: Path, content: str, mode: str, default_data: bytes
) -> bytes:
    """写既有文本文件时按其原编码+原换行写回(审计 #23):非 UTF-8/CRLF 文件不被静默改坏。

    仅当覆盖/追加且目标已存在才探测；无法读取或编码时不写盘，不生成混合编码文件。
    """
    if mode not in ("overwrite", "append") or not target.exists():
        return default_data
    return encode_like_original(content, target.read_bytes(), append=mode == "append")


def _system_ledger_write_blocked_result(message: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "write_file",
        False,
        message,
        error_code="SYSTEM_LEDGER_WRITE_BLOCKED",
        retryable=True,
        recommended_action=RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
    )


def _prepare_write_target(tool: WriteFileTool, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    return tool.resolve_write_path(target)


def _write_result(tool: str, target: Path, output: str, web_decision: Any) -> ToolHandlerOutcome:
    envelope = _artifact_integrity_envelope(web_decision, target)
    web_note = web_project_post_write_note(web_decision)
    blocker_codes = list(getattr(web_decision, "blocker_codes", []) or [])
    rendered_output = f"{output}\n{web_note}" if web_note else output
    if blocker_codes:
        return ToolHandlerOutcome(
            tool,
            False,
            rendered_output,
            result_envelope=envelope,
            error_code="ACCEPTANCE_FAILED",
        )
    return ToolHandlerOutcome(tool, True, rendered_output, result_envelope=envelope)


def _attach_reference_write_feedback(
    workspace_root: Path,
    target: Path,
    output: str,
    runtime_fact_roots: list[Path] | None = None,
) -> tuple[str, dict[str, Any]]:
    feedback = reference_write_feedback(
        workspace_root=workspace_root, target=target, fact_roots=runtime_fact_roots
    )
    if not feedback:
        return output, {}
    return f"{output}\n{_feedback_message(feedback)}", feedback


def _feedback_message(feedback: dict[str, Any]) -> str:
    message = str(feedback.get("message") or "").strip()
    load_errors = feedback.get("run_intent_load_errors")
    if not isinstance(load_errors, list) or not load_errors:
        return message
    first = load_errors[0] if isinstance(load_errors[0], dict) else {}
    context = str(first.get("context") or "").strip()
    path = str(first.get("path") or "").strip()
    detail = "；".join(
        part
        for part in (f"context={context}" if context else "", f"path={path}" if path else "")
        if part
    )
    return f"{message}\n软提醒详情：{detail}" if detail else message


def _artifact_integrity_envelope(web_decision: Any, target: Path) -> dict[str, object]:
    base: dict[str, object] = {
        "path": str(target),
        "target_path": str(target),
        "output_path": str(target),
        "artifact_ref": str(target),
    }
    if getattr(web_decision, "kind", "generic") == "generic":
        return base
    return {**base, "artifact_integrity": artifact_integrity_payload(web_decision, target)}


# LLM: 来源互斥已由公共 schema 门按字段存在性处理；这里保留“至少有一个正文来源”和各载荷解码，不再维护第二份互斥字段清单。
# 函数用途: 把已通过公共互斥校验的文本或 base64 参数转换为待写字节。
def _write_payload(params: dict[str, Any]) -> tuple[str | None, bytes]:
    has_text = "content" in params and params.get("content") is not None
    has_base64 = _has_base64_payload(params)
    if not has_text and not has_base64:
        raise ValueError(
            "write_file 必须提供 content 或 data_base64。"
            '写普通文本报告时用 content；超长文本用 mode="append" 分成多个规范 write_file 调用。'
        )
    if has_base64:
        raw = _text_param(
            params.get("data_base64"), name="data_base64", max_chars=_MAX_WRITE_TEXT_CHARS
        )
        try:
            return None, base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise ValueError("data_base64 不是有效 base64") from exc
    content = _text_param(
        params.get("content"),
        name="content",
        max_chars=_MAX_WRITE_TEXT_CHARS,
        allow_empty=True,
    )
    return content, content.encode("utf-8")


def _has_base64_payload(params: dict[str, Any]) -> bool:
    if "data_base64" not in params or params.get("data_base64") is None:
        return False
    value = params.get("data_base64")
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _write_mode(params: dict[str, Any]) -> str:
    raw = str(params.get("mode") or "overwrite").strip()
    if raw in {"overwrite", "append"}:
        return raw
    if raw == "write":
        raise ValueError(
            'write_file.mode 不接受 "write"。新建或覆盖文件时省略 mode，或显式使用 mode="overwrite"；追加时使用 mode="append"。'
        )
    raise ValueError("write_file.mode 必须精确为 overwrite 或 append。")


def _content_policy(raw_path: str, content: str | None, max_chars: int) -> Any | None:
    if content is None:
        return None
    return check_inline_write_content(
        InlineContentPolicyRequest(
            tool_name="write_file",
            field_name="content",
            content=content,
            path=raw_path,
            max_chars=max_chars,
        )
    )


def _system_ledger_write_error(target: Path) -> str:
    parts = target.resolve(strict=False).parts
    if _is_task_progress_ledger(parts):
        return (
            "系统账本写入被阻止: task_progress 进度账本不能用 write_file 直接覆盖。"
            "请使用 task_progress 工具更新进度项、事实和证据；最终用户报告仍可写到 output 或用户指定路径。"
        )
    return ""


def _is_task_progress_ledger(parts: tuple[str, ...]) -> bool:
    for index, part in enumerate(parts):
        if part != "memory_archive":
            continue
        if (
            index + 1 < len(parts)
            and parts[index + 1] == "task_progress"
            and parts[-1] == "progress.json"
        ):
            return True
    return False


def _write_output(request: WriteOutputRequest) -> str:
    action = "已追加文件" if request.mode == "append" else "已写入文件"
    notes = [f"{action}: {request.display_path}"]
    if request.content_policy and request.content_policy.message:
        notes.append(request.content_policy.message)
    if request.content is not None:
        integrity_note = html_post_write_note(request.target, request.content)
        if integrity_note:
            notes.append(integrity_note)
    return "\n".join(notes)


# LLM: 单文件发布保留原权限、二进制门及版本检查；诊断来自同一完整候选 fd，仅发布成功后返回，不构成发布门。
# 函数用途: 写临时文件并原子发布，返回可选语法观察；失败清理候选，不用发布后读回猜测写入内容。
def _atomic_write_bytes(
    target: Path, data: bytes, *, mode: str = "overwrite",
    create_only: bool = False, file_mode: int | None = None, expected_version: str | None = None,
    diagnostics: FileSyntaxDiagnostics | None = None,
) -> FileSyntaxObservation | None:
    target.parent.mkdir(parents=True, exist_ok=True)
    check_file_version(target, expected_version)
    original_mode = file_mode if file_mode is not None else (target.stat().st_mode & 0o777 if target.exists() else None)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=_temp_suffix_for(target), dir=str(target.parent)
    )
    try:
        observation = _write_temp_bytes(fd, target, data, mode=mode, diagnostics=diagnostics)
        _validate_final_artifact_candidate(Path(tmp_name), target)
        if original_mode is not None:
            os.chmod(tmp_name, original_mode & 0o777)
        check_file_version(target, expected_version)
        if create_only:
            os.link(tmp_name, target)
            _unlink_temp_file(tmp_name)
        else:
            os.replace(tmp_name, target)
        return observation
    except Exception:
        _unlink_temp_file(tmp_name)
        raise


# LLM: 诊断只读当前 mkstemp fd；关闭保留原写模式，普通诊断错误不拦发布，ToolCancelled 必须传播并清理候选。
# 函数用途: 写入并同步候选字节，再按开关从同一描述符取得有限语法观察。
def _write_temp_bytes(
    fd: int, target: Path, data: bytes, *, mode: str,
    diagnostics: FileSyntaxDiagnostics | None = None,
) -> FileSyntaxObservation | None:
    with os.fdopen(fd, "w+b" if diagnostics is not None else "wb") as file:
        _copy_existing_for_append(file, target, mode)
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
        if diagnostics is not None:
            try:
                return diagnostics.observe_candidate(target, file)
            except ToolCancelled:
                raise
            except Exception:
                return unavailable_observation(target)
    return None


def _copy_existing_for_append(file: object, target: Path, mode: str) -> None:
    if mode != "append" or not target.exists():
        return
    with target.open("rb") as existing:
        shutil.copyfileobj(existing, file)


def _unlink_temp_file(tmp_name: str) -> None:
    # 兜全部 OSError:此函数在原子写的异常清理路径被调,清理失败绝不能
    # 二次抛异常掩盖真正的写入错误(临时文件残留由进程退出/系统清理兜底)。
    try:
        os.unlink(tmp_name)
    except OSError:
        pass


def _temp_suffix_for(target: Path) -> str:
    suffix = target.suffix
    return suffix if suffix in _PREWRITE_VALIDATED_SUFFIXES else ".tmp"


_PREWRITE_VALIDATED_SUFFIXES = {
    ".docx",
    ".gz",
    ".gzip",
    ".pdf",
    ".png",
    ".xlsx",
    ".zip",
}


def _validate_final_artifact_candidate(candidate: Path, target: Path) -> None:
    if target.suffix.lower() not in _PREWRITE_VALIDATED_SUFFIXES:
        return
    report = validate_artifact(
        ArtifactAcceptanceRequest(path=candidate, workspace_root=target.parent)
    )
    if report.ok:
        return
    codes = ",".join(finding.code for finding in report.findings if finding.code)
    messages = "; ".join(finding.message for finding in report.findings if finding.message)
    raise ValueError(
        f"{codes or 'ARTIFACT_INVALID'}: {messages or 'artifact candidate failed objective validation'}"
    )
