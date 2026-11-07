# LLM: OAuth 只写原 owner 目录，所有保存含刷新均轮换目录代次；登录会话代次仍按原 CAS，网络始终在配置锁外。
# 模块用途: 管理登录和令牌刷新，并让旧模型建议识别凭据变化，不创建第二份认证状态或后台线程。
from __future__ import annotations

import hmac
import time
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from ..common.json_io import locked_json_path
from ..user_space.group_authorization_context import group_capability_guard
from .model_oauth_schema import (
    SIWC_CLIENT,
    chatgpt_callback_uri,
    has_credential,
    is_siwc,
    oauth_binding,
    siwc_client_id,
    stored_oauth,
)
from .model_oauth_wire import (
    browser_authorize,
    exchange_browser_code,
    poll_device,
    refresh_tokens,
    require_siwc_verifier,
    revoke_session,
    start_device,
)
from .model_provider_schema import ModelProfileError, validate_provider_id

BROWSER_OPERATIONS = frozenset({"auth_browser_start", "auth_browser_complete"})


# LLM: 延迟导入避免 schema/profiles 循环；锁与原子写仍使用配置模块原实现。
# 函数用途: 读取私有模型文件中的某个启用认证服务商。
def _provider(path: Path, provider_id: str) -> tuple[dict, dict]:
    from .model_profiles import read_model_profiles

    data = read_model_profiles(path)
    row = data["providers"].get(provider_id)
    if not row or not row.get("auth"):
        raise ModelProfileError("认证服务商不存在，请先在 /model 保存登录配置。")
    return data, row


# LLM: 仅结构化字段可公开，任何 token、device_code、account_id、client_secret 和存储路径都不返回。
# 函数用途: 提供登录状态及用户需要在网页输入的短期验证码。
def _projection(auth: dict, *, reveal_code: bool = False) -> dict:
    pending = auth.get("pending", {})
    result = {"ok": True, "status": "connected" if has_credential({"auth": auth}) else "signed_out"}
    if is_siwc(auth):
        enabled = "chatgpt.tokens.use.direct" in auth.get("scopes", [])
        result["plan_usage_enabled"] = enabled and bool(auth.get("access_token"))
        if auth.get("access_token") and not enabled:
            result.update(status="scope_not_granted", message="已登录 my-agent，但未授予 ChatGPT 套餐使用权限；请重新授权，"
                          "或使用本人的 API Key。用量管理：https://chatgpt.com/settings/usage")
    if pending:
        result.update(status="pending", attempt_id=pending["id"], interval=pending.get("interval", 5),
                      expires_at=pending.get("expires_at", 0))
        if reveal_code:
            result.update(user_code=pending.get("user_code", ""), verification_uri=pending.get("verification_uri", ""))
    return result


# LLM: 手动 OAuth 网络返回后重新查询群授权并持策略锁提交原 CAS；迟到结果、撤权及旧请求不能写回。
# 函数用途: 在配置未变且请求仍有效时提交登录状态。
def _commit(path: Path, provider_id: str, binding: str, attempt: str, update: dict, *, agent) -> dict:
    from .model_profiles import _save_profiles

    with group_capability_guard(agent, "assistant.configure"), locked_json_path(path):
        data, row = _provider(path, provider_id)
        if not row["enabled"] or oauth_binding(row) != binding or row["auth"].get("pending", {}).get("id") != attempt:
            raise ModelProfileError("登录已取消或配置已变化，迟到结果未保存。")
        draft = {**row["auth"], **update}
        if draft.get("pending") is None:
            draft.pop("pending", None)
        row["auth"] = stored_oauth(draft, row["api_base"])
        _save_profiles(path, data)
        return _projection(row["auth"], reveal_code=True)


