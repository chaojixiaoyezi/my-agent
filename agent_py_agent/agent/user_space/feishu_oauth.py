# LLM: 本片只提取原OAuth cancel为显式owner/输入服务；网络、exchange、refresh和已ready授权处理均保持。
# 模块用途: 冷C可取消原本人授权卡，不能借取消新增网络或账号权。
# LLM: 本人授权只绑定可信 owner/actor、部署应用和准确任务，网络遵守实际调用边界；秘密密文留原私密目录，不进入模型或日志。
# 模块用途: 管理飞书本人 OAuth 请求、回调、刷新与撤销，网络在记录锁外执行，沿原输入依赖恢复任务。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..capability.runtime_config_reload import deployment_capability_config
from ..common.authenticated_http import open_authenticated_request
from ..common.json_io import read_json_object_report
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable
from ..conversation.im_tool_approval import approval_recipient
from ..conversation.input_dependencies import (
    create_input_dependency,
    input_dependency_current,
    input_dependency_path,
    resolve_input_dependency,
)
from ..conversation.tool_approval_scope import tool_approval_scope, tool_approval_scope_is_current
from ..delivery import DeliveryContext
from ..path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
from .admin_channel_identity import find_admin_channel_identity
from .interaction_refs import interaction_key, sign_interaction_reference
from .owner_policy import resolve_effective_owner_policy
from .owner_resolver import owner_identity_from_config
from .task_credentials import _cipher, _credential_mutation, _save_credential_row

_API = "https://open.feishu.cn/open-apis"
# 常量用途: 原执行权、有界退出与工具写上下文的未结异常须同对象穿透，不能被本地收口成已知失败。
_UNSETTLED_EXECUTION_ERRORS = (RequestExecutionOutcomeUncertainError, BoundedCallExitUnconfirmedError,
                               RequestExecutionContextUnavailable, ToolWriteContextUnavailable)


class FeishuOAuthError(RuntimeError, ValueError):
    """携带 OAuth 阶段与脱敏原因，兼容旧调用方的 ValueError 捕获；授权卡失败另带本次操作 effect 与送达阶段事实。"""

    def __init__(
        self, message: str, *, phase: str, provider_code: str | None = None,
        reason_code: str = "", original_error_type: str = "",
        effect_outcome: str = "", delivery: dict | None = None,
    ) -> None:
        RuntimeError.__init__(self, message)
        self.phase = phase
        self.provider_code = provider_code
        self.reason_code = reason_code
        self.original_error_type = original_error_type
        self.effect_outcome = effect_outcome
        self.delivery = dict(delivery or {})


# LLM: 只读宿主 deployment 配置，普通 owner 不能扩大 OAuth 范围；未知配置拒绝，不用用户局部 YAML 替代。
# 函数用途: 校验准确 HTTPS 回调、开放开关和部署者声明的 scope。
def oauth_configuration(agent):
    config = deployment_capability_config(agent.home_paths)
    if not config.feishu_user_oauth_enabled:
        raise FeishuOAuthError("本人飞书授权功能未启用。", phase="authorize", reason_code="OAUTH_CONFIGURATION_UNAVAILABLE")
    uri = config.feishu_oauth_redirect_uri
    if not isinstance(uri, str):
        raise FeishuOAuthError("本人授权回调配置无效。", phase="authorize", reason_code="OAUTH_CONFIGURATION_UNAVAILABLE")
    parts = urlsplit(uri)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path != "/feishu/oauth/callback" or parts.query or parts.fragment):
        raise FeishuOAuthError("本人飞书授权回调尚未配置，请部署者登记准确 HTTPS 回调。", phase="authorize", reason_code="OAUTH_CONFIGURATION_UNAVAILABLE")
    scopes = config.feishu_oauth_scopes
    if (not isinstance(scopes, list) or any(not isinstance(value, str) or not value
            or any(character.isspace() for character in value) for value in scopes)):
        raise FeishuOAuthError("本人飞书授权范围配置无效。", phase="authorize", reason_code="OAUTH_CONFIGURATION_UNAVAILABLE")
    return uri, list(dict.fromkeys(scopes))


# LLM: 应用配置只读宿主冻结字段，不能回退到模型参数或局部配置；读取不创建签名库。
# 函数用途: 校验应用标识与密钥是否真实已配置，供 availability 和认证共用。
def _application_credentials(agent):
    app_id, secret = agent.config.feishu_app_id, agent.config.feishu_app_secret
    if not isinstance(app_id, str) or not app_id or not isinstance(secret, str) or not secret:
        raise FeishuOAuthError("宿主飞书应用尚未配置。", phase="authorize", reason_code="OAUTH_CONFIGURATION_UNAVAILABLE")
    return app_id, secret


