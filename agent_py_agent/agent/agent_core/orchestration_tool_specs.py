# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from ..subagents.role_templates import role_template_index_text
from ..tools import ToolSpec

_CREATE_USE_CASES = [
    "用户要求拆分任务、派多个子代理、开工单或让子代理分别处理事项",
    "需要把聊天里的计划落盘，后续由 dispatch_subagents 推进和验收",
    "材料很多且用户要求派工时，先读 README/目标/评分/目录等最小必要信息，再用 items/tasks 派小傻妞分别读取和分析正文",
]
_CREATE_KEYWORDS = ["子代理", "派工", "拆分", "工单", "任务", "subagent", "delegate", "spawn", "assign"]
_CREATE_PARAMETERS = {
    "goal": "单任务模式的总目标或任务描述；如果传 items/tasks，可省略",
    "items": "批量模式：独立子任务对象列表，每项必须有 goal，可单独写 role/agent_name/plan/acceptance_checks",
    "tasks": "items 的别名，兼容 Hermes 风格的 tasks[] 批量委托",
    "count": "单任务模式创建多少个同目标子代理，默认 1，受 max_subagents 限制；不同切片请用 items/tasks",
    "role": "子代理角色模板 id；默认 worker",
    "tool_preset": "默认 automatic；显式 read_only/coding/none 时才覆盖自动工具策略",
    "allowed_tools": "显式工具列表；一般省略。若同时传 frontend-dev/coding 等写作预设，系统会补齐必要读写工具，避免少填工具导致卡住。",
    "acceptance_checks": "验收标准列表",
    "plan": "每个子代理的初始步骤列表",
    "workflow_mode": "off/plan/auto；决定是否在建工单时挂 workflow 计划",
    "extra_write_roots": "额外写入目录列表；目录必须位于 workspace_root 列表允许范围内",
}
_CREATE_PARAMETER_DETAILS = {
    "goal": (
        "写清楚子代理要交付什么，不要只写一个空泛标题。"
        "必须保留用户原始硬约束，不得反向改写：例如用户说不要失灵按钮，就不能写“按钮可指向 #”或“可以用 #锚点”；"
        "用户说不要失效图片，就不要擅自要求远程图片 URL。"
    ),
    "count": (
        "例如 3 表示创建 3 个同目标并列子任务。不同工作切片不要用 count 复制同一个 goal；"
        "优先传 items/tasks，每项写独立 goal/agent_name；只有确实需要多个同质 worker 时才用 count。"
    ),
    "items": (
        "推荐批量入口，等价于 Hermes delegate_task 的 tasks[]："
        "[{\"goal\":\"研究市场\",\"role\":\"worker\",\"agent_name\":\"小傻妞-市场\"},"
        "{\"goal\":\"研究竞争\",\"role\":\"worker\",\"agent_name\":\"小傻妞-竞争\"}]。"
        "create_subagents 只创建任务记录；返回后要调用 dispatch_subagents 才会真实执行。"
        "如果任务材料很多，不要由 root 先读完所有正文再派工；root 只读最小必要信息，"
        "把具体正文、数据表和长报告的读取分析写进各 item 的 goal。"
    ),
    "tasks": "items 的兼容别名，字段规则相同。",
    "role": "优先用模板角色，而不是临时造小角色。可用角色模板索引：\n{role_template_index}",
    "tool_preset": "省略时自动：由 role template、任务目标和调度器决定工具；角色模板默认保留基础读写/汇报能力。`none` 只表示不覆盖自动策略。",
    "allowed_tools": "一般省略。只有受限环境才显式写 JSON 数组，例如 [\"read_file\", \"write_file\"]。",
    "acceptance_checks": "JSON 数组或多行文本，说明父代理后续怎样判断任务完成。",
    "plan": "JSON 数组或多行文本，给子代理的初始执行步骤。",
    "workflow_mode": "默认建议省略或写 off。只有用户明确要求 workflow/工作流时才写 plan/auto；明确文件交付 worker 会强制 off。",
    "extra_write_roots": (
        "JSON 数组，例如 [\"C:/Users/you/Desktop/work\"]；只给本次子代理任务增加写入边界。"
        "凡是要写真实交付物、恢复 worker、重试超时 worker，都必须保留用户给的绝对产物目录；"
        "不要只在 goal 里写“目标目录/同一目录/任务目录”。"
    ),
}
_CREATE_EXAMPLES = [
    (
        '{"tool":"create_subagents","items":['
        '{"goal":"研究市场环境并输出证据摘要","role":"worker","agent_name":"小傻妞-市场"},'
        '{"goal":"研究竞争格局并输出证据摘要","role":"worker","agent_name":"小傻妞-竞争"},'
        '{"goal":"制定进入策略并整合风险","role":"coordinator","agent_name":"小傻妞-策略"}],'
        '"acceptance_checks":["必须有证据","必须标注未确认信息"]}'
    ),
    '{"tool":"create_subagents","goal":"在隔离 fixture 项目里实现三个小功能并写报告","count":3,"role":"worker","workflow_mode":"off","acceptance_checks":["必须有文件证据","必须说明测试结果"]}',
    '{"tool":"create_subagents","goal":"在 /workspace/deliverables/shop/build 实现购物网站 HTML 骨架和 products.json","count":1,"role":"worker","agent_name":"小傻妞-基础结构","extra_write_roots":["/workspace/deliverables/shop/build"]}',
    '{"tool":"create_subagents","goal":"在 /workspace/deliverables/shop/build 实现购物网站 styles.css 和 app.js 交互","count":1,"role":"worker","agent_name":"小傻妞-样式交互","extra_write_roots":["/workspace/deliverables/shop/build"]}',
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
    "execute_acceptance_tests": "是否执行子代理输出的父级验收 tests；真实执行 runner 时默认 true",
    "auto_apply_acceptance_followup": "tests 通过后是否自动应用验收 follow-up；真实执行 runner 时默认 true",
    "planner": "是否启用父代理 planner，默认 false",
    "workflow_mode": "off/plan/auto；是否在 dispatch 前补做 workflow 规划或自动派工",
    "max_runners": "本轮最多推进多少个 runner，默认 1；0 表示不执行 runner",
    "limit": "每阶段最多处理多少条记录，默认 20；0 表示不限制",
    "run_ids": "精确指定本轮要推进的 run_id 列表，按给定顺序执行；也可写 include_run_ids",
    "runner_instruction": "给单个 runner 的额外指令；多 run_ids 同轮执行时会被忽略以防串线",
    "take_over_by": "显式指定执行接管/重挂动作的 leader run_id；用于 coordinator 挂掉后的领导权恢复",
    "locked_files": "本轮接管或重派时需要保守锁定的文件列表，避免恢复动作和仍在运行的分支互相覆盖",
}
_DISPATCH_PARAMETER_DETAILS = {
    "apply": "顶层默认 false 只生成计划和报告；当前 runner 内部默认 true，只推进当前节点的直接孩子。显式 false 会覆盖默认。",
    "execute_runners": (
        "顶层默认 false；当前 runner 内部且 apply=true 时默认 true，会消耗真实 API。"
        "如果目标是让某个 coordinator 亲自创建下一层 refs，必须对这个 coordinator 设置 execute_runners=true；"
        "不要把“下下层 worker 暂不执行”误写成当前 coordinator 的 execute_runners=false。"
    ),
    "execute_acceptance_tests": "apply=true 且 execute_runners=true 时固定为 true，用受控 TestExecutor 执行直接 child 声明的 tests 并写 follow-up refs；模型工具调用不能跳过父级验收，CLI 手动 --no-execute-tests 另走直达参数。",
    "auto_apply_acceptance_followup": "apply=true、execute_runners=true、tests 通过且 follow-up 指向 apply_acceptance 时默认 true，只落本轮直接 child 的验收状态；失败不会自动通过。",
    "planner": "true 会额外调用父代理 LLM planner；适合长任务统筹，但会多消耗一次模型调用。",
    "workflow_mode": "plan 只把 workflow 计划写回父任务；auto 会在计划 OK 时落成 worker 子工单；未知值保守按 off 处理。",
    "max_runners": "用来限制本轮推进数量；顶层默认 1，runner 内部默认 6，避免父节点只推进一个孩子就超时。",
    "run_ids": "适合父 runner 用 schedule_child_subagents 返回的 created_run_ids 指定本轮孩子，例如先跑 auth/catalog，再跑 cart/quality。",
    "runner_instruction": "只适合单个 run_id 的补充说明。多个不同子任务一起跑时不要写子任务专属内容；需要专属说明就拆成多次单 run_id dispatch。",
    "take_over_by": (
        "只在恢复动作需要新 leader 时填写。先用 subagent_board 或 due-check 找到可接管的已有 coordinator/leader run_id，"
        "再把它传给 dispatch_subagents；runner 内未填写时默认当前父 run 接管。不要凭空编 run_id。"
    ),
    "locked_files": "JSON 数组，填写相对或绝对文件路径；用于恢复/重派时向调度器声明这些文件暂时不能被其他分支并发修改。",
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
    "max_depth": "允许创建到的最大 depth；省略或 0 表示不限制",
    "max_children": "父节点最多能拥有多少直接 child，0 表示不限制",
}
_SCHEDULE_CHILD_PARAMETER_DETAILS = {
    "children": (
        "JSON 数组。每项可含 role、agent_name、goal、plan、allowed_tools、allowed_skills、"
    "acceptance_checks、extra_write_roots。参数必须在 tool JSON 顶层，不要包在 orchestration/filesystem 等二级字段里；"
        "长目标请分多次调用，每次 1-2 个 child。多层领导节点可用 role=coordinator/child_coordinator/grandchild_coordinator。"
        "优先从这些角色模板索引里选 role：\n{role_template_index}"
    ),
    "apply": "runner 内省略时默认 true；显式 false 只返回会创建什么，适合先检查。",
    "max_depth": "显式正数才限制层级；普通任务建议省略，让上级按任务需要决定。",
    "max_children": "显式正数才限制直接孩子数量；普通任务建议省略或 0。",
}
_SCHEDULE_CHILD_EXAMPLES = [
    (
        '{"tool":"schedule_child_subagents","apply":true,'
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
_SCHEDULE_CHILD_COORDINATOR_RULES = (
    "coordinator/lead 的权限应覆盖下级，便于检查、接管和救援；小任务、用户明确要求或下级卡住时也可以亲自完成。"
    "请用本工具创建 worker/writer/leaf_worker，并把父级给定的路径、文件名和验收条件原样传下去。"
    "如果父级要求 4 层链路，深度未到孙孙层前先创建下一层 coordinator。"
    "需要通知下级时用 subagent_message：少量不同消息用 direct+descendants，大量统一消息用 broadcast+descendants；"
    "平级讨论用 direct+peers，不能越权通知别的分支。"
)


# LLM: build_create_subagents_spec 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理spec所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def build_create_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="create_subagents",
        category="orchestration",
        description="创建一个或多个子代理任务记录。不同工作切片优先用 items/tasks；创建后必须 dispatch_subagents 才会真实执行。",
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
            '{"tool":"dispatch_subagents","apply":true,"execute_runners":true,"run_ids":["coordinator-id"],"runner_instruction":"只创建下一层 refs，不要执行 leaf worker"}',
            '{"tool":"dispatch_subagents","apply":true,"execute_runners":true,"run_ids":["child-auth","child-catalog"],"max_runners":2}',
            '{"tool":"dispatch_subagents","apply":true,"execute_runners":true,"run_ids":["child-auth"],"max_runners":1,"runner_instruction":"只补充 auth 子任务自己的执行重点"}',
            '{"tool":"dispatch_subagents","apply":true,"execute_runners":false,"take_over_by":"subagent-new-leader","max_runners":0}',
        ],
    )


# LLM: build_schedule_child_subagents_spec exposes hierarchy scheduling only inside runner context.
# 函数用途: 构建“当前节点创建下一层子节点”的模型工具规格，区别于顶层 create_subagents。
def build_schedule_child_subagents_spec() -> ToolSpec:
    return ToolSpec(
        name="schedule_child_subagents",
        category="orchestration",
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
