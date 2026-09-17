# LLM: provider/model 的结构和采样校验只有这个事实源；公开投影不能包含 key 或自定义头值。
# 模块用途: 校验服务商、模型引用与可选采样，让多个模型共享一份私有连接配置。
from __future__ import annotations

import math
import re
from urllib.parse import urlsplit

from ..backends.provider_headers import validate_headers, validate_session_header
from ..backends.sampling import validate_top_p

SCHEMA = "owner_model_profiles.v2"
BACKENDS = {"openai_compatible", "anthropic_compatible", "openai_responses"}


# LLM: 此类错误必须仅使用固定脱敏文案；HTTP/TUI 可直接公开。
# 类用途: 表示用户可以在模型表单中修正的配置错误。
class ModelProfileError(ValueError):
    pass


# LLM: 地址只接受显式 HTTP(S)，不接受内嵌认证/query；不对内网地址做模型名称猜测。
# 函数用途: 检查基础地址并保留用户配置的路径前缀。
def validate_base_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 33 for c in value):
        raise ModelProfileError("接口地址不能为空或含有空白/控制字符。")
    try:
        url = urlsplit(value)
        valid = url.scheme in {"http", "https"} and bool(url.hostname) and not (url.username or url.password or url.query or url.fragment)
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise ModelProfileError("地址须为 http(s) 接口基础地址，不要包含密钥、查询参数或片段。")
    return value.rstrip("/")


# LLM: ID 是 owner 文件内稳定键，不是目录；后续编辑必须保持 ID 不变。
# 函数用途: 校验服务商的短编号，避免不可见字符及歧义。
def validate_provider_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ModelProfileError("Provider ID 请使用 1 至 96 个字母、数字、点、横线或下划线。")
    return value


# LLM: 保留/清除 secret 是明确字段操作；OAuth 私有状态只校验不公开，表单入口另拦状态注入。
# 函数用途: 检查服务商配置；允许无密钥保存草稿，发请求前另行检查。
def validate_provider(value: object) -> dict:
    if not isinstance(value, dict):
        raise ModelProfileError("服务商配置须为对象。")
    name = value.get("display_name", "")
    key = value.get("api_key", "")
    if any(not isinstance(text, str) or len(text) > 4096 or any(ord(c) < 32 for c in text) for text in (name, key)):
        raise ModelProfileError("展示名或密钥格式不合法。")
    if not name.strip():
        raise ModelProfileError("服务商展示名不能为空。")
    enabled = value.get("enabled", True)
    capabilities = value.get("capabilities", ["agentic"])
    if type(enabled) is not bool or not isinstance(capabilities, list) or not capabilities or any(c not in {"agentic", "embedding"} for c in capabilities):
        raise ModelProfileError("请明确选择启用状态及 Agentic/Embedding 能力。")
    try:
        headers = validate_headers(value.get("custom_headers", {}))
        session = validate_session_header(value.get("session_header", ""))
    except ValueError as exc:
        raise ModelProfileError(str(exc)) from exc
    if session and session.lower() in {key.lower() for key in headers}:
        raise ModelProfileError("会话头请使用专门选项，不要同时填写静态值。")
    result = {"display_name": name.strip(), "api_base": validate_base_url(value.get("api_base")),
            "api_key": key.strip(), "enabled": enabled, "capabilities": sorted(set(capabilities)),
            "custom_headers": headers, "session_header": session}
    if value.get("auth"):
        from .model_oauth_schema import stored_oauth

        if key.strip():
            raise ModelProfileError("同一服务商不能同时保存 API Key 和 OAuth 登录，请分开创建。")
        result["auth"] = stored_oauth(value["auth"], result["api_base"])
    return result