# LLM: 应用密钥绑定只存宿主密钥派生的摘要；scope/回调/app 在发卡时冻结，变更必须重新授权。
# 函数用途: 取得当前部署认证事实，不输出应用秘密。
def _configuration_facts(agent, *, create_key=False):
    uri, scopes = oauth_configuration(agent)
    app_id, secret = _application_credentials(agent)
    binding = hmac.new(interaction_key(agent.home_paths.owner_home_dir, create=create_key),
                       (app_id + "\0" + secret).encode(), hashlib.sha256).hexdigest()
    return {"app_id": app_id, "app_binding": binding, "redirect_uri": uri, "requested_scopes": scopes}


# LLM: 缺失旧字段也不是兼容豁免，不从新配置补造旧授权范围；改变应用/回调/scope 即拒绝。
# 函数用途: 核对保存授权仍属于准确部署配置。
def _assert_configuration(agent, row):
    facts = _configuration_facts(agent)
    if any(row.get(key) != value for key, value in facts.items()):
        raise ValueError("本人授权的应用、回调或范围已变化，请重新授权。")


# LLM: 主/子/后台使用同一实际网络字段；callback 再读准确 child 的持久权限，不借 owner 宽权限覆盖 task 收紧。
# 函数用途: 在发卡、回调和取 token 前核验网络上界，不执行网络。
def _assert_network(agent, scope=None, *, invocation_mode=None):
    current = getattr(agent, "_current_run_params", None)
    boundary = getattr(current, "write_boundary", None)
    boundary = boundary if isinstance(boundary, dict) else {}
    permissions = boundary.get("effective_permissions") or {}
    if (invocation_mode == "disabled" or boundary.get("network_access_mode") == "disabled"
            or (isinstance(permissions, dict) and permissions.get("network_enabled") is False)
            or not resolve_effective_owner_policy(agent.home_paths).network_enabled
            or getattr(getattr(agent, "owner_policy", None), "network_enabled", True) is False):
        raise FeishuOAuthError("当前任务或用户网络权限不可用。", phase="authorize", reason_code="NETWORK_DISABLED")
    kind = scope.get("agent_kind") if isinstance(scope, dict) else getattr(scope, "agent_kind", "")
    run_id = scope.get("run_id") if isinstance(scope, dict) else getattr(scope, "run_id", "")
    if kind == "subagent":
        task = agent.subagents.load(run_id)
        if (task.effective_permissions or {}).get("network_enabled") is False:
            raise FeishuOAuthError("当前子代理网络权限不可用。", phase="authorize", reason_code="NETWORK_DISABLED")


# LLM: 群不能拥有共享个人身份；普通 owner 只认自身准确 ID，本机管理员还要保持原私人绑定。
# 函数用途: 确定当前用户本人飞书账号，不从显示名、参数或群成员猜身份。
def oauth_actor(agent, scope=None):
    owner = owner_identity_from_config(agent.config)
    if owner.owner_kind == "group":
        raise FeishuOAuthError("群资源使用应用身份；本人授权请在私人助手会话进行。", phase="authorize", reason_code="GROUP_SESSION_OAUTH_UNSUPPORTED")
    if owner.provider == "feishu" and owner.owner_kind == "user":
        return owner.owner_id
    if owner.provider != "local" or owner.owner_kind != "main" or scope is None:
        raise FeishuOAuthError("当前 owner 没有本人飞书身份。", phase="authorize", reason_code="PERSONAL_IDENTITY_UNAVAILABLE")
    channel, actor = approval_recipient(agent, scope)
    if channel != "feishu" or find_admin_channel_identity(agent.home_paths.root, channel, actor) is None:
        raise FeishuOAuthError("当前管理员本人绑定已失效。", phase="authorize", reason_code="PERSONAL_IDENTITY_UNAVAILABLE")
    return actor


# LLM: 回调 owner 与 actor 都来自已验签记录，仍复核当前普通用户或管理员绑定；签名不能保留被撤销的管理员身份。
# 函数用途: 验证原授权发起人仍属于当前 owner。
def _assert_actor(agent, actor):
    owner = owner_identity_from_config(agent.config)
    valid = (owner.provider == "feishu" and owner.owner_kind == "user" and actor == owner.owner_id)
    if owner.provider == "local" and owner.owner_kind == "main":
        valid = find_admin_channel_identity(agent.home_paths.root, "feishu", actor) is not None
    if not valid:
        raise ValueError("本人授权身份已失效。")


