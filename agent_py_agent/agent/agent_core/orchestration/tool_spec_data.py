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
    "description": "可选的 3-12 字职责短标题，只说明这个子代理大概负责什么，供 TUI/Web 单行展示",
    "role": "子代理角色模板 id，默认 worker",
    "agent_name": "可选展示名；只影响状态树和报告里的名字，不改变权限",
    "tool_preset": "工具预设；通常省略。有效值：coding/read_only/none",
    "allowed_tools": "工具偏好提示；通常省略，基础读写工具会自动补齐",
    "allowed_skills": "可选；只把当前 owner Skill 快照中点名的技能授权给子代理",
    "covers": '该子代理负责的 task_progress 清单项 id 列表（普通 items 或 coverage.targets，例如 ["req-03"]）：已有清单时每个 child 必填且不能跨 child 重复；完成后系统按 exact id 自动把对应项标 done',
    "plan": "子代理初始步骤",
    "input_refs": "交给子代理读取的文件、URL 或 artifact refs",
    "output_files": "目标产物或写入集合，用于交付归属、权限预检和冲突锁；已有计划时，直接写产品代码的每个 child 必须逐项声明当前 workspace 内互不重叠的路径",
    "artifact_refs": "已有交付物或参考产物引用",
    "replacement_for_run_ids": "新子代理要接管的旧 run_id",
    "related_finding_id": "可选；把本次委派关联到当前会话中已经持久化的一个 Audit finding。程序只校验关系，是否调查和怎样调查仍由你决定",
    "long_running": "true 声明这是故意长期运行的守望/常驻任务(持续监控数小时~数天)；系统放开其上下文压缩续跑深度上限(无进展仍会熔断)。只在任务本质是持续盯守/常驻服务时声明",
    "service_window_seconds": "可选,配合 long_running:持续型任务的最短值守窗口(秒)。窗口未走完时子代理不会因'已产出一次成果'被系统提前收口;若仍提前退出,父代理会收到'窗口未走完'的结构化事实以便重派或接管。派盯守/常驻任务时把用户要求的守候时长写进来",
    "audit_source_id": "仅当前命名 Audit 已发布结构化来源时使用；为这个叶子选择一个返回给你的精确 source_id。程序会把已验证的传输事实交给子代理，别把 URL 或 watch_id 重新写进任务步骤",
}
_CREATE_PARAMETER_DETAILS = {
    "goal": "工具内部的整批派工说明，与用户命令 /goal 无关；普通聊天任务也可派工。写清子代理要交付什么，保留用户原始硬约束；用户声明的产物格式要求（输出路径、最少字数、文件路径:行号引用、必含章节）要原样写进相关子代理 goal，汇总时保留这些格式要素。",
    "items": "仅一次派多个不同任务时用；顶层 goal 写整批目的，每个元素必须含自己的独立 goal、别传空 items。资料线索放 item.input_refs；已有 Todo 时每项必须带独占 covers，直接写产品代码的 item 还必须声明当前 workspace 内互不重叠的 output_files。创建成功后会立即运行。",
    "description": "只写一句职责短标题，例如“实现超级玛丽核心玩法”；不要写过程、状态、路径或完整任务要求。省略时界面会截取 goal 开头。",
    "role": "优先用模板角色。可用角色模板索引：\n{role_template_index}",
    "agent_name": "展示名不是角色；需要职责差异时仍应使用 role 或 goal 表达。",
    "tool_preset": "省略时自动；coding 给基础读写工具；read_only 只给读取/搜索/查看工具；none 只表示不覆盖自动策略。",
    "allowed_tools": "一般省略；不完整列表不会剥夺子代理基础读写能力。",
    "allowed_skills": "可选 Skill 名称或 stable_id 列表；创建时会解析为不可变快照引用，未知或禁用项整批拒绝。",
    "input_refs": "这是交给子代理的资料线索；单个子代理自己的输入放在对应 item.input_refs。",
    "output_files": (
        "用户明确保存路径时必须填写；已有 task_progress 计划时，直接写产品代码的每个 item 即使没有"
        "用户点名文件，也要声明当前 workspace 内互不重叠的写入集合。它记录交付身份与冲突范围，"
        "普通 child 的父级工作区写权仍由宿主继承。没有计划的轻量派工可省略。阅读/分析目录是 input_refs。"
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
    "covers": "每个 item 只绑它自己负责且仍 open 的清单项（id 来自 task_progress 的 items 或 coverage.targets）；已有 Todo 时这是创建硬合同。未知、已关闭或跨 item 重复的 id 会使整批原子拒绝，不能复制全部 id 或另建同义清单。",
}
_CREATE_ITEM_PARAMETER_DETAILS = {
    "goal": "每个 item 都必填；只写这一个子代理要完成和交付的具体工作，不要复制顶层整批 goal。",
    "description": "每个 item 可选；职责短标题只用 3-12 字概括这一个子代理负责什么，不要复制顶层整批 description。",
}
_CREATE_EXAMPLES = [
    '{"tool":"create_subagents","goal":"实现用户认证模块并写到 platform/auth/,要可运行","description":"实现用户认证","output_files":["platform/auth/"]}',
    '{"tool":"create_subagents","goal":"并行完成认证实现与资料核对","items":[{"goal":"实现注册登录模块","description":"实现注册登录","output_files":["platform/auth/"]},{"goal":"读资料B并写证据摘要","description":"核对资料B","input_refs":["data/b.md"],"output_files":["reports/b.md"]}]}',
    '{"tool":"create_subagents","goal":"完成现有 Todo","items":[{"goal":"实现 req-03 用户模块","description":"实现用户模块","covers":["req-03"],"output_files":["src/auth/"]},{"goal":"补齐 req-04 测试","description":"补齐模块测试","role":"tester","covers":["req-04"]}]}',
]
