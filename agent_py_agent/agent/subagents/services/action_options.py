# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""option objects for subagent action apply.

给人看的解释：
动作执行入口保留原有关键字兼容，但内部统一收成 options，避免长参数列表继续扩散。
"""

from dataclasses import dataclass, replace


# LLM: ActionApplyOptions 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存动作应用选项字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ActionApplyOptions:
    """User-selected filters and apply flags for action execution."""

    apply: bool = False
    action_filter: str = ""
    run_id: str = ""
    take_over_by: str = ""
    locked_files: list[str] | None = None
    limit: int = 0

    # LLM: from_values 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 转换values的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    @classmethod
    def from_values(
        cls,
        options: ActionApplyOptions | None = None,
        *,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
    ):
        """Build the options bundle from explicit legacy fields."""

        base = options or cls()
        updates = {
            "apply": apply,
            "action_filter": action_filter,
            "run_id": run_id,
            "take_over_by": take_over_by,
            "locked_files": locked_files,
            "limit": limit,
        }
        clean = {key: value for key, value in updates.items() if value is not None}
        return replace(base, **clean)