# LLM: 手动写阶段持群能力锁；读取/取消免新增 grant，自动 refresh 不走此手动入口；每服务商一个请求，网络不持文件锁。
# 函数用途: 执行用户明确选择的认证动作；空闲不会自行登录或调用模型。
def execute_oauth(agent: object, operation: str, payload: dict) -> dict:
    from .model_profiles import _save_profiles, model_profiles_path

    path = model_profiles_path(agent.home_paths)
    provider_id = validate_provider_id(payload.get("provider_id"))
    if operation in BROWSER_OPERATIONS:
        return _browser_login(path, provider_id, operation, payload, agent=agent)
    if operation == "auth_logout":
        return _logout(path, provider_id, agent=agent)
    guard = (nullcontext() if operation in {"auth_status", "auth_parameters", "auth_cancel"}
             else group_capability_guard(agent, "assistant.configure"))
    with guard, locked_json_path(path):
        data, row = _provider(path, provider_id)
        auth = row["auth"]
        binding = oauth_binding(row)
        if operation == "auth_status":
            return _projection(auth)
        if operation == "auth_parameters":
            return {"ok": True, "parameters": {key: auth.get(key, "") for key in
                ("mode", "client_id", "device_url", "token_url", "scope", "audience")}}
        if operation == "auth_cancel":
            if payload.get("attempt_id") == auth.get("pending", {}).get("id"):
                auth.pop("pending", None)
            _save_profiles(path, data)
            return _projection(auth)
        if not row["enabled"]:
            raise ModelProfileError("服务商已停用，请先启用再登录。")
        if operation == "auth_start":
            if is_siwc(auth):
                raise ModelProfileError("官方 SIWC 请使用 Continue with ChatGPT 浏览器授权；未改用其他应用的设备码身份。")
            attempt = uuid4().hex
            auth["pending"] = {"id": attempt}
            _save_profiles(path, data)
        elif operation == "auth_poll":
            pending = auth.get("pending", {})
            attempt = str(payload.get("attempt_id") or "")
            if not attempt or attempt != pending.get("id") or not pending.get("device_code"):
                raise ModelProfileError("登录请求不存在或已取消。")
            if time.time() >= pending["expires_at"]:
                auth.pop("pending", None)
                _save_profiles(path, data)
                return {"ok": False, "status": "expired", "message": "登录验证码已过期，请重新登录。"}
            if time.time() < pending.get("next_poll_at", 0):
                return _projection(auth)
            # 抢占这一次轮询时间，多个客户端不会同时兑换同一设备码。
            pending["next_poll_at"] = time.time() + pending["interval"] + 15
            _save_profiles(path, data)
        else:
            raise ModelProfileError("未知认证操作。")
    if operation == "auth_start":
        try:
            pending = {"id": attempt, **start_device(auth)}
        except Exception:
            execute_oauth(agent, "auth_cancel", {"provider_id": provider_id, "attempt_id": attempt})
            raise
        pending["next_poll_at"] = time.time() + pending["interval"]
        return _commit(path, provider_id, binding, attempt, {"pending": pending}, agent=agent)
    status, fields = poll_device(auth, pending)
    if status == "connected":
        return _commit(path, provider_id, binding, attempt, {**fields, "generation": uuid4().hex, "pending": None}, agent=agent)
    pending["interval"] += 5 if status == "slow_down" else 0
    pending["next_poll_at"] = time.time() + pending["interval"]
    return _commit(path, provider_id, binding, attempt, {"pending": pending}, agent=agent)