# LLM: 文件名只由可信 actor 摘要生成，目录复用原保护；模型没有 ref/路径参数。
# 函数用途: 定位本人密文 OAuth 记录。
# LLM: 原Agent与显式home共用同一路径公式，actor只取已认证事实，不创建目录。
# 函数用途: 返回原本人OAuth记录位置。
def oauth_path(agent, actor):
    return oauth_path_for_home(agent.home_paths.owner_home_dir, actor)


# LLM: 官方认证请求拒绝重定向，原响应仅留宿主；错误不包含 token、URL、供应商正文或异常详情。
# 函数用途: 交换、刷新 token 或核对登录账号。
def _oauth_api(method, path, body=None, token=""):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(_API + path, data=data, method=method, headers=headers)
    phase = "probe"
    if path == "/authen/v2/oauth/token":
        phase = "refresh" if isinstance(body, dict) and body.get("grant_type") == "refresh_token" else "token_exchange"
    provider_code = None
    try:
        with open_authenticated_request(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
        result = json.loads(raw) if len(raw) <= 1024 * 1024 else None
        if isinstance(result, dict) and type(result.get("code")) is int and result["code"] != 0:
            provider_code = str(result["code"])
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError("OAuth 返回无效")
        if "code" in result and (type(result["code"]) is not int or result["code"] != 0):
            raise ValueError("OAuth 返回无效")
        # 官方 v2 OAuth 成功响应可省略 code，必须明确有 access_token；user_info 沿原 API code 合同。
        if path == "/authen/v2/oauth/token":
            if not isinstance(result.get("access_token"), str) or not result["access_token"]:
                raise ValueError("OAuth 没有返回 token")
        elif "code" not in result:
            raise ValueError("本人证明没有返回状态码")
        return result
    except (OSError, ValueError, TypeError):
        raise FeishuOAuthError(
            "飞书本人授权接口未返回可验证结果，请重新发起授权。", phase=phase, provider_code=provider_code,
        ) from None


# LLM: 实际 open_id 必须与宿主 actor 相等；state 中的身份不代替供应商证明。
# 函数用途: 在保存 token 前验证授权账号本人。
def _verify_actor(token, actor):
    result = _oauth_api("GET", "/authen/v1/user_info", token=token)
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict) or data.get("open_id") != actor:
        raise FeishuOAuthError("登录的飞书账号不是原发起人，未保存授权。", phase="probe")


# LLM: 期限只认正整数；bool/字符串/NaN/无限均拒绝，不能凭缺失字段延长 refresh 授权。
# 函数用途: 取得供应商明确声明的 token 有效秒数。
def _expires_seconds(value, *, phase="token_exchange"):
    if type(value) is not int or not 0 < value <= 31536000:
        raise FeishuOAuthError("本人授权有效期无效。", phase=phase)
    return value


# LLM: token 和 scope 只读结构化响应；没有 refresh token 可保留仅 access 授权，有 refresh token 则必须给准确期限。
# 函数用途: 校验响应、验证本人并立即加密 token，不生成默认授权期限或伪造已授 scope。
def _ready_row(agent, row, result, *, phase="token_exchange"):
    if not isinstance(result, dict):
        raise FeishuOAuthError("本人授权响应无效。", phase=phase)
    token, refresh = result.get("access_token"), result.get("refresh_token", "")
    if (not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() for c in token)
            or not isinstance(refresh, str) or len(refresh) > 8192 or any(c.isspace() for c in refresh)):
        raise FeishuOAuthError("本人授权没有有效 token。", phase=phase)
    if "token_type" in result and (not isinstance(result["token_type"], str) or result["token_type"].casefold() != "bearer"):
        raise FeishuOAuthError("本人授权的 token 类型未支持。", phase=phase)
    expires = _expires_seconds(result.get("expires_in"), phase=phase)
    refresh_expires = _expires_seconds(result.get("refresh_token_expires_in"), phase=phase) if refresh else 0
    if "refresh_token_expires_in" in result and not refresh:
        _expires_seconds(result["refresh_token_expires_in"], phase=phase)
    granted = result.get("scope", row.get("granted_scopes", ""))
    if not isinstance(granted, str) or not set(granted.split()).issubset(row["requested_scopes"]):
        raise FeishuOAuthError("本人授权返回的范围无效。", phase=phase)
    _verify_actor(token, row["actor_id"])
    secret = {"access_token": token, "refresh_token": refresh}
    return {**row, "status": "ready", "ciphertext": _cipher(agent.home_paths.owner_home_dir).encrypt(
        json.dumps(secret).encode()).decode(), "ready_expires_at": time.time() + expires,
        "refresh_expires_at": time.time() + refresh_expires, "granted_scopes": granted}


