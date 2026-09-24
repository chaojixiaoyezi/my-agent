# LLM: 能力决策材料只读原授权快照；短名单不授予执行权，不读取Skill正文，不按自然语言推断必要能力。
# 模块用途: 为一次能力推荐准备可核对的候选和独立适用性题目，并将答案映射回原精确引用。
from __future__ import annotations

import hashlib

from ..backends.decision_protocol import DecisionInputError, decision_json
from ..tooling.models import TOOL_DISCOVERY_ENTRY_NAMES

_NON_SELECTIONS = {
    "not_needed": "此候选与用户任务无关，无需初始展示",
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


# LLM: 工具候选行只取模型规格；provider_id 是宿主写入的结构化归属（插件代理为 "plugin:<ID>"），为空时不写该键，
# 内置工具行形状与版本摘要保持不变。
# 函数用途: 生成一条工具候选行。
def _tool_row(runtime) -> dict:
    spec = runtime.model_spec
    row = {"kind": "tool", "ref": spec.name, "name": spec.name, "description": spec.description,
           "category": spec.category, "version": spec.schema_hash}
    provider = str(getattr(spec.hints, "provider_id", "") or "").strip()
    if provider:
        row["provider_id"] = provider
    return row


# LLM: 工具取真实ToolRuntimeSnapshot，Skill取原scoped snapshot；不查另一注册表或从外部字符串扩权。
# 函数用途: 提取指定可选类别的工具及可发现Skill名卡，保留所有候选、不预读正文。
def capability_candidates(snapshot, skills, *, categories: list[str], skills_discoverable: bool,
                          allowed_tools: list[str] | None = None) -> list[dict]:
    rows = [_tool_row(runtime)
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


# LLM: 只按结构化 provider_id 合并同一插件的工具，一个插件一题；不读描述、关键词或用户原话判断归属。
# 内置工具与 Skill 原样逐项；合并行保留成员工具的精确引用，采用时展开回原工具；位置取第一个成员的位置。
# 函数用途: 把同一插件的工具候选并成一行，减少题数，并避免同一插件的工具被部分隐藏。
def group_provider_candidates(rows: list[dict]) -> list[dict]:
    grouped: list[dict] = []
    providers: dict[str, dict] = {}
    for row in rows:
        provider = row.get("provider_id") if row.get("kind") == "tool" else None
        if not provider:
            grouped.append(row)
            continue
        entry = providers.get(provider)
        if entry is None:
            entry = providers[provider] = {"kind": "provider", "ref": provider, "name": provider,
                                           "tools": [], "tool_refs": []}
            grouped.append(entry)
        entry["tools"].append({"name": row["name"], "description": row["description"], "version": row["version"]})
        entry["tool_refs"].append(row["ref"])
    for entry in providers.values():
        entry["version"] = candidate_digest([[tool["name"], tool["version"]] for tool in entry["tools"]])
    return grouped


# LLM: Jev每题独立并行，不能用多个选择槽假设互相看见答案；每项候选只评一次，完整材料受原JSON/模型预算约束。
# 函数用途: 为每个能力生成独立的适用性选择题，不预选候选或要求模型跨题去重。
def selection_questions(rows: list[dict]) -> dict:
    if not rows:
        raise DecisionInputError("能力推荐需要当前授权候选。")
    questions = {f"candidate_{index}": {
        "type": "choice", "instructions": {
            "question": "这个candidate是否适合协助完成state.query中的任务？分别判断每项能力，多项可以同时适合。",
            "candidate": row,
            "boundary": "只推荐初始名卡或schema展示，不决定权限，不要求现在执行或读取正文。",
            **({"provider": "kind=provider 表示同一插件提供的全部工具，按插件整体判断；适合时展示它的全部工具。"}
               if row.get("kind") == "provider" else {}),
        }, "criteria": {"include": "这项能力与任务相关，有助于完成任务，建议初始展示", **_NON_SELECTIONS},
    } for index, row in enumerate(rows)}
    decision_json(questions)
    return questions


# LLM: 按原题候选读取答案，不解析模型文字；任何无效/不确定题保持全集，明确无需能力才允许空短名单。
# 函数用途: 将独立适用性答案映射回对应候选；缺数据或坏答案保持原输入并保留结构化原因。
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
        indices.add(int(key.removeprefix("candidate_")))
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
