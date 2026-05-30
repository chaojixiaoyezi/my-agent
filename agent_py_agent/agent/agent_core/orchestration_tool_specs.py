# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from ..subagents.role_templates import role_template_index_text
from ..tools import ToolSpec
from .orchestration_tool_spec_data import (
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
        description="创建一个或多个子代理并默认立刻启动。不同工作切片优先用 items；相对时间要沿用 Workspace Context 的 current_local_date/current_local_year；只有 defer_start=true 才只建不跑。",
        use_cases=_CREATE_USE_CASES,
        avoid_when=["只是解释思路、不需要真正创建任务时，不要调用；先直接回答即可"],
        keywords=_CREATE_KEYWORDS,
        parameters=_CREATE_PARAMETERS,
        parameter_details=_with_role_template_index(_CREATE_PARAMETER_DETAILS),
        examples=_CREATE_EXAMPLES,
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


def build_raise_event_spec() -> ToolSpec:
    return ToolSpec(
        name="raise_event",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="记录普通进展、阻塞、观察或需要主代理处理的事件；urgent 会写 wake queue。",
        use_cases=[
            "子代理发现非紧急现象、阶段进展、阻塞原因或证据变化，需要主代理后续判断",
            "子/孙代理发现紧急事件、阻塞、用户需要知道的结果，不能等普通定时汇报",
            "长期监控任务把状态变化写入持久会话账本，但不需要立刻打断主代理",
        ],
        avoid_when=["只是给当前模型自己看的临时想法，不需要主代理或用户知道时不要调用"],
        keywords=["观察", "事件", "进展", "阻塞", "上报", "observation", "event", "wake", "urgent"],
        parameters=_OBSERVATION_PARAMETERS,
        examples=[
            '{"tool":"raise_event","task_id":"task-1","event_type":"progress",'
            '"summary":"子代理完成一轮检查，发现一个待复核现象","requires_main_agent":true}',
            '{"tool":"raise_event","task_id":"task-1","event_type":"runtime_alert",'
            '"urgency":"urgent","summary":"发现需要主代理马上判断的事件","dedupe_key":"task-1:event"}'
        ],
    )


def build_task_progress_spec() -> ToolSpec:
    return ToolSpec(
        name="task_progress",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="记录或读取当前任务的进度清单，帮助长任务和 compact 后续接；它只是软账本，不代表验收通过。",
        use_cases=[
            "任务很长，需要记下哪些小块已完成、正在做、下一步是什么",
            "任务要求覆盖多个对象，例如每个项目、每篇论文、每周数据、每个 API 或每个文件",
            "compact 后要恢复当前代理自己的工作进度",
            "父代理查看 tree 前，希望子代理有简短进度摘要",
        ],
        avoid_when=["只做一句普通回复、不需要跨轮保存进度时可以不用"],
        keywords=["进度", "清单", "todo", "checkpoint", "继续做", "compact", "任务账本"],
        parameters={
            "action": "read 或 update；create/init/start/begin/set/save/record/write 会按 update 处理；不填默认 read",
            "run_id": "可选。读取指定代理 run 的进度；更新默认写当前代理自己的 run",
            "summary": "可选。当前整体进展一句话",
            "next_action": "可选。下一步最应该做什么",
            "items": "可选。进度项列表，每项可含 id/title/status/evidence/notes/next",
            "coverage": "可选。覆盖账本，含 goal/dimensions/targets；用于记录哪些对象已覆盖到哪些检查点；也可先写一句当前覆盖进度。",
            "coverage_targets": "可选。coverage.targets 的简写列表，每项可含 id/name/title/status/checks/expected_fields/fields/fields_needed/missing_fields/evidence/notes/next；也可写成“对象名: 字段A,字段B”。",
        },
        parameter_details={
            "items": "这是开放清单，不是业务模板。status 可写 pending/in_progress/done/skipped/blocked，也可写更适合当前任务的短状态。",
            "coverage": "这是开放世界覆盖清单，不限定对象类型。targets 可以是项目、论文、API、日志源、文件、模块或任何当前任务对象；checks 的键由当前任务自己定义；如果你只知道要覆盖哪些字段，也可先填 expected_fields/fields_needed。",
        },
        examples=[
            '{"tool":"task_progress","action":"update","summary":"已读完两个项目","next_action":"继续读第三个项目","items":[{"id":"project-a","title":"阅读项目A","status":"done"}]}',
            '{"tool":"task_progress","action":"update","coverage":{"goal":"每个项目都要读 README、分析模块、写进报告","dimensions":["读 README","分析模块","写进报告"],"targets":[{"id":"project-a","checks":{"读 README":"done","分析模块":"pending"}}]}}',
            '{"tool":"task_progress","action":"read"}',
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
        description="推进、恢复、重跑或查看已有子代理状态；普通创建开跑优先用 create_subagents，单纯补一句提示优先用 send_guidance。",
        use_cases=["用户要求继续推进、恢复、重跑、查看或处理卡住项", "需要给某个 runner 注入提示并立刻推进它继续执行"],
        avoid_when=["只是第一次派新子代理时优先用 create_subagents；只是运行中补一句话、纠偏或提醒时优先用 send_guidance"],
        keywords=["调度", "推进", "运行", "验收", "派工", "dispatch", "runner", "acceptance"],
        parameters=_DISPATCH_PARAMETERS,
        parameter_details=_DISPATCH_PARAMETER_DETAILS,
        examples=[
            '{"tool":"dispatch_subagents","dry_run":true,"workflow_mode":"plan","max_runners":1}',
            '{"tool":"dispatch_subagents","dry_run":false,"run_ids":["coordinator-id"],"runner_instruction":"只创建下一层 refs，不要执行 leaf worker"}',
            '{"tool":"dispatch_subagents","dry_run":false,"run_ids":["child-phase-a","child-phase-b"],"max_runners":2}',
            '{"tool":"dispatch_subagents","dry_run":false,"run_ids":["child-phase-a"],"max_runners":1,"runner_instruction":"补充给 phase-a，并立刻推进它继续执行"}',
            '{"tool":"dispatch_subagents","dry_run":false,"take_over_by":"subagent-new-leader","max_runners":0}',
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
