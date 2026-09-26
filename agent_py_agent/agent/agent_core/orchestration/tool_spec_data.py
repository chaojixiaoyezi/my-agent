# LLM: 参数说明只补充工具级规则；嵌套 role 复用顶层索引，产物线索不是权限，不指定已废弃的固定输出目录。
# 模块用途: 集中保存派工参数与示例；瘦身时保留 exact covers、模型选择、角色和工作区含义。

from __future__ import annotations

_CREATE_DEPENDENCY_ORDER_RULE = (
    "items 内所有 child 创建成功后都会立即并发运行，goal 里写‘先 A 后 B’不会形成执行顺序。"
    "若 B 要读取 A 尚未产生的修复、产物或结论，不能把 A/B 放进同一批：先只创建 A，"
    "等 A 的生命周期完成事件自动唤醒后，再单独创建 B。"
)
_CREATE_DISJOINT_WRITE_SCOPE_RULE = (
    "并行编码任务必须拆成互不重叠的文件或模块写入范围，并在每项 goal 里写清共同目标目录和该项独占范围；"
    "会修改同一文件、同一模块，或职责宽到会覆盖兄弟项的工作不能放进同一批。"
    "output_files 可辅助说明交付范围；省略时宿主不猜。它不是完整写集、权限或机器锁，"
    "同批 child 可以共享父级 task root；是否会修改同一文件仍由你按 goal 中的职责边界判断并分批。"
)
_CREATE_USE_CASES = [
    "任务能拆成 2+ 个可并行的独立子任务(各自跑、不互相等)——一个一个派、每个一句 goal，或一次用 items 列多个且每项含独立 goal，并行推进省主代理上下文",
    "要动多个文件/多个模块/多个目标,或需要不同角色(研究/实现/检查/汇总)分头干",
    "需要在隔离上下文里跑一段重活(大范围检索、独立验证、整块审计)——派出去、只收结论回来,不拿一堆中间过程塞满主代理上下文",
    "是否委派、派几个、怎样分工由你根据用户目标、可并行性、当前负载、可用工具和运行事实自主决定。持续任务可用 long_running 与 service_window_seconds 表达生命周期；不要为了某种任务类别固定子代理数量、角色、层级或执行顺序",
]
_CREATE_KEYWORDS = ["子代理", "派工", "拆分", "任务", "分别", "分头", "并行", "不同项目", "各项目", "subagent", "delegate", "spawn"]
_CREATE_PARAMETERS = {
    "goal": "只派一个子代理时必填，写这个子代理的完整目标；使用 items 批量派工时可选，只作整批说明，不替代每项自己的 goal",
    "persistent_goal": "可选持续目标正文；提供时为这个 child 创建唯一 Goal，正常回合结束后继续到完成或明确阻塞。省略是普通 prompt 派工，子代理的 Todo 可自行选择使用",
    "items": (
        "一次派多个可同时立即运行、彼此不等结果的任务时使用；每项必须自带独立 goal，顶层 goal 可省略。"
        "编码项还必须在 goal 中说明互不重叠的文件或模块写入职责；可共享同一父级 task root。只派一个时直接传 goal"
    ),
    "covers": '可选的 task_progress exact-id 映射（普通 items 或 coverage.targets，例如 ["req-03"]）；凡 child 原样承接一个已存在 open 项，都应复制该项 id，DONE 后系统按 id 打勾。只有额外工作或关系不能确定时才省略；省略后 child 用自己的 run_id 记进度，不关闭原 Todo',
    "description": "可选的 3-12 字职责短标题，只说明这个子代理大概负责什么，供 TUI/Web 单行展示",
    "role": "子代理角色模板 id，默认 worker",
    "agent_name": "可选展示名；只影响状态树和报告里的名字，不改变权限",
    "model": "可选；指定 /model 已新增的模型名称或配置编号。省略继承父级当前模型；只影响新建 child，不切换主代理",
    "effort": "可选智能程度（推理强度）：auto 服务商默认、off 关闭思考、low/medium/high/max。省略继承当前会话的档位；只影响新建 child",
    "tool_preset": "工具预设；通常省略。有效值：coding/read_only/none",
    "allowed_tools": "工具偏好提示；通常省略，基础读写工具会自动补齐",
    "allowed_skills": "可选；只把当前 owner Skill 快照中点名的技能授权给子代理",
    "plan": "子代理初始步骤",
    "input_refs": "交给子代理读取的文件、URL 或 artifact refs",
    "output_files": "可选目标产物，用于交付归属和协调提示；不是权限、完整写集或机器锁，提供时必须位于当前 workspace，同批可共享父级 task root",
    "artifact_refs": "已有交付物或参考产物引用",
    "replacement_for_run_ids": "新子代理要接管的旧 run_id",
    "related_finding_id": "可选；把本次委派关联到当前会话中已经持久化的一个 Audit finding。程序只校验关系，是否调查和怎样调查仍由你决定",
    "long_running": "true 声明这是故意长期运行的守望/常驻任务(持续监控数小时~数天)；系统放开其上下文压缩续跑深度上限(无进展仍会熔断)。只在任务本质是持续盯守/常驻服务时声明",
    "service_window_seconds": "可选,配合 long_running:持续型任务的最短值守窗口(秒)。窗口未走完时子代理不会因'已产出一次成果'被系统提前收口;若仍提前退出,父代理会收到'窗口未走完'的结构化事实以便重派或接管。派盯守/常驻任务时把用户要求的守候时长写进来",
    "audit_source_id": "仅当前命名 Audit 已发布结构化来源时使用；为这个叶子选择一个返回给你的精确 source_id。程序会把已验证的传输事实交给子代理，别把 URL 或 watch_id 重新写进任务步骤",
}
_CREATE_PARAMETER_DETAILS = {
    "goal": "与用户命令 /goal 无关；普通聊天任务也可派工。单派时写清这个子代理要交付什么并保留全部硬约束；items 批量模式可省略顶层 goal，每项自己的 goal 才是 child 的完整工作边界。",
    "items": (
        "批量并行任务；非空列表，每项包含独立 goal，顶层 goal 可省略。依赖顺序与写入分工见工具说明。"
        "资料放各 item.input_refs；covers/output_files 是可选结构化线索。"
    ),
    "description": "只写一句职责短标题，例如“实现超级玛丽核心玩法”；不要写过程、状态、路径或完整任务要求。省略时界面会截取 goal 开头。",
    "role": "优先用模板角色。可用角色模板索引：\n{role_template_index}",
    "agent_name": "展示名不是角色；需要职责差异时仍应使用 role 或 goal 表达。",
    "model": "仅当用户要求或任务需要独立模型时填写。只认当前 owner 已新增配置；同名多配置用回执中的 id，未知模型先请用户 /model 新增，不传地址或密钥。每个 item 可分别选择；子孙默认继续继承其直接父级。",
    "effort": "仅当用户要求或任务明显需要不同思考深度时填写（例如简单检索用 low、难题用 max）。宿主按模型实际支持的方式换算，模型不支持时按服务商默认运行；每个 item 可分别选择，子孙默认继承直接父级。",
    "tool_preset": "省略时自动；coding 给基础读写工具；read_only 只给读取/搜索/查看工具；none 只表示不覆盖自动策略。",
    "allowed_tools": "一般省略；不完整列表不会剥夺子代理基础读写能力。",
    "allowed_skills": "可选 Skill 名称或 stable_id 列表；创建时会解析为不可变快照引用，未知或禁用项整批拒绝。",
    "input_refs": "这是交给子代理的资料线索；单个子代理自己的输入放在对应 item.input_refs。",
    "output_files": (
        "可选；用户明确保存路径时用于保留交付身份与协调线索。它不是权限、完整写集或创建前置条件，"
        "路径仍服从当前 workspace 权限。阅读资料用 input_refs。"
        "同批多个 item 可以声明共同 task root；具体输出使用约定业务目录或工具返回的真实路径，不猜内部目录。"
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
    "covers": (
        "承接已有 Todo 时复制 items/coverage.targets 的 exact id，不要因为字段可选而漏掉；额外工作可省略。"
        "未知、已关闭或跨 item 重复 id 会整批拒绝。返工先对原 id 更新 in_progress、correction=true。"
        "绑定项随 child DONE 打勾；省略则由父级据实更新，不拿无关 id 顶替。"
    ),
}
_CREATE_ITEM_PARAMETER_DETAILS = {
    "role": "与顶层 role 使用同一模板索引；省略时为 worker。",
    "goal": (
        "每个 item 都必填；只写这一个子代理要完成和交付的具体工作，不要复制顶层整批 goal。"
        "该 goal 是 child 的完整工作边界，不要把兄弟 item 也塞进来；编码任务要同时写清共同目标目录"
        "以及与兄弟项互不重叠的文件或模块范围。"
    ),
    "description": "每个 item 可选；职责短标题只用 3-12 字概括这一个子代理负责什么，不要复制顶层整批 description。",
}
_CREATE_EXAMPLES = [
    '{"tool":"create_subagents","goal":"完成现有 Todo","items":[{"goal":"实现 req-03 用户模块","covers":["req-03"],"description":"实现用户模块","output_files":["src/auth/"]},{"goal":"补齐 req-04 测试","covers":["req-04"],"description":"补齐模块测试","role":"tester"}]}',
    '{"tool":"create_subagents","goal":"实现用户认证模块并写到 platform/auth/,要可运行","description":"实现用户认证","output_files":["platform/auth/"]}',
    '{"tool":"create_subagents","items":[{"goal":"完成清单外的独立资料A","description":"核对资料A","input_refs":["data/a.md"]},{"goal":"完成清单外的独立资料B","description":"核对资料B","input_refs":["data/b.md"]}]}',
]
