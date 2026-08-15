
from __future__ import annotations

from dataclasses import dataclass

from ..settings.defaults import DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS

MAX_INLINE_WRITE_CONTENT_CHARS = DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS
STREAMING_INLINE_WRITE_ABORT_CHARS = 32_000
RECOMMENDED_WRITE_CHUNK_CHARS = "1500-2000"
RECOVERY_WRITE_CHUNK_CHARS = 2000
RECOVERY_STREAMING_INLINE_WRITE_ABORT_CHARS = STREAMING_INLINE_WRITE_ABORT_CHARS


def filesystem_text_mutation_rule() -> str:
    return (
        "已有文本文件的局部修改、新增、移动或删除都用 apply_patch；"
        "删除单个文本文件时使用 *** Delete File，不要改用 rm/rmdir/unlink"
    )


@dataclass(frozen=True)
class InlineContentPolicyRequest:
    tool_name: str
    field_name: str
    content: str
    path: str = ""
    max_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS


@dataclass(frozen=True)
class InlineContentPolicyDecision:
    allowed: bool
    actual_chars: int
    max_chars: int
    message: str = ""


@dataclass(frozen=True)
class LongContentTransportHintRequest:
    tool_name: str
    field_name: str
    actual_chars: int
    path: str = ""
    max_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS


def long_content_avoidance_rule() -> str:
    return (
        "内容很长、CSS/JS/HTML 很大或容易被模型输出截断时，优先用 "
        "write_file mode=\"append\" 分段写入；二进制内容用 data_base64；"
        f"{filesystem_text_mutation_rule()}；"
        f"解析失败后再降到 {RECOVERY_WRITE_CHUNK_CHARS} 字符以内。"
    )


def write_file_content_parameter_detail(max_inline_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS) -> str:
    limit = inline_write_content_limit(max_inline_chars)
    return (
        "会直接成为文件的新内容；原文件存在时会被整体覆盖。长文件应使用 "
        "write_file mode=\"append\" 分段追加，"
        f"单次 inline content 推荐不超过 {limit} 字符；如果合法工具调用已经包含更长正文，"
        "工具层会自动落盘，但后续请改用小块追加或 data_base64。"
    )


def tool_content_transport_protocol(max_inline_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS) -> str:
    limit = inline_write_content_limit(max_inline_chars)
    return (
        "# Tool Content Transport Protocol\n"
        f"- 大内容边界：write_file.content 单次推荐不超过 {limit} 字符。\n"
        "- 如果要生成完整 HTML/CSS/JS、长脚本、长报告或大段数据，不要把完整大文件正文塞进一个 JSON 工具参数。\n"
        "- 长文本报告可以先用 write_file 覆盖写入标题/目录，再用同一个 write_file 的 mode=\"append\" 分段追加后续章节。\n"
        "- 写完整单文件成品时，第一块用 write_file mode=\"overwrite\"，后续块对同一路径使用 mode=\"append\"；每次只发一个完整、符合 Schema 的工具调用并等待结果。\n"
        "- PDF、XLSX、图片、压缩包等二进制产物用脚本生成后，通过 write_file.data_base64 写入最终文件。\n"
        f"- 如果上一轮工具调用解析失败、超时或被截断，下一轮建议每块降到 {RECOVERY_WRITE_CHUNK_CHARS} 字符以内；"
        "这是恢复建议，不是流式截断上限。完整闭合的中等长度写入会先交给工具层处理。\n"
        f"- {filesystem_text_mutation_rule()}；需要脚本生成大文件时，直接用 run_command，"
        "由运行时 access_mode 决定命令是否能在目标目录执行。"
    )


def check_inline_write_content(request: InlineContentPolicyRequest) -> InlineContentPolicyDecision:
    actual = len(request.content)
    limit = inline_write_content_limit(request.max_chars)
    if actual <= limit:
        return InlineContentPolicyDecision(True, actual, limit)
    return InlineContentPolicyDecision(
        True,
        actual,
        limit,
        long_content_transport_hint(
            LongContentTransportHintRequest(
                tool_name=request.tool_name,
                field_name=request.field_name,
                actual_chars=actual,
                path=request.path,
                max_chars=limit,
            )
        ),
    )


def inline_write_content_limit(value: int | None = None) -> int:
    if value is None:
        return MAX_INLINE_WRITE_CONTENT_CHARS
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return MAX_INLINE_WRITE_CONTENT_CHARS
    if limit <= 0:
        return MAX_INLINE_WRITE_CONTENT_CHARS
    return limit


def streaming_inline_write_abort_limit(value: int | None = None) -> int:
    limit = inline_write_content_limit(value)
    if _explicit_small_stream_limit(value):
        return limit
    return max(limit, STREAMING_INLINE_WRITE_ABORT_CHARS)


def _explicit_small_stream_limit(value: int | None) -> bool:
    if value is None:
        return False
    try:
        return 0 < int(value) < MAX_INLINE_WRITE_CONTENT_CHARS
    except (TypeError, ValueError):
        return False


def long_content_transport_hint(request: LongContentTransportHintRequest) -> str:
    limit = inline_write_content_limit(request.max_chars)
    target = f" path={request.path}" if request.path else ""
    return (
        f"{request.tool_name}.{request.field_name} inline content 超过推荐值：{request.actual_chars} 字符，"
        f"推荐最多 {limit} 字符。{target}\n"
        "本次工具调用已被合法解析时，工具层会保留内容并写入文件；长期规则仍是不要把大文件正文塞进一个 JSON 工具参数。\n"
        f"请改用 write_file mode=\"append\" 分段追加，或用 write_file.data_base64 提交二进制产物；普通文本 content 建议 "
        f"{RECOMMENDED_WRITE_CHUNK_CHARS} 字符，工具解析失败后降到不超过 "
        f"{RECOVERY_WRITE_CHUNK_CHARS} 字符。\n"
        f"{filesystem_text_mutation_rule()}；需要脚本生成大文件时，直接用 run_command，"
        "由运行时 access_mode 决定命令是否能在目标目录执行，不要引入额外 grant 流程。"
    )
