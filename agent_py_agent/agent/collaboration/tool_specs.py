# LLM: Collaboration tool specs describe generic case-room actions for models.
# 模块用途: 构建 open_case/request_collaboration/submit_evidence/case_status 的工具说明。

from __future__ import annotations

from ..tools import ToolSpec

_CASE_PARAMETERS = {
    "thread_id": "会话线程 ID；不知道时可传 task_id 让系统反查",
    "task_id": "任务 ID；thread_id 为空时用于反查会话",
    "title": "case 标题，描述这次需要协作处理的事",
    "summary": "结构化事实摘要，不要求业务专项格式",
    "priority": "优先级，urgent 会让 coordinator 更快升级；保留原始值，不做封闭枚举硬拒",
    "created_by": "创建 case 的代理 ID",
    "required_capabilities": "本 case 需要哪些能力，如 query/analyze/notify/act",
    "entities": "相关实体字典；开放世界，不写死字段，建议只放轻量索引和说明",
}
_REQUEST_PARAMETERS = {
    "case_id": "协作 case ID",
    "requester_agent_id": "发起协作请求的代理 ID",
    "target_agent_ids": "明确目标代理列表；省略时按 required_capabilities 匹配",
    "required_capabilities": "希望响应者具备的能力",
    "question": "希望对方补充什么事实或证据",
    "entities": "兼容旧字段：本次请求携带的轻量实体字典；新请求优先用 observed_facts/query_hints",
    "problem_statement": "这次协作要判断的问题，用普通语言描述，不绑定业务类型",
    "observed_facts": "已观察到的线索事实列表；每项可含 fact_id/label/kind/value/source_refs/queryable 等开放世界字段",
    "query_intent": "希望响应者完成的查询意图，例如收集佐证、排除可能性、扩大范围；开放世界对象",
    "query_hints": "LLM 给响应者的软查询提示列表；响应者可以完整查、拆分查、改写查或换来源",
    "routing_requirements": "路由要求对象，例如需要的来源范围、角色、时效或负载偏好；开放世界对象",
    "response_contract": "响应形状建议，例如要报告 matched/evidence_refs/queried_scopes/limitations；不是业务专项模板",
    "context_refs": "可选上下文引用列表，指向触发事件、工具结果、产物或外部证据",
    "deadline_at": "可选截止时间戳；到点后 coordinator 可关闭收集窗口并带部分结果通知上级；省略时使用配置默认 deadline",
    "deadline_seconds": "可选相对等待秒数；例如 30 表示从当前调用起 30 秒后仍未响应也继续推进；省略时使用配置默认 deadline，配置 0 才不自动补",
    "priority": "请求优先级；省略时继承 case priority",
}
_RAISE_EVENT_PARAMETERS = {
    "thread_id": "会话线程 ID；不知道时可传 task_id 或留空由当前 runner 反查",
    "task_id": "任务 ID；thread_id 为空时用于反查会话",
    "title": "协作事件标题，描述这次为什么需要多人参与",
    "summary": "已知事实摘要，不要求业务专项格式",
    "priority": "优先级；urgent 可让后台主代理更快处理，开放世界字符串",
    "created_by": "发现事件的代理 ID；省略时工具会尽量用当前 runner 身份",
    "requester_agent_id": "发起协作请求的代理 ID；省略时继承 created_by",
    "target_agent_ids": "明确目标代理列表；省略时按 required_capabilities 匹配",
    "required_capabilities": "希望响应者具备的能力，如 query/analyze/notify/act",
    "entities": "相关实体字典；开放世界，只放轻量索引和说明",
    "question": "希望其他代理补充什么事实或证据",
    "problem_statement": "这次协作要判断的问题，用普通语言描述，不绑定业务类型",
    "observed_facts": "已观察到的线索事实列表；每项可含 fact_id/label/kind/value/source_refs/queryable 等开放世界字段",
    "query_intent": "希望响应者完成的查询意图，例如收集佐证、排除可能性、扩大范围；开放世界对象",
    "query_hints": "LLM 给响应者的软查询提示列表；响应者可以完整查、拆分查、改写查或换来源",
    "routing_requirements": "路由要求对象，例如需要的来源范围、角色、时效或负载偏好；开放世界对象",
    "response_contract": "响应形状建议，例如报告 matched/evidence_refs/queried_scopes/limitations；不是业务专项模板",
    "context_refs": "可选上下文引用列表，指向触发事件、工具结果、产物或外部证据",
    "deadline_at": "可选截止时间戳；到点后 coordinator 可关闭收集窗口并带部分结果通知上级；省略时使用配置默认 deadline",
    "deadline_seconds": "可选相对等待秒数；例如 30 表示从当前调用起 30 秒后仍未响应也继续推进；省略时使用配置默认 deadline，配置 0 才不自动补",
    "metadata": "可选结构化补充信息；开放世界，不写死字段",
}
_LIST_REQUESTS_PARAMETERS = {
    "agent_id": "可选代理 ID；省略时工具会优先使用当前 runner 的 run_id",
    "agent_name": "可选代理名；用于匹配模型可见名字",
    "agent_role": "可选代理角色；用于匹配按角色点名的协作请求",
    "limit": "最多返回多少条；0 表示不限制，默认 10",
}
_EVIDENCE_PARAMETERS = {
    "case_id": "协作 case ID",
    "request_id": "对应的协作请求 ID，可为空",
    "source_agent_id": "提交证据的代理 ID",
    "matched": "是否找到匹配证据",
    "summary": "证据摘要",
    "evidence_refs": "证据引用列表，指向工具结果、产物、数据库快照或外部证据 ID",
    "queried_scopes": "响应者实际查询过的范围；开放世界字符串列表",
    "used_query_hints": "使用过的 query_hints/hint_id 列表；没有使用也可为空",
    "miss_reason": "未命中原因；matched=false 时建议说明，不作为封闭枚举",
    "response_facts": "响应者产生的开放世界事实列表，可记录命中、未命中、派生观察或限制",
    "followup_suggestions": "后续协作建议列表，例如建议其他能力/来源继续查；开放世界对象",
    "query_actions": "实际查询动作摘要列表，轻量记录查了什么，不塞大结果",
    "confidence": "可信度数值，0-1；只记录，不作为通用硬门",
    "limitations": "证据限制说明列表",
}
_UPDATE_STATUS_PARAMETERS = {
    "case_id": "协作 case ID",
    "status": "新状态；推荐 open/close 表达收集窗口状态，也可用项目自定义状态；关闭/解决语义必须带摘要",
    "actor_agent_id": "执行状态推进的代理 ID",
    "summary": "状态推进摘要；关闭/解决类状态必须提供摘要、决策或既有决策",
    "decision_type": "可选决策类型，如 triaged_by_main_agent/resolved_by_main_agent",
    "evidence_ids": "可选关联证据包 ID 列表",
}
_UPDATE_REQUEST_PARAMETERS = {
    "case_id": "协作 case ID",
    "request_id": "协作请求 ID",
    "status": "请求状态；开放世界字段，如 working/completed/blocked，也可用项目自定义状态",
    "actor_agent_id": "更新请求状态的代理 ID",
    "summary": "状态更新摘要，说明已完成、阻塞原因或下一步需要什么",
    "target_agent_ids": "可选；需要换路时写新的目标代理列表，系统会把它落到请求目标上",
    "metadata": "可选结构化补充信息；开放世界，不写死字段",
}
_REROUTE_REQUEST_PARAMETERS = {
    "case_id": "协作 case ID",
    "request_id": "需要换路的协作请求 ID",
    "actor_agent_id": "执行换路判断的代理 ID，通常是 main 或 coordinator",
    "target_agent_ids": "新的目标代理列表；必须是结构化列表，不能只写在自然语言摘要里",
    "status": "换路后的请求状态；默认 pending，表示等待新目标继续处理",
    "summary": "换路原因和下一步，例如原目标不可用、换到其他来源继续查",
    "metadata": "可选结构化补充信息；开放世界，不写死字段",
}


