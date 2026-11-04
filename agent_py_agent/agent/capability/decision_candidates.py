"""能力决策只读原授权快照与描述符，不预读正文或授予权限。
发给 Jev 的候选保持原白名单和字节标定；顶层选择的后代引用仅留宿主。
提供共用的固定方法描述符、完整检索卡和层级候选投影。"""
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


# LLM: 工具和包保持原快照；固定方法只读绑定 Agent 的同 owner 清单，子项随顶层父选择，不单独出题。
# 函数用途: 提取顶层方法候选，不把顶层过滤应用到完整搜索卡集合。
def capability_candidates(snapshot, skills, *, categories: list[str], skills_discoverable: bool,
                          allowed_tools: list[str] | None = None) -> list[dict]:
    rows = [_tool_row(runtime)
            for runtime in snapshot.runtimes
            if runtime.exposure.model_visible and runtime.model_spec.category in categories
            and (allowed_tools is None or runtime.model_spec.name in allowed_tools)
            and runtime.model_spec.name not in TOOL_DISCOVERY_ENTRY_NAMES]
    if skills_discoverable:
        agent = next((getattr(runtime.handler, "agent", None) for runtime in snapshot.runtimes
                      if runtime.model_spec.name == "skill_search"), None)
        descriptors = method_descriptors_for_agent(agent, skill_snapshot=skills)
        rows.extend(_top_level_method_rows(descriptors, skills))
        rows.extend({"kind": "capability_package", "ref": package.stable_id,
                     "name": package.package_id, "description": package.description,
                     "when_to_use": package.summary, "keywords": list(package.keywords),
                     "version": package.package_sha256, "tools_required": []}
                    for package in skills.packages)
    return rows


# LLM: 只从传入宿主Owner/workspace根经原只读覆盖列元数据；不加载代码、不创建锁、不改变工具权限。
# 函数用途: 为统一候选池投影实际保存的脚本/流程，停用项不推荐。
def asset_candidate_rows(owner_home, workspace_root=None):
    if owner_home is None:
        return []
    return [_asset_candidate_row(row) for row in _stored_method_descriptors(owner_home, workspace_root)
            if _visible_method(row)]


# LLM: 唯一来源是已授权 Store 列表的只读覆盖；不构造可写 Store、不调用 get、不读取代码或 WORKFLOW.md。
# 函数用途: 按 workspace 优先列出脚本和工作流描述符，保留隐藏 composition 给纯树处理。
def _stored_method_descriptors(owner_home, workspace_root=None) -> list[dict]:
    from ..assets.workspace_assets import WorkspaceAssetOverlay

    overlay = WorkspaceAssetOverlay(workspace_root or None, owner_home=owner_home)
    return [{key: value for key, value in row.items() if key not in {"code", "entrypoint_path", "steps", "body_markdown"}}
            for kind in ("script", "workflow") for row in overlay.list_assets(kind)]


# LLM: 技能只从 enabled_entries 的冻结字段投影，不用路径重建目录或重新读取 sidecar；准确来源覆盖附加元数据。
# 函数用途: 生成带稳定身份、合并 refs 和安全资产 ID 的技能描述符。
def _snapshot_method_descriptors(snapshot) -> list[dict]:
    from ..assets import skill_asset_id

    if snapshot is None:
        return []
    rows = []
    for entry in snapshot.enabled_entries():
        metadata = getattr(entry, "asset_metadata", {})
        refs = getattr(entry, "refs", {})
        rows.append({"kind": "skill", "id": skill_asset_id(entry.name), "name": entry.name,
                     "summary": entry.description, "enabled": True, "tags": list(entry.tags),
                     "refs": dict(refs) if isinstance(refs, dict) else {}, "source_scope": entry.source,
                     "metadata": {**(metadata if isinstance(metadata, dict) else {}),
                                  "stable_id": entry.stable_id, "source": entry.source}})
    return rows


# LLM: 范围只按当前线程 runner、结构化 context_scope 和父身份裁决，先于任何固定资产枚举。
# 函数用途: 判断本轮是否允许读取固定方法清单，不从自然语言或路径反推权限。
def _fixed_inventory_allowed(agent) -> bool:
    from ..runtime_context import current_subagent_run_id, current_task_attributes

    if agent is None or current_subagent_run_id(agent):
        return False
    params = getattr(agent, "_current_run_params", None)
    sources = (agent, params)
    if any(getattr(source, "context_scope", "") in {"task_local", "isolated", "control_plane"}
           for source in sources):
        return False
    if any(getattr(source, key, "") for source in sources for key in ("parent_id", "parent_run_id", "subagent_run_id")):
        return False
    attributes = (current_task_attributes(agent), getattr(params, "task_attributes", None))
    return not any(isinstance(attrs, dict) and (attrs.get("context_scope") in {"task_local", "isolated", "control_plane"}
                   or any(attrs.get(key) for key in ("parent_id", "parent_run_id", "subagent_run_id"))) for attrs in attributes)


# LLM: 直接提供者的None也是权威；仅缺失时沿绑定Router取冻结快照，不重建目录或更换Owner。
# 函数用途: 按显式提供者、Agent内存快照、Router的顺序取得当前授权快照。
def _snapshot_for(agent):
    provider = getattr(agent, "current_skill_snapshot", None)
    if callable(provider):
        return provider()
    snapshot = getattr(agent, "_current_skill_snapshot", None)
    if snapshot is not None:
        return snapshot
    provider = getattr(getattr(agent, "capability_router", None), "_snapshot", None)
    return provider() if callable(provider) else None


