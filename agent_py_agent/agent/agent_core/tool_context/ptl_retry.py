# LLM: 单轮 PTL（prompt-too-long）重试的回收原语，属于 agent_core/tool_context。
#   契约：与 microcompact 不同，这里是**持久突变**——直接改传入的 tool_context 列表，
#   后续轮也看到回收后的形态（该回收操作有损，须公开恢复引用）。
#   只在 provider 实报上下文超限、且本轮重试上限内调用；带锚点的条目保留 read_artifact
#   取回线索，无锚点条目正文进 audit 流水后丢弃。改动时同步检查
#   tests/test_tool_context_ptl_retry.py 与 _tool_loop_service.next_tool_loop_model_response。
# 模块用途: compact 三件套之三。单轮 context 溢出时丢最老的工具结果正文直接重试，
#   轻量自救成功就不动重量级 compact/resume；救不回再走原有 context_overflow 路径。
from __future__ import annotations

from .microcompact import _RECLAIM_ANCHOR_KEYS, _TOOL_OUTPUT_MARKER

DEFAULT_PTL_RETRY_MAX = 3
_PTL_DROP_FRACTION = 0.2
_PTL_MIN_RECLAIM_CHARS = 200
_PTL_NOTE = (
    "[tool-output-ptl-reclaimed] 上下文超限，本条旧工具输出正文已回收重试；"
    "有 read_artifact_hint 的可取回完整内容，其余以 audit 流水为准。"
)


# LLM: 突变入口。每次回收最老 fraction 比例（至少 1 条）的完整工具结果正文；
#   已回收过的条目（带 _PTL_NOTE）和太小的条目不重复处理。返回 0 表示无可回收，
#   调用方必须停止重试、落回 compact 路径。
# 函数用途: PTL 重试前给 tool_context 瘦身。
def reclaim_oldest_tool_results_for_ptl(
    tool_context: list[str],
    *,
    fraction: float = _PTL_DROP_FRACTION,
) -> int:
    indices = [
        index
        for index, item in enumerate(tool_context)
        if _TOOL_OUTPUT_MARKER in item and _PTL_NOTE not in item and len(item) >= _PTL_MIN_RECLAIM_CHARS
    ]
    if not indices:
        return 0
    count = max(1, int(len(indices) * fraction))
    for index in indices[:count]:
        tool_context[index] = _force_reclaim_entry(tool_context[index])
    return count


# LLM: 与 microcompact._reclaim_entry 的区别：无锚点条目也回收（PTL 是最后手段），
#   保留调用头 + output 标记行 + 锚点行（如有）+ PTL 占位说明。
# 函数用途: 把一条工具结果正文强制换成占位。
def _force_reclaim_entry(item: str) -> str:
    marker_pos = item.find(_TOOL_OUTPUT_MARKER)
    if marker_pos < 0:
        return item
    head = item[:marker_pos]
    result_lines = item[marker_pos:].splitlines()
    kept = [result_lines[0]] if result_lines else []
    kept.extend(line for line in result_lines[1:] if any(key in line for key in _RECLAIM_ANCHOR_KEYS))
    kept.append(_PTL_NOTE)
    return head + "\n".join(kept)


__all__ = ["DEFAULT_PTL_RETRY_MAX", "reclaim_oldest_tool_results_for_ptl"]
