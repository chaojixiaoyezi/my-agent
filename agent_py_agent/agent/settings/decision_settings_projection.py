# LLM: 有效层与字段来源必须遵守 schema 的同一范围登记；后台不叠加线程值，不返回凭据或拥有运行中时钟。
# 模块用途: 为界面、工具和决策服务读回按作用范围计算的有效值、来源及请求上限。
from __future__ import annotations

from .decision_settings_defaults import decision_defaults
from .decision_settings_schema import (
    POINT_RUNTIME_SCOPES,
    decision_field_scopes,
    empty_decision_settings,
    validate_decision_settings,
)
from .model_provider_schema import ModelProfileError, resolved_model
from .shared_model_catalog import resolve_shared_model, shared_profile_key


# LLM: 显式保存引用只检查原目录的合法 Decision 用途；可用性另查，失效服务不得阻止用户关闭。
# 函数用途: 复用私有/共享原引用解析，供保存或只读状态检查，不创建后端。
def decision_profile(context: object, data: dict, profile_id: str, *, require_enabled: bool = True) -> dict:
    if shared_profile_key(profile_id):
        return resolve_shared_model(context.home_paths, profile_id, capability="decision", require_enabled=require_enabled)
    if profile_id not in data["profiles"]:
        raise ModelProfileError("决策模型引用不存在，请重新选择已保存的 Decision 模型。")
    return resolved_model(data, profile_id, capability="decision", require_enabled=require_enabled)


# LLM: 可用性仅检查本地配置与共享授权；未探测网络，不能把 configured 冒充服务在线。
# 函数用途: 返回不含凭据的连接状态，即使失效引用也能进入设置关闭能力。
def _profile_status(context: object, data: dict, profile_id: str) -> dict:
    if not profile_id:
        return {"configured": False, "reason": "unbound", "network": "unchecked"}
    try:
        decision_profile(context, data, profile_id)
    except ModelProfileError:
        return {"configured": False, "reason": "unavailable", "network": "unchecked"}
    return {"configured": True, "reason": "configured", "network": "unchecked"}


# LLM: 缺点值只从该点允许的配置层继承；后台用 owner 与后台预算，旧线程后台覆盖只展示供清理，不参加有效值。
# 函数用途: 返回可写范围、真实字段来源和静态等待上限，运行服务仍须扣除原阶段已耗时间。
def decision_settings_projection(context: object, data: dict, thread: object = None, *, scope: str = "owner") -> dict:
    owner = validate_decision_settings(data["decision_settings"])
    temporary = validate_decision_settings(thread.decision_settings) if thread is not None else empty_decision_settings()
    field_scopes = decision_field_scopes()
    owner_values, owner_sources = decision_defaults(context)
    for key, value in owner["overrides"].items():
        owner_values[key], owner_sources[key] = value, "owner"
    effective, sources = dict(owner_values), dict(owner_sources)
    for key, value in temporary["overrides"].items():
        if "thread" in field_scopes[key]:
            effective[key], sources[key] = value, "thread"
    general = {key: value for key, value in effective.items() if not key.startswith("points.")}
    points = {}
    for point, runtime_scope in POINT_RUNTIME_SCOPES.items():
        values = owner_values if runtime_scope == "owner_background" else effective
        origins = owner_sources if runtime_scope == "owner_background" else sources
        prefix = f"points.{point}."
        time_key = "background_timeout_seconds" if runtime_scope == "owner_background" else "timeout_seconds"
        budget_key = "background_timeout_seconds" if runtime_scope == "owner_background" else "stage_timeout_seconds"
        for field, fallback in (("timeout_seconds", time_key), ("profile_id", "profile_id")):
            if prefix + field not in values:
                values[prefix + field] = values[fallback]
                origins[prefix + field] = f"inherit:{fallback}:{origins[fallback]}"
        sources.update({prefix + field: origins[prefix + field] for field in ("mode", "timeout_seconds", "profile_id")})
        seconds = values[prefix + "timeout_seconds"]
        points[point] = {field: values[prefix + field] for field in ("mode", "timeout_seconds", "profile_id")}
        points[point].update(
            runtime_scope=runtime_scope, enabled=values["enabled"], enabled_source=origins["enabled"],
            effective_mode=points[point]["mode"] if values["enabled"] else "off",
            max_request_seconds=min(seconds, values[budget_key]),
            limiting_field=budget_key if values[budget_key] < seconds else prefix + "timeout_seconds",
            connection=_profile_status(context, data, points[point]["profile_id"]),
        )
    return {"ok": True, "schema": "decision_settings_view.v1", "scope": scope,
            "thread_id": str(getattr(thread, "thread_id", "")),
            "revision": {"owner": owner["revision"], "thread": temporary["revision"]},
            "overrides": {"owner": owner["overrides"], "thread": temporary["overrides"]},
            "effective": {**general, "points": points}, "sources": sources, "field_scopes": field_scopes,
            "effective_from": "next_request", "stage_budget_policy": "preserve_started_stage",
            "temporary_until": "reset_or_thread_end" if thread is not None else ""}
