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
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from ..capability.runtime_config_reload import deployment_capability_config
from ..common.authenticated_http import open_authenticated_request
from ..common.json_io import read_json_object_report
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
from ..settings.secret_ref import resolve_secret_ref
from . import feishu_device_oauth as device
from .admin_channel_identity import find_admin_channel_identity
from .interaction_refs import interaction_key, sign_interaction_reference
from .owner_policy import resolve_effective_owner_policy
from .owner_resolver import owner_identity_from_config
from .task_credentials import _cipher, _credential_mutation, _save_credential_row

_API = "https://open.feishu.cn/open-apis"
_frozen_consent_policy = None


def freeze_consent_policy(home):
    """在服务监听前固定此代 Gateway 的部署设置，涉及授权的错误只影响本人 OAuth。"""
    global _frozen_consent_policy
    try:
        hours = deployment_capability_config(home).feishu_oauth_reauthorize_hours
    except (OSError, ValueError, TypeError):
        hours = None
    _frozen_consent_policy = (home.root, hours)


def _consent_current(agent, row):
    if _frozen_consent_policy is not None and _frozen_consent_policy[0] == agent.home_paths.root:
        hours = _frozen_consent_policy[1]
    else:
        hours = deployment_capability_config(agent.home_paths).feishu_oauth_reauthorize_hours
    if type(hours) is not int or hours < 1:
        raise ValueError('部署级本人授权小时数无效，请管理员修正配置并重启 Gateway。')
    # 旧 PKCE 记录保留签发时设置的原卡截止，扣除固定 1800 秒可保守还原起点。
    at = row.get('authorized_at')
    if at is None:
        at = _stored_deadline(row.get('expires_at')) - 1800
    return _stored_deadline(at) + hours * 3600 > time.time()



# LLM: 只读宿主 deployment 配置，普通 owner 不能扩大 OAuth 范围；未知配置拒绝，不用用户局部 YAML 替代。
# 函数用途: 校验准确 HTTPS 回调、开放开关和部署者声明的 scope。
def oauth_configuration(agent):
    config = deployment_capability_config(agent.home_paths)
    if not config.feishu_user_oauth_enabled:
        raise ValueError("本人飞书授权功能未启用。")
    uri = config.feishu_oauth_redirect_uri
    if not isinstance(uri, str):
        raise ValueError("本人授权回调配置无效。")
    parts = urlsplit(uri)
    if uri and (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path != "/feishu/oauth/callback" or parts.query or parts.fragment):
        raise ValueError("本人飞书授权回调尚未配置，请部署者登记准确 HTTPS 回调。")
    scopes = config.feishu_oauth_scopes
    if (not isinstance(scopes, list) or any(not isinstance(value, str) or not value
            or any(character.isspace() for character in value) for value in scopes)):
        raise ValueError("本人飞书授权范围配置无效。")
    return uri, list(dict.fromkeys(scopes))


# LLM: 应用配置只读宿主冻结字段，不能回退到模型参数或局部配置；读取不创建签名库。
# 函数用途: 校验应用标识与密钥是否真实已配置，供 availability 和认证共用。
def _application_credentials(agent):
    app_id, secret = agent.config.feishu_app_id, agent.config.feishu_app_secret
    if not isinstance(app_id, str) or not app_id or not isinstance(secret, str) or not secret:
        raise ValueError("宿主飞书应用尚未配置。")
    return app_id, resolve_secret_ref(secret, required=True)


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
        raise ValueError("当前任务或用户网络权限不可用。")
    kind = scope.get("agent_kind") if isinstance(scope, dict) else getattr(scope, "agent_kind", "")
    run_id = scope.get("run_id") if isinstance(scope, dict) else getattr(scope, "run_id", "")
    if kind == "subagent":
        task = agent.subagents.load(run_id)
        if (task.effective_permissions or {}).get("network_enabled") is False:
            raise ValueError("当前子代理网络权限不可用。")


