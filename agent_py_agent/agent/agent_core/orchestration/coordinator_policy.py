"""主代理与递归协调者共用的软分工说明。"""

# LLM: This module is the canonical model-facing coordinator boundary shared by
# root tool discovery, child runner prompts, and create receipts. It is a soft
# execution contract, never a machine quality gate or natural-language parser.
# 模块用途: 统一主代理和多层子代理在派工后的协调职责，避免各入口出现互相矛盾的说明。

from __future__ import annotations


# LLM: 所有协调层共用此说明；它不授予路径、不判完成、不拦工具，改动需核对创建回执与 runner。
# 函数用途: 返回按用户目标分工、避免活跃任务重复的软指导，不把派工变成永久禁写。
def coordinator_execution_policy_lines() -> list[str]:
    return [
        "- " + coordinator_tool_boundary_text(),
        "- 是否派工由目标规模、可并行性和用户要求决定；把有明确边界、可并行推进的工作交给 child。"
        "紧急且下一步直接依赖的工作可在用户允许范围内自己处理，不要为了派工绕远或闲等。",
        "- 下级返回后按真实结果整合、修正并验证，不要重做已完成工作。诚实列出未完成项不能代替继续工作；"
        "若当前还能推进，就继续处理授权内的缺口。只有确实依赖未返回结果时才等待，避免轮询或反复叙述准备做什么。",
        "- 创建 child 时，把目标路径、文件名和质量要求原样传给下一层；不需要额外推进。",
        "- 如果缺口只属于未来 child/leaf 的执行能力，例如 leaf 才需要 controlled_exec、shell、网络或某个 skill，"
        "coordinator/lead 不要替后代提前提交 capability_request 后停止；先创建对应 child，"
        "由真正需要该能力的 runner 正式申请，父级再 route grant 并继续推进。",
        "- child 自然回复时交回实际成果和引用；内部结果文件由宿主登记，不要求下级手写运行时 JSON。",
        "- coordinator 可以继续创建 coordinator 作为下一层领导节点；"
        "需要多层协作时不要误以为只能创建 worker；不要为了层数或角色扩充没有实际作用的节点。",
        "- 只创建父级任务确实需要的 child；父级明确点名 tester、reviewer 等角色时才创建对应 run，"
        "不要为了凑角色或验收格式自动扩容。",
        '- 下一层仍使用统一的 create_subagents，例如 {"tool":"create_subagents","items":[{"goal":"子任务A"},{"goal":"子任务B"}]}；'
        "长目标可分多次创建，每个 child 的 goal 必须自包含。",
        "- 不要让 worker/writer 代写 coordinator 自己的协调证据；需要共享时引用 artifact_refs/evidence_refs。",
        "- 同一次 create_subagents 可以混建 coordinator、worker 或 tester；创建回执只说明是否已记录并交给运行时，"
        "进展、阻塞或完成由宿主事件送回直接父级。",
        "- 下级失败或阻塞时先读取真实 refs 和原因；不要自动创建整批 repair/QA 子代理。",
        "- 少数下属需要不同纠偏、路径修正或需求变更时，优先用 send_guidance 点名具体 run_id；"
        "平级讨论要走允许的定向通道，不能广播到兄弟分支的子孙。",
    ]


# LLM: 工具发现、runner 与唤醒共享同一分工纪律；只指导模型，不从自然语言计算权限、写集或等待状态。
#   委派默认值与交回核对只是软引导（参照 Codex spawn_agent 合同）：不改变能否派工，也不构成完成门。
# 函数用途: 为工具说明、runner 和后台唤醒提供同一份分工范围，避免三处互相矛盾；改文字会改变稳定提示前缀，需同步三处测试。
def coordinator_tool_boundary_text() -> str:
    return (
        "用户或项目说明没有要求委派时，优先自己完成；只把边界清楚、能与你的工作并行的部分交给下级。"
        "派工不会永久改变你的职责或用户授权。不要重复下级正在执行的工作；"
        "派工前先确定自己接下来做什么，再把可独立推进的部分交给下级；用户明确分给你的工作不要一并转交。"
        "分工包括你自己和各下级的文件或模块范围；已约定接口时，可以先做不依赖下级结果的部分。"
        "派工时写清要交回的具体产出（文件、数字或结论）以及怎样核对。"
        "等待期间继续做用户授权内不冲突的工作，收到结果后及时整合、修正和测试："
        "先用工具对照原始资料抽查下级交回的关键数字或改动，再汇总；不要直接转述下级的“通过”。"
        "确实依赖尚未返回的结果且没有其它有用工作时自然等待，不为保持忙碌编造文档或反复查状态。"
        "用户明确要求主代理不写功能代码时，保留该限制，把实现缺口交给具体下级，主代理仍做允许的整合与测试；"
        "没有这项限制时，不因派过子代理而放弃必要的本地工作。"
        "接手下级范围前确认其已结束或已停止冲突操作；慢或暂时没结果不等于失败，不盲目启动重复实现。"
    )


# LLM: The structured create receipt restates the post-delegation scope without
# granting or denying tools. Consumers may display or inject it, but must not use
# it as lifecycle, completion, or filesystem authority.
# 函数用途: 给创建回执附上 v2 分工说明，明确不冲突的本地工作仍取决于用户授权。
def coordinator_parent_execution_scope() -> dict[str, object]:
    return {
        "schema": "coordinator_execution_scope.v2",
        "mode": "collaborative_delegation",
        "allowed_work": [
            "coordinate_children",
            "read_declared_results",
            "integrate_existing_artifacts",
            "run_allowed_tests",
            "report_results",
            "user_authorized_nonoverlapping_work",
        ],
        "delegated_work": "avoid_duplicate_active_work_respect_user_scope",
        "gap_action": "authorized_local_work_or_scoped_child",
        "authority": "model_execution_guidance_only",
    }


__all__ = [
    "coordinator_execution_policy_lines",
    "coordinator_parent_execution_scope",
    "coordinator_tool_boundary_text",
]
