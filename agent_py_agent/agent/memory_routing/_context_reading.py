# LLM: Memory routing module; keep context selection and read-receipt records stable.
# 模块用途: 根据任务上下文选择可注入记忆，并记录读取路径。

"""Authority-file reading and receipt helpers for routed memory context."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ._context_paths import _ReadTarget


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _read_authority_file 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 read authority file 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def _read_authority_file(
    target: _ReadTarget,
    *,
    max_chars_per_file: int,
) -> tuple[str, dict[str, Any]]:
    """Read one safe authority file and return a prompt section plus receipt."""
    started = time.perf_counter()
    receipt = _new_receipt(target, status="planned")
    try:
        if not target.absolute_path.exists():
            receipt["status"] = "missing"
            receipt["error"] = f"authority file does not exist: {target.path}"
            return "", _finish_receipt(receipt, started)
        if not target.absolute_path.is_file():
            receipt["status"] = "not_file"
            receipt["error"] = f"authority path is not a file: {target.path}"
            return "", _finish_receipt(receipt, started)
        content = target.absolute_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        receipt["status"] = "error"
        receipt["error"] = str(exc)
        return "", _finish_receipt(receipt, started)

    receipt["status"] = "read"
    receipt["content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    section = _build_injected_section(target.path, content, max_chars_per_file=max_chars_per_file)
    return section, _finish_receipt(receipt, started)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _build_injected_section 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build injected section 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _build_injected_section(path: str, content: str, *, max_chars_per_file: int) -> str:
    """Format bounded authority text for prompt injection."""
    limit = max(max_chars_per_file, 0)
    truncated = len(content) > limit
    body = content[:limit]
    suffix = ""
    if truncated:
        suffix = f"\n\n[truncated: {len(content) - limit} chars omitted]"
    return f"### Routed memory authority: {path}\n\n{body}{suffix}".strip()


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _new_receipt 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 new receipt 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
def _new_receipt(target: _ReadTarget, *, status: str) -> dict[str, Any]:
    """Create the stable receipt shape required by runtime callers."""
    return {
        "route_id": target.route_id,
        "path": target.path,
        "status": status,
        "content_hash": "",
        "elapsed_ms": 0.0,
        "error": "",
        "reasons": list(target.reasons),
    }


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _finish_receipt 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 finish receipt 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
def _finish_receipt(receipt: dict[str, Any], started: float) -> dict[str, Any]:
    """Record elapsed time for a read attempt."""
    receipt["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return receipt