# LLM: 群不能拥有共享个人身份；普通 owner 只认自身准确 ID，本机管理员还要保持原私人绑定。
# 函数用途: 确定当前用户本人飞书账号，不从显示名、参数或群成员猜身份。
def oauth_actor(agent, scope=None):
    owner = owner_identity_from_config(agent.config)
    if owner.owner_kind == "group":
        raise ValueError("群资源使用应用身份；本人授权请在私人助手会话进行。")
    if owner.provider == "feishu" and owner.owner_kind == "user":
        return owner.owner_id
    if owner.provider != "local" or owner.owner_kind != "main" or scope is None:
        raise ValueError("当前 owner 没有本人飞书身份。")
    channel, actor = approval_recipient(agent, scope)
    if channel != "feishu" or find_admin_channel_identity(agent.home_paths.root, channel, actor) is None:
        raise ValueError("当前管理员本人绑定已失效。")
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
class OAuthCheckError(ValueError):
    def __init__(self, code, message, **facts):
        self.code = code
        self.facts = facts
        super().__init__(message)


def _oauth_api(method, path, body=None, token=""):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(_API + path, data=data, method=method, headers=headers)
    try:
        try:
            response = open_authenticated_request(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response as stream:
            status = getattr(stream, 'status', 200)
            raw = stream.read(1024 * 1024 + 1)
        result = json.loads(raw) if len(raw) <= 1024 * 1024 else None
    except (OSError, ValueError, TypeError):
        raise OAuthCheckError('official_response_unreadable', '飞书本人授权接口响应不可读。', endpoint=path) from None
    platform_code = result.get('code') if isinstance(result, dict) else None
    if (not 200 <= status < 300 or not isinstance(result, dict) or result.get('error')
            or ('code' in result and (type(platform_code) is not int or platform_code != 0))):
        raise OAuthCheckError('official_request_rejected', '飞书本人授权接口未返回可验证结果。',
                              endpoint=path, http_status=status, platform_code=platform_code if type(platform_code) is int else None)
    if path == "/authen/v2/oauth/token":
        if not isinstance(result.get("access_token"), str) or not result["access_token"]:
            raise OAuthCheckError('missing_access_token', '飞书本人授权没有返回有效 token。')
    elif 'code' not in result:
        raise OAuthCheckError('missing_user_info_status', '本人证明没有返回状态码。')
    return result


# LLM: 实际 open_id 必须与宿主 actor 相等；state 中的身份不代替供应商证明。
# 函数用途: 在保存 token 前验证授权账号本人。
def _verify_actor(token, actor):
    result = _oauth_api("GET", "/authen/v1/user_info", token=token)
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict) or data.get("open_id") != actor:
        raise OAuthCheckError("actor_mismatch", "登录的飞书账号不是原发起人，未保存授权。")


# LLM: 期限只认正整数；bool/字符串/NaN/无限均拒绝，不能凭缺失字段延长 refresh 授权。
# 函数用途: 取得供应商明确声明的 token 有效秒数。
def _expires_seconds(value, field="expires_in"):
    if type(value) is not int or not 0 < value <= 31536000:
        raise OAuthCheckError("invalid_token_lifetime", "本人授权有效期无效。", field=field,
                              value=value if type(value) in {int, bool} else None, value_type=type(value).__name__)
    return value


# LLM: token 和 scope 只读结构化响应；没有 refresh token 可保留仅 access 授权，有 refresh token 则必须给准确期限。
# 函数用途: 校验响应、验证本人并立即加密 token，不生成默认授权期限或伪造已授 scope。
def _ready_row(agent, row, result):
    if not isinstance(result, dict):
        raise OAuthCheckError("token_response_shape", "本人授权响应无效。")
    token, refresh = result.get("access_token"), result.get("refresh_token", "")
    if (not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() for c in token)
            or not isinstance(refresh, str) or len(refresh) > 8192 or any(c.isspace() for c in refresh)):
        raise OAuthCheckError("token_shape_invalid", "本人授权没有有效 token。")
    if "token_type" in result and (not isinstance(result["token_type"], str) or result["token_type"].casefold() != "bearer"):
        raise OAuthCheckError("unsupported_token_type", "本人授权的 token 类型未支持。",
                              token_type="dpop" if str(result.get("token_type")).lower() == "dpop" else "other")
    expires = _expires_seconds(result.get("expires_in"))
    refresh_expires = _expires_seconds(result.get("refresh_token_expires_in"), "refresh_token_expires_in") if refresh else 0
    if "refresh_token_expires_in" in result and not refresh:
        lifetime = result["refresh_token_expires_in"]
        if type(lifetime) is not int or lifetime != 0:
            _expires_seconds(lifetime, "refresh_token_expires_in")
    granted = result.get("scope", row.get("granted_scopes", ""))
    if not isinstance(granted, str):
        raise OAuthCheckError("unexpected_granted_scopes", "飞书返回的 scope 字段应为以空格分隔的字符串。")
    reported = set(granted.split())
    extra = reported.difference(row["requested_scopes"])
    if row.get("flow") == "device":
        # 平台可一并返回历史授权或细分权限。只保存本次申请的交集；刷新也不能追加业务权限。
        if row.get("status") == "ready":
            reported.intersection_update(row["granted_scopes"].split())
        granted = " ".join(scope for scope in row["requested_scopes"] if scope in reported)
    elif extra:
        raise OAuthCheckError("unexpected_granted_scopes", "本人授权返回了当前回调未申请的权限。",
                              extra_scope_count=len(extra))
    _verify_actor(token, row["actor_id"])
    secret = {"access_token": token, "refresh_token": refresh}
    return {**row, "status": "ready", "ciphertext": _cipher(agent.home_paths.owner_home_dir).encrypt(
        json.dumps(secret).encode()).decode(), "ready_expires_at": time.time() + expires,
        "refresh_expires_at": time.time() + refresh_expires, "granted_scopes": granted,
        "ignored_provider_scope_count": len(extra),
        "authorized_at": (row.get("authorized_at", row.get("expires_at", 1800) - 1800)
                          if row.get("status") == "ready" else time.time())}


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


