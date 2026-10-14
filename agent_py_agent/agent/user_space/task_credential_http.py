# LLM: 所有秘密只在宿主内存和原加密凭据记录中；send 必须是 web_fetch 原网络门，登录/刷新不允许任意脚本、目标或环境变量。
# 模块用途: 用任务引用执行同源 JSON 登录、Cookie/CSRF、有限会话刷新和声明式请求签名。
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import replace
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from ..capability.runtime_config_reload import capability_config_for_agent
from ..common.json_io import read_json_object_report
from ..conversation.tool_approval_scope import tool_approval_scope, tool_approval_scope_is_current
from ..tooling.models import ToolHandlerOutcome
from ..tooling.web_fetch_runtime import RawResponseParts
from .task_credentials import (
    _assert_credential_scope,
    _cipher,
    _credential_mutation,
    _save_credential_row,
    credential_headers,
    credential_origin,
    credential_path,
)


# LLM: 解密前核原 owner/actor/root/monitor/TTL，网络入口只能把本次同源请求交宿主，不返回秘密到工具参数。
# 函数用途: 执行带私人认证的请求，并把本次全部秘密交给 web 的归档前净化器。
def credential_authenticated_request(agent, reference, request, *, send):
    capability = capability_config_for_agent(agent)
    if capability is None or not capability.im_task_credentials_enabled:
        raise ValueError('私人凭据认证未启用')
    current = agent._current_run_params
    scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    if not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise ValueError('原任务执行已失效')
    path = credential_path(agent.home_paths.owner_home_dir, reference)
    row = read_json_object_report(path, context='credentials.http').payload
    _assert_credential_scope(agent, row, scope, request.url)
    cipher = _cipher(agent.home_paths.owner_home_dir)
    values = json.loads(cipher.decrypt(row['ciphertext'].encode()))
    config = row.get('auth_config', {})
    secrets = list(values.values())
    if row['authentication'] in {'basic', 'bearer', 'header'}:
        headers, secret_values = credential_headers(agent, reference, request.url)
        secrets.extend(secret_values)
    elif row['authentication'] == 'signature':
        headers = _signed_headers(config, values, request)
    elif row['authentication'] == 'login':
        headers, session_secrets = _login_headers(agent, path, row, values, request, send, cipher)
        secrets.extend(session_secrets)
    else:
        raise ValueError('宿主认证方式无效')
    secrets.extend(headers.values())
    result = send(_authenticated(request, headers, row['origin']))
    # 仅 GET/HEAD 可在明确 401 后重试；变更请求不重做未知副作用。
    if row['authentication'] == 'login' and request.method in {'GET', 'HEAD'} and _unauthorized(result):
        row = {**row, 'session_expires_at': 0}
        headers, session_secrets = _login_headers(agent, path, row, values, request, send, cipher)
        secrets.extend((*session_secrets, *headers.values()))
        result = send(_authenticated(request, headers, row['origin']))
    return result, tuple(set(secrets))


# LLM: 同名认证头冲突在发送前拒绝；authorization_origin 由原 fetch 逐跳核验，防止跨源 307 携带登录正文。
# 函数用途: 合并宿主认证头并设置本次不可跨源的传输边界。
def _authenticated(request, headers, origin):
    if {key.lower() for key in request.headers} & {key.lower() for key in headers}:
        raise ValueError('不能同时指定同名认证头和凭据引用')
    return replace(request, headers={**request.headers, **headers}, authorization_origin=origin)


# LLM: 签名只组合固定 schema 字段与准确请求事实，不接受表达式或模型提供明文；输出是本次请求秘密的一部分。
# 函数用途: 生成 SHA256 或 HMAC-SHA256 的声明式请求签名头。
def _signed_headers(config, values, request):
    facts = {'method': request.method, 'path': urlsplit(request.url).path or '/',
             'body_sha256': hashlib.sha256(request.data or b'').hexdigest(), 'timestamp': str(int(time.time()))}
    parts = [values[value] if kind == 'field' else facts[value] if kind == 'request' else value
             for part in config['parts'] for kind, value in part.items()]
    payload = ''.join(parts).encode()
    signature = (hmac.digest(values[config['secret_field']].encode(), payload, 'sha256').hex()
                 if config['algorithm'] == 'hmac-sha256' else hashlib.sha256(payload).hexdigest())
    return {config['header']: signature}


