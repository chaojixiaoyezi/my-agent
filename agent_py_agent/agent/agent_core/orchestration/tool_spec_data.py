"""Compact model-facing metadata for orchestration tools."""

from __future__ import annotations

_CREATE_USE_CASES = [
    "任务能拆成 2+ 个可并行的独立子任务(各自跑、不互相等)——一个一个派、每个一句 goal,或一次给总 goal 并用 items 列多个(每项含独立 goal),并行推进省主代理上下文",
    "要动多个文件/多个模块/多个目标,或需要不同角色(研究/实现/检查/汇总)分头干",
    "需要在隔离上下文里跑一段重活(大范围检索、独立验证、整块审计)——派出去、只收结论回来,不拿一堆中间过程塞满主代理上下文",
    "是否委派、派几个、怎样分工由你根据用户目标、可并行性、当前负载、可用工具和运行事实自主决定。持续任务可用 long_running 与 service_window_seconds 表达生命周期；不要为了某种任务类别固定子代理数量、角色、层级或执行顺序",
]
_CREATE_KEYWORDS = ["子代理", "派工", "拆分", "任务", "分别", "分头", "并行", "不同项目", "各项目", "subagent", "delegate", "spawn"]
_CREATE_PARAMETERS = {
    "goal": "本次派工要完成的具体目标(始终必填)。只派一个时它就是子代理目标；使用 items 时它是整批派工的总目标",
    "items": "只在一次派多个不同任务时才用;顶层 goal 仍必填，且每项必须自带独立 goal。只派一个别用 items",
    "role": "子代理角色模板 id，默认 worker",
    "agent_name": "可选展示名；只影响状态树和报告里的名字，不改变权限",
    "tool_preset": "工具预设；通常省略。有效值：coding/read_only/none",
    "allowed_tools": "工具偏好提示；通常省略，基础读写工具会自动补齐",
    "allowed_skills": "可选；只把当前 owner Skill 快照中点名的技能授权给子代理",
    "covers": '该子代理负责的 coverage 清单项 id 列表(如 ["req-03"]):派工时绑定,子代理完成后系统按 id 自动把对应清单项标 done,不用你回头逐项标',
    "plan": "子代理初始步骤",
    "input_refs": "交给子代理读取的文件、URL 或 artifact refs",
    "output_files": "用户明确指定的目标产物路径，用于交付归属和冲突锁；没明确指定时不要从输入目录推断",
    "artifact_refs": "已有交付物或参考产物引用",
    "replacement_for_run_ids": "新子代理要接管的旧 run_id",
    "related_finding_id": "可选；把本次委派关联到当前会话中已经持久化的一个 Audit finding。程序只校验关系，是否调查和怎样调查仍由你决定",
    "long_running": "true 声明这是故意长期运行的守望/常驻任务(持续监控数小时~数天)；系统放开其上下文压缩续跑深度上限(无进展仍会熔断)。只在任务本质是持续盯守/常驻服务时声明",
    "service_window_seconds": "可选,配合 long_running:持续型任务的最短值守窗口(秒)。窗口未走完时子代理不会因'已产出一次成果'被系统提前收口;若仍提前退出,父代理会收到'窗口未走完'的结构化事实以便重派或接管。派盯守/常驻任务时把用户要求的守候时长写进来",
    "audit_source_id": "仅当前命名 Audit 已发布结构化来源时使用；为这个叶子选择一个返回给你的精确 source_id。程序会把已验证的传输事实交给子代理，别把 URL 或 watch_id 重新写进任务步骤",
}
_CREATE_PARAMETER_DETAILS = {
    "goal": "工具内部的整批派工说明，与用户命令 /goal 无关；普通聊天任务也可派工。写清子代理要交付什么，保留用户原始硬约束；用户声明的产物格式要求（输出路径、最少字数、文件路径:行号引用、必含章节）要原样写进相关子代理 goal，汇总时保留这些格式要素。",
    "items": "仅一次派多个不同任务时用；顶层 goal 写整批目的，每个元素必须含自己的独立 goal、别传空 items。资料线索放 item.input_refs；用户指定了保存目录或文件时，每个负责写入的 item 都必须把实际目标写进 item.output_files。创建成功后会立即运行。",
    "role": "优先用模板角色。可用角色模板索引：\n{role_template_index}",
    "agent_name": "展示名不是角色；需要职责差异时仍应使用 role 或 goal 表达。",
    "tool_preset": "省略时自动；coding 给基础读写工具；read_only 只给读取/搜索/查看工具；none 只表示不覆盖自动策略。",
    "allowed_tools": "一般省略；不完整列表不会剥夺子代理基础读写能力。",
    "allowed_skills": "可选 Skill 名称或 stable_id 列表；创建时会解析为不可变快照引用，未知或禁用项整批拒绝。",
    "input_refs": "这是交给子代理的资料线索；单个子代理自己的输入放在对应 item.input_refs。",
    "output_files": (
        "用户明确保存路径时必须填写，且批量派工要在每个写入 item 里分别填写；"
        "它记录交付身份与冲突范围，普通 child 的父级工作区写权由宿主继承。"
        "没有明确路径时可省略。阅读/分析目录是 input_refs，不是 output_files。"
        "协作阶段的中间产物优先放当前任务 work/child_outputs 或工具返回的默认路径；"
        "output_dir 更适合最终交付，或用户明确要求放到某个普通输出目录时使用。"
    ),
    "replacement_for_run_ids": "用于结构化接管卡住或过时的旧 run。",
    "related_finding_id": (
        "仅当你决定为一个已收到的 Audit finding 创建调查、复核或补证子代理时填写。"
        "必须原样使用事件中的 finding_id；系统会验证 owner、会话、活跃 Audit 和真实 run，"
        "并在工具结果的 finding_investigations 中返回 investigation_run_id 与真实状态。"
    ),
    "audit_source_id": (
        "只在当前 Audit 的结构化 source_bindings 列表中选择一个原样 source_id。每个实际来源"
        "创建一个叶子 item；子代理只需调用 watch_stream(action=open)，URL、请求体、游标位置"
        "和文档引用由运行时从该绑定补入，禁止猜 watch_id。"
    ),
    "covers": "每个 item 只绑它自己负责的清单项(id 来自 task_progress coverage);别把全部 id 复制给每个子代理,绑不存在的 id 不生效。",
}
_CREATE_EXAMPLES = [
    '{"tool":"create_subagents","goal":"实现用户认证模块并写到 platform/auth/,要可运行","output_files":["platform/auth/"]}',
    '{"tool":"create_subagents","goal":"并行完成认证实现与资料核对","items":[{"goal":"实现注册登录模块","output_files":["platform/auth/"]},{"goal":"读资料B并写证据摘要","input_refs":["data/b.md"],"output_files":["reports/b.md"]}]}',
]

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
