
from __future__ import annotations

from ...subagents.role_templates import role_template_index_text
from ...tooling.models import ToolSpec
from .tool_spec_data import (
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


def build_create_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="create_subagents",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="创建一个或多个子代理并默认立刻启动。不同工作切片优先用 items；相对时间要沿用当前日期/年份；只有 defer_start=true 才只建不跑。",
        use_cases=_CREATE_USE_CASES,
        avoid_when=["只是解释思路、不需要真正创建任务时，不要调用；先直接回答即可"],
        keywords=_CREATE_KEYWORDS,
        parameters=_CREATE_PARAMETERS,
        parameter_details=_with_role_template_index(_CREATE_PARAMETER_DETAILS),
        examples=_CREATE_EXAMPLES,
    )


def build_inspect_agent_tree_spec() -> ToolSpec:
    return ToolSpec(
        name="inspect_agent_tree",
        category="orchestration",
        effect="read_only",
        description="按需只读查看主代理、子代理、孙代理状态树；不会创建、调度、恢复或验收任务。",
        use_cases=["用户问当前有哪些代理在做什么", "只想看子代理/孙代理状态、心跳、当前工具、产物和阻塞原因"],
        avoid_when=[
            "子代理只是正在运行、没有新事实时不要循环查看；用 wait 等完成事件或继续做自己的汇总",
            "用户明确要求继续推进、恢复、重派或执行验收时，应使用 dispatch_subagents",
        ],
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
            "action": "read 或 update；不填默认 read。不要使用 create/init/start/begin/set/save/record/write 这类旧别名。",
            "run_id": "可选。读取指定代理 run 的进度；更新默认写当前代理自己的 run",
            "summary": "可选。当前整体进展一句话",
            "next_action": "可选。下一步最应该做什么",
            "items": "可选。进度项列表，每项可含 id/title/status/evidence/notes/next",
            "coverage": "可选。覆盖账本，含 goal/dimensions/targets；用于记录哪些对象已覆盖到哪些检查点；也可先写一句当前覆盖进度。",
            "coverage_targets": "可选。coverage.targets 的简写列表，每项可含 id/name/title/status/checks/expected_fields/fields/fields_needed/missing_fields/evidence/notes/next；也可写成“对象名: 字段A,字段B”。",
        },
        parameter_details={
            "items": "这是开放清单，不是业务模板。status 只用 pending/in_progress/done/skipped/blocked；completed/read/ok 这类说明写 notes/summary，不要写进 status。长文、长清单、逐章/逐项任务里，优先每个对象写一个 item；notes 写真实读到的短事实，evidence 写文件、offset/行号、artifact_ref 或来源说明。不要只写“章节001-012已覆盖”来代替逐项事实。",
            "coverage": "这是开放世界覆盖清单，不限定对象类型。targets 可以是项目、论文、API、日志源、文件、模块或任何当前任务对象；checks 的键由当前任务自己定义；如果你只知道要覆盖哪些字段，也可先填 expected_fields/fields_needed。长任务里建议边读、边分析、边写报告时更新，不要最后一次性随便打钩；范围进度和逐项事实最好分开写。",
        },
        examples=[
            '{"tool":"task_progress","action":"update","summary":"已读完两个项目","next_action":"继续读第三个项目","items":[{"id":"project-a","title":"阅读项目A","status":"done","evidence":["project-a/README.md","project-a/src/core.py"]}]}',
            '{"tool":"task_progress","action":"update","coverage":{"goal":"每个项目都要读 README、分析模块、写进报告","dimensions":["读 README","分析模块","写进报告"],"targets":[{"id":"project-a","checks":{"读 README":"done","分析模块":"pending"},"evidence":["project-a/README.md"]}]}}',
            '{"tool":"task_progress","action":"read"}',
        ],
    )


