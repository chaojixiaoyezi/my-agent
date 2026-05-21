# LLM: Content transport policy keeps large generated bodies out of single JSON tool calls.
# 模块用途: 统一限制工具参数里的大段正文，提示模型改用分块、patch 或受控执行引用。

from __future__ import annotations

from dataclasses import dataclass

from ..settings.tool_config import DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS

MAX_INLINE_WRITE_CONTENT_CHARS = DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS
STREAMING_INLINE_WRITE_ABORT_CHARS = 32_000
RECOMMENDED_WRITE_CHUNK_CHARS = "1500-2000"
RECOVERY_WRITE_CHUNK_CHARS = 800


# LLM: InlineContentPolicyRequest is the bundle passed by write-like tools before mutating files.
# 类用途: 保存一次正文传输检查所需的工具名、字段名、正文和目标路径。
@dataclass(frozen=True)
class InlineContentPolicyRequest:
    tool_name: str
    field_name: str
    content: str
    path: str = ""
    max_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS


# LLM: InlineContentPolicyDecision reports whether inline content exceeded the recommended transport size.
# 类用途: 表示工具参数正文是否超过推荐 inline 尺寸，并给模型一个后续写大文件的稳定提示。
@dataclass(frozen=True)
class InlineContentPolicyDecision:
    allowed: bool
    actual_chars: int
    max_chars: int
    message: str = ""


# LLM: LongContentTransportHintRequest keeps hint rendering bundle-based as policy fields grow.
# 类用途: 保存长正文失败提示所需的工具名、字段名、长度、目标路径和当前上限。
@dataclass(frozen=True)
class LongContentTransportHintRequest:
    tool_name: str
    field_name: str
    actual_chars: int
    path: str = ""
    max_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS


# LLM: long_content_avoidance_rule is shared by tool specs so model-facing guidance stays consistent.
# 函数用途: 生成写入工具的“什么时候不要整段写入”说明；供公开和内部工具入口复用。
def long_content_avoidance_rule() -> str:
    return (
        "内容很长、CSS/JS/HTML 很大或容易被模型输出截断时，完整单文件优先用 "
        "WRITE_FILE_RAW 结构化块提交；需要可恢复分块时再用 file_write_session；"
        f"正常分块单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符，"
        f"解析失败后再降到 {RECOVERY_WRITE_CHUNK_CHARS} 字符以内。"
    )


# LLM: append_chunk_use_case describes append_file's primary large-body role without duplicating constants.
# 函数用途: 生成 append_file 在大文件分块场景下的用途说明。
def append_chunk_use_case() -> str:
    return (
        "把较长的 CSS/JS/HTML 或代码文件分块追加，"
        f"正常分块单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符，"
        f"解析失败后再降到 {RECOVERY_WRITE_CHUNK_CHARS} 字符以内。"
    )


# LLM: write_file_content_parameter_detail is the single source for write_file content docs.
# 函数用途: 生成 write_file.content 的模型可读说明，集中维护 inline 上限和分块建议。
def write_file_content_parameter_detail(max_inline_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS) -> str:
    limit = inline_write_content_limit(max_inline_chars)
    return (
        "会直接成为文件的新内容；原文件存在时会被整体覆盖。长文件请保持短骨架，"
        f"正常分块单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符，"
        "后续用 append_file 分块补齐；"
        f"单次 inline content 推荐不超过 {limit} 字符；如果合法工具调用已经包含更长正文，"
        "工具层会自动落盘，但后续请改用分块写入。"
    )


# LLM: append_file_content_parameter_detail is the single source for append_file content docs.
# 函数用途: 生成 append_file.content 的模型可读说明，集中维护 inline 上限和分块建议。
def append_file_content_parameter_detail(max_inline_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS) -> str:
    limit = inline_write_content_limit(max_inline_chars)
    return (
        "会直接拼接到文件尾部，不会替换已有内容。长文件分多次追加，"
        f"正常分块单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符；"
        f"单次 inline content 推荐不超过 {limit} 字符；如果合法工具调用已经包含更长正文，"
        "工具层会自动落盘，但后续请改用分块写入。"
    )