# LLM: 浏览器登录与设备码共用唯一 pending 槽位和 _commit 的 CAS：开始只写本次 PKCE 等待状态（不联网），完成时锁内核对
#   请求编号、期限与 state，锁外兑换一次授权码，再按配置绑定和请求编号提交；state 不符不清等待状态，伪造回调不能打断真实登录。
#   只支持已启用的 ChatGPT 订阅服务商；通用 OAuth 仍走设备码。改动须同步 test_model_oauth 的浏览器登录用例。
# 函数用途: 处理 TUI 发起的 ChatGPT 浏览器登录的开始与完成两步；会写 owner 私有模型目录，完成时向授权服务器换一次令牌。
def _browser_login(path: Path, provider_id: str, operation: str, payload: dict, *, agent) -> dict:
    from .model_profiles import _save_profiles

    with group_capability_guard(agent, "assistant.configure"), locked_json_path(path):
        data, row = _provider(path, provider_id)
        auth = row["auth"]
        if not row["enabled"] or auth["mode"] != "chatgpt":
            raise ModelProfileError("浏览器登录只支持已启用的 ChatGPT 订阅服务商。")
        if is_siwc(auth) and getattr(agent.home_paths, "owner_kind", "user") == "group":
            raise ModelProfileError("SIWC 只允许本人的私有账号使用，不能把个人 ChatGPT 套餐存入群共享空间。")
        if operation == "auth_browser_start":
            if is_siwc(auth):
                require_siwc_verifier()
            redirect = chatgpt_callback_uri(payload.get("redirect_uri"),
                issued_client=is_siwc(auth) and auth["client_id"] != SIWC_CLIENT)
            if is_siwc(auth) and not auth.get("ext_agent_host_id"):
                auth["ext_agent_host_id"] = "urn:uuid:" + str(uuid4())
            url, pending = browser_authorize(auth, redirect)
            auth["pending"] = {"id": uuid4().hex, **pending}
            _save_profiles(path, data)
            return {**_projection(auth), "authorize_url": url, "state": pending["state"]}
        binding = oauth_binding(row)
        pending = dict(auth.get("pending", {}))
        problem = _browser_callback_problem(pending, payload)
        if problem == "expired":
            auth.pop("pending", None)
            _save_profiles(path, data)
            raise ModelProfileError("浏览器登录已超时，请重新登录。")
        if problem:
            raise ModelProfileError(problem)
        if is_siwc(auth):
            supplied = payload.get("client_id")
            if auth["client_id"] == SIWC_CLIENT:
                pending["issued_client_id"] = siwc_client_id(supplied, issued=True)
            elif supplied and supplied != auth["client_id"]:
                raise ModelProfileError("SIWC 回调的 Client ID 与所选账号登记不一致，未兑换令牌。")
            else:
                pending["issued_client_id"] = auth["client_id"]
    try:
        fields = exchange_browser_code(auth, pending, str(payload["code"]))
    except ModelProfileError as error:
        if (is_siwc(auth) and auth["client_id"] == SIWC_CLIENT
                and error.reason == "authorization_code_invalid"):
            # 仅保留本次登记元数据，不采用身份或令牌；原CAS同时保护取消及后来发起的登录。
            _commit(path, provider_id, binding, pending["id"],
                    {"client_id": pending["issued_client_id"], "pending": None}, agent=agent)
        raise
    return _commit(path, provider_id, binding, pending["id"], {**fields, "generation": uuid4().hex, "pending": None}, agent=agent)


# LLM: 只比结构化字段：请求编号、期限、state（常量时间比较）和授权码形状；不解析网页或错误文字。
# 函数用途: 判断浏览器带回的登录结果能否兑换；可以兑换返回空串，超时返回 "expired"，其它返回给用户看的原因。
def _browser_callback_problem(pending: dict, payload: dict) -> str:
    if pending.get("flow") != "browser" or not pending.get("id") or payload.get("attempt_id") != pending["id"]:
        return "浏览器登录请求不存在或已取消，请重新登录。"
    if time.time() >= pending["expires_at"]:
        return "expired"
    if not hmac.compare_digest(str(payload.get("state") or "").encode(), pending["state"].encode()):
        return "登录回调与本次登录不匹配，已拒绝。"
    if payload.get("error"):
        return "ChatGPT 授权未完成，未兑换令牌；可返回 my-agent 重新授权。"
    code = payload.get("code")
    if not isinstance(code, str) or not code or len(code) > 8192 or any(ord(c) < 33 for c in code):
        return "登录回调缺少有效的授权码，请重新登录。"
    return ""