# LLM: build_open_case_spec exposes the generic collaboration-room creation contract to ToolRegistry.
# 函数用途: 构建 open_case 工具说明。
def build_open_case_spec() -> ToolSpec:
    return ToolSpec(
        name="open_case",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "打开一个通用协作 case，让多个代理围绕同一件事交换请求和证据。"
            "open_case 只创建协作房间；如果只是记录事件，可以停在这里。"
            "如果需要其他代理回应，再调用 request_collaboration，"
            "也可以直接改用 raise_collaboration_event 一步创建 case 和 request。"
        ),
        use_cases=["一个代理发现线索，需要多个代理协作研判", "需要把普通 observation 升级成多人协作事件"],
        avoid_when=["只是记录普通进展时，用 raise_observation 即可"],
        keywords=["协作", "case", "事件房间", "联合判断", "collaboration"],
        parameters=_CASE_PARAMETERS,
        examples=[
            '{"tool":"open_case","task_id":"task-1","title":"需要多源协作","required_capabilities":["query"]}',
            '{"tool":"request_collaboration","case_id":"case-1","requester_agent_id":"agent-a",'
            '"required_capabilities":["query"],"question":"请围绕这条线索补充证据"}',
        ],
    )


# LLM: build_request_collaboration_spec exposes agent-to-agent evidence requests without task templates.
# 函数用途: 构建 request_collaboration 工具说明。
def build_request_collaboration_spec() -> ToolSpec:
    return ToolSpec(
        name="request_collaboration",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="在 case 内向具备某些能力的代理发协作请求；可携带开放世界线索包和软查询提示。",
        use_cases=["需要其他代理按实体、范围或问题补充证据", "需要并行查询多个来源后汇总"],
        avoid_when=["已经有足够证据、只需要主代理收口时，不再继续发请求"],
        keywords=["协作请求", "补证据", "关联", "correlation", "request"],
        parameters=_REQUEST_PARAMETERS,
        examples=[
            '{"tool":"request_collaboration","case_id":"case-1","requester_agent_id":"agent-a",'
            '"required_capabilities":["query"],"question":"请围绕这些线索补充证据",'
            '"observed_facts":[{"fact_id":"fact-1","kind":"caller-defined","value":"..."}],'
            '"query_hints":[{"hint_id":"hint-1","purpose":"可完整查、拆分查或换来源"}]}'
        ],
    )


