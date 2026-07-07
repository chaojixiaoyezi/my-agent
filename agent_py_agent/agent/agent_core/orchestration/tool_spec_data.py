"""Compact model-facing metadata for orchestration tools."""

from __future__ import annotations

_CREATE_USE_CASES = [
    "任务能拆成 2+ 个可并行的独立子任务(各自跑、不互相等)——一个一个派、每个一句 goal,或一次用 items 列多个(每个含 goal),并行推进省主代理上下文",
    "要动多个文件/多个模块/多个目标,或需要不同角色(研究/实现/检查/汇总)分头干",
    "需要在隔离上下文里跑一段重活(大范围检索、独立验证、整块审计)——派出去、只收结论回来,不拿一堆中间过程塞满主代理上下文",
    "判断准则:任务形状'宽'(涉及多个文件/多个目标、可并行、或需要独立验证)就派子代理分头干;形状'窄'(已知单一改动点、一两步就能完)自己直接做、别拆;持续盯守/监控类(盯流/定时查接口/盯日志)不论宽窄都该派——每路源一个 long_running=true 子代理长驻盯(service_window_seconds=用户要求时长),你保持空闲随时响应用户,亲自盯会占死;子代理确认真事 record_finding 入账并 raise_event(urgent)叫回你",
]
_CREATE_KEYWORDS = [
    "子代理",
    "派工",
    "拆分",
    "任务",
    "分别",
    "分头",
    "并行",
    "不同项目",
    "各项目",
    "subagent",
    "delegate",
    "spawn",
]
_CREATE_PARAMETERS = {
    "goal": "这个子代理要干的具体任务(必填)。最稳:只派一个就只传 goal,别配空 items",
    "items": "只在一次派多个不同任务时才用;每项必须自带 goal。只派一个别用 items,传顶层 goal 即可",
    "count": "创建多少个同目标子代理；不同切片请用 items",
    "role": "子代理角色模板 id，默认 worker",
    "agent_name": "可选展示名；只影响状态树和报告里的名字，不改变权限",
    "tool_preset": "工具预设；通常省略。有效值：coding/read_only/none",
    "allowed_tools": "工具偏好提示；通常省略，基础读写工具会自动补齐",
    "acceptance_checks": "父代理后续判断完成的标准",
    "covers": '该子代理负责的 coverage 清单项 id 列表(如 ["req-03"]):派工时绑定,子代理完成后系统按 id 自动把对应清单项标 done,不用你回头逐项标',
    "plan": "子代理初始步骤",
    "input_refs": "交给子代理读取的文件、URL 或 artifact refs",
    "output_files": "用户明确指定的目标产物路径；没明确指定时不要从输入目录推断",
    "artifact_refs": "已有交付物或参考产物引用",
    "replacement_for_run_ids": "新子代理要接管的旧 run_id",
    "defer_start": "true 表示只建不跑；默认创建后启动",
    "long_running": "true 声明这是故意长期运行的守望/常驻任务(持续监控数小时~数天)；系统放开其上下文压缩续跑深度上限(无进展仍会熔断)。只在任务本质是持续盯守/常驻服务时声明",
    "service_window_seconds": "可选,配合 long_running:持续型任务的最短值守窗口(秒)。窗口未走完时子代理不会因'已产出一次成果'被系统提前收口;若仍提前退出,父代理会收到'窗口未走完'的结构化事实以便重派或接管。派盯守/常驻任务时把用户要求的守候时长写进来",
}
_CREATE_PARAMETER_DETAILS = {
    "goal": "写清子代理要交付什么，保留用户原始硬约束；用户声明的产物格式要求（输出路径、最少字数、文件路径:行号引用、必含章节）要原样写进相关子代理 goal，汇总时保留这些格式要素。",
    "count": "只用于派多个目标完全相同的子代理(配合 goal);不同切片各调一次或用 items。",
    "items": "仅一次派多个不同任务时用,每个元素必须含自己的 goal、别传空 items;资料线索放 item.input_refs;只有 defer_start=true 才只建不跑。",
    "role": "优先用模板角色。可用角色模板索引：\n{role_template_index}",
    "agent_name": "展示名不是角色；需要职责差异时仍应使用 role 或 goal 表达。",
    "tool_preset": "省略时自动；coding 给基础读写工具；read_only 只给读取/搜索/查看工具；none 只表示不覆盖自动策略。",
    "allowed_tools": "一般省略；不完整列表不会剥夺子代理基础读写能力。",
    "input_refs": "这是交给子代理的资料线索；单个子代理自己的输入放在对应 item.input_refs。",
    "output_files": (
        "只在用户明确保存路径时填写；没有明确路径时可省略。阅读/分析目录是 input_refs，不是 output_files。"
        "协作阶段的中间产物优先放当前任务 work/child_outputs 或工具返回的默认路径；"
        "output_dir 更适合最终交付，或用户明确要求放到某个普通输出目录时使用。"
    ),
    "replacement_for_run_ids": "用于结构化接管卡住或过时的旧 run。",
    "defer_start": "普通生产任务默认不要传；依赖前置产物的测试/验收/汇总项可传 true。",
    "covers": "每个 item 只绑它自己负责的清单项(id 来自 task_progress coverage);别把全部 id 复制给每个子代理,绑不存在的 id 不生效。",
}
_CREATE_EXAMPLES = [
    '{"tool":"create_subagents","goal":"实现用户认证模块并写到 platform/auth/,要可运行"}',
    '{"tool":"create_subagents","items":[{"goal":"实现注册登录模块","covers":["req-01"]},{"goal":"读资料B并写证据摘要","input_refs":["data/b.md"]}]}',
]

