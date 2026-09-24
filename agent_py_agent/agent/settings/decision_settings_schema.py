# LLM: 决策字段及范围由此唯一登记；v1 显式迁移为无实验授权的 v2，授权独立于可 patch 字段，写入仍检查 scope。
# 模块用途: 校验原 owner/thread 决策覆盖，并提供界面和运行服务共用的字段作用范围。
from __future__ import annotations

import math
from copy import deepcopy
from types import MappingProxyType
from uuid import UUID

from .model_provider_schema import ModelProfileError

DECISION_SETTINGS_SCHEMA = "decision_settings.v2"
POINT_RUNTIME_SCOPES = MappingProxyType({
    "model_selection": "thread", "subagent_model": "thread", "skill_tool": "thread",
    "pre_recall": "thread", "recall": "thread", "curator": "owner_background", "curator_relation": "owner_background",
    "external_material_order": "thread", "planning": "thread", "delivery_quality": "thread",
    "skill_proposal_review": "owner_background",
})
POINTS = tuple(POINT_RUNTIME_SCOPES)
GENERAL_FIELDS = ("enabled", "experiment_enabled", "timeout_seconds", "stage_timeout_seconds", "background_timeout_seconds", "profile_id")
POINT_FIELDS = ("mode", "timeout_seconds", "profile_id")
_POINT_EXTRA_SCHEMAS = {"subagent_model": {
    "candidate_profile_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
}, "skill_tool": {
    "context_policy": {"type": "string", "enum": ["metadata", "progressive"]},
    "optional_categories": {"type": "array", "items": {"type": "string", "minLength": 1}},
}}


# LLM: 接入点专属字段只在此登记，配置投影和 schema 消费同一字段集合，不给其它点注入无用字段。
# 函数用途: 返回指定原接入点的完整可配置字段。
def decision_point_fields(point: str) -> tuple[str, ...]:
    return (*POINT_FIELDS, *_POINT_EXTRA_SCHEMAS.get(point, {}))


# LLM: 返回副本，模型工具不能修改全局字段登记；宿主运行时仍须调用 validate_decision_field。
# 函数用途: 提供原配置工具需要的 JSON 字段类型与选项。
def decision_field_schema(path: str) -> dict:
    if path.startswith("points."):
        _, point, field = path.split(".")
        if field in _POINT_EXTRA_SCHEMAS.get(point, {}):
            return deepcopy(_POINT_EXTRA_SCHEMAS[point][field])
    return ({"type": "boolean"} if path in {"enabled", "experiment_enabled"} else
            {"type": "number", "exclusiveMinimum": 0} if path.endswith("timeout_seconds") else
            {"type": "string", "enum": ["off", "observe", "apply"]} if path.endswith(".mode") else
            {"type": "string", "description": "原模型目录的 Decision 编号或 shared:编号；空字符串明确不绑定。"})


# LLM: 配置冲突是独立机器类型，入口应重新读取而非用旧整份快照重试覆盖。
# 类用途: 表示另一个窗口或用户已经修改了待写设置。
class DecisionSettingsConflict(ModelProfileError):
    code = "decision_settings_conflict"


# LLM: 身份错误与字段错误分开，工具只按此类型映射权限失败，不读取中文错误文本。
# 类用途: 表示没有可信当前 owner 或试图访问其他用户的会话。
class DecisionSettingsAccessError(ModelProfileError):
    code = "decision_settings_access_denied"


# LLM: 缺配置只产生空覆盖及空实验授权，默认值由原模块拥有；此函数不写文件或建立实验预算。
# 函数用途: 为新 owner 或迁移的旧线程构造初始覆盖信封。
def empty_decision_settings() -> dict:
    return {"schema": DECISION_SETTINGS_SCHEMA, "revision": 0, "overrides": {}, "experiment_authorization": None}


# LLM: 不把 bool 当秒数，也不以零表示关闭或无限等待；此校验供 YAML 和覆盖共用。
# 函数用途: 拒绝非有限、非正的决策等待时间。
def positive_seconds(value: object) -> float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ModelProfileError("决策等待时间必须是有限正秒数，不能使用布尔值或零。")
    return float(value)


# LLM: 引用只能指向原模型目录或原共享命名空间，不能携带路径、凭据或任意服务地址；候选列表逐项复用此校验。
# 函数用途: 校验决策模型引用的语法；空字符串明确表示未绑定。
def profile_reference(value: object) -> str:
    if type(value) is not str:
        raise ModelProfileError("决策模型引用须为已保存的模型编号。")
    if not value:
        return value
    raw = value.removeprefix("shared:")
    try:
        if str(UUID(raw)) != raw:
            raise ValueError
    except ValueError as exc:
        raise ModelProfileError("决策模型引用须为已保存的模型编号。") from exc
    return value


