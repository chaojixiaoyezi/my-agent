# LLM: 能力决策材料只读原授权快照；包只提供公开摘要，私有资源不出题、不计入 Skill，也不预读正文或授予权限。
#   发给 Jev 的候选是白名单字段、按字段字节上限截短的投影（标定依据见上限常数），完整行只留在宿主。
# 模块用途: 为一次能力推荐准备可核对的候选和独立适用性题目，并将答案映射回原精确引用。
from __future__ import annotations

import hashlib
import json

from ..backends.decision_protocol import DecisionInputError, decision_json
from ..tooling.models import TOOL_DISCOVERY_ENTRY_NAMES

# 发给 Jev 的候选文字按请求体实际编码（json.dumps 默认转义，一个汉字 6 字节）计字节，按 jev_wire_bytes.v1 倒推：
#   53 题、state 不超过 4096 字节、每题三段文字都按上限填满汉字时 C = ceil(B/2) + 256×53 + 1024 = 57,204 ≤ 57,600。
#   题数更多或插件工具很多时仍由经验上界拒发（input_bound_out_of_calibration），不外推。
# Jev 候选名称上限 96 字节：约 16 个汉字或 96 个英文字符，插件组内工具名同用。
_JEV_CANDIDATE_NAME_MAX_BYTES = 96
# Jev 候选说明上限 180 字节：约 30 个汉字，插件组内每个工具的说明同用。
_JEV_CANDIDATE_DESCRIPTION_MAX_BYTES = 180
# Jev 候选适用场景上限 144 字节：约 24 个汉字。
_JEV_CANDIDATE_WHEN_TO_USE_MAX_BYTES = 144
_WIRE_TEXT_CAPS = {"name": _JEV_CANDIDATE_NAME_MAX_BYTES, "description": _JEV_CANDIDATE_DESCRIPTION_MAX_BYTES,
                   "when_to_use": _JEV_CANDIDATE_WHEN_TO_USE_MAX_BYTES}
# 发给 Jev 的候选只带这些字段；ref/version/tool_refs/keywords/category 等宿主身份与摘要不上线，答案按题号映射回完整行。
_WIRE_CANDIDATE_FIELDS = ("kind", "name", "description", "when_to_use", "tools_required")
# 每题都相同的判断说明只在 state 里放一次（Jev 每题都看到 state + 本题），题里只留候选和插件说明。
SELECTION_INSTRUCTIONS = {
    "question": "这个candidate是否适合协助完成state.query中的任务？分别判断每项能力，多项可以同时适合。",
    "boundary": "只推荐初始名卡或schema展示，不决定权限，不要求现在执行或读取正文。",
}

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


# LLM: 工具、Skill 与包摘要都取原 scoped snapshot；包内资源不进入候选，现有决策开关仍控制是否请求模型。
# 函数用途: 提取公开可发现能力名卡，不预读方法或脚本正文。
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
        rows.extend({"kind": "capability_package", "ref": package.stable_id,
                     "name": package.package_id, "description": package.description,
                     "when_to_use": package.summary, "keywords": list(package.keywords),
                     "version": package.package_sha256, "tools_required": []}
                    for package in skills.packages)
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


# LLM: 按字段名查 _WIRE_TEXT_CAPS，只截字符串，按转义后字节从前往后取最长前缀，不按内容挑词；没有上限的字段原样返回。
#   截了就在 cut[字段名] 上累计条数与截掉的字节（同一转义口径），供观察记录复盘 Jev 在多少信息量下做的选择。
# 函数用途: 把一段候选文字截到它所在字段的上限内，并记下截了多少。
def _clipped(field: str, value: object, cut: dict) -> object:
    cap = _WIRE_TEXT_CAPS.get(field)
    if cap is None or type(value) is not str:
        return value
    kept, used = [], 0
    for char in value:
        size = len(json.dumps(char)) - 2
        if used + size > cap:
            break
        kept.append(char)
        used += size
    if len(kept) == len(value):
        return value
    counts = cut[field]
    counts["fields"] += 1
    counts["bytes"] += len(json.dumps(value)) - 2 - used
    return "".join(kept)


# LLM: 只取 _WIRE_CANDIDATE_FIELDS 白名单；插件组的 tools 只留每个工具的 name/description，同样按字段上限截。
#   完整行仍由宿主保留，用于版本摘要和按题号映射答案；这里的投影只进 Jev 请求，不回写宿主任何状态。
# 函数用途: 生成一条发给 Jev 的有上限候选投影。
def _wire_candidate(row: dict, cut: dict) -> dict:
    wire = {field: _clipped(field, row[field], cut) for field in _WIRE_CANDIDATE_FIELDS if field in row}
    if row.get("kind") == "provider":
        wire["tools"] = [{field: _clipped(field, tool[field], cut) for field in ("name", "description") if field in tool}
                         for tool in row["tools"]]
    return wire


# LLM: Jev每题独立并行，不能用多个选择槽假设互相看见答案；每项候选只评一次。共用判断说明在 SELECTION_INSTRUCTIONS（由调用方
#   放进 state），每题只带有上限的候选投影；第二个返回值是结构化截断计数（每个有上限字段的截断条数与截掉字节），供观察记录。
# 函数用途: 为每个能力生成独立的适用性选择题，不预选候选或要求模型跨题去重，并报告投影截了多少。
def selection_questions(rows: list[dict]) -> tuple[dict, dict]:
    if not rows:
        raise DecisionInputError("能力推荐需要当前授权候选。")
    cut = {field: {"fields": 0, "bytes": 0} for field in _WIRE_TEXT_CAPS}
    questions = {f"candidate_{index}": {
        "type": "choice", "instructions": {
            "candidate": _wire_candidate(row, cut),
            **({"provider": "kind=provider 表示同一插件提供的全部工具，按插件整体判断；适合时展示它的全部工具。"}
               if row.get("kind") == "provider" else {}),
        }, "criteria": {"include": "这项能力与任务相关，有助于完成任务，建议初始展示", **_NON_SELECTIONS},
    } for index, row in enumerate(rows)}
    decision_json(questions)
    return questions, {"schema": "jev_candidate_projection.v1", "caps_bytes": dict(_WIRE_TEXT_CAPS), "truncated": cut}


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


# LLM: 必要引用只来自宿主合同与授权快照，包含包级引用但不展开私有资源，包不会隐式增加工具。
# 函数用途: 在缩短展示时保留明确任务义务、子代理 Skill 与能力包摘要。
def required_capabilities(params, contract, skills) -> tuple[set[str], tuple[str, ...]]:
    tools = {name for action in tuple(getattr(contract, "required_actions", ()) or ())
             if getattr(action, "status", "") == "open" for name in tuple(getattr(action, "allowed_tools", ()) or ())}
    attrs = getattr(params, "task_attributes", None) or {}
    refs = attrs.get("skill_snapshot_refs", [])
    required = {row.get("stable_id") for row in refs if isinstance(row, dict)} if isinstance(refs, list) else set()
    entries = skills.reference_entries() if skills is not None else ()
    if getattr(params, "context_scope", "default") == "task_local":
        required.update(entry.stable_id for entry in entries)
    retained = tuple(entry.stable_id for entry in entries if entry.stable_id in required)
    tools.update(name for entry in entries if entry.stable_id in retained for name in entry.tools_required)
    return tools, retained