# LLM: build_raise_collaboration_event_spec gives models a one-step case+request action.
# 函数用途: 构建 raise_collaboration_event 工具说明，避免模型只把协作意图写进产物。
def build_raise_collaboration_event_spec() -> ToolSpec:
    return ToolSpec(
        name="raise_collaboration_event",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="发现需要多代理协作时，一次性打开 case 并创建协作请求；字段开放世界，不绑定具体业务。",
        use_cases=[
            "任意子代理发现线索，需要其他代理或数据源补证据",
            "不知道应该找谁，但知道需要 query/analyze/notify 等能力协助",
            "普通 observation 已升级为需要多人响应的协作事件",
        ],
        avoid_when=[
            "只是记录普通进展时，用 raise_observation",
            "已经有 case_id/request_id 时，优先复用 request_collaboration 或 submit_evidence",
        ],
        keywords=["协作事件", "raise collaboration", "补证据", "联合研判", "coordination"],
        parameters=_RAISE_EVENT_PARAMETERS,
        examples=[
            '{"tool":"raise_collaboration_event","title":"需要多源协作",'
            '"required_capabilities":["query"],"question":"请围绕这些线索补充证据",'
            '"observed_facts":[{"fact_id":"fact-1","kind":"caller-defined","value":"..."}],'
            '"query_hints":[{"hint_id":"hint-1","purpose":"可完整查、拆分查或换来源"}]}'
        ],
    )


# LLM: build_list_collaboration_requests_spec lets responders discover pending work without already knowing case_id.
# 函数用途: 构建 list_collaboration_requests 工具说明，暴露只读待响应请求发现能力。
def build_list_collaboration_requests_spec() -> ToolSpec:
    return ToolSpec(
        name="list_collaboration_requests",
        category="orchestration",
        effect="read_only",
        description="列出点名给当前代理或指定代理、且尚未被该代理响应的协作请求。",
        use_cases=[
            "子代理不知道 case_id/request_id，但需要发现是否有协作请求在等自己",
            "协调代理想确认某个响应者是否还有待处理请求",
        ],
        avoid_when=["已经拿到明确 case_id 且只想看单个 case 时，用 case_status"],
        keywords=["协作待办", "pending collaboration", "request discovery", "待响应请求"],
        parameters=_LIST_REQUESTS_PARAMETERS,
        examples=['{"tool":"list_collaboration_requests","agent_id":"source-b","limit":10}'],
    )


# LLM: build_submit_evidence_spec describes refs-first evidence submission for collaboration cases.
# 函数用途: 构建 submit_evidence 工具说明。
def build_submit_evidence_spec() -> ToolSpec:
    return ToolSpec(
        name="submit_evidence",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="向 case 提交证据包；只交 refs、查询范围、命中/未命中摘要和限制，不把大正文塞进协作账本。",
        use_cases=["响应协作请求", "把某个工具/文件/API/数据库查询结果作为证据交给 coordinator"],
        avoid_when=["只是临时思路、没有可引用证据时，不要伪造 evidence_refs"],
        keywords=["证据", "evidence", "refs", "协作响应"],
        parameters=_EVIDENCE_PARAMETERS,
        examples=[
            '{"tool":"submit_evidence","case_id":"case-1","request_id":"creq-1",'
            '"source_agent_id":"agent-b","matched":true,"evidence_refs":["artifact://e1"]}'
        ],
    )