# LLM: 原行匹配 input/state 后才写失败；不覆盖新的授权，失败依赖沿原 Wake 结算，不把网络异常留成 exchanging。
# 函数用途: 收口本次失败授权并清除 PKCE/token 密文，不返回秘密。
def _fail_oauth(agent, actor, input_id):
    path = oauth_path(agent, actor)
    with _credential_mutation(agent, path) as admission:
        row = read_json_object_report(path, context="oauth.fail").payload
        if row and row.get("input_id") == input_id and row.get("status") in {"pending", "exchanging"}:
            _save_credential_row(admission, path, {**row, "status": "failed", "ciphertext": ""})
    dependency = read_json_object_report(input_dependency_path(agent, input_id), context="oauth.fail_input").payload
    if dependency and dependency.get("status") == "pending":
        resolve_input_dependency(agent, input_id, status="failed")


# LLM: private link/state 绑定本 owner 原任务，缺执行上下文先拒绝；容量或明确未发送写 failed 清密文，送达未确认保留原请求与输入，缺依赖不补偿建账。
# 函数用途: 先登记必要输入再发送本人 OAuth 卡，登记失败不发卡，立即返回可归档的等待引用或固定错误。
def request_oauth(agent, *, invocation_mode=None):
    current = getattr(agent, '_current_run_params', None)
    if current is None:
        raise FeishuOAuthError("当前本人操作没有有效执行上下文。", phase="authorize", reason_code="EXECUTION_SCOPE_INACTIVE")
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise FeishuOAuthError("当前任务执行已失效。", phase="authorize", reason_code="EXECUTION_SCOPE_INACTIVE")
    _assert_network(agent, scope, invocation_mode=invocation_mode)
    actor = oauth_actor(agent, scope)
    facts = _configuration_facts(agent, create_key=True)
    nonce, verifier = "oauth_" + secrets.token_hex(16), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    expires = time.time() + 1800
    payload = {"kind": "feishu_oauth", "owner": asdict(owner_identity_from_config(agent.config)),
               "channel": "feishu", "actor_id": actor, "expires_at": expires, "input_id": nonce}
    state = sign_interaction_reference(agent.home_paths.owner_home_dir, payload)
    row = {**payload, **facts, "schema_version": "feishu_oauth.v1", "state": state, "status": "pending",
           "ciphertext": _cipher(agent.home_paths.owner_home_dir).encrypt(verifier.encode()).decode()}
    with _credential_mutation(agent, oauth_path(agent, actor)) as admission:
        previous = read_json_object_report(oauth_path(agent, actor), context="oauth.request").payload
        if previous and previous.get("status") in {"pending", "exchanging"} and previous.get("expires_at", 0) > time.time():
            raise FeishuOAuthError("本人授权已有待处理请求，请完成或撤销原授权。", phase="authorize", reason_code="OAUTH_PENDING_REQUEST_EXISTS")
        _save_credential_row(admission, oauth_path(agent, actor), row)
    try:
        create_input_dependency(agent, scope, input_id=nonce, provider="feishu_oauth", expires_at=expires,
                                reference=nonce, metadata={"actor_id": actor})
    except _UNSETTLED_EXECUTION_ERRORS:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        # 登记在发卡之前失败，卡片确定未发送，收口本次待处理请求。
        _fail_oauth(agent, actor, nonce)
        raise _card_delivery_error("failed", {"stage": "input_registration", "input_id": nonce,
                                              "effect_outcome": "not_started"}) from None
    _deliver_oauth_card(agent, scope, row, challenge)
    return {"input_id": nonce, "status": "waiting_input", "expires_at": expires}


