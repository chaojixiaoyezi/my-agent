# LLM: 能力决策材料只读原授权快照；短名单不授予执行权，不读取Skill正文，不按自然语言推断必要能力。
# 模块用途: 为一次能力推荐准备可核对的候选和有界选择槽，并将答案映射回原精确引用。
from __future__ import annotations

import hashlib

from ..backends.decision_protocol import DecisionInputError, decision_json
from ..tooling.models import TOOL_DISCOVERY_ENTRY_NAMES

_NON_SELECTIONS = {
    "not_needed": "此槽无需额外能力，可留空；其余槽独立选择",
    "no_match": "候选不匹配，保持本轮原展示",
    "abstain": "无法可靠判断，保持本轮原展示",
    "need_goal": {"meaning": "缺少任务目标", "required_refs": ["user_request"]},
    "need_contract": {"meaning": "缺少工具参数或执行条件", "required_refs": ["tool_schema"]},
    "need_procedure": {"meaning": "缺少技能具体步骤", "required_refs": ["skill_body"]},
    "need_environment": {"meaning": "缺少环境事实", "required_refs": ["runtime_environment"]},
}


# LLM: 摘要共用原有界JSON，错误时放弃增强，不截断材料后冒充完整候选。
# 函数用途: 绑定输入、范围、配置和版本，返回不带原文的比较值。
def candidate_digest(value: object) -> str:
    return hashlib.sha256(decision_json(value)).hexdigest()


# LLM: 工具取真实ToolRuntimeSnapshot，Skill取原scoped snapshot；不查另一注册表或从外部字符串扩权。
# 函数用途: 提取指定可选类别的工具及可发现Skill名卡，保留所有候选、不预读正文。
def capability_candidates(snapshot, skills, *, categories: list[str], skills_discoverable: bool,
                          allowed_tools: list[str] | None = None) -> list[dict]:
    rows = [{"kind": "tool", "ref": runtime.model_spec.name, "name": runtime.model_spec.name,
             "description": runtime.model_spec.description, "category": runtime.model_spec.category,
             "version": runtime.model_spec.schema_hash}
            for runtime in snapshot.runtimes
            if runtime.exposure.model_visible and runtime.model_spec.category in categories
            and (allowed_tools is None or runtime.model_spec.name in allowed_tools)
            and runtime.model_spec.name not in TOOL_DISCOVERY_ENTRY_NAMES]
    if skills_discoverable:
        rows.extend({"kind": "skill", "ref": entry.stable_id, "name": entry.name,
                     "description": entry.description, "when_to_use": entry.when_to_use,
                     "version": entry.content_sha256, "tools_required": list(entry.tools_required)}
                    for entry in skills.enabled_entries())
    return rows


# LLM: 每组最多240候选，每组最多8个选择槽；全部候选都进入state，槽只限制初始展示量，完整目录仍可搜索。
# 函数用途: 让超过64项的能力目录仍可一次推荐；超出原协议资源上限时整体放弃而非隐式裁掉尾部。
def selection_questions(rows: list[dict]) -> dict:
    questions = {}
    for start in range(0, len(rows), 240):
        group = rows[start:start + 240]
        choices = {f"candidate_{start + index}": None for index in range(len(group))}
        for slot in range(min(8, len(group))):
            questions[f"group_{start // 240}_slot_{slot}"] = {
                "type": "choice", "instructions": {
                    "question": "从本组推荐适合当前任务的不同能力。只影响初始名卡/可选schema展示，不是授权或已加载正文。",
                    "slot": slot, "group": start // 240,
                    "references": "candidate_N 精确引用 state.candidates[N]，候选说明只在state列出一次。",
                    "uncertainty": "无需更多能力选not_needed；缺数据选对应need项，宿主保持原输入，不自动补读。",
                }, "criteria": {**choices, **_NON_SELECTIONS},
            }
    if not 1 <= len(questions) <= 64:
        raise DecisionInputError("能力推荐题数超出单次资源上限。")
    return questions


# LLM: 按原题候选读取答案，不解析模型文字；任何无效/不确定题保持全集，明确无需能力才允许空短名单。
# 函数用途: 解码可独立选择的槽，重复推荐去重，保留缺数据的结构化原因。
def selected_capabilities(response, questions: dict, rows: list[dict]) -> tuple[list[dict], str]:
    answers = {answer.question_id: answer for answer in response.answers}
    indices = set()
    for key, question in questions.items():
        answer = answers.get(key)
        if answer is None or answer.error_code or answer.kind != "choice" or answer.value not in question["criteria"]:
            return [], "invalid_answer"
        if answer.value == "not_needed":
            continue
        if answer.value in _NON_SELECTIONS:
            return [], answer.value
        indices.add(int(answer.value.removeprefix("candidate_")))
    return [rows[index] for index in sorted(indices)], ""


# LLM: 必要引用只来自宿主合同和已授权Skill快照；未知ref不会获得名卡、schema或执行权。
# 函数用途: 保留明确任务义务以及已授予子代理的技能，在缩短展示时不丢必需工具。
def required_capabilities(params, contract, skills) -> tuple[set[str], tuple[str, ...]]:
    tools = {name for action in tuple(getattr(contract, "required_actions", ()) or ())
             if getattr(action, "status", "") == "open" for name in tuple(getattr(action, "allowed_tools", ()) or ())}
    attrs = getattr(params, "task_attributes", None) or {}
    refs = attrs.get("skill_snapshot_refs", [])
    required = {row.get("stable_id") for row in refs if isinstance(row, dict)} if isinstance(refs, list) else set()
    entries = skills.enabled_entries() if skills is not None else ()
    if getattr(params, "context_scope", "default") == "task_local":
        required.update(entry.stable_id for entry in entries)
    retained = tuple(entry.stable_id for entry in entries if entry.stable_id in required)
    tools.update(name for entry in entries if entry.stable_id in retained for name in entry.tools_required)
    return tools, retained
