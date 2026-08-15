# LLM: tool_context 渲染期的回溯回收入口，属于 agent_core/tool_context 模块。
#   契约：纯函数、不修改传入列表本身；只回收"窗口外 + 正文够大 + 带可取回锚点"的
#   工具结果条目，系统提示/近窗口/无锚点条目必须原样保留。改动时同步检查
#   prompting_parts/builder.py 的接线和 tests/test_tool_context_microcompact.py。
# 模块用途: 工具结果在 context 里累积过多时，把窗口外的旧结果正文换成 artifact
#   锚点占位（read_artifact 可取回完整），只保留最近 keep_recent 个完整。
#   现有 reducer 只在单条结果"生成时"按大小外置；
#   microcompact 补"事后回溯"——一条结果当时不大、但累积几十轮后早期那些一直占满 context。
#   在渲染 prompt 时调用，不改累积状态，原始历史仍完整保留。
from __future__ import annotations

_TOOL_OUTPUT_MARKER = "[tool-output-record"
_RECLAIM_ANCHOR_KEYS = ("scoped_call_id", "artifact_ref", "read_artifact_hint")
DEFAULT_MICROCOMPACT_KEEP_RECENT = 8
DEFAULT_MICROCOMPACT_MIN_CHARS = 1500
_RECLAIM_NOTE = (
    "[tool-output-microcompacted] 旧工具输出正文已回收以省 context；"
    "需要时用上面的 read_artifact_hint 取回完整内容。"
)


# LLM: 对外唯一入口。keep_recent<=0 表示关闭（与配置项"0 表示关闭"语义一致），
#   返回值始终是新列表。不要在这里加状态或写文件副作用。
# 函数用途: 渲染 prompt 前调用，回收窗口外的旧工具结果正文，保留最近 keep_recent 个完整。
def microcompact_tool_context(
    tool_context: list[str],
    *,
    keep_recent: int = DEFAULT_MICROCOMPACT_KEEP_RECENT,
    min_chars: int = DEFAULT_MICROCOMPACT_MIN_CHARS,
) -> list[str]:
    if keep_recent <= 0:
        return list(tool_context)
    result_indices = [i for i, item in enumerate(tool_context) if _TOOL_OUTPUT_MARKER in item]
    if len(result_indices) <= keep_recent:
        return list(tool_context)
    reclaim_set = set(result_indices[: len(result_indices) - keep_recent])
    return [
        _reclaim_entry(item) if (i in reclaim_set and len(item) >= min_chars and _has_anchor(item)) else item
        for i, item in enumerate(tool_context)
    ]


# LLM: 锚点判定决定"可取回才可回收"；扩展锚点键时同步更新 _reclaim_entry 的保留行规则。
# 函数用途: 判断条目里是否带 read_artifact 可取回的锚点信息。
def _has_anchor(item: str) -> bool:
    return any(key in item for key in _RECLAIM_ANCHOR_KEYS)


# LLM: 回收单条条目：保留调用头 + output 标记行 + 锚点行 + 回收说明，丢弃正文。
#   必须保证回收后的条目仍能让模型定位原始调用并取回完整输出。
# 函数用途: 把一条工具结果的正文换成锚点占位。
def _reclaim_entry(item: str) -> str:
    marker_pos = item.find(_TOOL_OUTPUT_MARKER)
    if marker_pos < 0:
        return item
    head = item[:marker_pos]  # [tool-record ...] + 调用 payload，保留
    result_lines = item[marker_pos:].splitlines()
    kept = [result_lines[0]] if result_lines else []  # [tool-output-record ...] 标记
    kept.extend(line for line in result_lines[1:] if any(key in line for key in _RECLAIM_ANCHOR_KEYS))
    kept.append(_RECLAIM_NOTE)
    return head + "\n".join(kept)


__all__ = [
    "DEFAULT_MICROCOMPACT_KEEP_RECENT",
    "DEFAULT_MICROCOMPACT_MIN_CHARS",
    "microcompact_tool_context",
]
