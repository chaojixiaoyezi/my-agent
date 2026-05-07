# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""action service Params bundles shared by records and handlers."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import SubAgentTask
    from ..reports import ActionPlanItem


# LLM: RecordAfterTaskActionParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存记录after任务动作参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RecordAfterTaskActionParams:
    """Params bundle for creating a post-mutation action apply record."""

    # LLM: 动作记录创建和应用上下文在轻量整理后仍以参数包传递。
    action: ActionPlanItem
    task: SubAgentTask
    before_status: str
    before_channel_status: str
    message: str
    evidence_paths: list[str] | None = None
