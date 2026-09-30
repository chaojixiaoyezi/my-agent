# LLM: 只有 /model 显式 discover/probe 能进入本模块；无 Agent、工具、任务队列或历史写入，错误不包含请求秘密。
# 模块用途: 获取模型目录或发送一次短问候来验证接口，复用正式 HTTP 适配器。
from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace

from ..backends import get_backend
from ..backends.gateway_helpers import GatewayRequest, get_json
from ..backends.provider_headers import provider_runtime_scope, request_headers
from .model_oauth_schema import CHATGPT_CATALOG_CLIENT_VERSION
from .model_provider_schema import (
    ModelProfileError,
    resolved_model,
    validate_model,
    validate_provider,
)
from .shared_model_catalog import resolve_shared_model, shared_profile_key


# LLM: 目录只投影 ID/显式容量；OAuth 只在显式动作解析引用。ChatGPT 订阅读订阅接口自己的 /models 目录（不猜普通 /v1/models）。
# 函数用途: 读取服务商模型目录，供用户挑选后填写接口和上下文；订阅账号直接给出可一键添加的完整条目。
def _discover(provider: dict, *, auth_ref: dict | None = None) -> dict:
    account_headers = {}
    if auth_ref:
        from .model_oauth import request_credentials

        key, account_headers = request_credentials(auth_ref, provider["api_base"])
        if auth_ref["mode"] == "chatgpt":
            return _subscription_catalog(provider, key, account_headers)
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


# LLM: 只读订阅目录的结构化字段（slug、display_name、context_window、visibility），服务商标成隐藏的不列；
#   带账号头与令牌、不跟随重定向；容量不合法的条目不给出，避免一键添加出无效模型。一次 GET，不调用模型、不写配置。
# 函数用途: 列出 ChatGPT 订阅账号可用的模型（名称、显示名、上下文），供 TUI 勾选后直接添加。
def _subscription_catalog(provider: dict, key: str, account_headers: dict) -> dict:
    headers = request_headers({"Authorization": "Bearer " + key, "Accept": "application/json", **account_headers},
                              provider["custom_headers"], provider["session_header"])
    obj = get_json(GatewayRequest(api_base=provider["api_base"], api_key=key,
                                  path="/models?client_version=" + CHATGPT_CATALOG_CLIENT_VERSION, payload={},
                                  headers=headers, timeout=15, connect_timeout=8, allow_redirects=False))
    items = obj.get("models") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        raise ModelProfileError("订阅接口没有返回模型列表，请稍后重试。")
    rows = [_subscription_row(item) for item in items[:500] if isinstance(item, dict) and item.get("visibility") != "hide"]
    rows = [row for row in rows if row]
    return {"ok": True, "models": rows, "message": f"这个账号可用 {len(rows)} 个模型。"}


# LLM: 形状不对的条目丢弃而不是猜；容量沿用 validate_model 的 4096..2^31-1 口径。
# 函数用途: 把订阅目录的一条记录整理成可直接保存的模型条目，不合规返回 None。
def _subscription_row(item: dict) -> dict | None:
    slug, window = item.get("slug"), item.get("context_window")
    if not isinstance(slug, str) or not slug or len(slug) > 200 or any(ord(c) < 33 for c in slug):
        return None
    if isinstance(window, bool) or not isinstance(window, int) or not 4096 <= window <= 2**31 - 1:
        return None
    name = item.get("display_name")
    display = name if isinstance(name, str) and name and len(name) <= 80 and name.isprintable() else slug
    return {"model_name": slug, "display_name": display, "model_context_window_tokens": window,
            "model_backend": "openai_responses"}


# LLM: 短测复用可信 owner 解析与正式认证后端；仅完整正文算连接可用，不能宣称任务/Plus/Pro 权益已全部验证。
# 函数用途: 使用已保存配置发一个问候并返回耗时、少量正文和 token 数，不保存聊天历史。
def _probe(agent: object, data: dict, profile_id: str) -> dict:
    if profile_id not in data["profiles"]:
        raise ModelProfileError("请先保存并选择要测试的模型。")
    from .model_profiles import _resolved_profile

    row = _resolved_profile(agent, data, profile_id)
    # 输出上限沿用正式请求的值（后端工厂按窗口夹取），短测才能发现供应商不接受这个上限的情况。
    config = replace(agent.config, **row, api_key_env="", request_timeout=60, stream_enabled=True)
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
#   discover 可带未保存的 connection（新增模型先拉列表再保存），该连接同样纳入报错脱敏。
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
            if payload.get("connection") is not None and not payload.get("provider_id"):
                from .model_connections import connection_provider

                # 未保存的连接只在内存里参与本次目录读取和报错脱敏，不写入配置。
                provider = connection_provider(payload["connection"])
                data = {**data, "providers": {**data["providers"], "unsaved-connection": provider}}
                return _discover(provider)
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
