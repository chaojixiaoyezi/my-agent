# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from ..tools import ToolSpec

_CREATE_USE_CASES = [
    "用户要求拆分任务、派多个子代理、开工单或让子代理分别处理事项",
    "需要把聊天里的计划落盘，后续由 dispatch_subagents 推进和验收",
]
_CREATE_KEYWORDS = ["子代理", "派工", "拆分", "工单", "任务", "subagent", "delegate", "spawn", "assign"]
_CREATE_PARAMETERS = {
    "goal": "总目标或任务描述，必填",
    "count": "创建多少个子代理，默认 1，受 max_subagents 限制",
    "tool_preset": "默认 read_only；coding 会授予文件读写工具；none 不授予工具",
    "allowed_tools": "显式工具列表；传了它就覆盖 tool_preset",
    "acceptance_checks": "验收标准列表",
    "plan": "每个子代理的初始步骤列表",
    "workflow_mode": "off/plan/auto；决定是否在建工单时挂 workflow 计划",
    "extra_write_roots": "额外写入目录列表；目录必须位于 workspace_root 列表允许范围内",
}
_CREATE_PARAMETER_DETAILS = {
    "goal": "写清楚子代理要交付什么，不要只写一个空泛标题。",
    "count": "例如 3 表示创建 3 个并列子任务；如果任务需要人工精细拆分，可以多次调用本工具。",
    "tool_preset": "`read_only` 只允许 list/read/search；`coding` 允许读写和替换文件；`none` 不授予工具。",
    "allowed_tools": "JSON 数组，例如 [\"read_file\", \"write_file\"]。如果需要写代码，通常至少给 read_file/search_text/write_file/replace_in_file。",
    "acceptance_checks": "JSON 数组或多行文本，说明父代理后续怎样判断任务完成。",
    "plan": "JSON 数组或多行文本，给子代理的初始执行步骤。",
    "workflow_mode": "默认跟随配置：auto->auto，manual->plan，off->off。显式传值会覆盖配置。",
    "extra_write_roots": "JSON 数组，例如 [\"C:/Users/you/Desktop/work\"]；只给本次子代理任务增加写入边界。",
}
_CREATE_EXAMPLES = [
    '{"tool":"create_subagents","goal":"在隔离 fixture 项目里实现三个小功能并写报告","count":3,"tool_preset":"coding","workflow_mode":"auto","acceptance_checks":["必须有文件证据","必须说明测试结果"]}',
    '{"tool":"create_subagents","goal":"调研 gateway 失败场景","count":2,"tool_preset":"read_only"}',
]

_BOARD_PARAMETERS = {
    "limit": "最多返回多少条明细，默认 10",
    "status": "按状态过滤，可选，如 PLANNING/DONE/BLOCKED",
}

_DISPATCH_PARAMETERS = {
    "apply": "是否写回低风险动作，默认 false",
    "execute_runners": "是否真实调用模型执行 runner，必须配合 apply=true",
    "planner": "是否启用父代理 planner，默认 false",
    "workflow_mode": "off/plan/auto；是否在 dispatch 前补做 workflow 规划或自动派工",
    "max_runners": "本轮最多推进多少个 runner，默认 1；0 表示不执行 runner",
    "limit": "每阶段最多处理多少条记录，默认 20；0 表示不限制",
    "runner_instruction": "给 runner 的额外指令",
}
_DISPATCH_PARAMETER_DETAILS = {
    "apply": "false 只生成计划和报告；true 会写审计日志并可能改变任务状态。",
    "execute_runners": "true 会消耗真实 API；只有用户明确要求开跑/真实执行/完整测试时才打开。",
    "planner": "true 会额外调用父代理 LLM planner；适合长任务统筹，但会多消耗一次模型调用。",
    "workflow_mode": "plan 只把 workflow 计划写回父任务；auto 会在计划 OK 时落成 worker 子工单。",
    "max_runners": "用来限制本轮推进数量，避免一次把太多子代理同时跑起来。",
}


# LLM: build_create_subagents_spec 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理spec所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def build_create_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="create_subagents",
        category="orchestration",
        description="创建一个或多个子代理任务记录，适合把复杂任务正式拆给子代理。",
        use_cases=_CREATE_USE_CASES,
        avoid_when=["只是解释思路、不需要真正创建任务时，不要调用；先直接回答即可"],
        keywords=_CREATE_KEYWORDS,
        parameters=_CREATE_PARAMETERS,
        parameter_details=_CREATE_PARAMETER_DETAILS,
        examples=_CREATE_EXAMPLES,
    )


# LLM: build_subagent_board_spec 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理看板spec所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def build_subagent_board_spec() -> ToolSpec:
    return ToolSpec(
        name="subagent_board",
        category="orchestration",
        description="查看当前子代理看板和状态摘要，用来判断任务是否待执行、待验收或卡住。",
        use_cases=["用户问当前任务进度、有哪些子代理、哪些任务卡住或完成", "调度前先查看任务树状态，避免重复派工"],
        avoid_when=["已经知道具体 run_id 且只需要执行 dispatch 时，可以直接调用 dispatch_subagents"],
        keywords=["任务状态", "看板", "进度", "子代理", "board", "status", "subagent"],
        parameters=_BOARD_PARAMETERS,
        examples=['{"tool":"subagent_board","limit":10}', '{"tool":"subagent_board","status":"BLOCKED","limit":20}'],
    )


# LLM: build_dispatch_subagents_spec 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理spec所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def build_dispatch_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="dispatch_subagents",
        category="orchestration",
        description="执行一轮子代理调度，可 dry-run，也可 apply 并调用真实 runner。",
        use_cases=["已经创建子代理后，用户要求推进、开跑、验收、处理卡住项", "需要让父代理检查 due-check、路由能力、执行 runner、审核 patch 或验收结果"],
        avoid_when=["只是创建任务时先用 create_subagents；没有明确推进意图时默认 dry-run 更稳"],
        keywords=["调度", "推进", "运行", "验收", "派工", "dispatch", "runner", "acceptance"],
        parameters=_DISPATCH_PARAMETERS,
        parameter_details=_DISPATCH_PARAMETER_DETAILS,
        examples=[
            '{"tool":"dispatch_subagents","apply":false,"workflow_mode":"plan","max_runners":1}',
            '{"tool":"dispatch_subagents","apply":true,"execute_runners":true,"workflow_mode":"auto","max_runners":2,"runner_instruction":"只在隔离 fixture 目录内写文件，并输出可验收证据"}',
        ],
    )
