# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from ..subagents.role_templates import role_template_index_text
from ..tools import ToolSpec

_CREATE_USE_CASES = [
    "用户要求拆分任务、派多个子代理、开工单或让子代理分别处理事项",
    "需要把聊天里的计划落盘，后续由 dispatch_subagents 推进和验收",
]
_CREATE_KEYWORDS = ["子代理", "派工", "拆分", "工单", "任务", "subagent", "delegate", "spawn", "assign"]
_CREATE_PARAMETERS = {
    "goal": "总目标或任务描述，必填",
    "count": "创建多少个子代理，默认 1，受 max_subagents 限制",
    "role": "子代理角色模板 id；默认 worker",
    "tool_preset": "默认 automatic；显式 read_only/coding/none 时才覆盖自动工具策略",
    "allowed_tools": "显式工具列表；传了它就覆盖 role template 和 tool_preset",
    "acceptance_checks": "验收标准列表",
    "plan": "每个子代理的初始步骤列表",
    "workflow_mode": "off/plan/auto；决定是否在建工单时挂 workflow 计划",
    "extra_write_roots": "额外写入目录列表；目录必须位于 workspace_root 列表允许范围内",
}
_CREATE_PARAMETER_DETAILS = {
    "goal": "写清楚子代理要交付什么，不要只写一个空泛标题。",
    "count": "例如 3 表示创建 3 个并列子任务；如果任务需要人工精细拆分，可以多次调用本工具。",
    "role": "优先用模板角色，而不是临时造小角色。可用角色模板索引：\n{role_template_index}",
    "tool_preset": "省略时自动：由 role template、任务目标和调度器决定工具；`read_only` 只允许 list/read/search；`coding` 允许读写和替换文件；`none` 不授予工具。",
    "allowed_tools": "一般省略。只有受限环境才显式写 JSON 数组，例如 [\"read_file\", \"write_file\"]。",
    "acceptance_checks": "JSON 数组或多行文本，说明父代理后续怎样判断任务完成。",
    "plan": "JSON 数组或多行文本，给子代理的初始执行步骤。",
    "workflow_mode": "默认跟随配置：auto->auto，manual->plan，off->off。显式 coordinator/root/lead 入口会强制 off，孩子必须由该 coordinator 自己创建。",
    "extra_write_roots": "JSON 数组，例如 [\"C:/Users/you/Desktop/work\"]；只给本次子代理任务增加写入边界。",
}
_CREATE_EXAMPLES = [
    '{"tool":"create_subagents","goal":"在隔离 fixture 项目里实现三个小功能并写报告","count":3,"role":"worker","workflow_mode":"auto","acceptance_checks":["必须有文件证据","必须说明测试结果"]}',
    '{"tool":"create_subagents","goal":"检查多个 worker 的购物网站实现","count":1,"role":"bug_finder"}',
    '{"tool":"create_subagents","goal":"验收购物网站从注册到下单的完整流程","count":1,"role":"acceptor"}',
]

_BOARD_PARAMETERS = {
    "limit": "最多返回多少条明细，默认 10",
    "status": "按状态过滤，可选，如 PLANNING/DONE/BLOCKED",
}

