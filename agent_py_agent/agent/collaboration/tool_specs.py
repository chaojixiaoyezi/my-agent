# LLM: Collaboration tool specs describe generic case-room actions for models.
# 模块用途: 构建合并后的协作工具说明，只暴露 raise/inspect/submit/update 四个模型入口。

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


# LLM: build_raise_collaboration_spec exposes one model action for opening cases and sending requests.
# 函数用途: 构建 raise_collaboration 工具说明，合并 open_case/request_collaboration/raise_collaboration_event。
def build_raise_collaboration_spec() -> ToolSpec:
    return ToolSpec(
        name="raise_collaboration",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "发起通用协作：没有 case_id 时打开新 case；有 question/target/capability 时同时发请求；"
            "已有 case_id 时在该 case 里继续发请求。"
        ),
        use_cases=[
            "任意代理发现线索，需要其他代理或数据源补证据",
            "不知道应该找谁，但知道需要 query/analyze/notify 等能力协助",
            "已有协作 case，需要继续向其他代理发协作请求",
        ],
        avoid_when=["只是记录普通进展时，用 raise_event", "只是查看协作进展时，用 inspect_collaboration"],
        keywords=["协作", "case", "补证据", "联合判断", "collaboration", "coordination"],
        parameters={**_CASE_PARAMETERS, **_REQUEST_PARAMETERS, **_RAISE_EVENT_PARAMETERS},
        examples=[
            '{"tool":"raise_collaboration","title":"需要多源协作","required_capabilities":["query"],'
            '"question":"请围绕这些线索补充证据",'
            '"observed_facts":[{"fact_id":"fact-1","kind":"caller-defined","value":"..."}],'
            '"query_hints":[{"hint_id":"hint-1","purpose":"可完整查、拆分查或换来源"}]}',
            '{"tool":"raise_collaboration","case_id":"case-1","target_agent_ids":["agent-b"],'
            '"question":"请查你负责的来源里是否有同一线索"}',
        ],
    )


# LLM: build_inspect_collaboration_spec gives one read-only action for case status and pending requests.
# 函数用途: 构建 inspect_collaboration 工具说明，合并 case_status/list_collaboration_requests。
def build_inspect_collaboration_spec() -> ToolSpec:
    return ToolSpec(
        name="inspect_collaboration",
        category="orchestration",
        effect="read_only",
        description="只读查看协作：传 case_id 看 case；不传 case_id 时列出当前或指定代理的待处理协作请求。",
        use_cases=[
            "子代理不知道 case_id/request_id，但需要发现是否有协作请求在等自己",
            "协调代理想确认某个响应者是否还有待处理请求",
            "主代理或 coordinator 想看 case 是否可收口",
        ],
        avoid_when=["只是看代理树状态时，用 inspect_agent_tree"],
        keywords=["协作状态", "协作待办", "pending collaboration", "case status", "request discovery"],
        parameters={"case_id": "可选协作 case ID；有则查看 case 状态", **_LIST_REQUESTS_PARAMETERS},
        examples=[
            '{"tool":"inspect_collaboration","case_id":"case-1"}',
            '{"tool":"inspect_collaboration","agent_id":"source-b","limit":10}',
        ],
    )


# LLM: build_submit_collaboration_result_spec records refs-first collaboration responses.
# 函数用途: 构建 submit_collaboration_result 工具说明，合并 evidence/result 语义。
def build_submit_collaboration_result_spec() -> ToolSpec:
    return ToolSpec(
        name="submit_collaboration_result",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="向 case 提交协作结果；只交 refs、查询范围、命中/未命中摘要和限制，不把大正文塞进协作账本。",
        use_cases=["响应协作请求", "把某个工具/文件/API/数据库查询结果作为证据交给 coordinator"],
        avoid_when=["只是临时思路、没有可引用证据时，不要伪造 evidence_refs"],
        keywords=["证据", "evidence", "refs", "协作响应", "result"],
        parameters=_EVIDENCE_PARAMETERS,
        examples=[
            '{"tool":"submit_collaboration_result","case_id":"case-1","request_id":"creq-1",'
            '"source_agent_id":"agent-b","matched":true,"evidence_refs":["artifact://e1"]}'
        ],
    )


# LLM: build_update_collaboration_spec gives one lifecycle action for cases and requests.
# 函数用途: 构建 update_collaboration 工具说明，合并 case/request/reroute 状态推进。
def build_update_collaboration_spec() -> ToolSpec:
    return ToolSpec(
        name="update_collaboration",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="更新协作 case 或 request；有 request_id 时更新请求，无 request_id 时更新 case；带 target_agent_ids 可改派请求。",
        use_cases=[
            "主代理完成研判后标记 case 已处理",
            "响应者开始处理、完成处理或遇到阻塞时更新请求状态",
            "某个目标不可用时，把同一请求改派给新目标",
        ],
        avoid_when=["只是查看协作时，用 inspect_collaboration", "还没有实际进展或结论时不要虚构完成状态"],
        keywords=["case update", "request update", "reroute", "换路", "关闭协作", "状态推进"],
        parameters={**_UPDATE_STATUS_PARAMETERS, **_UPDATE_REQUEST_PARAMETERS},
        examples=[
            '{"tool":"update_collaboration","case_id":"case-1","status":"close","summary":"证据已收口，结论已同步。"}',
            '{"tool":"update_collaboration","case_id":"case-1","request_id":"creq-1",'
            '"status":"completed","summary":"已完成查询并提交证据。"}',
            '{"tool":"update_collaboration","case_id":"case-1","request_id":"creq-1",'
            '"target_agent_ids":["agent-b"],"summary":"agent-a 不可用，改由 agent-b 继续。"}',
        ],
    )


__all__ = [
    "build_inspect_collaboration_spec",
    "build_raise_collaboration_spec",
    "build_submit_collaboration_result_spec",
    "build_update_collaboration_spec",
]
