# LLM: OAuth 只写原 owner 目录，所有保存含刷新均轮换目录代次；登录会话代次仍按原 CAS，网络始终在配置锁外。
# 模块用途: 管理登录和令牌刷新，并让旧模型建议识别凭据变化，不创建第二份认证状态或后台线程。
from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

from ..common.json_io import locked_json_path
from .model_oauth_schema import has_credential, oauth_binding, stored_oauth
from .model_oauth_wire import poll_device, refresh_tokens, start_device
from .model_provider_schema import ModelProfileError, validate_provider_id


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
    if pending:
        result.update(status="pending", attempt_id=pending["id"], interval=pending.get("interval", 5),
                      expires_at=pending.get("expires_at", 0))
        if reveal_code:
            result.update(user_code=pending.get("user_code", ""), verification_uri=pending.get("verification_uri", ""))
    return result


# LLM: CAS 比较配置绑定及登录请求编号；旧网页授权不能覆盖新请求或恢复已退出账号。
# 函数用途: 在配置未变且请求仍有效时提交登录状态。
def _commit(path: Path, provider_id: str, binding: str, attempt: str, update: dict) -> dict:
    from .model_profiles import _save_profiles

    with locked_json_path(path):
        data, row = _provider(path, provider_id)
        if not row["enabled"] or oauth_binding(row) != binding or row["auth"].get("pending", {}).get("id") != attempt:
            raise ModelProfileError("登录已取消或配置已变化，迟到结果未保存。")
        draft = {**row["auth"], **update}
        if draft.get("pending") is None:
            draft.pop("pending", None)
        row["auth"] = stored_oauth(draft, row["api_base"])
        _save_profiles(path, data)
        return _projection(row["auth"], reveal_code=True)


# LLM: 每服务商一个登录请求；取消及启动失败只清匹配请求，退出才清凭据换代，网络不持有文件锁。
# 函数用途: 执行用户明确选择的认证动作；空闲不会自行登录或调用模型。
def execute_oauth(agent: object, operation: str, payload: dict) -> dict:
    from .model_profiles import _save_profiles, model_profiles_path

    path = model_profiles_path(agent.home_paths)
    provider_id = validate_provider_id(payload.get("provider_id"))
    with locked_json_path(path):
        data, row = _provider(path, provider_id)
        auth = row["auth"]
        binding = oauth_binding(row)
        if operation == "auth_status":
            return _projection(auth)
        if operation == "auth_parameters":
            return {"ok": True, "parameters": {key: auth.get(key, "") for key in
                ("mode", "client_id", "device_url", "token_url", "scope", "audience")}}
        if operation in {"auth_cancel", "auth_logout"}:
            if operation == "auth_logout":
                for field in ("access_token", "refresh_token", "account_id", "expires_at", "pending"):
                    auth.pop(field, None)
                auth["generation"] = uuid4().hex
            elif payload.get("attempt_id") == auth.get("pending", {}).get("id"):
                auth.pop("pending", None)
            _save_profiles(path, data)
            return _projection(auth)
        if not row["enabled"]:
            raise ModelProfileError("服务商已停用，请先启用再登录。")
        if operation == "auth_start":
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
        return _commit(path, provider_id, binding, attempt, {"pending": pending})
    status, fields = poll_device(auth, pending)
    if status == "connected":
        return _commit(path, provider_id, binding, attempt, {**fields, "generation": uuid4().hex, "pending": None})
    pending["interval"] += 5 if status == "slow_down" else 0
    pending["next_poll_at"] = time.time() + pending["interval"]
    return _commit(path, provider_id, binding, attempt, {"pending": pending})


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
            fields = refresh_tokens(auth)
            with locked_json_path(path):
                data, current = _provider(path, provider_id)
                _check_ref(current, ref, api_base)
                current["auth"] = stored_oauth({**current["auth"], **fields}, current["api_base"])
                _save_profiles(path, data)
                auth = current["auth"]
        headers = {"ChatGPT-Account-ID": auth["account_id"]} if auth["mode"] == "chatgpt" else {}
        return auth["access_token"], headers


# LLM: 退出、停用、改配置或重新登录都会使旧引用失效；这不是模型回退或自动选择另一个账号。
# 函数用途: 防止长期运行和并发请求混用旧账号或新端点。
def _check_ref(row: dict, ref: dict, api_base: str) -> None:
    if (not row["enabled"] or not has_credential(row) or row["api_base"] != api_base
            or oauth_binding(row) != ref.get("binding") or row["auth"].get("generation") != ref.get("generation")):
        raise ModelProfileError("登录已退出、被替换或配置已变化；请在 /model 确认后重试，未自动换用其他账号。")
