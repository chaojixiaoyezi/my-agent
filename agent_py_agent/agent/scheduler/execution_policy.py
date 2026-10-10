# LLM: 每项计划的显式执行配置保存在原 job，预留时复制到 run；不解析提示词，不存凭据或展开后的模型配置。
# 模块用途: 统一会话隔离、固定模型与预检配置的校验和工具 schema。
from __future__ import annotations

from .schedule import ScheduleValidationError

EXECUTION_DEFAULTS = {"mode": "followup", "model_policy": "inherit", "profile_id": ""}
EXECUTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": "followup 在原会话执行；isolated 每次独立执行并回原会话交付。inherit 跟随原会话模型；fixed 固定档案 ID。",
    "properties": {"mode": {"type": "string", "enum": ["followup", "isolated"]},
                   "model_policy": {"type": "string", "enum": ["inherit", "fixed"]},
                   "profile_id": {"type": "string"}},
}
PRECHECK_SCHEMA = {
    "type": ["object", "null"], "additionalProperties": False,
    "description": "可选轻量预检：经原工具权限执行只读工具，比较结构化结果摘要。无变化不调用模型；失败记错，不当作无变化。null 关闭。",
    "properties": {"tool_name": {"type": "string"}, "arguments": {"type": "object"},
                   "json_pointer": {"type": "string", "description": "可选 RFC 6901 指针；省略比较完整工具输出，提供时输出必须是 JSON。"}},
    "required": ["tool_name", "arguments"],
}


# LLM: 缺省保持原会话继承行为；固定引用必须是明确档案，不能用可变 default 别名代替固定语义。
# 函数用途: 校验一项计划的执行位置和模型来源，返回可冻结的规范对象。
def normalize_execution(value: object) -> dict:
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - EXECUTION_DEFAULTS.keys():
        raise ScheduleValidationError("execution 含不支持的字段")
    result = {**EXECUTION_DEFAULTS, **value}
    if result["mode"] not in ("followup", "isolated") or result["model_policy"] not in ("inherit", "fixed"):
        raise ScheduleValidationError("execution 的模式或模型策略无效")
    profile = result["profile_id"]
    if not isinstance(profile, str) or profile != profile.strip():
        raise ScheduleValidationError("profile_id 必须是明确档案 ID")
    if (result["model_policy"] == "fixed") != bool(profile) or profile == "default":
        raise ScheduleValidationError("只有 fixed 必须指定非 default 档案 ID")
    return result


# LLM: 写计划前解析引用只做本地配置校验，不发送模型请求；运行时仍重查撤销或权限变化，不能冻结凭据。
# 函数用途: 让不存在或无权使用的固定档案在创建/更新时明确失败，避免保存一项必然不能执行的计划。
def validate_execution_for_agent(agent, value: object) -> dict:
    from ..settings.model_profiles import ModelProfileError, selected_model_config

    execution = normalize_execution(value)
    if execution["model_policy"] == "fixed":
        try:
            selected_model_config(agent, profile_id=execution["profile_id"])
        except ModelProfileError as exc:
            raise ScheduleValidationError("固定模型档案不可用，请选择当前 owner 可用的档案 ID") from exc
    return execution


# LLM: 这里只做数据校验；实际只读、副作用与权限由执行时的同一工具快照裁决，不能凭名字认定安全。
# 函数用途: 校验预检工具和可选 JSON 指针，保留显式 null 关闭语义。
def normalize_precheck(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"tool_name", "arguments", "json_pointer"}:
        raise ScheduleValidationError("precheck 含不支持的字段")
    name, arguments = value.get("tool_name"), value.get("arguments")
    pointer = value.get("json_pointer")
    if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
        raise ScheduleValidationError("precheck 必须指定工具及参数")
    if pointer is not None and (not isinstance(pointer, str) or (pointer and not pointer.startswith("/"))):
        raise ScheduleValidationError("precheck.json_pointer 必须是 JSON 指针")
    if any(str(key).startswith("__") for key in arguments):
        raise ScheduleValidationError("precheck 不允许传入宿主身份参数")
    return {"tool_name": name.strip(), "arguments": dict(arguments),
            **({"json_pointer": pointer} if pointer is not None else {})}
