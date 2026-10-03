"""用户可见正文里的内部协议标记判定（vtm，2026-10-03）。

背景：旧实现在整段正文里用子串 `find` 找 `[run_`、`[subagent_` 这类标记，回复里只要出现
`self._run_cache[run_id] = ...` 这种代码，就从那里被整段截掉（projection_status=
internal_protocol_removed）。本文件钉住新判定：只有【独占行首的完整内部标签】才算协议，
普通文字、行内代码、代码块里的下标一律原样送达。
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.agent.conversation.user_visible_text import (
    contains_internal_protocol,
    sanitize_user_visible_text,
)

# 本次被截断的真实原文片段：正文里出现 Python 下标写法。
_TRUNCATED_EXCERPT = (
    "问题：347 行在窗口内也把结果写进缓存。\n"
    "```python\n"
    "344:  cached = self._run_cache.get(run_id)\n"
    "347:  self._run_cache[run_id] = (signature, self.load(run_id))\n"
    "```\n"
    "同样的问题也出现在 items[subagent_id] 和 calls[tool_call_index] 里。"
)

_SUBSCRIPT_SAMPLES = (
    "self._run_cache[run_id] = (signature, self.load(run_id))",
    "items[subagent_id] = item",
    "calls[tool_call_index] += 1",
    "results[tool_result_count] = 0",
)

# 拼出来的工具调用开标签，只用于构造“正文里单独提到一次”的场景；不成对、也不独占整行。
_BARE_TOOL_MARKER = "[" + "tool_call" + "]"


@pytest.mark.parametrize("sample", _SUBSCRIPT_SAMPLES)
def test_bracket_subscript_in_plain_prose_is_kept(sample: str) -> None:
    result = sanitize_user_visible_text(f"说明如下：\n{sample}\n以上。")
    assert result.removed_protocol is False
    assert sample in result.content
    assert contains_internal_protocol(sample) is False


@pytest.mark.parametrize("sample", _SUBSCRIPT_SAMPLES)
def test_bracket_subscript_in_inline_code_is_kept(sample: str) -> None:
    text = f"修复处是 `{sample}` 这一行。"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is False
    assert result.content == text


@pytest.mark.parametrize("sample", _SUBSCRIPT_SAMPLES)
def test_bracket_subscript_in_fenced_code_block_is_kept(sample: str) -> None:
    text = f"```python\n{sample}\n```"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is False
    assert result.content == text


def test_truncated_excerpt_reaches_user_intact() -> None:
    """本次真实截断的回归：整段（含代码块与行内下标）必须原样送出。"""
    result = sanitize_user_visible_text(_TRUNCATED_EXCERPT)
    assert result.removed_protocol is False
    assert result.content == _TRUNCATED_EXCERPT
    assert "self._run_cache[run_id] = (signature, self.load(run_id))" in result.content
    assert "items[subagent_id]" in result.content
    assert "calls[tool_call_index]" in result.content


def test_truncated_excerpt_projection_status_is_not_internal() -> None:
    projection = project_user_reply(_TRUNCATED_EXCERPT)
    assert projection.projection_status != "internal_protocol_removed"
    assert projection.content == _TRUNCATED_EXCERPT
    assert projection.internal_signal is False


# 宿主真实生成的每一种内部标记：整行出现时仍必须被剥掉。
_LINE_LEADING_MARKERS = (
    "[natural-user-reply]",
    "[RUN_CONTEXT_PRESSURE]",
    "[SUBAGENT_RESULT]",
    "[/SUBAGENT_RESULT]",
    "[tool_call]",
    "[/tool_call]",
    "[tool_result]",
    "[/tool_result]",
    "<tool_call>",
    "</tool_call>",
    "<function_call>",
    "</function_call>",
)


@pytest.mark.parametrize("marker", _LINE_LEADING_MARKERS)
def test_real_internal_marker_on_its_own_line_is_still_stripped(marker: str) -> None:
    text = f"给用户的正文\n{marker}\n内部内容"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is True
    assert result.content == "给用户的正文"


@pytest.mark.parametrize("marker", _LINE_LEADING_MARKERS)
def test_real_internal_marker_with_indent_is_still_stripped(marker: str) -> None:
    text = f"给用户的正文\n  {marker}  \n内部内容"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is True
    assert result.content == "给用户的正文"


def test_marker_substring_inside_a_word_is_not_a_marker() -> None:
    """`[run_` 只能以行首完整标签出现才算协议；跟在其它字符后面的不算。"""
    text = "见 prefix[run_1] 与 x[subagent_a] 两处。"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is False
    assert result.content == text


@pytest.mark.parametrize("prefix", ["[tool_call]", "[run_context]", "[SUBAGENT_RESULT]"])
def test_marker_prefix_inside_a_line_is_not_a_marker(prefix: str) -> None:
    """行首标记之后若还有正文（这一行不是“独占标签”），整行是普通文本，不算协议。

    钉住 mutation M4：去掉“标记后必须整行结束”的约束后，行内出现标记前缀会被误剥。
    """
    text = f"参数 {prefix} 是这行的开头写法，后面还有说明文字。"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is False
    assert result.content == text


def test_tool_envelope_block_still_stripped() -> None:
    """成对出现的工具块仍按原块正则剥掉，行为不变。"""
    text = "正文开头\n[TOOL_CALL]{\"name\": \"x\"}[/TOOL_CALL]\n正文结尾"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is True
    assert "[TOOL_CALL]" not in result.content
    assert "正文开头" in result.content


def test_bare_tool_marker_mention_is_not_a_block() -> None:
    """正文里单独提一次工具调用标签（没有成对闭标签、也不独占行）不算协议块，不得吞掉后文。"""
    text = f"参数 {_BARE_TOOL_MARKER} 用来标记工具调用开始。"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is False
    assert result.content == text


def test_bare_tool_marker_line_with_trailing_prose_is_not_a_block() -> None:
    """工具调用标签后同一行还有说明文字时，这一行是普通文本，不是协议块。"""
    text = f"{_BARE_TOOL_MARKER} 是这行的开头写法，后面还有说明文字。"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is False
    assert result.content == text


@pytest.mark.parametrize("sample", _SUBSCRIPT_SAMPLES)
def test_projection_status_for_subscript_is_not_internal(sample: str) -> None:
    projection = project_user_reply(f"修复处：\n{sample}")
    assert projection.projection_status != "internal_protocol_removed"
    assert sample in projection.content


def test_contains_internal_protocol_is_false_for_subscript_prose() -> None:
    assert contains_internal_protocol(_TRUNCATED_EXCERPT) is False


def test_uppercase_tool_marker_alone_on_line_is_stripped() -> None:
    """大写工具调用开标签独占一行、后面没有闭标签（模型输出被截断）时，仍算协议、要剥。

    钉住大小写口径：只按小写匹配会让这类全大写残块漏给用户（旧实现先 casefold，是剥掉的）。
    """
    text = "正文开头\n" + "[" + "TOOL_CALL" + "]\n正文结尾"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is True
    assert ("[" + "TOOL_CALL" + "]") not in result.content
    assert "正文开头" in result.content


@pytest.mark.parametrize("prefix", ["[" + "RUN_" + "]", "[" + "Run_" + "]", "[" + "ruN_" + "]"])
def test_mixed_case_run_marker_at_line_start_is_stripped(prefix: str) -> None:
    """运行标签在行首、大小写混写时仍算协议，要剥。"""
    text = f"正文开头\n{prefix}id 内部内容\n正文结尾"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is True
    assert "内部内容" not in result.content
    assert "正文开头" in result.content


def test_uppercase_marker_in_middle_of_line_is_kept() -> None:
    """大写标记出现在行中间（正文提及、代码下标）不算协议，原样送达。"""
    text = "这句提到 " + "[" + "TOOL_CALL" + "]" + " 但它在行中间，后面还有正文。"
    result = sanitize_user_visible_text(text)
    assert result.removed_protocol is False
    assert result.content == text


def test_memory_context_block_still_stripped() -> None:
    text = "给用户的正文\n<memory-context>\n{\"content\":\"internal\"}"
    result = sanitize_user_visible_text(text)
    assert result.content == "给用户的正文"
    assert result.removed_protocol is True