# LLM: effect只取登记阶段或送达回执的结构化事实；unknown保留原待处理请求和输入供核对，已知未送达才沿原文案收口失败。
# 函数用途: 构造授权卡阶段的结构化失败，供工具按原因与effect结算本次操作。
def _card_delivery_error(effect_outcome, delivery):
    message = "本人授权卡发送结果未确认，原待处理请求已保留。" if effect_outcome == "unknown" else "本人授权卡未送达或输入登记失败。"
    return FeishuOAuthError(message, phase="authorize", reason_code="OAUTH_CARD_DELIVERY_FAILED",
                            effect_outcome=effect_outcome, delivery=delivery)


# LLM: URL 只交私人控制卡运输，不写工具回执或普通聊天；准确接收者与任务都来自宿主冻结 scope。
# 回执明确未发送才收口失败；发送边界异常、未确认回执或送达后本地回执未保存都保留原请求，送达事实写入诊断。
# 函数用途: 在记录锁外发送标准 PKCE 授权链接。
def _deliver_oauth_card(agent, scope, row, challenge):
    link = "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode({
        "client_id": row["app_id"], "redirect_uri": row["redirect_uri"], "response_type": "code", "state": row["state"],
        "scope": " ".join(row["requested_scopes"]), "code_challenge": challenge, "code_challenge_method": "S256"})
    card = {"elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "请用本人飞书账号授权；凭据由宿主管理。"}},
                         {"tag": "action", "actions": [{"tag": "button", "type": "primary", "url": link,
                            "text": {"tag": "plain_text", "content": "授权我的飞书账号"}},
                            {"tag":"button","type":"default","text":{"tag":"plain_text","content":"取消"},
                             "value":{"kind":"my_agent_feishu_oauth","reference":row['state'],"decision":"cancel"}}]}]}
    delivery = {"stage": "delivery", "input_id": row["input_id"]}
    try:
        receipt = agent.delivery_service.deliver_control_card(DeliveryContext(channel="feishu", target=row["actor_id"],
            mode="reply", task_id=scope.run_id, thread_id=scope.thread_id, idempotency_key=row["input_id"]), card, summary="本人飞书授权")
    except _UNSETTLED_EXECUTION_ERRORS:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        raise _card_delivery_error("unknown", {**delivery, "effect_outcome": "unknown",
                                               "exception_type": type(exc).__name__}) from None
    if not receipt.ok:
        known = getattr(receipt, "effect_outcome", "not_started") == "not_started"
        delivery.update(effect_outcome="not_started" if known else "unknown",
                        error_code=str(getattr(receipt, "error_code", "") or ""))
        provider_code = getattr(receipt, "provider_code", None)
        if type(provider_code) is int:
            delivery["provider_code"] = provider_code
        if not known:
            raise _card_delivery_error("unknown", delivery)
        _fail_oauth(agent, row["actor_id"], row["input_id"])
        raise _card_delivery_error("failed", delivery)
    try:
        with _credential_mutation(agent,oauth_path(agent,row['actor_id'])) as admission:
            latest=read_json_object_report(oauth_path(agent,row['actor_id']),context='oauth.card_receipt').payload
            if latest and latest.get('state')==row['state'] and latest.get('input_id')==row['input_id']:
                _save_credential_row(admission,oauth_path(agent,row['actor_id']),{**latest,'im_control':{
                    'message_id':receipt.message_id,'chat_id':receipt.chat_id}})
    except _UNSETTLED_EXECUTION_ERRORS:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        raise _card_delivery_error("unknown", {"stage": "receipt_commit", "input_id": row["input_id"],
                                               "effect_outcome": "confirmed",
                                               "exception_type": type(exc).__name__}) from None


