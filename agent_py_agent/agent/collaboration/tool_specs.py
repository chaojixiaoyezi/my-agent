
from __future__ import annotations

from ..tooling.models import ToolSpec

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
    "entities": "本次请求携带的轻量实体字典；能用 observed_facts/query_hints 表达时优先用后者",
    "problem_statement": "这次协作要判断的问题，用普通语言描述，不绑定业务类型",
    "observed_facts": "已观察到的线索事实列表；每项可含 fact_id/label/kind/value/source_refs/queryable 等开放世界字段",
    "query_intent": "希望响应者完成的查询意图，例如收集佐证、排除可能性、扩大范围；开放世界对象",
    "query_hints": "LLM 给响应者的软查询提示列表；响应者可以完整查、拆分查、改写查或换来源",
    "routing_requirements": "路由要求对象，例如需要的来源范围、角色、时效或负载偏好；开放世界对象",
    "response_contract": "可选响应建议；不知道怎么写就省略，响应者仍可按任务自然回证据",
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
    "response_contract": "可选响应建议；不知道怎么写就省略，响应者仍可按任务自然回证据",
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
    "status": "新状态；推荐 open/closed 表达收集窗口状态，也可用项目自定义状态；closed 必须带摘要",
    "actor_agent_id": "执行状态推进的代理 ID",
    "summary": "状态推进摘要；closed 必须提供摘要、决策或既有决策",
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
        use_cases=[],
        avoid_when=[],
        keywords=["协作", "case", "补证据", "联合判断", "collaboration", "coordination"],
        parameters={**_CASE_PARAMETERS, **_REQUEST_PARAMETERS, **_RAISE_EVENT_PARAMETERS},
        examples=[],
    )


def build_inspect_collaboration_spec() -> ToolSpec:
    return ToolSpec(
        name="inspect_collaboration",
        category="orchestration",
        effect="read_only",
        description="只读查看协作：传 case_id 看 case；不传 case_id 时列出当前或指定代理的待处理协作请求。",
        use_cases=[],
        avoid_when=[],
        keywords=["协作状态", "协作待办", "pending collaboration", "case status", "request discovery"],
        parameters={"case_id": "可选协作 case ID；有则查看 case 状态", **_LIST_REQUESTS_PARAMETERS},
        examples=[],
    )


def build_submit_collaboration_result_spec() -> ToolSpec:
    return ToolSpec(
        name="submit_collaboration_result",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="向 case 提交协作结果；只交 refs、查询范围、命中/未命中摘要和限制，不把大正文塞进协作账本。",
        use_cases=[],
        avoid_when=[],
        keywords=["证据", "evidence", "refs", "协作响应", "result"],
        parameters=_EVIDENCE_PARAMETERS,
        examples=[],
    )


def build_update_collaboration_spec() -> ToolSpec:
    return ToolSpec(
        name="update_collaboration",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="更新协作 case 或 request；有 request_id 时更新请求，无 request_id 时更新 case；带 target_agent_ids 可改派请求。",
        use_cases=[],
        avoid_when=[],
        keywords=["case update", "request update", "reroute", "换路", "关闭协作", "状态推进"],
        parameters={**_UPDATE_STATUS_PARAMETERS, **_UPDATE_REQUEST_PARAMETERS},
        examples=[],
    )


__all__ = [
    "build_inspect_collaboration_spec",
    "build_raise_collaboration_spec",
    "build_submit_collaboration_result_spec",
    "build_update_collaboration_spec",
]