# LLM: private link/state 绑定本 owner 原任务，缺执行上下文先拒绝；容量/运输失败写 failed 清密文，缺依赖不补偿建账。
# 函数用途: 先登记必要输入再发送本人 OAuth 卡，登记失败不发卡，立即返回可归档的等待引用或固定错误。
def _reusable_grant(agent, previous, actor, facts):
    if (not previous or previous.get("status") != "ready" or previous.get("refresh_rejected") or previous.get("actor_id") != actor
            or any(previous.get(key) != value for key, value in facts.items())):
        return False
    granted = previous.get("granted_scopes")
    if not isinstance(granted, str) or not set(facts["requested_scopes"]).issubset(granted.split()):
        return False
    try:
        if not _consent_current(agent, previous):
            return False
        secret = _decrypt_oauth(agent, previous.get("ciphertext", ""))
        now = time.time()
        return (_stored_deadline(previous.get("ready_expires_at", 0)) > now
                or bool(secret.get("refresh_token")) and _stored_deadline(previous.get("refresh_expires_at", 0)) > now)
    except ValueError:
        # 已损坏的旧凭据不能让明确发起的新授权一直返回“已就绪”。
        return False


def request_oauth(agent, *, invocation_mode=None):
    current = getattr(agent, '_current_run_params', None)
    if current is None:
        raise OAuthCheckError("execution_scope_inactive", "当前本人操作没有有效执行上下文。")
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise ValueError("当前任务执行已失效。")
    _assert_network(agent, scope, invocation_mode=invocation_mode)
    actor = oauth_actor(agent, scope)
    facts = _configuration_facts(agent, create_key=True)
    use_device = not facts["redirect_uri"]
    nonce, verifier = "oauth_" + secrets.token_hex(16), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    expires = time.time() + 1800
    payload = {"kind": "feishu_oauth", "owner": asdict(owner_identity_from_config(agent.config)),
               "channel": "feishu", "actor_id": actor, "expires_at": expires, "input_id": nonce}
    state = sign_interaction_reference(agent.home_paths.owner_home_dir, payload)
    row = {**payload, **facts, "schema_version": "feishu_oauth.v1", "state": state, "status": "pending",
           "ciphertext": "" if use_device else _cipher(agent.home_paths.owner_home_dir).encrypt(verifier.encode()).decode()}
    with _credential_mutation(agent, oauth_path(agent, actor)) as admission:
        previous = read_json_object_report(oauth_path(agent, actor), context="oauth.request").payload
        if _reusable_grant(agent, previous, actor, facts):
            return {"status": "ready", "identity": "current_user", "new_card_sent": False}
        if previous and previous.get("status") in {"pending", "exchanging"} and previous.get("expires_at", 0) > time.time():
            raise ValueError("本人授权已有待处理请求，请完成或撤销原授权。")
        _save_credential_row(admission, oauth_path(agent, actor), row)
    try:
        metadata = {"actor_id": actor}
        grant = None
        if use_device:
            grant = device.start_device_authorization(*_application_credentials(agent), facts['requested_scopes'])
            expires = min(expires, time.time() + grant.expires)
            row = {**row, 'flow': 'device', 'expires_at': expires, 'poll_interval': grant.interval,
                   'next_poll_at': time.time() + grant.interval,
                   'ciphertext': _cipher(agent.home_paths.owner_home_dir).encrypt(grant.code.encode()).decode()}
            with _credential_mutation(agent, oauth_path(agent, actor)) as admission:
                latest = read_json_object_report(oauth_path(agent, actor), context='oauth.device_grant').payload
                if not latest or latest.get('input_id') != nonce or latest.get('status') != 'pending':
                    raise ValueError('设备授权发起期间原请求已撤销。')
                _save_credential_row(admission, oauth_path(agent, actor), row)
            metadata['flow'] = 'device'
        create_input_dependency(agent, scope, input_id=nonce, provider="feishu_oauth", expires_at=expires,
                                reference=nonce, metadata=metadata)
        if grant:
            _deliver_oauth_card(agent, scope, row, challenge, grant=grant)
        else:
            _deliver_oauth_card(agent, scope, row, challenge)
    except device.DeviceOAuthError:
        _fail_oauth(agent, actor, nonce)
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        _fail_oauth(agent, actor, nonce)
        raise ValueError("本人授权卡未送达或输入登记失败。") from None
    return {"input_id": nonce, "status": "waiting_input", "expires_at": expires}


