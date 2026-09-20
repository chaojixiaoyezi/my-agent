# LLM: 本模块只归并前后台同片 Compact 的结构化携带快照；不读 Agent、store 或 mailbox，不产生副作用。
# 模块用途: 统一超窗后保留哪些已执行工具和已提交插话；释放与压缩提交仍由各自运行器沿原合同执行。
from __future__ import annotations

from collections.abc import Iterable

from .active_turn_input import exclude_active_turn_user_input_ids, merge_active_turn_user_inputs


# LLM: 最新非空完整归档替换旧快照；插话按原 ID 合并并排除已释放编号，不能从正文恢复或合并工具增量。
# 函数用途: 为同一轮压缩重试计算工具和插话携带内容，不修改传入快照；同步前后台超窗及插话恢复回归。
def compact_overflow_carry(
    *,
    carried_archive_tool_calls: list[dict[str, object]],
    carried_active_turn_user_inputs: list[dict[str, object]],
    result_archive_tool_calls: object,
    result_active_turn_user_inputs: object,
    released_input_ids: Iterable[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    result_archive = [dict(item) for item in list(result_archive_tool_calls or []) if isinstance(item, dict)]
    next_inputs = exclude_active_turn_user_input_ids(
        merge_active_turn_user_inputs(carried_active_turn_user_inputs, result_active_turn_user_inputs),
        released_input_ids,
    )
    return result_archive or carried_archive_tool_calls, next_inputs