# LLM: auth_ref 保持原登录会话语义；刷新沿 _save_profiles 原事务轮换目录代次，使旧 pending 失效但不伪造换账号。
# 函数用途: 核验本次凭据并按原跨进程锁刷新；网络在目录锁外，不能在 generation guard 内调用。
def request_credentials(ref: dict, api_base: str) -> tuple[str, dict[str, str]]:
    from .model_profiles import _save_profiles

    path = Path(ref["path"])
    provider_id = validate_provider_id(ref["provider_id"])
    refresh_lock = path.with_name(path.name + "." + provider_id + ".refresh")
    with locked_json_path(refresh_lock):
        data, row = _provider(path, provider_id)
        _check_ref(row, ref, api_base)
        auth = row["auth"]
        if auth.get("expires_at", 0) <= time.time() + 60:
            try:
                fields = refresh_tokens(auth)
            except ModelProfileError as exc:
                if is_siwc(auth) and getattr(exc, "reason", "") == "credential_invalid":
                    with locked_json_path(path):
                        data, current = _provider(path, provider_id)
                        _check_ref(current, ref, api_base)
                        _clear_tokens(current["auth"])
                        _save_profiles(path, data)
                raise
            with locked_json_path(path):
                data, current = _provider(path, provider_id)
                _check_ref(current, ref, api_base)
                current["auth"] = stored_oauth({**current["auth"], **fields}, current["api_base"])
                _save_profiles(path, data)
                auth = current["auth"]
        headers = {"ChatGPT-Account-ID": auth["account_id"]} if auth["mode"] == "chatgpt" and not is_siwc(auth) else {}
        return auth["access_token"], headers


# LLM: 退出、停用、改配置或重新登录都会使旧引用失效；这不是模型回退或自动选择另一个账号。
# 函数用途: 防止长期运行和并发请求混用旧账号或新端点。
def _check_ref(row: dict, ref: dict, api_base: str) -> None:
    if (not row["enabled"] or not has_credential(row) or row["api_base"] != api_base
            or oauth_binding(row) != ref.get("binding") or row["auth"].get("generation") != ref.get("generation")):
        raise ModelProfileError("登录已退出、被替换或配置已变化；请在 /model 确认后重试，未自动换用其他账号。")


# LLM: 只清当前会话凭据，保留签发client/已验证身份、稳定host及正在进行的授权；代次使旧工作片失效。
# 函数用途: 在现有原子保存事务中清除可用凭据。
def _clear_tokens(auth: dict) -> None:
    for field in ("access_token", "refresh_token", "id_token", "account_id", "expires_at"):
        auth.pop(field, None)
    auth["generation"] = uuid4().hex


# LLM: 撤销网络在配置锁外；重进锁核对原登记、会话代次与pending编号，迟到退出不能删掉用户后来发起的新登录。
# 函数用途: 尝试撤销远端会话后本地退出；撤销未确认也清令牌并提示到ChatGPT设置断开。
def _logout(path: Path, provider_id: str, *, agent) -> dict:
    from .model_profiles import _save_profiles

    with group_capability_guard(agent, "assistant.configure"), locked_json_path(path):
        _data, row = _provider(path, provider_id)
        auth = dict(row["auth"])
        expected = (oauth_binding(row), auth.get("generation"), auth.get("pending", {}).get("id"))
    confirmed = revoke_session(auth)
    with group_capability_guard(agent, "assistant.configure"), locked_json_path(path):
        data, row = _provider(path, provider_id)
        current = row["auth"]
        if expected != (oauth_binding(row), current.get("generation"), current.get("pending", {}).get("id")):
            raise ModelProfileError("退出期间账号状态已变化，未清除后来发起的新登录；请确认当前账号。")
        _clear_tokens(current)
        current.pop("pending", None)
        _save_profiles(path, data)
        result = _projection(current)
    if is_siwc(auth):
        result["remote_revocation_confirmed"] = confirmed
        if confirmed is False:
            result["message"] = "已清除本地凭据；远端撤销未确认，可在 ChatGPT 设置中断开应用：https://chatgpt.com/settings/usage"
    return result