# LLM: 元数据只声明原字段可 patch 的范围，不授予身份权限；reset 仍可清理旧线程覆盖，调用方不能修改全局登记表。
# 函数用途: 返回菜单、写入校验和有效值叠加共用的字段范围副本。
def decision_field_scopes() -> dict[str, list[str]]:
    result = {key: ["owner"] if key == "background_timeout_seconds" else ["owner", "thread"] for key in GENERAL_FIELDS}
    for point, runtime_scope in POINT_RUNTIME_SCOPES.items():
        for field in decision_point_fields(point):
            result[f"points.{point}.{field}"] = ["owner"] if runtime_scope == "owner_background" else ["owner", "thread"]
    return result


# LLM: 新 patch 必须传已认证 scope，持久读取和 reset 不按当前范围拒绝旧数据；字段和值校验仍为唯一实现。
# 函数用途: 检查扁平字段和值；写入时拒绝把用户后台设置保存成线程临时覆盖。
def validate_decision_field(path: object, value: object, *, scope: str | None = None) -> object:
    scopes = decision_field_scopes()
    if type(path) is not str or path not in scopes:
        raise ModelProfileError("决策设置包含未登记的字段或接入点。")
    if scope is not None and scope not in scopes[path]:
        raise ModelProfileError("该决策设置不支持当前作用范围；后台字段请使用用户长期设置，线程旧覆盖仍可恢复继承。")
    field = path.rsplit(".", 1)[-1]
    if field in {"enabled", "experiment_enabled"}:
        if type(value) is not bool:
            raise ModelProfileError("决策总开关必须是布尔值。")
        return value
    if field.endswith("timeout_seconds"):
        return positive_seconds(value)
    if field == "profile_id":
        return profile_reference(value)
    if field == "candidate_profile_ids":
        if type(value) is not list:
            raise ModelProfileError("子代理模型候选必须是模型编号列表。")
        result = [profile_reference(item) for item in value]
        if any(not item for item in result) or len(result) != len(set(result)):
            raise ModelProfileError("子代理模型候选不能包含空编号或重复编号。")
        return result
    if field == "optional_categories":
        if type(value) is not list or any(type(item) is not str or not item.strip() for item in value):
            raise ModelProfileError("可选工具类别必须是字符串列表；空列表表示不额外收起任何类别。")
        return list(value)
    if field == "context_policy":
        if type(value) is not str or value not in decision_field_schema(path)["enum"]:
            raise ModelProfileError("上下文策略只能是 metadata 或 progressive。")
        return value
    if type(value) is not str or value not in {"off", "observe", "apply"}:
        raise ModelProfileError("决策模式只能是 off、observe 或 apply。")
    return value


# LLM: v1 只显式补无授权字段且不写盘；v2 严格保留唯一授权信封，普通覆盖不能授予或合并实验许可。
# 函数用途: 校验并复制决策设置，旧覆盖及 revision 原样迁移，未知版本与损坏授权拒绝读取。
def validate_decision_settings(value: object) -> dict:
    if type(value) is not dict:
        raise ModelProfileError("决策覆盖结构无效，原配置未修改。")
    if value.get("schema") == "decision_settings.v1" and set(value) == {"schema", "revision", "overrides"}:
        value = {**value, "schema": DECISION_SETTINGS_SCHEMA, "experiment_authorization": None}
    if set(value) != {"schema", "revision", "overrides", "experiment_authorization"}:
        raise ModelProfileError("决策覆盖结构无效，原配置未修改。")
    if value["schema"] != DECISION_SETTINGS_SCHEMA or type(value["revision"]) is not int or value["revision"] < 0:
        raise ModelProfileError("决策覆盖版本无效，原配置未修改。")
    overrides = value["overrides"]
    if type(overrides) is not dict or len(overrides) > len(decision_field_scopes()):
        raise ModelProfileError("决策覆盖字段无效，原配置未修改。")
    from .decision_experiment_schema import validate_experiment_authorization

    return {"schema": DECISION_SETTINGS_SCHEMA, "revision": value["revision"],
            "overrides": {key: validate_decision_field(key, item) for key, item in overrides.items()},
            "experiment_authorization": validate_experiment_authorization(value["experiment_authorization"])}


# LLM: 会话 v1-v9/无版本旧记录只补空覆盖；未知线程版本拒绝写回，不能降级清除未来状态。
# 函数用途: 显式迁移线程中的决策设置，当前 v10 必须携带有效信封。
def thread_decision_settings(data: dict) -> dict:
    schema = data.get("schema_version", "")
    if schema not in {"", *(f"conversation_thread.v{i}" for i in range(1, 11))}:
        raise ModelProfileError("会话配置版本不受支持，原记录未修改。")
    if "decision_settings" not in data and schema != "conversation_thread.v10":
        return empty_decision_settings()
    return validate_decision_settings(data.get("decision_settings"))
