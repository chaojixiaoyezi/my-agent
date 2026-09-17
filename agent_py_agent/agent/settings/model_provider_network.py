# LLM: 只有 /model 显式 discover/probe 能进入本模块；无 Agent、工具、任务队列或历史写入，错误不包含请求秘密。
# 模块用途: 获取模型目录或发送一次短问候来验证接口，复用正式 HTTP 适配器。
from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace

from ..backends import get_backend
from ..backends.gateway_helpers import GatewayRequest, get_json
from ..backends.provider_headers import provider_runtime_scope, request_headers
from .model_provider_schema import (
    ModelProfileError,
    resolved_model,
    validate_model,
    validate_provider,
)
from .shared_model_catalog import resolve_shared_model, shared_profile_key


# LLM: 目录只投影 ID/显式容量；OAuth 只在显式动作解析引用，订阅不猜普通 /v1/models 路径。
# 函数用途: 读取服务商模型目录，供用户挑选后填写接口和上下文。
def _discover(provider: dict, *, auth_ref: dict | None = None) -> dict:
    account_headers = {}
    if auth_ref:
        from .model_oauth import request_credentials

        if auth_ref["mode"] == "chatgpt":
            raise ModelProfileError("订阅模型请按账号可用目录手动填写名称和容量；不会用普通 API 目录探测订阅凭据。")
        key, account_headers = request_credentials(auth_ref, provider["api_base"])
        provider = {**provider, "api_key": key}
    base = provider["api_base"]
    for suffix in ("/chat/completions", "/responses", "/messages"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    path = "/models" if base.endswith("/v1") else "/v1/models"
    headers = request_headers({"Authorization": "Bearer " + provider["api_key"], "x-api-key": provider["api_key"],
                               "Accept": "application/json", **account_headers}, provider["custom_headers"], provider["session_header"])
    if auth_ref:
        headers.pop("x-api-key", None)
    obj = get_json(GatewayRequest(api_base=base, api_key=provider["api_key"], path=path, payload={},
                                 headers=headers, timeout=15, connect_timeout=8, allow_redirects=not bool(auth_ref)))
    items = obj.get("data", obj.get("models", []))
    if not isinstance(items, list):
        raise ModelProfileError("接口未返回有效的模型列表，可手动填写模型名称。")
    from ..backends.model_metadata import _context_window_from_record

    rows = [{"model_name": row["id"], "model_context_window_tokens": _context_window_from_record(row)}
            for row in items[:2000] if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]]
    return {"ok": True, "models": rows, "message": f"读取到 {len(rows)} 个模型；接口类型和容量仍需确认。"}


# LLM: 短测复用可信 owner 解析与正式认证后端；仅完整正文算连接可用，不能宣称任务/Plus/Pro 权益已全部验证。
# 函数用途: 使用已保存配置发一个问候并返回耗时、少量正文和 token 数，不保存聊天历史。
def _probe(agent: object, data: dict, profile_id: str) -> dict:
    if profile_id not in data["profiles"]:
        raise ModelProfileError("请先保存并选择要测试的模型。")
    from .model_profiles import _resolved_profile

    row = _resolved_profile(agent, data, profile_id)
    config = replace(agent.config, **row, api_key_env="", max_tokens=1024, request_timeout=60, stream_enabled=True,
                     model_temperature_explicit="temperature" in row or agent.config.model_temperature_explicit)
    backend = get_backend(config.model_backend, config)
    started = time.monotonic()
    response = backend.generate("你好，请简短回复一句问候。")
    elapsed = round(time.monotonic() - started, 2)
    text = _redact_network_text(str(response.text or ""), _current_redaction_data(agent, data))
    ok = bool(text.strip()) and not response.truncated and not response.tool_use_blocks
    return {"ok": ok, "profile_id": profile_id, "model_name": config.model_name, "elapsed_seconds": elapsed,
            "reply": text[:500], "usage": response.usage, "truncated": response.truncated,
            "message": "已收到正常回复；仅证明基本调用可用，未执行任务。" if ok else "请求已结束，但未得到完整文字回复；不能标记通过。"}