# LLM: 真实 owner 绑定与冻结 snapshot.owner_id 必须一致；受限上下文只保留内存中的授权技能，不触碰固定资产。
# 函数用途: 向目录、候选和 L1 共用同一描述符清单，缺直接快照时沿原Router回退，隐藏项留给纯树裁剪。
def method_descriptors_for_agent(agent, *, skill_snapshot=None) -> list[dict]:
    from ..assets.monitor_adapter import list_monitor_descriptors

    snapshot = skill_snapshot
    if snapshot is None:
        snapshot = _snapshot_for(agent)
    rows = _snapshot_method_descriptors(snapshot)
    home = getattr(agent, "home_paths", None)
    owner_id = getattr(home, "owner_id", "")
    owner_home = getattr(home, "owner_home_dir", None)
    if (not _fixed_inventory_allowed(agent) or not isinstance(owner_id, str) or not owner_id
            or not owner_home or snapshot is None or owner_id != snapshot.owner_id):
        return rows
    workspace = getattr(getattr(agent, "tools", None), "workspace_root", None)
    if workspace is None:
        workspace = getattr(snapshot, "workspace_root", None)
    rows.extend(_stored_method_descriptors(owner_home, workspace))
    rows.extend({**row, "source_scope": "owner"} for row in list_monitor_descriptors(owner_home))
    return rows


# LLM: 可见性只按结构化启停和 hidden 字段判定；监控是徽标，不是独立方法候选或父节点。
# 函数用途: 统一过滤搜索卡和候选中的不可见方法，不影响 loader 保留 composition。
def _visible_method(row: dict) -> bool:
    metadata = row.get("metadata")
    return (row.get("kind") in {"skill", "script", "workflow"} and row.get("enabled", True) is not False
            and not (isinstance(metadata, dict) and metadata.get("hidden") is True))


# LLM: 来源来自只读覆盖的实际层，不相信正文的 source；候选仍只保存摘要与精确工具读取入口。
# 函数用途: 把可见脚本或工作流描述符转为原候选行形状。
def _asset_candidate_row(row: dict) -> dict:
    source = row.get("source_scope") or "owner"
    return {"kind": row["kind"], "ref": f"{row['kind']}:{source}:{row['id']}",
            "name": row["name"], "description": row.get("summary", ""), "when_to_use": row.get("summary", ""),
            "version": str(row.get("revision", 0)), "tools_required": ["scripts" if row["kind"] == "script" else "workflows"],
            "asset_id": row["id"], "scope": source}


# LLM: 纯树负责可见父和 SCC 根，后代精确引用不进 Jev 白名单；遍历用 visited 防止环路。
# 函数用途: 仅对顶层方法出题，为采用后的传递选择继承保存同次清单后代。
def _top_level_method_rows(descriptors: list[dict], skills) -> list[dict]:
    entries = {entry.stable_id: entry for entry in skills.enabled_entries()}
    method_rows = {}
    for row in descriptors:
        if not _visible_method(row):
            continue
        if row["kind"] == "skill":
            entry = entries[row["metadata"]["stable_id"]]
            candidate = {"kind": "skill", "ref": entry.stable_id, "name": entry.name,
                         "description": entry.description, "when_to_use": entry.when_to_use,
                         "version": entry.content_sha256, "tools_required": list(entry.tools_required)}
            key = f"skill:{entry.stable_id}"
        else:
            candidate, key = _asset_candidate_row(row), f"{row['kind']}:{row['id']}"
        method_rows[key] = candidate
    if not any(row["kind"] in {"script", "workflow"} for row in method_rows.values()):
        return list(method_rows.values())
    from .method_tree import build_method_tree

    tree = build_method_tree(descriptors)
    result = []
    for root in tree["roots"]:
        if root not in method_rows:
            continue
        row = dict(method_rows[root])
        pending, seen, inherited = list(tree["children"].get(root, ())), {root}, []
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            if key in method_rows:
                inherited.append(method_rows[key])
            pending.extend(tree["children"].get(key, ()))
        if inherited:
            row["inherited_candidates"] = sorted(inherited, key=lambda item: (item["kind"], item["ref"]))
        result.append(row)
    return result


# LLM: 只使用当前 Agent 的同 owner 描述符；完整卡片保留可见子项，不复用顶层候选过滤。
# 函数用途: 将全部可见脚本/工作流投影为可搜索名卡，不自动读取正文或扩大权限。
def asset_cards_for_agent(agent, *, skill_snapshot=None, descriptors=None):
    from .router import CapabilityCard

    records = method_descriptors_for_agent(agent, skill_snapshot=skill_snapshot) if descriptors is None else descriptors
    return [CapabilityCard(row['ref'], row['kind'], row['name'], row['description'], source=row['scope'],
                           metadata={**row, 'stable_id':row['ref'], 'next_read':{
                               'tool':'scripts' if row['kind']=='script' else 'workflows',
                               'arguments':{'action':'get', row['kind']+'_id':row['asset_id']}}})
            for record in records if record.get('kind') in {'script', 'workflow'} and _visible_method(record)
            for row in [_asset_candidate_row(record)]]


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


# LLM: 按原题读取答案；只有明确采用的顶层父才展开同次清单后代，共享子按精确 kind/ref 去重，不另出题或读取。
# 函数用途: 映射选择并传递父选择给后代；无效/不确定答案仍保留原错误边界。
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
    selected, seen = [], set()
    for index in sorted(indices):
        row = rows[index]
        for candidate in (row, *row.get("inherited_candidates", ())):
            key = candidate["kind"], candidate["ref"]
            if key not in seen:
                selected.append(candidate)
                seen.add(key)
    return selected, ""


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
