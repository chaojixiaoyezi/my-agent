"""Compact model-facing metadata for orchestration tools."""

from __future__ import annotations

_CREATE_USE_CASES = [
    "需要把任务拆给多个子代理并行处理",
    "需要不同角色分别研究、实现、检查或汇总",
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
    "goal": "单任务目标；批量模式优先用 items",
    "items": "子任务列表，每项可含 goal/role/agent_name/input_refs/output_files/defer_start",
    "count": "创建多少个同目标子代理；不同切片请用 items",
    "role": "子代理角色模板 id，默认 worker",
    "agent_name": "可选展示名；只影响状态树和报告里的名字，不改变权限",
    "tool_preset": "工具预设；通常省略。有效值：coding/read_only/none",
    "allowed_tools": "工具偏好提示；通常省略，基础读写工具会自动补齐",
    "acceptance_checks": "父代理后续判断完成的标准",
    "plan": "子代理初始步骤",
    "input_refs": "交给子代理读取的文件、URL 或 artifact refs",
    "output_files": "用户明确指定的目标产物路径；没明确指定时不要从输入目录推断",
    "artifact_refs": "已有交付物或参考产物引用",
    "replacement_for_run_ids": "新子代理要接管的旧 run_id",
    "defer_start": "true 表示只建不跑；默认创建后启动",
}
_CREATE_PARAMETER_DETAILS = {
    "goal": "写清子代理要交付什么，保留用户原始硬约束；用户声明的产物格式要求（输出路径、最少字数、文件路径:行号引用、必含章节）要原样写进相关子代理 goal，汇总时保留这些格式要素。",
    "count": "不同工作切片不要用 count 复制同一个 goal；优先传 items。",
    "items": "推荐批量入口；资料线索放 item.input_refs；默认创建后立刻启动，只有 defer_start=true 才只建任务记录。",
    "role": "优先用模板角色。可用角色模板索引：\n{role_template_index}",
    "agent_name": "展示名不是角色；需要职责差异时仍应使用 role 或 goal 表达。",
    "tool_preset": "省略时自动；coding 给基础读写工具；read_only 只给读取/搜索/查看工具；none 只表示不覆盖自动策略。",
    "allowed_tools": "一般省略；不完整列表不会剥夺子代理基础读写能力。",
    "input_refs": "这是交给子代理的资料线索；单个子代理自己的输入放在对应 item.input_refs。",
    "output_files": (
        "只在用户明确保存路径时填写；没有明确路径时可省略。"
        "阅读/分析目录是 input_refs，不是 output_files。"
        "协作阶段的中间产物优先放当前任务 work/child_outputs 或工具返回的默认路径；"
        "output_dir 更适合最终交付，或用户明确要求放到某个普通输出目录时使用。"
    ),
    "replacement_for_run_ids": "用于结构化接管卡住或过时的旧 run。",
    "defer_start": "普通生产任务默认不要传；依赖前置产物的测试/验收/汇总项可传 true。",
}
_CREATE_EXAMPLES = [
    '{"tool":"create_subagents","items":[{"goal":"完成用户指定页面","role":"worker","output_files":["output/page/index.html"]}]}',
    '{"tool":"create_subagents","items":[{"goal":"读资料A并写证据摘要","input_refs":["data/a.md"]},{"goal":"读资料B并写证据摘要","input_refs":["data/b.md"]}]}',
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
_SCHEDULE_CHILD_EXAMPLES = [
    '{"tool":"schedule_child_subagents","dry_run":false,"children":[{"role":"worker","goal":"继续完成当前子任务的一部分"}]}',
]
_SCHEDULE_CHILD_COORDINATOR_RULES = "按当前层级创建自己的下级；平级补充提示用 send_guidance，推进已有下级用 dispatch_subagents。"