def build_dispatch_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="dispatch_subagents",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="推进、恢复或重跑已有子代理；普通查看状态用 inspect_agent_tree，普通创建开跑用 create_subagents。",
        use_cases=["用户要求继续推进、恢复、重跑或处理卡住项", "需要给某个子代理补充提示并立刻推进它继续执行"],
        avoid_when=["只是看状态时用 inspect_agent_tree；第一次派新子代理优先用 create_subagents；只补一句话优先用 send_guidance"],
        keywords=["调度", "推进", "运行", "验收", "派工", "dispatch", "subagent", "acceptance"],
        parameters=_DISPATCH_PARAMETERS,
        parameter_details=_DISPATCH_PARAMETER_DETAILS,
        examples=[
            '{"tool":"dispatch_subagents","dry_run":true,"max_runners":1}',
            '{"tool":"dispatch_subagents","dry_run":false,"run_ids":["child-phase-a","child-phase-b"],"max_runners":2}',
            '{"tool":"dispatch_subagents","dry_run":false,"run_ids":["child-phase-a"],"max_runners":1}',
        ],
    )


def build_cancel_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="cancel_subagents",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="取消已有子代理运行；会废弃 active attempt、记录取消审计，有关联 pid 时会尝试终止。",
        use_cases=[
            "用户要求停止某些子代理或整棵子代理树",
            "主代理发现子代理卡死、跑偏或不应继续消耗预算，需要显式收回",
            "后台 runner/channel 已损坏，需要把 agent tree 标成可见的取消/废弃状态",
        ],
        avoid_when=["只是查看状态时用 inspect_agent_tree；只是补充说明让它继续时用 send_guidance 或 dispatch_subagents"],
        keywords=["取消", "停止", "kill", "cancel", "subagent", "runner", "ABANDONED", "CANCELLED"],
        parameters={
            "run_id": "可选。单个子代理 run_id。",
            "run_ids": "可选。多个子代理 run_id。和 root_id/status 组合时会取并集后去重。",
            "root_id": "可选。取消某个 root_id 自己和它下面的子代理。",
            "status": "可选。只取消指定状态的子代理，例如 RUNNING/PLANNING/CHANNEL_ERROR；可写字符串或列表。",
            "reason": "可选。取消原因，会写入子代理 work log 和审计字段。",
            "kill_process": "可选。默认 true；如果任务记录里有关联 pid，会尝试 terminate。",
            "dry_run": "可选。默认 false；true 时只返回会取消哪些 run_id，不改状态。",
        },
        parameter_details={
            "root_id": "root_id 会匹配 root 自己以及 child_ids 递归子树；如果没有 run_id/run_ids/root_id/status，工具会返回错误，避免误取消全部。",
            "status": "status 只作为过滤条件；传 status 但不传 run_id/root_id 时，会匹配当前子代理账本里所有该状态任务。",
        },
        examples=[
            '{"tool":"cancel_subagents","run_ids":["subagent-1","subagent-2"],"reason":"用户要求停止"}',
            '{"tool":"cancel_subagents","root_id":"subagent-root","status":["RUNNING","PLANNING"],"reason":"重派前清理"}',
            '{"tool":"cancel_subagents","status":"CHANNEL_ERROR","dry_run":true}',
        ],
    )


def build_schedule_child_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="schedule_child_subagents",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="在当前子代理名下创建下一层子代理，保持层级树可恢复。"
        + _SCHEDULE_CHILD_COORDINATOR_RULES,
        use_cases=_SCHEDULE_CHILD_USE_CASES,
        avoid_when=["顶层主代理第一次派工时继续用 create_subagents；没有当前子代理上下文时不要调用"],
        keywords=_SCHEDULE_CHILD_KEYWORDS,
        parameters=_SCHEDULE_CHILD_PARAMETERS,
        parameter_details=_with_role_template_index(_SCHEDULE_CHILD_PARAMETER_DETAILS),
        examples=_SCHEDULE_CHILD_EXAMPLES,
    )


def _with_role_template_index(details: dict[str, str]) -> dict[str, str]:
    return {
        key: value.replace("{role_template_index}", role_template_index_text())
        for key, value in details.items()
    }