# LLM: URL 只交私人控制卡运输，不写工具回执或普通聊天；准确接收者与任务都来自宿主冻结 scope。
# 函数用途: 在记录锁外发送标准 PKCE 授权链接。
def _deliver_oauth_card(agent, scope, row, challenge, *, grant=None):
    link = grant.url if grant else "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode({
        "client_id": row["app_id"], "redirect_uri": row["redirect_uri"], "response_type": "code", "state": row["state"],
        "scope": " ".join(row["requested_scopes"]), "code_challenge": challenge, "code_challenge_method": "S256"})
    notice = "请用本人飞书账号授权；凭据由宿主管理。"
    if grant:
        notice += f" 核对飞书页面显示的授权码 {grant.user_code}，完成后原任务会自动继续。"
    card = {"elements": [{"tag": "div", "text": {"tag": "plain_text", "content": notice}},
                         {"tag": "action", "actions": [{"tag": "button", "type": "primary", "url": link,
                            "text": {"tag": "plain_text", "content": "授权我的飞书账号"}},
                            {"tag":"button","type":"default","text":{"tag":"plain_text","content":"取消"},
                             "value":{"kind":"my_agent_feishu_oauth","reference":row['state'],"decision":"cancel"}}]}]}
    receipt = agent.delivery_service.deliver_control_card(DeliveryContext(channel="feishu", target=row["actor_id"],
        mode="reply", task_id=scope.run_id, thread_id=scope.thread_id, idempotency_key=row["input_id"]), card, summary="本人飞书授权")
    if not receipt.ok:
        raise ValueError("本人授权卡未送达。")
    with _credential_mutation(agent,oauth_path(agent,row['actor_id'])) as admission:
        latest=read_json_object_report(oauth_path(agent,row['actor_id']),context='oauth.card_receipt').payload
        if latest and latest.get('state')==row['state'] and latest.get('input_id')==row['input_id']:
            _save_credential_row(admission,oauth_path(agent,row['actor_id']),{**latest,'im_control':{
                'message_id':receipt.message_id,'chat_id':receipt.chat_id}})


