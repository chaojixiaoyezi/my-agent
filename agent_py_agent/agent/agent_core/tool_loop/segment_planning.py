# LLM: 本模块只按既有调度描述和实时 Compact 查询选连续段，不接 Agent、回合参数、执行器或持久状态；查询异常原样上抛。
# 模块用途: 计算下一批可并行工具的范围及批上限，让执行轮继续独占取消、审批、线程和记账顺序。
from __future__ import annotations

from collections.abc import Callable

from ...tooling.concurrency import ToolConcurrencyDescriptor, concurrency_conflicts
from ...tooling.runtime_contracts import ToolCall

_DEFAULT_MAX_PARALLEL_TOOL_CALLS = 8


# LLM: 扫描每条候选时先查询 Compact，再查询调度描述；命中屏障、冲突或批上限即停，不能预取后续事实或吞掉异常。
# 函数用途: 返回从 start 开始可共用一个并发段的尾索引，只做判定，不启动、取消或记录任何调用。
def parallel_segment_end(
    calls: list[ToolCall],
    start: int,
    *,
    batch_limit: int,
    defer_for_compact: Callable[[str], bool],
    describe_call: Callable[[ToolCall], ToolConcurrencyDescriptor],
) -> int:
    descriptors: list[ToolConcurrencyDescriptor] = []
    position = start
    while position < len(calls) and (
        batch_limit <= 0 or position - start < batch_limit
    ):
        call = calls[position]
        if defer_for_compact(call.tool_name):
            break
        descriptor = describe_call(call)
        if not descriptor.parallel_eligible:
            break
        if any(concurrency_conflicts(descriptor, prior) for prior in descriptors):
            break
        descriptors.append(descriptor)
        position += 1
    return position


# LLM: 输入已由装配点按任务属性优先于配置解析；并发负数/缺省仍用 8，批大小非正数不限制，两个正上限取小值。
# 函数用途: 合并并发与每批数量限制，保留显式 0 不限制的既有合同，不限制整个 provider turn 的调用总数。
def resolve_parallel_batch_limit(
    parallel_limit: int | None,
    tool_batch_limit: int | None,
) -> int:
    if parallel_limit is None or parallel_limit < 0:
        parallel_limit = _DEFAULT_MAX_PARALLEL_TOOL_CALLS
    if tool_batch_limit is None or tool_batch_limit <= 0:
        tool_batch_limit = 0
    limits = [value for value in (parallel_limit, tool_batch_limit) if value > 0]
    return min(limits) if limits else 0


# LLM: 只复用旧整数转换合同，布尔值无效，TypeError/ValueError 表示缺省；OverflowError 等其它异常仍由调用方看到。
# 函数用途: 解析任务或配置的批大小数值，保留缺省、零和负数供各自限制规则裁决。
def parse_optional_batch_limit(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
