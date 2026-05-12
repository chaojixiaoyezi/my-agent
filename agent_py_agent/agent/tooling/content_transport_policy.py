# LLM: Content transport policy keeps large generated bodies out of single JSON tool calls.
# 模块用途: 统一限制工具参数里的大段正文，提示模型改用分块、patch 或受控执行引用。

from __future__ import annotations

from dataclasses import dataclass

MAX_INLINE_WRITE_CONTENT_CHARS = 4_000
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


# LLM: InlineContentPolicyDecision lets tools fail closed with a stable recovery message.
# 类用途: 表示工具参数正文是否适合 inline 传输，以及失败时给模型的修复说明。
@dataclass(frozen=True)
class InlineContentPolicyDecision:
    allowed: bool
    actual_chars: int
    max_chars: int
    message: str = ""


# LLM: long_content_avoidance_rule is shared by tool specs so model-facing guidance stays consistent.
# 函数用途: 生成写入工具的“什么时候不要整段写入”说明；供公开和内部工具入口复用。
def long_content_avoidance_rule() -> str:
    return (
        "内容很长、CSS/JS/HTML 很大或容易被模型输出截断时，先写短骨架再用 "
        "append_file 分块追加；"
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
def write_file_content_parameter_detail() -> str:
    return (
        "会直接成为文件的新内容；原文件存在时会被整体覆盖。长文件请保持短骨架，"
        f"正常分块单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符，"
        "后续用 append_file 分块补齐；"
        f"单次 inline content 硬上限为 {MAX_INLINE_WRITE_CONTENT_CHARS} 字符。"
    )


# LLM: append_file_content_parameter_detail is the single source for append_file content docs.
# 函数用途: 生成 append_file.content 的模型可读说明，集中维护 inline 上限和分块建议。
def append_file_content_parameter_detail() -> str:
    return (
        "会直接拼接到文件尾部，不会替换已有内容。长文件分多次追加，"
        f"正常分块单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符；"
        f"单次 inline content 硬上限为 {MAX_INLINE_WRITE_CONTENT_CHARS} 字符。"
    )


# LLM: check_inline_write_content is the central guard for write_file and append_file content bodies.
# 函数用途: 判断正文是否过长；过长时生成分块/patch/controlled_exec 的长期修复提示。
def check_inline_write_content(request: InlineContentPolicyRequest) -> InlineContentPolicyDecision:
    actual = len(request.content)
    if actual <= MAX_INLINE_WRITE_CONTENT_CHARS:
        return InlineContentPolicyDecision(True, actual, MAX_INLINE_WRITE_CONTENT_CHARS)
    return InlineContentPolicyDecision(
        False,
        actual,
        MAX_INLINE_WRITE_CONTENT_CHARS,
        long_content_transport_hint(
            tool_name=request.tool_name,
            field_name=request.field_name,
            actual_chars=actual,
            path=request.path,
        ),
    )


# LLM: long_content_transport_hint teaches the model the durable fix instead of increasing limits.
# 函数用途: 生成统一长内容失败提示，避免模型重复输出同一个超长工具调用。
def long_content_transport_hint(
    *,
    tool_name: str,
    field_name: str,
    actual_chars: int,
    path: str = "",
) -> str:
    target = f" path={path}" if path else ""
    return (
        f"{tool_name}.{field_name} inline content 过长：{actual_chars} 字符，"
        f"最多 {MAX_INLINE_WRITE_CONTENT_CHARS} 字符。{target}\n"
        "长期规则：不要把大文件正文塞进一个 JSON 工具参数；流式输出也不能修复坏掉的工具 JSON。\n"
        f"请先用 write_file 写短骨架，再用 append_file 分块追加；每块 content 建议 "
        f"{RECOMMENDED_WRITE_CHUNK_CHARS} 字符，工具解析失败后降到不超过 "
        f"{RECOVERY_WRITE_CHUNK_CHARS} 字符。\n"
        "修改已有文件时优先用 replace_in_file/patch 风格的小 diff；"
        "如果已有父级 controlled_exec grant，可用 controlled_exec 在授权目录内运行脚本生成文件，"
        "并只返回 stdout_ref/audit_ref，不要回传完整正文。"
    )
