# LLM: 有效层与字段来源遵守 schema 同一范围登记；后台不叠加线程值，纯开关入口不能调用连接/目录读取。
# 模块用途: 提供设置完整投影与宿主已有读取的零 I/O 开关检查，不返回凭据或拥有运行中时钟。
from __future__ import annotations

from .decision_settings_defaults import (
    POINT_TIMEOUT_FLOOR_SECONDS,
    decision_config_fields,
    decision_defaults,
)
from .decision_settings_schema import (
    POINT_RUNTIME_SCOPES,
    decision_field_scopes,
    decision_point_fields,
    empty_decision_settings,
    validate_decision_settings,
)
from .model_provider_schema import ModelProfileError, resolved_model
from .shared_model_catalog import resolve_shared_model, shared_profile_key


# LLM: 值已由原配置与覆盖 schema 校验；范围只读同一登记，不检查连接、共享目录或任何文件。
# 函数用途: 计算一个接入点是否开启，供完整投影与零 I/O 宿主快退共用。
def _effective_point_mode(values: dict, point: str) -> str:
    return values[f"points.{point}.mode"] if values["enabled"] else "off"


# LLM: 只支持 AgentConfig 所属点，未知配置归属明确拒绝；原开关与覆盖按同一范围表叠加，不调用会读盘的完整投影。
# 函数用途: 从宿主已冻结的 owner 设置和刚读出的线程计算开关，不读取候选、凭据或后台配置文件。
def decision_point_mode_from_read(config: object, owner_settings: dict, thread: object, *, point: str) -> str:
    from .decision_settings_schema import validate_decision_field
    from .defaults import default_config_value

    fields = decision_config_fields()
    paths = ("enabled", f"points.{point}.mode")
    if point not in POINT_RUNTIME_SCOPES or any(fields[path][0] != "agent" for path in paths):
        raise ModelProfileError("此接入点不能使用 AgentConfig 的只读开关投影。")
    values = {path: validate_decision_field(path, getattr(config, fields[path][1], default_config_value(fields[path][1])))
              for path in paths}
    layers = [validate_decision_settings(owner_settings)["overrides"]]
    if thread is not None and POINT_RUNTIME_SCOPES[point] == "thread":
        layers.append(validate_decision_settings(thread.decision_settings)["overrides"])
    for layer in layers:
        values.update({path: layer[path] for path in paths if path in layer})
    return _effective_point_mode(values, point)


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


# LLM: 原地补齐点位缺的单次期限与模型引用（会改 values/origins）。覆盖层已有的点位值一律不动；单次期限缺省时，
#   有登记下限（POINT_TIMEOUT_FLOOR_SECONDS）且下限大于通用期限就取下限、来源记 point_default:<秒>，
#   其余按原作用层继承通用值、来源记 inherit:<通用字段>:<原来源>。改动须同步 test_decision_settings.py 与 TUI _source。
# 函数用途: 给每个点位补上没被覆盖的期限和模型；选模型等点位先套仓库默认的单次期限下限，再继承通用值。
def _fill_point_fields(values: dict, origins: dict, point: str, time_key: str) -> None:
    prefix, floor = f"points.{point}.", POINT_TIMEOUT_FLOOR_SECONDS.get(point)
    if prefix + "timeout_seconds" not in values and floor is not None and floor > values[time_key]:
        values[prefix + "timeout_seconds"], origins[prefix + "timeout_seconds"] = floor, f"point_default:{floor:g}"
    for field, fallback in (("timeout_seconds", time_key), ("profile_id", "profile_id")):
        if prefix + field not in values:
            values[prefix + field] = values[fallback]
            origins[prefix + field] = f"inherit:{fallback}:{origins[fallback]}"


# LLM: 缺点值按原作用层继承；实验授权只读当前 thread 的完整信封，绝不拼接 owner/thread 或从开关推导许可。
#   静态等待上限与 decision_service._point_deadline 同口径：前台点位取点位预算与 stage_timeout_seconds 的较小值；
#   普通后台点位只看自己的 timeout_seconds（缺省继承 background_timeout_seconds），不再受阶段预算封顶；
#   observe 转后台的会话点位（observe_nonblocking_enabled）只看 background_timeout_seconds，并标 blocking=false。
# 函数用途: 返回可写范围、真实字段来源和静态等待上限；前台运行服务仍须扣除原阶段已耗时间，后台点位各自完整计时。
def decision_settings_projection(context: object, data: dict, thread: object = None, *, scope: str = "owner") -> dict:
    owner = validate_decision_settings(data["decision_settings"])
    temporary = validate_decision_settings(thread.decision_settings) if thread is not None else empty_decision_settings()
    if owner["experiment_authorization"] is not None:
        raise ModelProfileError("实验授权首片只支持准确会话，用户长期层中的授权不能使用。")
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
        budget_key = prefix + "timeout_seconds" if runtime_scope == "owner_background" else "stage_timeout_seconds"
        _fill_point_fields(values, origins, point, time_key)
        sources.update({prefix + field: origins[prefix + field] for field in decision_point_fields(point)})
        seconds = values[prefix + "timeout_seconds"]
        mode = _effective_point_mode(values, point)
        # 与 decision_service._nonblocking 同口径：会话点位处于 observe 且开关打开时转后台，上限只看后台单次等待，调用方不等。
        nonblocking = runtime_scope == "thread" and mode == "observe" and values["observe_nonblocking_enabled"] is True
        if nonblocking:
            seconds, budget_key = values["background_timeout_seconds"], "background_timeout_seconds"
        points[point] = {field: values[prefix + field] for field in decision_point_fields(point)}
        points[point].update(
            runtime_scope=runtime_scope, enabled=values["enabled"], enabled_source=origins["enabled"],
            effective_mode=mode, blocking=not nonblocking,
            max_request_seconds=min(seconds, values[budget_key]),
            limiting_field=budget_key if nonblocking or values[budget_key] < seconds else prefix + "timeout_seconds",
            connection=_profile_status(context, data, points[point]["profile_id"]),
        )
    return {"ok": True, "schema": "decision_settings_view.v1", "scope": scope,
            "thread_id": str(getattr(thread, "thread_id", "")),
            "revision": {"owner": owner["revision"], "thread": temporary["revision"]},
            "overrides": {"owner": owner["overrides"], "thread": temporary["overrides"]},
            "experiment_authorization": temporary["experiment_authorization"],
            "effective": {**general, "points": points}, "sources": sources, "field_scopes": field_scopes,
            "effective_from": "next_request", "stage_budget_policy": "preserve_started_stage",
            "temporary_until": "reset_or_thread_end" if thread is not None else ""}