_INSPECT_TREE_PARAMETERS = {
    "root_id": "可选：只查看某棵根代理树",
    "run_id": "可选：查看某个 run 或它的子树",
    "scope": "root_tree/own_subtree/subtree/all；省略时自动选择",
}

_OBSERVATION_PARAMETERS = {
    "thread_id": "会话线程 ID；不知道时可用 task_id 反查",
    "task_id": "任务 ID",
    "event_type": "事件类型，保留开放字符串",
    "summary": "事实摘要",
    "urgency": "urgent 会唤醒主代理，其他值只记录",
    "severity": "严重程度，保留原始值",
    "source_agent_id": "上报者 run_id",
    "parent_agent_id": "上报者父级 run_id",
    "root_task_id": "根任务 ID",
    "evidence_refs": "证据引用列表",
    "requires_main_agent": "是否需要主代理处理",
    "requires_llm_report": "是否需要 LLM 写面向用户的报告",
    "dedupe_key": "可选幂等键",
}

_DISPATCH_PARAMETERS = {
    "dry_run": "true 预览，false 真实推进",
    "max_runners": "本轮最多推进几个子代理",
    "run_ids": "精确指定要推进的 run_id 列表",
    "recovery_mode": "可选。只有恢复策略明确给出时传，例如 rerun_from_continue_packet 或 rerun_from_checkpoint",
}
_DISPATCH_PARAMETER_DETAILS = {
    "dry_run": "模型只需要填写这一套预览开关，不要再制造第二套执行字段。",
    "max_runners": "不知道时省略；0 表示不执行。",
    "run_ids": "适合按 create/schedule 返回的 run_id 精确推进。",
    "recovery_mode": "这是机器字段，不从 runner_instruction 文本猜。普通推进不要填写；恢复建议 payload 给了才原样传入。",
}

_SCHEDULE_CHILD_USE_CASES = [
    "当前子代理需要把任务继续拆给下一层",
    "需要保持 main -> child -> grandchild 的层级边界",
]
_SCHEDULE_CHILD_KEYWORDS = ["下一层", "孙代理", "层级", "hierarchy", "child", "grandchild"]
_SCHEDULE_CHILD_PARAMETERS = {
    "children": "下一层子任务列表",
    "dry_run": "true 预览，false 真实创建；子代理内默认 false",
    "max_depth": "最大层级；0 或省略表示不限制",
    "max_children": "直接孩子数量上限；0 或省略表示不限制",
}
_SCHEDULE_CHILD_PARAMETER_DETAILS = {
    "children": "每项可含 goal/role/agent_name/input_refs/output_files/plan；优先从角色模板索引里选 role：\n{role_template_index}",
    "dry_run": "显式 true 只预览；省略时真实创建。",
    "max_depth": "显式正数才限制层级。",
    "max_children": "显式正数才限制直接孩子数量。",
}
_SCHEDULE_CHILD_EXAMPLES = ['{"tool":"schedule_child_subagents","dry_run":false,"children":[{"role":"worker","goal":"继续完成当前子任务的一部分"}]}']
_SCHEDULE_CHILD_COORDINATOR_RULES = "按当前层级创建自己的下级；平级补充提示用 send_guidance，推进已有下级用 dispatch_subagents。"
