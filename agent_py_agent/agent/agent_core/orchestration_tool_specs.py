# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from ..subagents.role_templates import role_template_index_text
from ..tools import ToolSpec
from .orchestration_tool_spec_data import (
    _BOARD_PARAMETERS,
    _CREATE_EXAMPLES,
    _CREATE_KEYWORDS,
    _CREATE_PARAMETER_DETAILS,
    _CREATE_PARAMETERS,
    _CREATE_USE_CASES,
    _DISPATCH_PARAMETER_DETAILS,
    _DISPATCH_PARAMETERS,
    _INSPECT_TREE_PARAMETERS,
    _OBSERVATION_PARAMETERS,
    _SCHEDULE_CHILD_COORDINATOR_RULES,
    _SCHEDULE_CHILD_EXAMPLES,
    _SCHEDULE_CHILD_KEYWORDS,
    _SCHEDULE_CHILD_PARAMETER_DETAILS,
    _SCHEDULE_CHILD_PARAMETERS,
    _SCHEDULE_CHILD_USE_CASES,
)


# LLM: build_create_subagents_spec 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理spec所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def build_create_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="create_subagents",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="创建一个或多个子代理并默认立刻启动。不同工作切片优先用 items/tasks；只有 defer_start=true 才只建不跑。",
        use_cases=_CREATE_USE_CASES,
        avoid_when=["只是解释思路、不需要真正创建任务时，不要调用；先直接回答即可"],
        keywords=_CREATE_KEYWORDS,
        parameters=_CREATE_PARAMETERS,
        parameter_details=_with_role_template_index(_CREATE_PARAMETER_DETAILS),
        examples=_CREATE_EXAMPLES,
    )


# LLM: build_subagent_board_spec 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理看板spec所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def build_subagent_board_spec() -> ToolSpec:
    return ToolSpec(
        name="subagent_board",
        category="orchestration",
        effect="read_only",
        description="查看当前子代理看板和状态摘要，用来判断任务是否待执行、待收口或卡住。",
        use_cases=["用户问当前任务进度、有哪些子代理、哪些任务卡住或完成", "调度前先查看任务树状态，避免重复派工"],
        avoid_when=["已经知道具体 run_id 且只需要执行 dispatch 时，可以直接调用 dispatch_subagents"],
        keywords=["任务状态", "看板", "进度", "子代理", "board", "status", "subagent"],
        parameters=_BOARD_PARAMETERS,
        examples=['{"tool":"subagent_board","limit":10}', '{"tool":"subagent_board","status":"BLOCKED","limit":20}'],
    )


# LLM: build_inspect_agent_tree_spec gives models a read-only status path distinct from dispatch.
# 函数用途: 构建 inspect_agent_tree 工具说明，明确它只看状态、不推进任务。
def build_inspect_agent_tree_spec() -> ToolSpec:
    return ToolSpec(
        name="inspect_agent_tree",
        category="orchestration",
        effect="read_only",
        description="只读查看主代理、子代理、孙代理状态树；不会创建、调度、恢复或验收任务。",
        use_cases=["用户问当前有哪些代理在做什么", "只想看子代理/孙代理状态、心跳、当前工具、产物和阻塞原因"],
        avoid_when=["用户明确要求继续推进、恢复、重派或执行验收时，应使用 dispatch_subagents"],
        keywords=["代理树", "状态树", "看一眼", "子代理状态", "孙代理", "inspect", "agent tree"],
        parameters=_INSPECT_TREE_PARAMETERS,
        examples=[
            '{"tool":"inspect_agent_tree"}',
            '{"tool":"inspect_agent_tree","root_id":"subagent-123"}',
            '{"tool":"inspect_agent_tree","run_id":"subagent-456","scope":"own_subtree"}',
        ],
    )


def build_raise_observation_spec() -> ToolSpec:
    return ToolSpec(
        name="raise_observation",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="子/孙代理写入一条观察事实；普通观察不会自己推进任务，主代理下次醒来会看到。",
        use_cases=[
            "子代理发现非紧急现象、阶段进展、阻塞原因或证据变化，需要主代理后续判断",
            "长期监控任务把状态变化写入持久会话账本，但不需要立刻打断主代理",
        ],
        avoid_when=["只是给当前模型自己看的临时想法，不需要主代理或用户知道时不要调用"],
        keywords=["观察", "事件", "进展", "阻塞", "上报", "observation", "event"],
        parameters=_OBSERVATION_PARAMETERS,
        examples=[
            '{"tool":"raise_observation","task_id":"task-1","event_type":"progress",'
            '"summary":"子代理完成一轮检查，发现一个待复核现象","requires_main_agent":true}'
        ],
    )


