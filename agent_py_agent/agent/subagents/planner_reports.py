# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""parent-planner report dataclasses split from reports.py.

给人看的解释：
父级 planner 的记录字段比较多，单独放这里，让 reports.py 继续作为兼容导出入口。
"""

from dataclasses import dataclass, field


# LLM: ParentPlannerParsedOutput 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存父级收口和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class ParentPlannerParsedOutput:
    """父代理 planner 的结构化模型输出。"""

    found: bool
    ok: bool
    decision: str = ""
    summary: str = ""
    should_dispatch: bool = True
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    actions: list[dict[str, object]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    parse_error: str = ""
    raw_json: dict[str, object] = field(default_factory=dict)


# LLM: ParentPlannerRecord 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存父级规划器记录字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class ParentPlannerRecord:
    """父代理 planner 的一次审计记录。"""

    id: str
    dry_run: bool
    triggered: bool
    ok: bool
    decision: str
    message: str
    gate_summary: dict[str, int] = field(default_factory=dict)
    backend: str = ""
    tool_rounds: int = 0
    parse_error: str = ""
    summary: str = ""
    actions: list[dict[str, object]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    prompt_path: str = ""
    response_path: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


# LLM: ParentPlannerReport 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存父级规划器报告字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class ParentPlannerReport:
    """父代理 planner 报告。"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ParentPlannerRecord]
