# LLM: 决策字段及运行范围由此唯一登记；持久校验保留旧覆盖可清理，新的写入须检查 scope；同步设置投影与服务范围测试。
# 模块用途: 校验原 owner/thread 决策覆盖，并提供界面和运行服务共用的字段作用范围。
from __future__ import annotations

import math
from types import MappingProxyType
from uuid import UUID

from .model_provider_schema import ModelProfileError

DECISION_SETTINGS_SCHEMA = "decision_settings.v1"
POINT_RUNTIME_SCOPES = MappingProxyType({
    "model_selection": "thread", "subagent_model": "thread", "skill_tool": "thread",
    "recall": "thread", "curator": "owner_background",
})
POINTS = tuple(POINT_RUNTIME_SCOPES)
GENERAL_FIELDS = ("enabled", "timeout_seconds", "stage_timeout_seconds", "background_timeout_seconds", "profile_id")
POINT_FIELDS = ("mode", "timeout_seconds", "profile_id")


# LLM: 配置冲突是独立机器类型，入口应重新读取而非用旧整份快照重试覆盖。
# 类用途: 表示另一个窗口或用户已经修改了待写设置。
class DecisionSettingsConflict(ModelProfileError):
    code = "decision_settings_conflict"


# LLM: 身份错误与字段错误分开，工具只按此类型映射权限失败，不读取中文错误文本。
# 类用途: 表示没有可信当前 owner 或试图访问其他用户的会话。
class DecisionSettingsAccessError(ModelProfileError):
    code = "decision_settings_access_denied"


# LLM: 缺配置只产生空覆盖，默认值由原模块拥有；此函数不写文件。
# 函数用途: 为新 owner 或迁移的旧线程构造初始覆盖信封。
def empty_decision_settings() -> dict:
    return {"schema": DECISION_SETTINGS_SCHEMA, "revision": 0, "overrides": {}}


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


# LLM: 引用只能指向原模型目录或原共享命名空间，不能携带路径、凭据或任意服务地址。
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
        for field in POINT_FIELDS:
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
    if field == "enabled":
        if type(value) is not bool:
            raise ModelProfileError("决策总开关必须是布尔值。")
        return value
    if field.endswith("timeout_seconds"):
        return positive_seconds(value)
    if field == "profile_id":
        return profile_reference(value)
    if type(value) is not str or value not in {"off", "observe", "apply"}:
        raise ModelProfileError("决策模式只能是 off、observe 或 apply。")
    return value


# LLM: 持久结构严格校验，不丢未知覆盖；保留历史线程后台字段供显式 reset，不能在读取时静默删改或重新授权。
# 函数用途: 复制有效的版本化覆盖；当前不适用的旧范围仍可读取，损坏结构保持失败。
def validate_decision_settings(value: object) -> dict:
    if type(value) is not dict or set(value) != {"schema", "revision", "overrides"}:
        raise ModelProfileError("决策覆盖结构无效，原配置未修改。")
    if value["schema"] != DECISION_SETTINGS_SCHEMA or type(value["revision"]) is not int or value["revision"] < 0:
        raise ModelProfileError("决策覆盖版本无效，原配置未修改。")
    overrides = value["overrides"]
    if type(overrides) is not dict or len(overrides) > len(GENERAL_FIELDS) + len(POINTS) * len(POINT_FIELDS):
        raise ModelProfileError("决策覆盖字段无效，原配置未修改。")
    return {"schema": DECISION_SETTINGS_SCHEMA, "revision": value["revision"],
            "overrides": {key: validate_decision_field(key, item) for key, item in overrides.items()}}


# LLM: 会话 v1-v9/无版本旧记录只补空覆盖；未知线程版本拒绝写回，不能降级清除未来状态。
# 函数用途: 显式迁移线程中的决策设置，当前 v10 必须携带有效信封。
def thread_decision_settings(data: dict) -> dict:
    schema = data.get("schema_version", "")
    if schema not in {"", *(f"conversation_thread.v{i}" for i in range(1, 11))}:
        raise ModelProfileError("会话配置版本不受支持，原记录未修改。")
    if "decision_settings" not in data and schema != "conversation_thread.v10":
        return empty_decision_settings()
    return validate_decision_settings(data.get("decision_settings"))