# LLM: 已验签请求仍须核对 owner、actor、期限、pending 输入和原配置；ready 重放仅补原依赖，不重新兑换 code。
# 函数用途: 准备准确 callback，在锁外兑换与核对本人，再原子保存和幂等恢复任务。
def complete_oauth(agent, payload, state, code):
    actor, input_id = payload["actor_id"], payload["input_id"]
    if payload.get("owner") != asdict(owner_identity_from_config(agent.config)):
        raise ValueError("本人授权 owner 不匹配。")
    _assert_actor(agent, actor)
    path = oauth_path(agent, actor)
    dependency = read_json_object_report(input_dependency_path(agent, input_id), context="oauth.input").payload
    with _credential_mutation(agent, path) as admission:
        row = read_json_object_report(path, context="oauth.callback").payload
        if not row or row.get("state") != state or row.get("input_id") != input_id:
            raise ValueError("本人授权引用已变化。")
        if row.get("flow") == "device":
            raise ValueError("设备授权只能由原任务向飞书查询确认。")
        replay = row.get("status") == "ready"
        if not replay and (row.get("status") != "pending" or time.time() >= row["expires_at"]):
            raise ValueError("本人授权已结束或到期。")
        if not replay:
            if not dependency or dependency.get("status") != "pending" or not input_dependency_current(agent, dependency):
                raise ValueError("原授权输入或任务已结束。")
            _save_credential_row(admission, path, {**row, "status": "exchanging"})
    try:
        _assert_configuration(agent, row)
        _assert_network(agent, dependency.get("scope") if dependency else None)
        if not replay:
            ready = _exchange_oauth(agent, row, code)
            _commit_oauth_ready(agent, path, row, ready, dependency)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        if not replay:
            _fail_oauth(agent, actor, input_id)
        raise ValueError("本人授权未完成，请重新发起授权。") from None
    # ready 先持久化；恢复提交失败保留它，重复 callback 只补原 Wake，不能重新兑换一次性 code。
    try:
        resolve_input_dependency(agent, input_id, status="ready", result_refs=(input_id,))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        raise ValueError("本人授权已保存，原任务恢复提交未完成，请重试原回调。") from None
    return {"ok": True, "status": "ready"}


# LLM: 此入口不持 quota/凭据锁；PKCE 与 app secret 只存在本次宿主请求，响应校验后立即加密。
# 函数用途: 用原配置交换授权码并核对账号本人。
def _exchange_oauth(agent, row, code):
    verifier = _decrypt_oauth(agent, row["ciphertext"], verifier=True)
    result = _oauth_api("POST", "/authen/v2/oauth/token", {"grant_type": "authorization_code",
        "code": code, "client_id": row["app_id"], "client_secret": _application_credentials(agent)[1],
        "redirect_uri": row["redirect_uri"], "code_verifier": verifier})
    return _ready_row(agent, row, result)


# LLM: 网络中撤销、换请求、停止或改配置都不能被旧响应覆盖；只在原 exchanging 仍有效时保存本人授权。
# 函数用途: 在短记录锁内提交刚验证的密文 token。
def _commit_oauth_ready(agent, path, original, ready, dependency):
    _assert_configuration(agent, original)
    _assert_network(agent, dependency["scope"])
    if not input_dependency_current(agent, dependency):
        raise ValueError("原授权任务已结束。")
    with _credential_mutation(agent, path) as admission:
        latest = read_json_object_report(path, context="oauth.commit").payload
        if not latest or latest.get("input_id") != original["input_id"] or latest.get("status") != "exchanging":
            raise ValueError("原授权请求已变化。")
        _save_credential_row(admission, path, ready)


# LLM: UAT 只给真实当前执行，缺上下文须明确拒绝；刷新在锁外，重核本人/配置/网络与原密文，撤销不得被覆盖。
# 函数用途: 为本人云调用取得 token，必要时刷新；返回仅供宿主调用，不给模型。
def user_access_token(agent, *, scope_groups=()):
    current = getattr(agent, '_current_run_params', None)
    if current is None:
        raise OAuthCheckError("execution_scope_inactive", "当前本人操作没有有效执行上下文。")
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise OAuthCheckError("execution_scope_inactive", "当前本人操作没有有效执行权。")
    actor = oauth_actor(agent, scope)

    def revalidate():
        _assert_network(agent, scope)
        if not tool_approval_scope_is_current(agent, scope):
            raise OAuthCheckError("execution_scope_inactive", "原本人执行已失效。")

    return _user_access_token_with_authority(agent, actor, revalidate=revalidate, scope_groups=scope_groups)


def _check_device_scopes(row, scope_groups):
    if row.get("flow") != "device":
        return
    allowed = set(row.get("granted_scopes", "").split())
    missing = [sorted(group) for group in scope_groups if allowed.isdisjoint(group)]
    if not scope_groups or missing:
        raise OAuthCheckError("operation_scope_not_granted", "本次本人授权未包括此操作所需权限，未向业务接口发送请求。",
                              required_any_of=missing)