def build_raise_main_event_spec() -> ToolSpec:
    return ToolSpec(
        name="raise_main_event",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="子/孙代理上报需要主代理处理的事件；urgent 会进入 wake queue 立即唤醒主代理。",
        use_cases=[
            "子代理发现紧急事件、阻塞、用户需要知道的结果，不能等普通定时汇报",
            "孙代理发现需要主代理二次分析或调度的信号，要穿透父子层级叫醒主代理",
        ],
        avoid_when=["只是普通流水进展且不需要主代理介入时，用 raise_observation"],
        keywords=["叫醒主代理", "紧急事件", "主代理处理", "wake", "urgent", "main event"],
        parameters=_OBSERVATION_PARAMETERS,
        examples=[
            '{"tool":"raise_main_event","task_id":"task-1","event_type":"runtime_alert",'
            '"urgency":"urgent","summary":"发现需要主代理马上判断的事件","dedupe_key":"task-1:event"}'
        ],
    )


# LLM: build_dispatch_subagents_spec 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理spec所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def build_dispatch_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="dispatch_subagents",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="给已有子代理补充提示、催促、推进、恢复、重跑或查看状态；普通创建开跑优先用 create_subagents。",
        use_cases=["用户要求催一下、继续推进、恢复、重跑、查看或处理卡住项", "需要给某个 runner 注入一条补充提示并让它继续执行"],
        avoid_when=["只是第一次派新子代理时优先用 create_subagents；它默认会创建并启动"],
        keywords=["调度", "推进", "运行", "验收", "派工", "dispatch", "runner", "acceptance"],
        parameters=_DISPATCH_PARAMETERS,
        parameter_details=_DISPATCH_PARAMETER_DETAILS,
        examples=[
            '{"tool":"dispatch_subagents","dry_run":true,"workflow_mode":"plan","max_runners":1}',
            '{"tool":"dispatch_subagents","dry_run":false,"run_ids":["coordinator-id"],"runner_instruction":"只创建下一层 refs，不要执行 leaf worker"}',
            '{"tool":"dispatch_subagents","dry_run":false,"run_ids":["child-phase-a","child-phase-b"],"max_runners":2}',
            '{"tool":"dispatch_subagents","dry_run":false,"run_ids":["child-phase-a"],"max_runners":1,"runner_instruction":"只补充 phase-a 子任务自己的执行重点"}',
            '{"tool":"dispatch_subagents","apply":true,"execute_runners":false,"take_over_by":"subagent-new-leader","max_runners":0}',
        ],
    )


# LLM: build_schedule_child_subagents_spec exposes hierarchy scheduling only inside runner context.
# 函数用途: 构建“当前节点创建下一层子节点”的模型工具规格，区别于顶层 create_subagents。
def build_schedule_child_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="schedule_child_subagents",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="在当前 subagent runner 的名下创建下一层 child runs，保持层级树可恢复。"
        + _SCHEDULE_CHILD_COORDINATOR_RULES,
        use_cases=_SCHEDULE_CHILD_USE_CASES,
        avoid_when=["顶层主代理第一次派工时继续用 create_subagents；没有当前 runner 上下文时不要调用"],
        keywords=_SCHEDULE_CHILD_KEYWORDS,
        parameters=_SCHEDULE_CHILD_PARAMETERS,
        parameter_details=_with_role_template_index(_SCHEDULE_CHILD_PARAMETER_DETAILS),
        examples=_SCHEDULE_CHILD_EXAMPLES,
    )


# LLM: _with_role_template_index injects lightweight catalog metadata only when building specs.
# 函数用途: 运行时生成工具规格时插入模板索引，避免模块 import 阶段加载完整模板详情。
def _with_role_template_index(details: dict[str, str]) -> dict[str, str]:
    return {
        key: value.replace("{role_template_index}", role_template_index_text())
        for key, value in details.items()
    }