# LLM: cache 只存同源会话密文，ready TTL 和 generation 复核阻止更新/撤销期间旧登录覆盖；不持配额锁发网络。
# 函数用途: 取得有效会话或用私人字段登录，保存有限期限的 Cookie/Token/CSRF 密文。
def _login_headers(agent, path, row, values, request, send, cipher):
    config = row['auth_config']
    if credential_origin(config['url']) != row['origin']:
        raise ValueError('登录地址必须同源')
    if row.get('session_ciphertext') and time.time() < float(row.get('session_expires_at', 0)):
        headers = json.loads(cipher.decrypt(row['session_ciphertext'].encode()))
    else:
        body = json.dumps({key: values[field] for key, field in config['body_fields'].items()}).encode()
        login = replace(request, url=config['url'], method='POST', data=body,
                        headers={'Content-Type': 'application/json'}, authorization_origin=row['origin'])
        response = send(login)
        if not isinstance(response, RawResponseParts):
            raise ValueError('宿主登录未成功')
        headers = _session_headers(config, response)
        with _credential_mutation(agent, path) as admission:
            latest = read_json_object_report(path, context='credentials.session').payload
            if latest.get('status') != 'ready' or latest.get('generation') != row['generation'] or time.time() >= latest.get('ready_expires_at', 0):
                raise ValueError('登录期间原凭据已失效')
            latest.update(session_ciphertext=cipher.encrypt(json.dumps(headers).encode()).decode(),
                          session_expires_at=min(time.time()+config['session_seconds'], latest['ready_expires_at']))
            _save_credential_row(admission, path, latest)
    secrets = list(headers.values())
    if config['token_path']:
        secrets.append(headers[config['token_header']][len(config['token_prefix']):])
    if config['cookies']:
        cookies = SimpleCookie()
        cookies.load(headers['Cookie'])
        secrets.extend(cookie.value for cookie in cookies.values())
    return headers, tuple(secrets)


# LLM: Header/token/cookie 只从结构化响应解析，禁止换行/framing；不将响应正文或登录结果返回模型。
# 函数用途: 从同源登录响应生成 Cookie、CSRF 与 Token 请求头。
def _session_headers(config, response):
    headers = {}
    if config['token_path']:
        token = json.loads(response.body)
        for key in config['token_path']:
            token = token[key]
        if not isinstance(token, str) or not token or '\r' in token or '\n' in token:
            raise ValueError('登录令牌无效')
        headers[config['token_header']] = config['token_prefix'] + token
    cookies = SimpleCookie()
    if config['cookies']:
        values = response.headers.get_all('Set-Cookie', []) if hasattr(response.headers, 'get_all') else [response.headers.get('Set-Cookie', '')]
        for value in values:
            cookies.load(value)
        if not cookies:
            raise ValueError('登录没有返回 Cookie')
        headers['Cookie'] = '; '.join(key+'='+morsel.value for key, morsel in cookies.items())
        if name := config['csrf_cookie']:
            if name not in cookies:
                raise ValueError('登录没有返回 CSRF Cookie')
            headers[config['csrf_header']] = cookies[name].value
    if any('\r' in value or '\n' in value for value in headers.values()):
        raise ValueError('登录认证头无效')
    return headers


# LLM: 仅结构化 HTTP status 允许安全读取请求刷新，不解析错误文案或从网络异常推断认证失效。
# 函数用途: 判断宿主 HTTP 返回是否明确为 401。
def _unauthorized(response):
    if isinstance(response, RawResponseParts):
        return response.status == 401
    return isinstance(response, ToolHandlerOutcome) and response.result_envelope.get('http_status') == 401