def _user_access_token_with_authority(agent, actor, *, revalidate, scope_groups=()):
    """宿主执行或原持久规则复用同一刷新 CAS；调用者须反复核验其规范授权。"""
    revalidate()
    _assert_network(agent)
    _assert_actor(agent, actor)
    path = oauth_path(agent, actor)
    row = read_json_object_report(path, context="oauth.token").payload
    if not row or row.get("status") != "ready" or row.get("actor_id") != actor:
        raise ValueError("本人飞书尚未授权，请先使用 feishu_auth。")
    _assert_configuration(agent, row)
    _check_device_scopes(row, scope_groups)
    if not _consent_current(agent, row):
        raise ValueError("距上次本人授权已到部署规定时限，请重新点击本人授权卡。")
    value = _decrypt_oauth(agent, row["ciphertext"])
    if _stored_deadline(row.get("ready_expires_at")) <= time.time() + 30:
        if _stored_deadline(row.get("refresh_expires_at")) <= time.time() or not value.get("refresh_token"):
            raise ValueError("本人飞书授权已过期，请重新授权。")
        if row.get('flow') == 'device':
            if row.get('refresh_rejected'):
                raise OAuthCheckError('refresh_rejected', '飞书已拒绝这份续期凭据，请在当前私人会话重新授权。')
            try:
                result = device.refresh_device_token(*_application_credentials(agent), value['refresh_token'])
            except device.DeviceOAuthError as exc:
                if exc.http_status != 400 or exc.code not in {'invalid_grant', 'expired_token', '20026', '20037', '20064', '20073'}:
                    raise
                # 旧 token 也可能已由并行请求轮换。只标记原密文，不能擦除已提交或在途成功的新一代。
                revalidate()
                _assert_actor(agent, actor)
                _assert_configuration(agent, row)
                with _credential_mutation(agent, path) as admission:
                    latest = read_json_object_report(path, context="oauth.refresh_rejection").payload
                    if latest and latest.get('status') == 'ready' and latest.get('ciphertext') == row['ciphertext']:
                        _save_credential_row(admission, path, {**latest, 'refresh_rejected': True,
                            'provider_error': exc.code, 'provider_http_status': exc.http_status, 'validation_phase': 'refresh'})
                raise OAuthCheckError('refresh_rejected', '飞书已拒绝本次续期。请先查本人授权状态，已换新可重查，需重新授权时再发卡。') from None
        else:
            result = _oauth_api("POST", "/authen/v2/oauth/token", {"grant_type": "refresh_token",
                "refresh_token": value["refresh_token"], "client_id": row["app_id"], "client_secret": _application_credentials(agent)[1]})
        ready = _ready_row(agent, row, result)
        _assert_configuration(agent, row)
        revalidate()
        _assert_network(agent)
        _assert_actor(agent, actor)
        with _credential_mutation(agent, path) as admission:
            latest = read_json_object_report(path, context="oauth.refresh_commit").payload
            if not latest or latest.get("status") != "ready" or latest.get("ciphertext") != row["ciphertext"]:
                raise ValueError("刷新期间原授权已变化。")
            _save_credential_row(admission, path, ready)
        value = _decrypt_oauth(agent, ready["ciphertext"])
        _check_device_scopes(ready, scope_groups)
        row = ready
    revalidate()
    _assert_network(agent)
    _assert_actor(agent, actor)
    _assert_configuration(agent, row)
    if not _consent_current(agent, row):
        raise ValueError("距上次本人授权已到部署规定时限，请重新点击本人授权卡。")
    latest = read_json_object_report(path, context="oauth.release").payload
    if not latest or latest.get("status") != "ready" or latest.get("ciphertext") != row["ciphertext"]:
        raise ValueError("取用期间本人授权已变化，本次不提供令牌。请重查本人授权状态。")
    return value["access_token"]


# LLM: 密文/JSON 损坏只产生固定安全错误；缓存 token 必须保持原两字段形状，不能把损坏内容回显或当作可用身份。
# 函数用途: 在宿主解密 PKCE 或 token，并拒绝无效类型，不写盘或联网。
def _decrypt_oauth(agent, ciphertext, *, verifier=False):
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
        raise ValueError("本人授权密文记录不可读，请重新授权。") from None