# LLM: 已验签请求仍须核对 owner、actor、期限、pending 输入和原配置；ready 重放仅补原依赖，不重新兑换 code。
# 函数用途: 准备准确 callback，在锁外兑换与核对本人，再原子保存和幂等恢复任务。
def complete_oauth(agent, payload, state, code):
    actor, input_id = payload["actor_id"], payload["input_id"]
    if payload.get("owner") != asdict(owner_identity_from_config(agent.config)):
        raise FeishuOAuthError("本人授权 owner 不匹配。", phase="token_exchange")
    _assert_actor(agent, actor)
    path = oauth_path(agent, actor)
    dependency = read_json_object_report(input_dependency_path(agent, input_id), context="oauth.input").payload
    with _credential_mutation(agent, path) as admission:
        row = read_json_object_report(path, context="oauth.callback").payload
        if not row or row.get("state") != state or row.get("input_id") != input_id:
            raise FeishuOAuthError("本人授权引用已变化。", phase="token_exchange")
        replay = row.get("status") == "ready"
        if not replay and (row.get("status") != "pending" or time.time() >= row["expires_at"]):
            raise FeishuOAuthError("本人授权已结束或到期。", phase="token_exchange")
        if not replay:
            if not dependency or dependency.get("status") != "pending" or not input_dependency_current(agent, dependency):
                raise FeishuOAuthError("原授权输入或任务已结束。", phase="token_exchange")
            _save_credential_row(admission, path, {**row, "status": "exchanging"})
    try:
        _assert_configuration(agent, row)
        _assert_network(agent, dependency.get("scope") if dependency else None)
        if not replay:
            ready = _exchange_oauth(agent, row, code)
            _commit_oauth_ready(agent, path, row, ready, dependency)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        if not replay:
            _fail_oauth(agent, actor, input_id)
        phase = exc.phase if isinstance(exc, FeishuOAuthError) and exc.phase == "probe" else "token_exchange"
        provider_code = exc.provider_code if isinstance(exc, FeishuOAuthError) else None
        raise FeishuOAuthError(
            "本人授权未完成，请重新发起授权。", phase=phase, provider_code=provider_code,
        ) from None
    # ready 先持久化；恢复提交失败保留它，重复 callback 只补原 Wake，不能重新兑换一次性 code。
    try:
        resolve_input_dependency(agent, input_id, status="ready", result_refs=(input_id,))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        raise FeishuOAuthError("本人授权已保存，原任务恢复提交未完成，请重试原回调。", phase="token_exchange") from None
    return {"ok": True, "status": "ready"}


# LLM: 此入口不持 quota/凭据锁；PKCE 与 app secret 只存在本次宿主请求，响应校验后立即加密。
# 函数用途: 用原配置交换授权码并核对账号本人。
def _exchange_oauth(agent, row, code):
    verifier = _decrypt_oauth(agent, row["ciphertext"], verifier=True, phase="token_exchange")
    result = _oauth_api("POST", "/authen/v2/oauth/token", {"grant_type": "authorization_code",
        "code": code, "client_id": row["app_id"], "client_secret": agent.config.feishu_app_secret,
        "redirect_uri": row["redirect_uri"], "code_verifier": verifier})
    return _ready_row(agent, row, result)


# LLM: 网络中撤销、换请求、停止或改配置都不能被旧响应覆盖；只在原 exchanging 仍有效时保存本人授权。
# 函数用途: 在短记录锁内提交刚验证的密文 token。
def _commit_oauth_ready(agent, path, original, ready, dependency):
    _assert_configuration(agent, original)
    _assert_network(agent, dependency["scope"])
    if not input_dependency_current(agent, dependency):
        raise FeishuOAuthError("原授权任务已结束。", phase="token_exchange")
    with _credential_mutation(agent, path) as admission:
        latest = read_json_object_report(path, context="oauth.commit").payload
        if not latest or latest.get("input_id") != original["input_id"] or latest.get("status") != "exchanging":
            raise FeishuOAuthError("原授权请求已变化。", phase="token_exchange")
        _save_credential_row(admission, path, ready)


# LLM: UAT 只给真实当前执行，缺上下文须明确拒绝；刷新在锁外，重核本人/配置/网络与原密文，撤销不得被覆盖。
# 函数用途: 为本人云调用取得 token，必要时刷新；返回仅供宿主调用，不给模型。
def user_access_token(agent):
    current = getattr(agent, '_current_run_params', None)
    if current is None:
        raise ValueError('当前本人操作没有有效执行上下文。')
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise ValueError("当前本人操作没有有效执行权。")
    actor = oauth_actor(agent, scope)

    def revalidate():
        _assert_network(agent, scope)
        if not tool_approval_scope_is_current(agent, scope):
            raise ValueError("原本人执行已失效。")

    return _user_access_token_with_authority(agent, actor, revalidate=revalidate)