# LLM: 模型引用不持有第二份 secret；采样、容量和排队预算按模型保存，未知型号允许透传。
# 函数用途: 检查模型协议、用途、容量、采样及额外首事件等待；非法数值不能保存或发送。
def validate_model(value: object) -> dict:
    if not isinstance(value, dict) or value.get("model_backend") not in BACKENDS:
        raise ModelProfileError("请选择 OpenAI Chat、OpenAI Responses 或 Anthropic；登录认证在服务商中配置。")
    name = value.get("model_name")
    if not isinstance(name, str) or not name.strip() or len(name) > 4096 or any(ord(c) < 32 for c in name):
        raise ModelProfileError("模型名称不能为空或包含控制字符。")
    window = value.get("model_context_window_tokens")
    if isinstance(window, bool) or not str(window).isascii() or not str(window).isdigit() or not 4096 <= int(window) <= 2**31 - 1:
        raise ModelProfileError("上下文窗口请填写 4096 至 2147483647 之间的整数 tokens。")
    capability = value.get("capability", "agentic")
    if capability not in {"agentic", "embedding"} or type(value.get("enabled", True)) is not bool:
        raise ModelProfileError("模型用途或启用状态不合法。")
    result = {"provider_id": validate_provider_id(value.get("provider_id")), "model_name": name.strip(),
            "model_backend": value["model_backend"], "model_context_window_tokens": int(window),
            "capability": capability, "enabled": value.get("enabled", True)}
    temperature = value.get("temperature")
    if temperature not in (None, ""):
        try:
            number = float(temperature)
        except (TypeError, ValueError) as exc:
            raise ModelProfileError("温度须留空或填写 0 至 2 的数值。") from exc
        if isinstance(temperature, bool) or not math.isfinite(number) or not 0 <= number <= 2:
            raise ModelProfileError("温度须留空或填写 0 至 2 的数值。")
        result["temperature"] = str(number)
    try:
        top_p = validate_top_p(value.get("top_p"))
    except ValueError as exc:
        raise ModelProfileError(str(exc)) from exc
    if top_p is not None:
        result["top_p"] = top_p
    queue = value.get("model_queue_wait_seconds")
    if queue not in (None, ""):
        try:
            seconds = float(queue)
        except (TypeError, ValueError):
            raise ModelProfileError("排队预算须为 0 至 86400 秒。") from None
        if isinstance(queue, bool) or not math.isfinite(seconds) or not 0 <= seconds <= 86400:
            raise ModelProfileError("排队预算须为 0 至 86400 秒。")
        result["model_queue_wait_seconds"] = seconds
    return result


# LLM: 扁平输入立即变成 provider 引用；保留显式采样，未知字段不传入 AgentConfig。
# 函数用途: 校验快捷新增和旧 v1 迁移的完整连接信息，避免该入口悄悄丢掉温度或 top_p。
def validate_model_profile(value: object) -> dict:
    if not isinstance(value, dict):
        raise ModelProfileError("模型配置须为对象。")
    model = validate_model({**value, "provider_id": "validation"})
    provider = validate_provider({"display_name": model["model_name"], **value,
        "custom_headers": value.get("model_custom_headers", {}), "session_header": value.get("model_session_header", "")})
    if not provider["api_key"]:
        raise ModelProfileError("模型名称、地址和密钥不能为空。")
    row = {key: model[key] for key in ("model_name", "model_backend", "model_context_window_tokens", "temperature", "top_p", "model_queue_wait_seconds") if key in model}
    row.update(api_base=provider["api_base"], api_key=provider["api_key"])
    if provider["custom_headers"]:
        row["model_custom_headers"] = provider["custom_headers"]
    if provider["session_header"]:
        row["model_session_header"] = provider["session_header"]
    return row


# LLM: 迁移保持 selected/profile UUID，不写源文件；只在显式保存时把唯一存储升级为 v2。
# 函数用途: 将旧版逐模型密钥转换为一对一服务商，避免覆盖或丢失已有子代理模型引用。
def migrate_v1(data: dict) -> dict:
    migrated = {"schema": SCHEMA, "selected": data["selected"], "providers": {}, "profiles": {}}
    for profile_id, value in data["profiles"].items():
        row = validate_model_profile(value)
        provider_id = "provider-" + profile_id
        migrated["providers"][provider_id] = validate_provider({**row, "display_name": row["model_name"]})
        migrated["profiles"][profile_id] = validate_model({**row, "provider_id": provider_id})
    return migrated


# LLM: 唯一解析点合并连接；OAuth 只返回代次绑定引用，不返回 token，路径由可信 owner 调用方补齐。
# 函数用途: 获得可发送请求的连接和采样配置，不返回展示或管理字段，不发模型请求。
def resolved_model(data: dict, profile_id: str, *, require_enabled: bool = True) -> dict:
    from .model_oauth_schema import has_credential, oauth_binding

    model = data["profiles"][profile_id]
    provider = data["providers"][model["provider_id"]]
    if require_enabled and (not model["enabled"] or not provider["enabled"] or not has_credential(provider)
                            or model["capability"] != "agentic" or "agentic" not in provider["capabilities"]):
        raise ModelProfileError("这个模型或服务商未启用、缺少密钥或尚未登录，或不是 Agentic 模型。")
    result = {**{key: model[key] for key in ("model_name", "model_backend", "model_context_window_tokens", "temperature", "top_p", "model_queue_wait_seconds") if key in model},
            "api_base": provider["api_base"], "api_key": provider["api_key"],
            "model_custom_headers": dict(provider["custom_headers"]), "model_session_header": provider["session_header"]}
    auth = provider.get("auth")
    if auth:
        if auth["mode"] == "chatgpt" and model["model_backend"] != "openai_responses":
            raise ModelProfileError("ChatGPT 订阅登录需要 OpenAI Responses 接口。")
        result["model_auth_ref"] = {"provider_id": model["provider_id"], "mode": auth["mode"],
                                   "generation": auth.get("generation", ""), "binding": oauth_binding(provider)}
    else:
        result["model_auth_ref"] = {}
    return result