# LLM: tool_content_transport_protocol keeps large-body tool guidance visible even in compact catalogs.
# 函数用途: 生成工具目录顶部的大内容传输协议，防止模型把完整网页/脚本塞进单个 JSON 参数。
def tool_content_transport_protocol(max_inline_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS) -> str:
    limit = inline_write_content_limit(max_inline_chars)
    return (
        "# Tool Content Transport Protocol\n"
        f"- 大内容边界：write_file/append_file 的 content 单次推荐不超过 {limit} 字符。\n"
        "- 如果要生成完整 HTML/CSS/JS、长脚本、长报告或大段数据，不要把完整大文件正文塞进一个 JSON 工具参数。\n"
        "- 写完整单文件成品时优先用 WRITE_FILE_RAW，一次提交整份文件，避免 JSON 转义、chunk 错位和忘记 finish：\n"
        "[WRITE_FILE_RAW path=\"outputs/file.html\"]\n"
        "<!doctype html>\n"
        "...\n"
        "[/WRITE_FILE_RAW]\n"
        "- WRITE_FILE_RAW 会被系统转换成 write_file；正文只按机器 marker 边界读取，不做自然语言判断。\n"
        "- 只有需要可恢复分块或超大文件时才使用 file_write_session，并用结构化 raw block 承载正文：\n"
        "[FILE_WRITE_SESSION_APPEND session_id=\"stable-id\" target_path=\"outputs/file.html\" chunk_index=0]\n"
        "<!doctype html>\n"
        "...\n"
        "[/FILE_WRITE_SESSION_APPEND]\n"
        "- raw block 会被系统转换成 file_write_session.append；随后调用 file_write_session finish 提交。\n"
        "- 备选做法：先用 write_file 写短骨架，再用 append_file 分块追加；"
        f"正常分块每块 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符。\n"
        f"- 如果上一轮工具调用解析失败、超时或被截断，下一轮每块降到 {RECOVERY_WRITE_CHUNK_CHARS} 字符以内，"
        "闭合工具调用后等待结果。\n"
        "- 修改已有文件时优先用 replace_in_file/patch 风格小 diff；已有 controlled_exec 授权时，"
        "可在授权目录内运行脚本生成大文件，只返回 stdout_ref/audit_ref。"
    )


# LLM: check_inline_write_content treats the configured value as a recommended transport size, not a data-loss boundary.
# 函数用途: 判断正文是否超过推荐 inline 尺寸；超过时仍允许合法调用落盘，但返回后续分块提示。
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


# LLM: inline_write_content_limit normalizes per-registry write limits without mutating global policy.
# 函数用途: 把配置传入的单次 inline 正文上限转换成正整数；无效值回退到默认 12K。
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


# LLM: streaming_inline_write_abort_limit keeps slow unfinished tool calls bounded without cutting normal pages early.
# 函数用途: 默认给网页/脚本完整闭合机会；显式小测试上限仍按调用方配置，失控流才早停恢复。
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


# LLM: long_content_transport_hint teaches the model the durable fix while the tool still preserves valid content.
# 函数用途: 生成统一长内容提示，避免模型继续输出同一个超长工具调用。
def long_content_transport_hint(request: LongContentTransportHintRequest) -> str:
    limit = inline_write_content_limit(request.max_chars)
    target = f" path={request.path}" if request.path else ""
    return (
        f"{request.tool_name}.{request.field_name} inline content 超过推荐值：{request.actual_chars} 字符，"
        f"推荐最多 {limit} 字符。{target}\n"
        "本次工具调用已被合法解析时，工具层会保留内容并写入文件；长期规则仍是不要把大文件正文塞进一个 JSON 工具参数。\n"
        f"请先用 write_file 写短骨架，再用 append_file 分块追加；每块 content 建议 "
        f"{RECOMMENDED_WRITE_CHUNK_CHARS} 字符，工具解析失败后降到不超过 "
        f"{RECOVERY_WRITE_CHUNK_CHARS} 字符。\n"
        "修改已有文件时优先用 replace_in_file/patch 风格的小 diff；"
        "如果已有父级 controlled_exec grant，可用 controlled_exec 在授权目录内运行脚本生成文件，"
        "并只返回 stdout_ref/audit_ref，不要回传完整正文。"
    )
