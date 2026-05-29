
# LLM: 写入前必须经过工作区和子代理边界校验，错误文案也服务上层决策。
# 模块用途: 内部文件写入、追加和替换工具实现。

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import Any

from ..contracts.artifact_format_lint import lint_artifact_format
from ._filesystem_helpers import _MAX_WRITE_TEXT_CHARS, _required_path, _text_param
from ._filesystem_read import FileSystemAccessOptions, FileSystemTool
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
from .models import ToolExecutionResult, ToolSpec


# LLM: WriteFileTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: WriteFileTool 数据模型，集中保存 工具系统 的结构化状态。
class WriteFileTool(FileSystemTool):

    # LLM: WriteFileTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 WriteFileTool 的依赖、配置和运行期字段。
    def __init__(
        self,
        workspace_root: Path,
        workspace_roots: list[Path] | None = None,
        *,
        max_inline_content_chars: int | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        super().__init__(
            workspace_root,
            workspace_roots,
            access_options,
        )
        self.max_inline_content_chars = inline_write_content_limit(max_inline_content_chars)
        self.spec = ToolSpec(
            name="write_file",
            category="filesystem",
            effect="mutating",
            requires_idempotency=True,
            description="原子写入或覆盖完整文件；支持文本 content 或二进制 data_base64，缺失父目录会自动创建。",
            use_cases=[
                "新建代码文件、配置文件、文档或二进制产物",
                "已经明确要重写某个文件的完整内容",
            ],
            avoid_when=[
                "只想局部改已有文件时优先用 apply_patch",
                long_content_avoidance_rule(),
            ],
            keywords=["写文件", "生成代码", "创建文件", "覆盖", "save file", "write", "binary", "base64"],
            parameters={
                "path": "要写入的文件路径",
                "content": "完整文本内容；和 data_base64 二选一",
                "data_base64": "完整二进制内容的 base64；和 content 二选一",
            },
            parameter_details={
                "path": "相对工作区的目标文件路径；缺失父目录会自动创建。",
                "content": write_file_content_parameter_detail(self.max_inline_content_chars),
                "data_base64": "可选。用于 PDF、XLSX、图片、压缩包等二进制文件；传入后按原始字节写入。",
            },
            examples=[
                '{"tool": "write_file", "path": "src/demo.py", "content": "print(\\"hello\\")\\n"}',
                '{"tool": "write_file", "path": "outputs/report.pdf", "data_base64": "JVBERi0xLjQK..."}',
            ],
        )

    # LLM: WriteFileTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 WriteFileTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            raw_path = _required_path(params.get("path"))
            content, data = _write_payload(params)
            content_policy = _content_policy(raw_path, content, self.max_inline_content_chars)
            if content_policy and not content_policy.allowed:
                return ToolExecutionResult("write_file", False, content_policy.message)
            target = self.resolve_path(raw_path)
        except ValueError as exc:
            return ToolExecutionResult("write_file", False, str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self.resolve_path(target)
        try:
            _atomic_write_bytes(target, data)
        except ValueError as exc:
            return ToolExecutionResult(
                "write_file",
                False,
                str(exc),
                error_code="ARTIFACT_VALIDATION_FAILED",
                retryable=True,
                recommended_action="rewrite valid artifact bytes or write a draft to a non-final extension first",
            )
        web_decision = check_web_project_post_write(target, self.workspace_root)
        output = _write_output(self.display_path(target), target, content, content_policy)
        return _write_result("write_file", target, output, web_decision)


# LLM: _write_result keeps web-project validation attached to every mutating file write.
# 函数用途: 写入已发生但站点验收失败时返回结构化失败，促使模型修复而不是假完成。
def _write_result(tool: str, target: Path, output: str, web_decision: Any) -> ToolExecutionResult:
    envelope = _artifact_integrity_envelope(web_decision, target)
    web_note = web_project_post_write_note(web_decision)
    if not web_note:
        return ToolExecutionResult(tool, True, output, result_envelope=envelope)
    return ToolExecutionResult(
        tool,
        False,
        f"{output}\n{web_note}",
        result_envelope=envelope,
        error_code="ACCEPTANCE_FAILED",
    )


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
    has_base64 = "data_base64" in params and params.get("data_base64") is not None
    if has_text == has_base64:
        raise ValueError("content 和 data_base64 必须二选一")
    if has_base64:
        raw = _text_param(params.get("data_base64"), name="data_base64", max_chars=_MAX_WRITE_TEXT_CHARS)
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


# LLM: _content_policy isolates inline-size checking from the write side effect.
# 函数用途: 文本写入前生成长度策略结果；二进制写入不走文本策略。
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


# LLM: _write_output keeps post-write notes assembly small and deterministic.
# 函数用途: 汇总写入成功、长内容提示和 HTML 完整性提示。
def _write_output(
    display_path: str,
    target: Path,
    content: str | None,
    content_policy: Any | None,
) -> str:
    notes = [f"已写入文件: {display_path}"]
    if content_policy and content_policy.message:
        notes.append(content_policy.message)
    if content is not None:
        integrity_note = html_post_write_note(target, content)
        if integrity_note:
            notes.append(integrity_note)
    return "\n".join(notes)


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=_temp_suffix_for(target), dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        _validate_final_artifact_candidate(Path(tmp_name), target)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


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
    report = lint_artifact_format(path=candidate, workspace_root=target.parent)
    if report.ok:
        return
    codes = ",".join(finding.code for finding in report.findings if finding.code)
    messages = "; ".join(finding.message for finding in report.findings if finding.message)
    raise ValueError(f"{codes or 'ARTIFACT_INVALID'}: {messages or 'artifact candidate failed objective validation'}")