# LLM: build_update_case_status_spec exposes audited lifecycle updates for collaboration cases.
# 函数用途: 构建 update_case_status 工具说明。
def build_update_case_status_spec() -> ToolSpec:
    return ToolSpec(
        name="update_case_status",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="推进协作 case 生命周期，并在需要时写入决策摘要。",
        use_cases=["主代理完成研判后标记 case 已处理", "coordinator 或父代理把 case 从 open 推进到 close/closed/resolved"],
        avoid_when=["只是查看 case 时用 case_status", "还没有任何结论时，不要关闭 case"],
        keywords=["case update", "case close", "resolved", "关闭协作", "状态推进"],
        parameters=_UPDATE_STATUS_PARAMETERS,
        examples=[
            '{"tool":"update_case_status","case_id":"case-1","status":"close",'
            '"summary":"证据已收口，结论已同步。"}'
        ],
    )


# LLM: build_update_collaboration_request_spec exposes request lifecycle updates to responders.
# 函数用途: 构建 update_collaboration_request 工具说明。
def build_update_collaboration_request_spec() -> ToolSpec:
    return ToolSpec(
        name="update_collaboration_request",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="更新协作请求的生命周期状态，让主代理能看见哪些请求完成、阻塞或仍在等待。",
        use_cases=["响应者开始处理、完成处理或遇到阻塞时更新请求状态", "父代理查看 case 前先让子代理记录当前请求进展"],
        avoid_when=["只是查看请求时用 case_status", "还没有实际进展或阻塞事实时不要虚构完成状态"],
        keywords=["request update", "request status", "协作请求状态", "阻塞", "完成"],
        parameters=_UPDATE_REQUEST_PARAMETERS,
        examples=[
            '{"tool":"update_collaboration_request","case_id":"case-1","request_id":"creq-1",'
            '"status":"completed","summary":"已完成查询并提交证据。"}'
        ],
    )


# LLM: build_reroute_collaboration_request_spec gives models a single explicit action for route changes.
# 函数用途: 构建 reroute_collaboration_request 工具说明，避免模型只把换路写进 metadata。
def build_reroute_collaboration_request_spec() -> ToolSpec:
    return ToolSpec(
        name="reroute_collaboration_request",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="把协作请求从失败或不合适的目标代理结构化换到新的目标代理。",
        use_cases=[
            "case_status.rework 提示请求阻塞且有替代目标",
            "某个来源、代理、参数路线失败后，需要改派其他代理继续同一个请求",
        ],
        avoid_when=["只是备注换路想法时不要调用；没有新目标时先 case_status 或 request_collaboration"],
        keywords=["reroute", "换路", "改派", "切换目标", "alternate source", "blocked request"],
        parameters=_REROUTE_REQUEST_PARAMETERS,
        examples=[
            '{"tool":"reroute_collaboration_request","case_id":"case-1","request_id":"creq-1",'
            '"actor_agent_id":"main","target_agent_ids":["agent-b"],'
            '"summary":"agent-a 不可用，改由 agent-b 继续。"}'
        ],
    )


# LLM: build_case_status_spec exposes read-only case inspection for main and child agents.
# 函数用途: 构建 case_status 工具说明。
def build_case_status_spec() -> ToolSpec:
    return ToolSpec(
        name="case_status",
        category="orchestration",
        effect="read_only",
        description="查看协作 case 的请求、证据、参与者和决策摘要。",
        use_cases=["主代理或 coordinator 想看 case 是否可收口", "用户询问某个协作事件进展"],
        avoid_when=["只是看代理树状态时，用 inspect_agent_tree"],
        keywords=["case status", "协作状态", "证据数量", "参与者"],
        parameters={"case_id": "协作 case ID"},
        examples=['{"tool":"case_status","case_id":"case-1"}'],
    )


__all__ = [
    "build_case_status_spec",
    "build_list_collaboration_requests_spec",
    "build_open_case_spec",
    "build_raise_collaboration_event_spec",
    "build_request_collaboration_spec",
    "build_reroute_collaboration_request_spec",
    "build_submit_evidence_spec",
    "build_update_collaboration_request_spec",
    "build_update_case_status_spec",
]