def _user_access_token_with_authority(agent, actor, *, revalidate):
    """宿主执行或原持久规则复用同一刷新 CAS；调用者须反复核验其规范授权。"""
    revalidate()
    _assert_network(agent)
    _assert_actor(agent, actor)
    path = oauth_path(agent, actor)
    row = read_json_object_report(path, context="oauth.token").payload
    if not row or row.get("status") != "ready" or row.get("actor_id") != actor:
        raise ValueError("本人飞书尚未授权，请先使用 feishu_auth。")
    _assert_configuration(agent, row)
    value = _decrypt_oauth(agent, row["ciphertext"])
    if _stored_deadline(row.get("ready_expires_at")) <= time.time() + 30:
        if _stored_deadline(row.get("refresh_expires_at")) <= time.time() or not value.get("refresh_token"):
            raise FeishuOAuthError("本人飞书授权已过期，请重新授权。", phase="refresh")
        result = _oauth_api("POST", "/authen/v2/oauth/token", {"grant_type": "refresh_token",
            "refresh_token": value["refresh_token"], "client_id": row["app_id"], "client_secret": agent.config.feishu_app_secret})
        ready = _ready_row(agent, row, result, phase="refresh")
        _assert_configuration(agent, row)
        revalidate()
        _assert_network(agent)
        _assert_actor(agent, actor)
        with _credential_mutation(agent, path) as admission:
            latest = read_json_object_report(path, context="oauth.refresh_commit").payload
            if not latest or latest.get("status") != "ready" or latest.get("ciphertext") != row["ciphertext"]:
                raise FeishuOAuthError("刷新期间原授权已变化。", phase="refresh")
            _save_credential_row(admission, path, ready)
        value = _decrypt_oauth(agent, ready["ciphertext"])
    revalidate()
    _assert_network(agent)
    _assert_actor(agent, actor)
    _assert_configuration(agent, row)
    return value["access_token"]


# LLM: 密文/JSON 损坏只产生固定安全错误；缓存 token 必须保持原两字段形状，不能把损坏内容回显或当作可用身份。
# 函数用途: 在宿主解密 PKCE 或 token，并拒绝无效类型，不写盘或联网。
def _decrypt_oauth(agent, ciphertext, *, verifier=False, phase="probe"):
    try:
        raw = _cipher(agent.home_paths.owner_home_dir).decrypt(ciphertext.encode()).decode()
        if verifier:
            if not 43 <= len(raw) <= 128 or any(character.isspace() for character in raw):
                raise ValueError("PKCE无效")
            return raw
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"access_token", "refresh_token"}:
            raise ValueError("token形状无效")
        if any(not isinstance(token, str) or len(token) > 8192 or any(c.isspace() for c in token) for token in value.values()) or not value["access_token"]:
            raise ValueError("token类型无效")
        return value
    except Exception:  # noqa: BLE001 密文/解析阶段包含可选加密库异常，绝不能泄漏原材料或留 exchanging。
        raise FeishuOAuthError("本人授权密文记录不可读，请重新授权。", phase=phase) from None


