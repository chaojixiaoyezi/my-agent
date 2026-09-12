# LLM: 此模块只把完成工具的公开展示保存为不可变页；模型输入/副作用账本不变，失败不能重执行工具。
# 模块用途: 在有界预览之前保留完整工具原文，让TUI通过已鉴权引用按页读取而非读取宿主文件。

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any


# LLM: 调用方必须是完成的真实ToolResult；child使用agent_thread_id而非父conversation_thread_id。
# 函数用途: 对本次输出保存公开原文归档；无会话或归档失败时返回明确展示缺口，不影响执行结果。
def archive_tool_display(event: Any, raw_display: object) -> dict[str, object]:
    agent = event.request.agent
    attrs = getattr(event.request.params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    thread_id = str(attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or "")
    if not thread_id or getattr(agent, "conversation_store", None) is None:
        return {}
    from ...conversation.display_archive import archive_display_rows
    try:
        return archive_display_rows(agent, thread_id=thread_id, rows=_safe_rows(event, raw_display))
    except Exception:  # noqa: BLE001 显示归档失败不能使已执行工具重跑，错误不泄露宿主路径
        return {"schema": "display_archive_error.v1", "error_code": "DISPLAY_ARCHIVE_WRITE_FAILED"}


# LLM: 富展示只读取白名单字段并按现有公开文本策略脱敏；不因全文查看暴露内部参数或密钥。
# 函数用途: 逐行产出安全原文，先于客户端预览裁剪保存，不复制巨量JSON事件。
def _safe_rows(event: Any, raw_display: object) -> Iterator[dict[str, str]]:
    from .round_execution import _public_progress_text
    rows = _display_rows(raw_display) if isinstance(raw_display, dict) and raw_display.get("kind") in {"patch", "diff", "write", "command"} else (
        {"kind": "text", "text": row} for row in _original_output(event).split("\n")
    )
    for row in rows:
        yield {"kind": row["kind"], "text": _public_progress_text(event, row["text"], max_chars=None)}


# LLM: 只读取handler结构化采集事实，旧预览truncated同样不能当完整原文；不解析输出中的省略文字。
# 函数用途: 判断命令原展示是否已经丢失内容，让事件和完整页同时如实提示缺口。
def command_display_incomplete(display: object) -> bool:
    return isinstance(display, dict) and display.get("kind") == "command" and (
        display.get("capture_complete") is False or display.get("stdout_truncated") is True
        or display.get("stderr_truncated") is True
    )


# LLM: 原始工具内容只能沿注册索引和哈希验证读取，禁止把客户端路径作为宿主读权限。
# 函数用途: 外置结果复用已有工具归档；小结果使用真实ToolResult，不读取当前业务文件。
def _original_output(event: Any) -> str:
    result = event.result
    archive = result.metadata.get("archive_output_record")
    if not isinstance(archive, Mapping) or not archive.get("output_externalized"):
        return str(result.output or "")
    from ...memory_archive.artifact.reader import (
        ReadToolOutputArtifactRequest,
        read_tool_output_artifact,
    )
    from ..run_task_workspace_writer import current_run_tool_output_archive_root
    ref = str(archive.get("scoped_call_id") or archive.get("call_id") or "")
    payload = read_tool_output_artifact(ReadToolOutputArtifactRequest(
        root=current_run_tool_output_archive_root(event.request.agent, event.request.params),
        artifact_ref=ref, max_chars=-1, run_id=str(event.call.run_id or ""),
        task_id=str(getattr(event.request.params, "task_id", "") or ""),
    ))
    if not payload.get("ok"):
        raise ValueError("original display output unavailable")
    return str(payload.get("content") or "")


# LLM: kind和capture字段来自handler协议；不把文本解释为状态，采集缺口在第一页显示且不能隐式恢复。
# 函数用途: 序列化实际写入、差异和stdout/stderr；超采集上限只展示已保留部分并说明缺失。
def _display_rows(display: dict[str, Any]) -> Iterator[dict[str, str]]:
    kind = display.get("kind")
    if display.get("path"):
        yield {"kind": "header", "text": str(display["path"])}
    if kind == "patch":
        for item in display.get("files", []):
            if isinstance(item, dict):
                yield from _display_rows(item)
    if kind == "diff":
        for row in display.get("lines", []):
            if isinstance(row, dict):
                row_kind = str(row.get("kind") or "context")
                mark = "+" if row_kind == "add" else "-" if row_kind == "remove" else " "
                number = row.get("new_line") if mark == "+" else row.get("old_line")
                yield {"kind": row_kind, "text": f"{number or ''} {mark} {row.get('text') or ''}"}
    if kind == "write":
        for number, text in enumerate(display.get("lines", []), 1):
            yield {"kind": "text", "text": f"{number} {text}"}
        if display.get("binary"):
            yield {"kind": "text", "text": f"二进制内容：{display.get('bytes', 0)} 字节"}
    if kind == "command":
        if command_display_incomplete(display):
            yield {"kind": "notice", "text": "命令输出采集不完整：以下仅为已保存内容；未保留部分无法恢复。"}
        code = display.get("return_code")
        yield {"kind": "header", "text": f"退出码：{code if code is not None else '未知'}"}
        for stream in ("stdout", "stderr"):
            for text in str(display.get(stream) or "").split("\n"):
                yield {"kind": stream, "text": text}
    if kind not in {"patch", "diff", "write", "command"}:
        yield {"kind": "text", "text": str(display.get("summary") or "")}
    for field in ("hidden_lines", "hidden_files"):
        if display.get(field):
            yield {"kind": "notice", "text": f"执行工具已省略 {display[field]} 项，归档不能恢复未提供的原文。"}
