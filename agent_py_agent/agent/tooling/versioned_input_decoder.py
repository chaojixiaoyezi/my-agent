# LLM: 本模块是宿主指定版本的输入边界；模型参数不得选择版本，规范handler不保留旧形态分支。
# 模块用途: 将未执行的旧工具请求转换为规范输入，不重执行完成历史。
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from ..contracts.error_taxonomy import error_contract


# LLM: sol2分类表未集成时仅补这个已约定的参数错误；其余未知码保持原UNKNOWN_ERROR，禁止注册或热改全局表。
# 函数用途: 为工具结果提供可删除的本地错误分类兼容读口。
def canonical_error_contract(code):
    contract = error_contract(code)
    if code == "TOOL_SCHEMA_INVALID_MODE_COMBINATION" and contract.code == "UNKNOWN_ERROR":
        return replace(error_contract("TOOL_INVALID_ARGUMENTS"), code=code)
    return contract


# LLM: 版本只取宿主协议而非模型字段；本函数纯转换，不授权、不执行或重放历史；新输入原样交现行schema校验。
# 函数用途: 将显式v1请求转换为唯一规范工具名和独立参数副本。
def decode_tool_input(name, params, *, version=2):
    if type(version) is not int or version not in {1, 2} or not isinstance(params, dict):
        raise ValueError("无效工具输入协议")
    values = deepcopy(params)
    if version == 2:
        return name, values
    if name == "list_tools":
        if values:
            raise ValueError("旧目录调用不接受参数")
        return "tool_search", {"action": "catalog"}
    if name == "skill_feedback":
        if set(values) - {"operation", "action", "skill_id"}:
            raise ValueError("未知反馈参数")
        if "operation" in values:
            operation = values.pop("operation")
            if operation not in {"status", "adopted"} or ("action" in values and values["action"] != operation):
                raise ValueError("反馈动作冲突")
            values["action"] = operation
    elif name == "task_progress" and values.get("action") == "create":
        values["action"] = "update"
    elif name == "session_search":
        values = _session_v1(values)
    elif name == "create_subagents":
        values = _create_v1(values)
    return name, values


# LLM: v1仍先核原字段类型和范围，再沿真实优先级选有效字段；显式mode不得混进旧语法。
# 函数用途: 将旧会话检索的六种隐式形态转换为规范mode。
def _session_v1(values):
    from ..capability.session_search_tool import SESSION_SEARCH_MODES

    strings = {"query", "around_id", "source_type", "message_id", "thread_id"}
    bounds = {"limit": (1, 20), "window": (1, 20), "offset": (0, None),
        "max_chars": (200, 20000), "cursor": (0, None)}
    if set(values) - strings - set(bounds) - {"current_thread"}:
        raise ValueError("旧会话请求含未知字段或混入mode")
    for key, value in values.items():
        if key in strings and not isinstance(value, str):
            raise ValueError("旧会话字符串字段类型错误")
        if key == "current_thread" and type(value) is not bool:
            raise ValueError("current_thread必须是布尔值")
        if key in bounds:
            low, high = bounds[key]
            if type(value) is not int or value < low or (high is not None and value > high):
                raise ValueError("旧会话数值字段越界")
    if values.get("message_id", "").strip():
        mode = "message"
    elif values.get("around_id", "").strip():
        mode = "around"
    elif values.get("current_thread") is True:
        mode = "thread_search" if values.get("query", "").strip() else "thread_browse"
    else:
        mode = "search" if values.get("query", "").strip() else "recent"
    return {"mode": mode, **{key: value for key, value in values.items() if key in SESSION_SEARCH_MODES[mode][1]}}


# 旧批次父级交付物/计划/说明/covers从未广播，不得借归一化改变孩子边界。
_V1_NON_BROADCAST = frozenset({"items", "goal", "description", "plan", "context_manifest", "covers",
    "output_files", "output_refs", "artifact_refs", "required_output_files", "required_output_refs",
    "deliverables", "final_output", "final_output_path"})
# 冻结的v1协议字段，不依赖上层agent_core或后续v2模型schema的演化。
_CREATE_V1_FIELDS = frozenset({"goal", "persistent_goal", "covers", "description", "role", "agent_name", "model",
    "effort", "tool_preset", "allowed_tools", "allowed_skills", "plan", "input_refs", "output_files", "artifact_refs",
    "replacement_for_run_ids", "related_finding_id", "long_running", "service_window_seconds", "audit_source_id", "input_media_refs"})


# LLM: 精确保留旧空items回单项与item显式空列表覆盖；模型不携内部标记，原显式字段事实由创建服务保存。
# 函数用途: 在输入边界展开旧顶层默认值，规范业务只消费独立items。
def _create_v1(values):
    allowed = _CREATE_V1_FIELDS | {"items"}
    if set(values) - allowed:
        raise ValueError("未知旧派工字段")
    items = values.get("items")
    if items is not None and not isinstance(items, list):
        raise ValueError("items必须是数组")
    if not items:
        item = {key: value for key, value in values.items() if key != "items"}
        if not isinstance(item.get("goal"), str) or not item["goal"].strip():
            raise ValueError("派工缺少非空goal")
        return {"items": [item]}
    defaults = {key: value for key, value in values.items() if key not in _V1_NON_BROADCAST}
    output = []
    for item in items:
        if (not isinstance(item, dict) or set(item) - _CREATE_V1_FIELDS or not isinstance(item.get("goal"), str)
                or not item["goal"].strip()):
            raise ValueError("每个item必须自带非空goal且字段合法")
        output.append({**defaults, **item})
    summary = values.get("goal")
    return {**({"batch_summary": summary} if summary else {}), "items": output}