# LLM: 已保存的墙钟期限也要验证类型与有限值；坏账不能假装永远 ready。
# 函数用途: 核对存储的绝对期限，拒绝 NaN/无限或负值。
def _stored_deadline(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError("本人授权记录期限无效。")
    return value


# LLM: 普通飞书用户的只读状态从 canonical owner 取本人身份，不要求晋升任务；管理员及撤销保留原执行范围。
# 状态不返回密文/state/URL；撤销结算原输入，使旧回调不能复活，联测 Gateway 首次查询及原 OAuth 回调矩阵。
# 函数用途: 查询或撤销本人授权；查询不创建任务，撤销会清密文并结算原输入，不伪造供应商远端撤销。
def oauth_status(agent, *, revoke=False):
    phase = "revoke" if revoke else "probe"
    try:
        return _oauth_status(agent, revoke=revoke)
    except FeishuOAuthError as exc:
        if exc.phase == phase:
            raise
        raise FeishuOAuthError(
            str(exc), phase=phase, provider_code=exc.provider_code, reason_code=exc.reason_code,
        ) from None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise FeishuOAuthError(
            "本人授权状态暂时无法核对。", phase=phase, original_error_type=type(exc).__name__,
        ) from None


def _oauth_status(agent, *, revoke):
    current = getattr(agent, "_current_run_params", None)
    owner = owner_identity_from_config(agent.config)
    needs_scope = revoke or owner.provider != "feishu" or owner.owner_kind != "user"
    scope = (tool_approval_scope(agent, str(getattr(current, "run_id", "") or getattr(current, "task_id", "")))
             if current and needs_scope else None)
    actor = oauth_actor(agent, scope)
    with _credential_mutation(agent, oauth_path(agent, actor)) as admission:
        row = read_json_object_report(oauth_path(agent, actor), context="oauth.status").payload
        if revoke and row:
            _save_credential_row(admission, oauth_path(agent, actor), {**row, "status": "revoked", "ciphertext": ""})
        status = "revoked" if revoke else (row.get("status", "not_authorized") if row else "not_authorized")
        if status == "ready":
            try:
                _assert_configuration(agent, row)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                status = "configuration_changed"
            if status == "ready" and _stored_deadline(row.get("ready_expires_at")) <= time.time():
                status = "expired"
    if revoke and row and row.get("status") in {"pending", "exchanging"}:
        resolve_input_dependency(agent, row["input_id"], status="revoked")
    return {"status": status, "expires_at": row.get("ready_expires_at", 0) if row else 0,
            "identity": "current_user", "granted_scopes": row.get("granted_scopes", "") if row else ""}


# LLM: 上游已验证MAC/原actor且cancel可过期；这里只CAS退休原pending/state，不撤销ready token，不把迟到exchange变成成功。
# 函数用途: 关闭准确OAuth卡和原输入依赖，重放展示首次点击者，不能撤销既有有效授权。
# LLM: 原Agent与C共用cancel-only事务；不改变网络授权/刷新/ready token，不重建owner。
# 函数用途: 沿原输入资源取消本人准确OAuth卡。
def cancel_oauth_card(agent,payload,body,*,actor_id):
    from ..conversation.input_dependencies import input_dependency_access
    from .interaction_actor_audit import record_interaction_actor
    return cancel_oauth_card_for_owner(agent.home_paths.owner_home_dir,owner_identity_from_config(agent.config),
        input_dependency_access(agent),payload,body,actor_id=actor_id,
        audit=lambda actor:record_interaction_actor(agent,payload,decision='cancel',actor=actor))


# LLM: 显式原owner/输入服务仅执行原cancel CAS；上游已验真实actor/MAC，不授网络或新工具权。
# 函数用途: 让冷C与Count0复用同一取消实现，插件票据可在原卡锁内末核版本。
def cancel_oauth_card_for_owner(home, owner, inputs, payload, body, *, actor_id, audit):
    from ..common.json_io import locked_json_path
    from .interaction_actor_audit import interaction_actor_text
    if (body.get('decision')!='cancel' or actor_id!=payload.get('actor_id')
            or payload.get('owner')!=asdict(owner)):
        raise ValueError('授权卡取消归属不匹配')
    actor,input_id=actor_id,payload['input_id']
    path=oauth_path_for_home(home,actor)
    # LLM: 原卡CAS仅在同一真实C短门中执行，quota先于行锁，不在此窗口处理其它输入。
    # 函数用途: 退休准确原OAuth行，保持ready不可取消及首次点击者。
    def transition():
        with inputs.quota.admission(blocking=inputs.blocking) as admission, locked_json_path(path,blocking=inputs.blocking):
            row=read_json_object_report(path,context='oauth.cancel').payload
            control=row.get('im_control') or {} if row else {}
            if (not row or row.get('input_id')!=input_id or row.get('state')!=body['reference']
                    or control.get('message_id')!=body.get('message_id') or control.get('chat_id')!=body.get('chat_id')):
                raise ValueError('卡片不是原本人授权请求')
            from ..gateway_parts.private_interaction_service import check_plugin_input_record
            check_plugin_input_record(row)
            if row.get('status')=='cancelled':
                facts=row['cancelled_by']
            elif row.get('status') in {'pending','exchanging'}:
                facts=audit(actor)
                _save_credential_row(admission,path,{**row,'status':'cancelled','ciphertext':'','cancelled_by':facts})
            else:
                return {'ok':False,'error_code':'IM_INTERACTION_ALREADY_HANDLED',
                        'message':'原卡已处理，取消没有撤销或修改已完成的操作。'}
        return facts
    facts = inputs.perform(transition)
    if facts.get("ok") is False:
        return facts
    dependency=read_json_object_report(inputs.path(input_id),context='oauth.cancel_input').payload
    if dependency and dependency.get('status') in {'pending','cancelled'}:
        inputs.resolve(input_id,status='cancelled')
    return {'ok':True,'status':'cancelled','clicked_by':facts,
            'message':interaction_actor_text(facts)+' 已取消这次授权卡；既有授权和其他任务未改变。'}


# LLM: home由唯一owner解析器给出，保原H2目录和actor哈希，不新建OAuth库。
# 函数用途: 为冷C和原Agent定位同一已存OAuth行。
def oauth_path_for_home(home, actor):
    return Path(home).joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS,
        "oauth_" + hashlib.sha256(actor.encode()).hexdigest() + ".json")