# LLM: 身份来自已认证 owner，共享 probe 只读取已发布的完整快照，纳入相同秘密脱敏；不落盘临时连接数据。
# 函数用途: 在不持有配置文件锁的情况下进行一次用户明确要求的目录/连接测试。
def execute_provider_network(agent: object, data: dict, operation: str, payload: dict) -> dict:
    identity = str(payload.get("session_id") or payload.get("conversation_id") or "model-menu")
    if operation == "probe" and shared_profile_key(payload.get("profile_id")):
        data = _shared_probe_data(agent, data, str(payload["profile_id"]))
    started = time.monotonic()
    model = data["profiles"].get(str(payload.get("profile_id") or ""), {})
    try:
        with provider_runtime_scope(agent, SimpleNamespace(thread_id="model-menu:" + identity)):
            if operation == "probe":
                return _probe(agent, data, str(payload.get("profile_id") or ""))
            provider = data["providers"].get(str(payload.get("provider_id") or ""))
            if not provider or not provider["enabled"]:
                raise ModelProfileError("请先保存并启用服务商。")
            auth_ref = None
            if provider.get("auth"):
                from .model_oauth_schema import oauth_binding
                from .model_profiles import model_profiles_path

                auth_ref = {"path": str(model_profiles_path(agent.home_paths)), "provider_id": str(payload["provider_id"]),
                            "mode": provider["auth"]["mode"], "generation": provider["auth"].get("generation", ""),
                            "binding": oauth_binding(provider)}
            return _discover(provider, auth_ref=auth_ref)
    except ModelProfileError:
        raise
    except Exception as exc:
        return {"ok": False, "message": "接口调用失败，请检查服务商地址、协议、模型和密钥。",
                "model_name": model.get("model_name", ""), "elapsed_seconds": round(time.monotonic() - started, 2),
                "provider_message": _provider_message(exc, _current_redaction_data(agent, data)),
                "error_type": type(exc).__name__, "status_code": int(getattr(exc, "status_code", 0) or 0)}


# LLM: 只构造一次请求的内存快照供 probe 和错误脱敏共用，不能保存至普通用户 provider 文件或返回客户端。
# 函数用途: 让共享模型连接测试复用正式请求与密钥清洗，不另造网络客户端。
def _shared_probe_data(agent: object, data: dict, profile_id: str) -> dict:
    row = resolve_shared_model(agent.home_paths, profile_id)
    provider_id = "shared-" + shared_profile_key(profile_id)
    provider = validate_provider({**row, "display_name": row["model_name"],
        "custom_headers": row.get("model_custom_headers", {}), "session_header": row.get("model_session_header", "")})
    model = validate_model({**row, "provider_id": provider_id})
    return {**data, "profiles": {**data["profiles"], profile_id: model},
            "providers": {**data["providers"], provider_id: provider}}


# LLM: OAuth 可能在探针中刷新令牌；旧/新凭据均参加回显清洗，不能只拿请求前的快照脱敏。
# 函数用途: 请求结束后重读私有凭据作为清洗材料；读取失败不公开未经确认的上游文本。
def _current_redaction_data(agent: object, data: dict) -> dict:
    if not any(row.get("auth") for row in data["providers"].values()):
        return data
    from .model_profiles import model_profiles_path, read_model_profiles

    current = read_model_profiles(model_profiles_path(agent.home_paths))
    return {**data, "providers": {**data["providers"], **{
        "fresh:" + key: row for key, row in current["providers"].items()}}}


# LLM: 上游原因只作为用户诊断材料，不决定恢复/路由；只公开 message/code，统一移除已存密钥、头值和控制字符。
# 函数用途: 让连接失败能看出模型下线、区域限制等具体原因，而不是一律猜配置错。
def _provider_message(exc: Exception, data: dict) -> str:
    detail = getattr(exc, "details", None)
    detail = detail.get("provider_error", {}) if isinstance(detail, dict) else {}
    error = detail.get("error", detail) if isinstance(detail, dict) else {}
    if isinstance(error, dict):
        text = str(error.get("message") or error.get("code") or "")
    else:
        text = str(error) if isinstance(error, str) else ""
    return _redact_network_text(text, data)[:500]


# LLM: 成功回复和异常诊断复用同一秘密清洗，包含本次共享模型的私有快照；不把快照保存或公开。
# 函数用途: 清除上游回显的 API Key、所有自定义请求头值和控制字符。
def _redact_network_text(text: str, data: dict) -> str:
    from ..common.log_redaction import redact_sensitive_text

    for row in data["providers"].values():
        secrets = [row["api_key"], *row["custom_headers"].values(),
                   *[row.get("auth", {}).get(key, "") for key in ("access_token", "refresh_token", "client_secret")]]
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[已隐藏]")
    return "".join(char for char in redact_sensitive_text(text) if ord(char) >= 32)