# LLM: 已保存的墙钟期限也要验证类型与有限值；坏账不能假装永远 ready。
# 函数用途: 核对存储的绝对期限，拒绝 NaN/无限或负值。
def _stored_deadline(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError("本人授权记录期限无效。")
    return value


def _revoke_at_provider(agent, row, scope):
    """本地权利已撤销。锁外分别撤销可续期和仍在有效期的凭据，保留平台的逐项回执。"""
    try:
        _assert_network(agent, scope)
    except ValueError:
        return {"status": "not_sent", "reason": "NETWORK_DISABLED"}
    try:
        _assert_actor(agent, row["actor_id"])
        _assert_configuration(agent, row)
        app_id, secret = _application_credentials(agent)
        tokens = _decrypt_oauth(agent, row["ciphertext"])
    except (OSError, TypeError, ValueError, KeyError):
        return {"status": "not_sent", "reason": "STORED_GRANT_OR_APP_CHANGED"}
    receipts = {}
    for token_type in ("refresh_token", "access_token"):
        if not tokens[token_type]:
            continue
        try:
            device.revoke_device_token(app_id, secret, tokens[token_type], token_type)
            receipts[token_type] = {"status": "acknowledged"}
        except device.DeviceOAuthError as exc:
            receipts[token_type] = {"status": "unconfirmed", "http_status": exc.http_status, "code": exc.code}
    done = all(receipt["status"] == "acknowledged" for receipt in receipts.values())
    return {"status": "acknowledged" if done else "unconfirmed", "receipts": receipts}


# LLM: 普通飞书用户的只读状态从 canonical owner 取本人身份，不要求晋升任务；管理员及撤销保留原执行范围。
# 状态不返回密文/state/URL；撤销结算原输入，使旧回调不能复活，联测 Gateway 首次查询及原 OAuth 回调矩阵。
# 函数用途: 查询或撤销本人授权；查询不创建任务，撤销会清密文并结算原输入，不伪造供应商远端撤销。
def oauth_status(agent, *, revoke=False):
    current = getattr(agent, "_current_run_params", None)
    owner = owner_identity_from_config(agent.config)
    needs_scope = revoke or owner.provider != "feishu" or owner.owner_kind != "user"
    scope = (tool_approval_scope(agent, str(getattr(current, "run_id", "") or getattr(current, "task_id", "")))
             if current and needs_scope else None)
    if revoke and (scope is None or not scope.active or not tool_approval_scope_is_current(agent, scope)):
        raise ValueError("撤销本人授权需要在原用户的有效私人任务内执行。")
    actor = oauth_actor(agent, scope)
    refreshable = False
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
            if status == "ready" and (row.get("refresh_rejected") or not _consent_current(agent, row)):
                status = "reauthorization_required"
            if status == "ready" and _stored_deadline(row.get("ready_expires_at")) <= time.time():
                status = "expired"
                refreshable = _stored_deadline(row.get("refresh_expires_at", 0)) > time.time()
    if revoke and row and row.get("status") in {"pending", "exchanging"}:
        resolve_input_dependency(agent, row["input_id"], status="revoked")
    provider_revocation = {}
    if revoke and row and row.get("status") == "ready" and row.get("flow") == "device":
        provider_revocation = {"provider_revocation": _revoke_at_provider(agent, row, scope)}
    return {"status": status, "refreshable": refreshable, "provider_error": (row or {}).get("provider_error", ""),
            "provider_http_status": (row or {}).get("provider_http_status"), "flow": (row or {}).get("flow", ""),
            **provider_revocation,
            "validation_phase": (row or {}).get("validation_phase", ""), "validation_facts": (row or {}).get("validation_facts", {}),
            "expires_at": row.get("ready_expires_at", 0) if row else 0,
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


def device_poll_due(owner_home, dependency):
    """只读准确的 owner+actor 记录。下一次检查由同一条 task_input_deadline 驱动。"""
    end = dependency['expires_at']
    meta = dependency.get('metadata') or {}
    if not owner_home or meta.get('flow') != 'device' or not dependency.get('armed'):
        return end
    row = read_json_object_report(oauth_path_for_home(owner_home, meta['actor_id']), context='oauth.poll_due').payload
    if not row or row.get('input_id') != dependency['input_id']:
        return end
    if row.get('status') in {'ready', 'failed', 'revoked', 'cancelled'}:
        return 0.0
    return min(end, _stored_deadline(row.get('next_poll_at', end)))


def poll_device_inputs(agent, task_id):
    """每个已认领的任务只检查自身准确输入，不驻留线程，不在 RuntimeDB 事务里联网。"""
    from ..conversation.input_dependencies import input_dependency_access

    inputs = input_dependency_access(agent)
    link, error = inputs.store.tasks.load_report(task_id)
    if error or link is None:
        return
    for input_id in link.input_dependency_refs:
        dep = inputs.read(input_id, task_id=task_id)
        if (dep['provider'] == 'feishu_oauth' and dep.get('metadata', {}).get('flow') == 'device'
                and dep.get('armed') and dep['status'] == 'pending' and inputs.current(dep)):
            _poll_device_input(agent, dep)


def _poll_device_input(agent, dep):
    actor, input_id = dep['metadata']['actor_id'], dep['input_id']
    path = oauth_path(agent, actor)
    now = time.time()
    if dep['owner'] != asdict(owner_identity_from_config(agent.config)) or now >= dep['expires_at']:
        return
    with _credential_mutation(agent, path) as admission:
        row = read_json_object_report(path, context='oauth.poll.claim').payload
        if not row or row.get('input_id') != input_id or row.get('actor_id') != actor or row.get('flow') != 'device':
            return
        status = row['status']
        if status in {'pending', 'exchanging'}:
            if not row.get('im_control') or row['next_poll_at'] > now:
                return
            poll_id = secrets.token_hex(12)
            row = {**row, 'status': 'exchanging', 'poll_id': poll_id, 'next_poll_at': now + 35}
            _save_credential_row(admission, path, row)
        elif status not in {'ready', 'revoked', 'cancelled', 'failed'}:
            return
    if status in {'ready', 'revoked', 'cancelled', 'failed'}:
        resolve_input_dependency(agent, input_id, status=status, result_refs=(input_id,) if status == 'ready' else ())
        return
    phase = 'original_authority'
    token_response_status = None
    try:
        _assert_actor(agent, actor)
        _assert_network(agent, dep['scope'])
        _assert_configuration(agent, row)
        from cryptography.fernet import InvalidToken

        try:
            code = _cipher(agent.home_paths.owner_home_dir).decrypt(row['ciphertext'].encode()).decode()
        except (InvalidToken, UnicodeError, ValueError):
            raise ValueError('device_grant_unreadable') from None
        phase = 'device_token_exchange'
        result = device.exchange_device_token(*_application_credentials(agent), code)
        token_response_status = 200
        phase = 'token_and_identity_validation'
        ready = _ready_row(agent, row, result)
        phase = 'current_task_recheck'
        _assert_actor(agent, actor)
        _assert_network(agent, dep['scope'])
        _assert_configuration(agent, row)
        if time.time() >= dep['expires_at'] or not input_dependency_current(agent, dep):
            raise ValueError('input_retired')
        update = {**ready, 'poll_id': '', 'provider_error': '', 'provider_http_status': 200}
    except device.DeviceOAuthError as exc:
        retryable = (exc.code in {'authorization_pending', 'slow_down', 'temporarily_unavailable', 'transport_unavailable'}
                     or exc.http_status == 429 or exc.http_status >= 500)
        interval = row['poll_interval']
        if exc.code == 'slow_down' or exc.http_status == 429:
            interval += 5
        elif retryable and exc.code != 'authorization_pending':
            interval = min(max(10, interval * 2), max(60, interval))
        update = {**row, 'status': 'pending' if retryable else 'failed', 'poll_id': '', 'poll_interval': interval,
                  'next_poll_at': time.time() + interval, 'provider_error': exc.code, 'provider_http_status': exc.http_status}
        if not retryable:
            update['ciphertext'] = ''
    except (ValueError, TypeError, KeyError, RuntimeError) as exc:
        update = {**row, 'status': 'failed', 'poll_id': '', 'ciphertext': '',
                  'provider_error': exc.code if isinstance(exc, OAuthCheckError) else 'authorization_state_changed',
                  'validation_type': type(exc).__name__, 'validation_phase': phase,
                  'validation_facts': exc.facts if isinstance(exc, OAuthCheckError) else {},
                  'provider_http_status': token_response_status}
    with _credential_mutation(agent, path) as admission:
        latest = read_json_object_report(path, context='oauth.poll.commit').payload
        if not latest or latest.get('input_id') != input_id or latest.get('poll_id') != poll_id or latest['status'] != 'exchanging':
            return
        _save_credential_row(admission, path, update)
    if update['status'] in {'ready', 'failed'}:
        resolve_input_dependency(agent, input_id, status=update['status'], result_refs=(input_id,) if update['status'] == 'ready' else ())