_DISPATCH_PARAMETERS = {
    "apply": "是否写回低风险动作，默认 false",
    "execute_runners": "是否真实调用模型执行 runner，必须配合 apply=true",
    "execute_acceptance_tests": "是否执行子代理输出的父级验收 tests；runner 内部默认 true",
    "auto_apply_acceptance_followup": "tests 通过后是否自动应用验收 follow-up；runner 内部默认 true，顶层默认 false",
    "planner": "是否启用父代理 planner，默认 false",
    "workflow_mode": "off/plan/auto；是否在 dispatch 前补做 workflow 规划或自动派工",
    "max_runners": "本轮最多推进多少个 runner，默认 1；0 表示不执行 runner",
    "limit": "每阶段最多处理多少条记录，默认 20；0 表示不限制",
    "runner_instruction": "给 runner 的额外指令",
}
_DISPATCH_PARAMETER_DETAILS = {
    "apply": "顶层默认 false 只生成计划和报告；当前 runner 内部默认 true，只推进当前节点的直接孩子。显式 false 会覆盖默认。",
    "execute_runners": "顶层默认 false；当前 runner 内部且 apply=true 时默认 true，会消耗真实 API。显式 false 会覆盖默认。",
    "execute_acceptance_tests": "顶层默认 false；当前 runner 内部且 apply=true 时默认 true，用于执行直接 child 的 tests 并写 follow-up refs。",
    "auto_apply_acceptance_followup": "顶层默认 false；当前 runner 内部且 apply=true、tests 通过、follow-up 指向 apply_acceptance 时默认 true，只落当前直接 child 的验收状态。",
    "planner": "true 会额外调用父代理 LLM planner；适合长任务统筹，但会多消耗一次模型调用。",
    "workflow_mode": "plan 只把 workflow 计划写回父任务；auto 会在计划 OK 时落成 worker 子工单；未知值保守按 off 处理。",
    "max_runners": "用来限制本轮推进数量；顶层默认 1，runner 内部默认 6，避免父节点只推进一个孩子就超时。",
}

_SCHEDULE_CHILD_USE_CASES = [
    "当前 subagent runner 需要把自己的任务继续拆给下一层子/孙代理",
    "需要保持 main -> child -> grandchild 的层级边界，而不是外层直接创建叶子节点",
]
_SCHEDULE_CHILD_KEYWORDS = [
    "下一层",
    "子节点",
    "孙代理",
    "层级",
    "hierarchy",
    "child",
    "grandchild",
]
_SCHEDULE_CHILD_PARAMETERS = {
    "children": "下一层子任务列表，每项包含 goal/role/agent_name 等字段，必填",
    "apply": "是否真正创建下一层任务；runner 内默认 true，显式 false 只预览",
    "max_depth": "允许创建到的最大 depth，默认 3",
    "max_children": "父节点最多能拥有多少直接 child，0 表示不限制",
}
_SCHEDULE_CHILD_PARAMETER_DETAILS = {
    "children": (
        "JSON 数组。每项可含 role、agent_name、goal、plan、allowed_tools、allowed_skills、"
        "acceptance_checks、extra_write_roots。优先从这些角色模板索引里选 role：\n{role_template_index}"
    ),
    "apply": "runner 内省略时默认 true；显式 false 只返回会创建什么，适合先检查。",
    "max_depth": "用来避免子代理无限递归创建下级节点。",
    "max_children": "用来避免一个父节点一次挂太多直接孩子。",
}
_SCHEDULE_CHILD_EXAMPLES = [
    (
        '{"tool":"schedule_child_subagents","apply":true,"max_depth":3,'
        '"children":[{"role":"child_coordinator","agent_name":"catalog-lead",'
        '"goal":"继续拆分商品目录实现任务",'
        '"allowed_tools":["schedule_child_subagents","dispatch_subagents","subagent_board","read_file"]}]}'
    ),
    (
        '{"tool":"schedule_child_subagents","apply":true,'
        '"children":[{"role":"bug_finder","agent_name":"qa-finder","goal":"检查多个 worker 的实现和证据"},'
        '{"role":"tester","agent_name":"qa-tester","goal":"测试注册、登录、购物车和下单流程"},'
        '{"role":"acceptor","agent_name":"qa-acceptor","goal":"按验收标准判断是否可以交付"}]}'
    ),
]


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
        parameter_details=_with_role_template_index(_CREATE_PARAMETER_DETAILS),
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


# LLM: build_schedule_child_subagents_spec exposes hierarchy scheduling only inside runner context.
# 函数用途: 构建“当前节点创建下一层子节点”的模型工具规格，区别于顶层 create_subagents。
def build_schedule_child_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="schedule_child_subagents",
        category="orchestration",
        description="在当前 subagent runner 的名下创建下一层 child runs，保持层级树可恢复。",
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
