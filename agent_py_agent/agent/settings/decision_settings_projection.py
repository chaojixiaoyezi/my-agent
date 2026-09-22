# LLM: 此模块只投影原覆盖和连接可用性，不返回凭据、不发请求、不拥有运行中阶段时钟。
# 模块用途: 为界面和模型工具读回相同有效值、继承来源与决策上限。
from __future__ import annotations

from .decision_settings_defaults import decision_defaults
from .decision_settings_schema import POINTS, validate_decision_settings
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


# LLM: 缺点覆盖时动态继承有效通用值；关闭总开关不改变保存模式，时间上限不包含运行中阶段余额。
# 函数用途: 读回字段来源、持久覆盖和可用上限，调用方仍须与已冻结阶段/自身剩余期限取最小值。
def decision_settings_projection(context: object, data: dict, thread: object = None, *, scope: str = "owner") -> dict:
    from .decision_settings_schema import empty_decision_settings

    owner = validate_decision_settings(data["decision_settings"])
    temporary = validate_decision_settings(thread.decision_settings) if thread is not None else empty_decision_settings()
    effective, sources = decision_defaults(context)
    for name, settings in (("owner", owner), ("thread", temporary)):
        for key, value in settings["overrides"].items():
            effective[key], sources[key] = value, name
    general = {key: value for key, value in effective.items() if not key.startswith("points.")}
    points = {}
    for point in POINTS:
        prefix = f"points.{point}."
        time_key = "background_timeout_seconds" if point == "curator" else "timeout_seconds"
        for field, fallback in (("timeout_seconds", time_key), ("profile_id", "profile_id")):
            if prefix + field not in effective:
                effective[prefix + field] = effective[fallback]
                sources[prefix + field] = f"inherit:{fallback}:{sources[fallback]}"
        seconds = effective[prefix + "timeout_seconds"]
        points[point] = {field: effective[prefix + field] for field in ("mode", "timeout_seconds", "profile_id")}
        points[point].update(
            effective_mode=points[point]["mode"] if effective["enabled"] else "off",
            max_request_seconds=min(seconds, effective["stage_timeout_seconds"]),
            limiting_field="stage_timeout_seconds" if effective["stage_timeout_seconds"] < seconds else prefix + "timeout_seconds",
            connection=_profile_status(context, data, points[point]["profile_id"]),
        )
    return {"ok": True, "schema": "decision_settings_view.v1", "scope": scope,
            "thread_id": str(getattr(thread, "thread_id", "")),
            "revision": {"owner": owner["revision"], "thread": temporary["revision"]},
            "overrides": {"owner": owner["overrides"], "thread": temporary["overrides"]},
            "effective": {**general, "points": points}, "sources": sources,
            "effective_from": "next_request", "stage_budget_policy": "preserve_started_stage",
            "temporary_until": "reset_or_thread_end" if thread is not None else ""}
