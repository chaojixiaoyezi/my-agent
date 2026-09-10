# LLM: 本模块是 write_file 的 canonical 原子写入实现；富展示只能投影写入前后事实，不能改变路径、配额、persona 或 artifact 合同。
# 模块用途: 校验文本或二进制内容并安全写入文件，同时给模型和终端返回可追踪的写入结果。

from __future__ import annotations

import base64
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.encoding_detect import encode_like_original
from ..common.file_version import StaleFileVersionError, check_file_version, file_version
from ..contracts.artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from ..contracts.recovery import RecoveryAction
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
    InlineContentPolicyRequest,
    check_inline_write_content,
    inline_write_content_limit,
    long_content_avoidance_rule,
    write_file_content_parameter_detail,
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


@dataclass(frozen=True)
class WriteFileToolOptions:
    max_inline_content_chars: int | None = None
    access_options: FileSystemAccessOptions | None = None
    runtime_fact_roots: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class WriteRequest:
    raw_path: str
    content: str | None
    data: bytes
    mode: str
    target: Path
    content_policy: Any | None
    observed_version: str | None = None


@dataclass(frozen=True)
class WriteOutputRequest:
    display_path: str
    target: Path
    content: str | None
    content_policy: Any | None
    mode: str


class WriteFileTool(FileSystemTool):
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(parameter_names=("path",)),
        promotes_task=True,
        mutates_workspace=True,
    )

    def __init__(
        self,
        workspace_root: Path,
        workspace_roots: list[Path] | None = None,
        options: WriteFileToolOptions | None = None,
    ):
        options = options or WriteFileToolOptions()
        super().__init__(
            workspace_root,
            workspace_roots,
            options.access_options,
        )
        self.max_inline_content_chars = inline_write_content_limit(options.max_inline_content_chars)
        self.runtime_fact_roots = [
            Path(root).expanduser().resolve(strict=False) for root in options.runtime_fact_roots
        ]
        self.model_spec = ToolModelSpec(
            name="write_file",
            description="原子写入或覆盖完整文件；支持文本 content 或二进制 data_base64，缺失父目录会自动创建。",
            input_schema={
                "type": "object",
                "properties": {
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
                "additionalProperties": False,
            },
            hints=ToolModelHints(
                category="filesystem",
                use_cases=tuple(_WRITE_FILE_USE_CASES),
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

    # LLM: 写入成功除了 artifact 合同，还要提供有界结构化预览给富终端；预览不参与验收、路径授权或成功判断。
    # 函数用途: 校验并原子写入文本/二进制文件，同时返回终端可展示的行数和内容预览。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            request = _write_request(self, params)
        except StaleFileVersionError as exc:
            return ToolHandlerOutcome("write_file", False, str(exc), error_code="STALE_VERSION", effect_outcome="not_started", retryable=True)
        except OSError as exc:
            return ToolHandlerOutcome("write_file", False, f"读取原文件失败: {exc}", error_code="TOOL_EXECUTION_FAILED")
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
        persona_error = _persona_injection_write_error(request.target, request.content)
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
        write_error = _atomic_write_or_error(self, target, request)
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
        return result


def _atomic_write_or_error(
    tool: WriteFileTool,
    target: Path,
    request: WriteRequest,
) -> ToolHandlerOutcome | None:
    """执行原子写入；失败返回 ToolHandlerOutcome，成功返回 None。从 execute 抽出以控行数。"""
    try:
        change = OwnerQuotaChange(
            target,
            len(request.data),
            append=request.mode == "append",
        )
        with tool.quota_changes([change]):
            _atomic_write_bytes(target, request.data, mode=request.mode, expected_version=request.observed_version)
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
    return None


# LLM: 原格式探测前固定 observed version；显式 expected_version 绑定模型先前读取，不由路径或正文推断。
# 函数用途: 解析写入参数、核对版本，再按原格式编码；此阶段不修改文件。
def _write_request(tool: WriteFileTool, params: dict[str, Any]) -> WriteRequest:
    raw_path = _required_path(params.get("path"))
    content, data = _write_payload(params)
    write_mode = _write_mode(params)
    target = tool.resolve_write_path(raw_path)
    observed_version = check_file_version(target, params.get("expected_version"))
    if content is not None:  # 文本写入:写既有文件时保留其原编码/换行,不静默改成 utf-8/LF(审计 #23)
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
    )


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


def _write_payload(params: dict[str, Any]) -> tuple[str | None, bytes]:
    has_text = "content" in params and params.get("content") is not None
    has_base64 = _has_base64_payload(params)
    if has_text == has_base64:
        raise ValueError(
            "write_file 的 content 和 data_base64 必须二选一，且只能提供其中一个。"
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


# LLM: 单文件发布保留普通权限但不继承提权位；create_only 通过原子 link 防止检查后并发覆盖。
# 函数用途: 写临时文件、校验后发布；移动时继承源普通权限，新文件默认私有，失败清理临时文件。
def _atomic_write_bytes(
    target: Path, data: bytes, *, mode: str = "overwrite",
    create_only: bool = False, file_mode: int | None = None, expected_version: str | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    check_file_version(target, expected_version)
    original_mode = file_mode if file_mode is not None else (target.stat().st_mode & 0o777 if target.exists() else None)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=_temp_suffix_for(target), dir=str(target.parent)
    )
    try:
        _write_temp_bytes(fd, target, data, mode=mode)
        _validate_final_artifact_candidate(Path(tmp_name), target)
        if original_mode is not None:
            os.chmod(tmp_name, original_mode & 0o777)
        check_file_version(target, expected_version)
        if create_only:
            os.link(tmp_name, target)
            _unlink_temp_file(tmp_name)
        else:
            os.replace(tmp_name, target)
    except Exception:
        _unlink_temp_file(tmp_name)
        raise


def _write_temp_bytes(fd: int, target: Path, data: bytes, *, mode: str) -> None:
    with os.fdopen(fd, "wb") as file:
        _copy_existing_for_append(file, target, mode)
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


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
