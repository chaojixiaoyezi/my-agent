# LLM: 所有秘密只在宿主内存和原加密凭据记录中；send 必须是 web_fetch 原网络门，登录/刷新不允许任意脚本、目标或环境变量。
# 模块用途: 用任务引用执行同源认证及有限会话刷新，保留原HTTP/网络错误供安全回报，不自动重发卡或重做写请求。
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from ..capability.runtime_config_reload import capability_config_for_agent
from ..common.json_io import read_json_object_report
from ..conversation.lifecycle_wake_event import protected_resource_check
from ..conversation.tool_approval_scope import (
    ToolApprovalScopeUnavailable,
    tool_approval_scope,
    tool_approval_scope_is_current,
)
from ..tooling.host_output_redaction import credential_echo_variants, turn_credential_redaction
from ..tooling.models import ToolHandlerOutcome
from ..tooling.web_fetch_runtime import RawResponseParts
from . import task_credentials
from .task_credential_forms import assert_credential_record_signature
from .task_credential_storage import CredentialInputError, read_credential_row
from .task_credential_verification import credential_http_status, credential_response_outcome
from .task_credentials import (
    _cipher,
    _credential_for_use,
    _credential_headers_from_values,
    _credential_mutation,
    _map_credential_use_error,
    _save_credential_row,
    credential_origin,
    credential_path,
    current_credential_actor_scope,
)

# 仅在单次同步 web_fetch 认证执行栈内向调用层回传已就绪秘密，便于异常中断或返回前即时净化。
_active_http_secret_sink: ContextVar[Callable[[Iterable[str]], None] | None] = ContextVar(
    'active_http_secret_sink',
    default=None,
)


@contextmanager
def bind_http_credential_secret_sink(sink: Callable[[Iterable[str]], None] | None):
    """在单次 HTTP 工具调用内绑定实时秘密接收回调，退出时立即复位。"""
    token = _active_http_secret_sink.set(sink)
    try:
        yield
    finally:
        _active_http_secret_sink.reset(token)


class _SingleCallGroupAuthorityAgent:
    """仅在单次发包前 C1 同步授权期内复用同群同次成员快照，不改宿主对象、不跨网络请求缓存。"""

    def __init__(self, agent: object) -> None:
        object.__setattr__(self, '_agent', agent)
        object.__setattr__(self, '_snapshots', {})

    def __getattr__(self, name: str):
        base = object.__getattribute__(self, '_agent')
        if name != 'delivery_service':
            return getattr(base, name)
        delivery = getattr(base, 'delivery_service', None)
        registry = getattr(delivery, 'registry', None)
        if delivery is None or registry is None:
            return delivery
        snapshots = object.__getattribute__(self, '_snapshots')

        def adapter_for(provider: str):
            adapter = registry.adapter_for(provider)
            fn = getattr(adapter, 'group_authority_snapshot', None)
            if not callable(fn):
                return adapter

            def cached_snapshot(chat_id: str):
                key = (provider, chat_id)
                if key not in snapshots:
                    snapshots[key] = dict(fn(chat_id))
                return dict(snapshots[key])

            return SimpleNamespace(
                group_authority_snapshot=cached_snapshot,
                send_control_card=getattr(adapter, 'send_control_card', None),
            )

        return SimpleNamespace(registry=SimpleNamespace(adapter_for=adapter_for))

    def __setattr__(self, name: str, value: object) -> None:
        setattr(object.__getattribute__(self, '_agent'), name, value)


@contextmanager
def _single_group_snapshot_agent(agent: object):
    if getattr(getattr(agent, 'home_paths', None), 'owner_kind', '') == 'group':
        yield _SingleCallGroupAuthorityAgent(agent)
        return
    yield agent


# LLM: 解密前在原owner/origin选择实际凭据并只查一次成员；401写实际引用/代次，选择事实无秘密，GET/HEAD至多原会话刷新。
# 函数用途: 执行当前允许的认证请求，群按本人优先选账号，保存真实失败及净化值，写请求不重放。
def credential_authenticated_request(agent, reference, request, *, send, credential_use=None):
    capability = capability_config_for_agent(agent)
    if capability is None or not capability.im_task_credentials_enabled:
        raise ValueError('私人凭据认证未启用')
    current = getattr(agent, '_current_run_params', None)
    if current is None:
        raise CredentialInputError(
            'CREDENTIAL_NOT_AUTHORIZED',
            '当前调用缺少活动任务上下文，未使用凭据。',
            stage='execution_scope',
            effect_outcome='not_started',
        )
    try:
        scope = tool_approval_scope(agent, str(current.run_id or current.task_id))
    except ToolApprovalScopeUnavailable:
        raise CredentialInputError(
            'CREDENTIAL_TASK_NOT_ACTIVE',
            '当前任务执行范围不可用，未使用凭据。',
            stage='execution_scope',
            effect_outcome='not_started',
        ) from None
    if not scope.active or not tool_approval_scope_is_current(agent, scope):
        raise CredentialInputError(
            'CREDENTIAL_NOT_AUTHORIZED',
            '原任务执行已失效',
            stage='execution_scope',
            effect_outcome='not_started',
        )
    with _single_group_snapshot_agent(agent) as auth_agent:
        home_paths = getattr(auth_agent, 'home_paths', None)
        if home_paths is None:
            raise CredentialInputError(
                'CREDENTIAL_NOT_AUTHORIZED',
                '当前执行缺少受管用户目录上下文，未使用凭据。',
                stage='execution_scope',
                effect_outcome='not_started',
            )
        try:
            requested_path = credential_path(home_paths.owner_home_dir, reference)
        except ValueError:
            raise CredentialInputError(
                'TOOL_INVALID_ARGUMENTS',
                'credential_ref 格式无效，请传入宿主返回的 cred_<32位十六进制> 引用。',
                stage='preflight',
                effect_outcome='not_started',
                diagnostic={'field': 'credential_ref'},
            ) from None
        try:
            credential_origin(request.url)
        except ValueError:
            raise CredentialInputError(
                'TOOL_INVALID_ARGUMENTS',
                '目标 URL 格式无效，无法核验凭据同源范围。',
                stage='preflight',
                effect_outcome='not_started',
                diagnostic={'field': 'target_url'},
            ) from None
        current_credential_actor_scope(auth_agent)
        initial_row = read_credential_row(requested_path, stage='credential_use')
        if not initial_row:
            raise CredentialInputError(
                'CREDENTIAL_INPUT_UNAVAILABLE',
                '未找到对应的凭据记录；请用 request_credentials 查询可用引用，勿在聊天发送密码。',
                stage='credential_use',
                effect_outcome='not_started',
            )
        try:
            assert_credential_record_signature(auth_agent, initial_row)
            row = _credential_for_use(auth_agent, reference, scope, request.url)
            assert_credential_record_signature(auth_agent, row)
        except ValueError as exc:
            raise _map_credential_use_error(exc) from None
        path = credential_path(home_paths.owner_home_dir, row['credential_ref'])
        selection = {
            'schema': 'credential-selection.v1',
            'requested_credential_ref': reference,
            'effective_credential_ref': row['credential_ref'],
            'access_scope': row.get('access_scope', 'task'),
        }
        if row['authentication'] == 'collected':
            return _with_credential_selection(_collected_preflight_outcome(reference, row), selection), ()
        expected_generation = int(row.get('generation', 1))
        if callable(credential_use) and bool(getattr(credential_use, '_armed', True)):
            with credential_use(
                reference,
                consumer='http',
                target_url=request.url,
                expected_generation=expected_generation,
            ) as material:
                values = dict(material.values)
                effective_ref = str(material.credential_ref)
                effective_scope = str(material.access_scope)
                generation = material.generation
        else:
            with task_credentials.authorized_credential_material(
                auth_agent,
                reference,
                consumer='http',
                target_url=request.url,
                expected_generation=expected_generation,
            ) as material:
                values = dict(material.values)
                effective_ref = str(material.credential_ref)
                effective_scope = str(material.access_scope)
                generation = material.generation
        if effective_ref != row['credential_ref']:
            path = credential_path(home_paths.owner_home_dir, effective_ref)
            row = read_credential_row(path, stage='credential_use')
            selection = {
                'schema': 'credential-selection.v1',
                'requested_credential_ref': reference,
                'effective_credential_ref': effective_ref,
                'access_scope': effective_scope,
            }

    turn_red = turn_credential_redaction(agent)
    cipher = _cipher(agent.home_paths.owner_home_dir, existing_only=True)
    outbound_count = 0

    def guarded_send(outbound_request):
        nonlocal outbound_count
        outbound_count += 1
        try:
            return send(outbound_request)
        except CredentialInputError:
            raise
        except Exception as exc:
            _scrub_exception_secrets(exc, secrets)
            raise

    secrets = list(values.values())
    try:
        config = row.get('auth_config', {})
        if row['authentication'] in {'basic', 'bearer', 'header'}:
            headers, secret_values = _credential_headers_from_values(row, values)
            secrets.extend(secret_values)
            secrets.extend(headers.values())
            auth_req = _authenticated(request, headers, row['origin'])
            _register_pre_send_secrets(turn_red, secrets, credential_use=credential_use)
            result = guarded_send(auth_req)
        elif row['authentication'] == 'signature':
            headers = _signed_headers(config, values, request)
            secrets.extend(headers.values())
            auth_req = _authenticated(request, headers, row['origin'])
            _register_pre_send_secrets(turn_red, secrets, credential_use=credential_use)
            result = guarded_send(auth_req)
        elif row['authentication'] == 'login':
            session = _login_headers(
                agent,
                path,
                row,
                values,
                request,
                guarded_send,
                cipher,
                turn_red=turn_red,
                credential_use=credential_use,
                known_secrets=secrets,
                first_send=True,
                outbound_count_fn=lambda: outbound_count,
                business_request_sent=False,
            )
            if isinstance(session, ToolHandlerOutcome):
                secret_tuple = tuple(dict.fromkeys(secrets))
                return _with_credential_selection(session, selection), secret_tuple
            headers, session_secrets = session
            secrets.extend((*session_secrets, *headers.values()))
            auth_req = _authenticated(request, headers, row['origin'])
            result = guarded_send(auth_req)
            # 仅 GET/HEAD 可在明确 401 后重试；变更请求不重做未知副作用。
            if request.method in {'GET', 'HEAD'} and _unauthorized(result):
                refresh_row = {**row, 'session_expires_at': 0}
                session = _login_headers(
                    agent,
                    path,
                    refresh_row,
                    values,
                    request,
                    guarded_send,
                    cipher,
                    turn_red=turn_red,
                    credential_use=credential_use,
                    known_secrets=secrets,
                    first_send=False,
                    outbound_count_fn=lambda: outbound_count,
                    business_request_sent=True,
                )
                if isinstance(session, ToolHandlerOutcome):
                    secret_tuple = tuple(dict.fromkeys(secrets))
                    return _with_credential_selection(session, selection), secret_tuple
                headers, session_secrets = session
                secrets.extend((*session_secrets, *headers.values()))
                auth_req = _authenticated(request, headers, row['origin'])
                result = guarded_send(auth_req)
        else:
            raise ValueError('宿主认证方式无效')
        secret_tuple = tuple(dict.fromkeys(secrets))
        result = credential_response_outcome(agent, path, row, result)
        if isinstance(result, RawResponseParts) and headers:
            proof = protected_resource_check(current, credential_ref=effective_ref, generation=generation,
                method=request.method, http_status=result.status)
            if proof is not None and _confirmed_protected_read(request, result, row, send):
                result = replace(result, protected_resource_check=proof)
        return _with_credential_selection(result, selection), secret_tuple
    finally:
        values.clear()


# LLM: 只在本片已可出证（凭据就绪唤醒、GET/HEAD、2xx）之后调用。请求与实际响应都须是凭据记录的原目标资源，
#   scheme/host/port/path 只在内存比较、不落盘 URL；保护证据是同一目标不带任何凭据头时被要求认证（401/403）。
#   同源其它资源、跳到别处的响应或公开目标都不出证；只为这一次出证读取补发一个无凭据请求，普通认证读取不发。
# 函数用途: 判定一次成功的认证读取是否证明了原目标受保护资源的访问；证据读不到时不出证，业务结果照常返回。
def _confirmed_protected_read(request, result, row, send):
    target = _resource_identity(row.get('target_url'))
    if target is None or _resource_identity(request.url) != target or _resource_identity(result.url) != target:
        return False
    # 只留 User-Agent：模型自带的其它头可能被目标当作（错误的）认证而拒绝，不能让它伪造保护证据。
    probe = replace(request, headers={key: value for key, value in request.headers.items()
                                      if key.lower() == 'user-agent'}, data=None, authorization_origin='')
    try:
        challenged = send(probe)
    except Exception:  # noqa: BLE001 - 保护证据只决定是否出证，不能让已取得的业务结果失败。
        return False
    return credential_http_status(challenged) in (401, 403)


def _resource_identity(url):
    if not isinstance(url, str):
        return None
    try:
        return credential_origin(url), urlsplit(url).path or '/'
    except ValueError:
        return None


def _notify_http_secret_sinks(
    secrets: Iterable[str],
    *,
    credential_use: object | None,
    turn_red: object | None,
) -> None:
    clean = tuple(dict.fromkeys(v for v in secrets if isinstance(v, str) and v and v.strip()))
    if not clean:
        return
    sink = _active_http_secret_sink.get()
    if callable(sink):
        sink(clean)
    if credential_use is None:
        return
    use_turn = getattr(credential_use, 'turn_redaction', None)
    with_vals = getattr(getattr(credential_use, 'redaction', None), 'with_credential_values', None)
    if use_turn is turn_red and callable(with_vals) and hasattr(credential_use, '_redaction'):
        credential_use._redaction = with_vals(clean)
        return
    register_values = getattr(credential_use, 'register_values', None)
    if callable(register_values):
        register_values(clean)


def _register_pre_send_secrets(
    turn_red: object,
    secrets: Iterable[str],
    *,
    credential_use: object | None = None,
) -> None:
    """在首个认证 HTTP 请求发出前完成回合脱敏登记；若额度已满则零发包拒绝。"""
    clean = tuple(dict.fromkeys(v for v in secrets if isinstance(v, str) and v and v.strip()))
    if getattr(turn_red, 'withheld', False):
        raise CredentialInputError(
            'CREDENTIAL_INPUT_UNAVAILABLE',
            '当前回合凭据脱敏额度（256 项 / 512 KiB）已满，本次未发送任何认证 HTTP 请求；请开启新回合后继续。',
            stage='runtime_gate',
            effect_outcome='not_started',
        )
    if clean:
        try_reg = getattr(turn_red, 'try_register_values', None)
        try:
            if callable(try_reg):
                accepted = try_reg(clean) is not None
            else:
                turn_red.register_values(clean)
                accepted = not bool(getattr(turn_red, 'withheld', False))
        except ValueError:
            accepted = False
        if not accepted:
            raise CredentialInputError(
                'CREDENTIAL_INPUT_UNAVAILABLE',
                '当前回合凭据脱敏额度（256 项 / 512 KiB）已满，本次未发送任何认证 HTTP 请求；请开启新回合后继续。',
                stage='runtime_gate',
                effect_outcome='not_started',
            )
    _notify_http_secret_sinks(clean, credential_use=credential_use, turn_red=turn_red)


def _register_post_send_secrets(
    turn_red: object,
    secrets: Iterable[str],
    *,
    credential_use: object | None = None,
) -> bool:
    """在登录响应签发新会话凭据后、后续业务请求发出前立即登记；超额返回 False 供调用方扣留输出。"""
    clean = tuple(dict.fromkeys(v for v in secrets if isinstance(v, str) and v and v.strip()))
    _notify_http_secret_sinks(clean, credential_use=credential_use, turn_red=turn_red)
    if not clean:
        return not bool(getattr(turn_red, 'withheld', False))
    try:
        turn_red.register_values(clean)
    except ValueError:
        return False
    return not bool(getattr(turn_red, 'withheld', False))


def _scrub_exception_secrets(exc: BaseException, secrets: Iterable[str]) -> None:
    variants = credential_echo_variants(tuple(v for v in secrets if isinstance(v, str) and v))
    if not variants or not getattr(exc, 'args', None):
        return
    changed = False
    cleaned_args = []
    for item in exc.args:
        if isinstance(item, str):
            updated = item
            for secret in variants:
                if secret in updated:
                    updated = updated.replace(secret, '[private]')
                    changed = True
            cleaned_args.append(updated)
        else:
            cleaned_args.append(item)
    if not changed:
        return
    try:
        if isinstance(exc, OSError) and isinstance(getattr(exc, 'strerror', None), str):
            text = exc.strerror
            for secret in variants:
                text = text.replace(secret, '[private]')
            exc.strerror = text
        exc.args = tuple(cleaned_args)
    except Exception:  # noqa: BLE001
        pass


def _collected_preflight_outcome(requested_ref: str, row: dict) -> ToolHandlerOutcome:
    """未配置 collected 卡在 HTTP 预检零发包、零解密返回有效引用与明确消费路径，不按字段名猜测注入协议。"""
    effective_ref = str(row['credential_ref'])
    field_names = [
        str(item['name'])
        for item in (row.get('fields') or ())
        if isinstance(item, dict) and isinstance(item.get('name'), str)
    ]
    name_set = set(field_names)
    if name_set == {'secret'}:
        configure_call = (
            f'request_credentials(operation="configure", credential_ref="{effective_ref}", authentication="bearer")'
        )
    elif name_set == {'username', 'secret'}:
        configure_call = (
            f'request_credentials(operation="configure", credential_ref="{effective_ref}", authentication="basic")'
            f'（或 authentication="login" 并显式给出同源登录 url、body_fields 与 body_encoding="json"|"form"）'
        )
    else:
        configure_call = (
            f'request_credentials(operation="configure", credential_ref="{effective_ref}", authentication="login", '
            'auth_config={"url": "<同源登录URL>", "body_fields": {...}, "body_encoding": "json"})'
        )
    sample_field = 'secret' if 'secret' in name_set else (field_names[-1] if field_names else 'secret')
    browser_call = f'fill(selector="<控件选择器>", credential_ref="{effective_ref}", field="{sample_field}")'
    script_call = f'run_command(command="<同步查询命令>", credential_ref="{effective_ref}")'
    message = (
        f'凭据 {effective_ref} 已安全保存（authentication=collected），尚未绑定 HTTP 认证方案；'
        f'本次未解密且未发送任何 HTTP 请求（outbound_requests=0）。禁止重新发卡，请复用同一引用选择合适入口：'
        f'1) 明确 HTTP 认证先调用 {configure_call}，再调用 web_fetch；'
        f'2) 网页表单或页面 JS 加密登录改用受控浏览器 browser-lite {browser_call}；'
        f'3) 客户端自定义变换改用同步脚本 {script_call}（从 MY_AGENT_CREDENTIAL_FILE 读取原字段）。'
    )
    return ToolHandlerOutcome(
        'web_fetch',
        False,
        message,
        error_code='CREDENTIAL_BINDING_INVALID',
        effect_outcome='not_started',
        result_envelope={
            'credential_ref': effective_ref,
            'requested_credential_ref': requested_ref,
            'origin': row.get('origin', ''),
            'authentication': 'collected',
            'authentication_configured': False,
            'fields': field_names,
            'outbound_requests': 0,
            'available_consumers': ['configure_http', 'browser_fill', 'run_command'],
            'credential_error': {
                'stage': 'http_preflight',
                'error_code': 'CREDENTIAL_BINDING_INVALID',
                'reason': 'authentication_not_configured',
                'side_effect_state': 'not_started',
            },
        },
        next_action=(
            f'本次 HTTP 零发包（effect_outcome=not_started）。请直接复用 {effective_ref}：'
            f'标准 API 先 {configure_call}；网页登录用 browser-lite {browser_call}；'
            f'自定义变换用 {script_call}。不要重复发送凭据卡。'
        ),
    )


# LLM: 只有宿主认证入口可构造本事实；引用均已原路径校验，绝不复制账号、密文或认证头，不能据展示裁决权限。
# 函数用途: 让工具审计准确记录请求引用与实际使用的凭据，便于确认群成员使用了哪个账号范围。
def _with_credential_selection(response, selection):
    if selection['access_scope'] != 'group':
        return response
    if isinstance(response, ToolHandlerOutcome):
        return replace(response, result_envelope={**response.result_envelope, 'credential_selection': selection})
    return replace(response, credential_selection=selection) if isinstance(response, RawResponseParts) else response


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


# LLM: cache只存同源密文，原ready helper/generation阻止旧登录覆盖；会话期限始终有限，原ready不限期不使session无限期；不持锁发网络。
# 函数用途: 取得会话或同源登录，保留原typed失败；成功才在原锁内保存有限Cookie/Token/CSRF密文。
def _login_headers(
    agent,
    path,
    row,
    values,
    request,
    send,
    cipher,
    *,
    turn_red=None,
    credential_use=None,
    known_secrets: list[str] | None = None,
    first_send: bool = True,
    outbound_count_fn: Callable[[], int] | None = None,
    business_request_sent: bool = False,
):
    config = row['auth_config']
    if credential_origin(config['url']) != row['origin']:
        raise ValueError('登录地址必须同源')
    active_turn = turn_red if turn_red is not None else turn_credential_redaction(agent)
    tracked = known_secrets if known_secrets is not None else list(values.values())
    if row.get('session_ciphertext') and time.time() < float(row.get('session_expires_at', 0)):
        headers = json.loads(cipher.decrypt(row['session_ciphertext'].encode()))
        session_secrets = _extract_session_secrets(config, headers)
        _authenticated(request, headers, row['origin'])
        tracked.extend((*session_secrets, *headers.values()))
        if first_send:
            _register_pre_send_secrets(active_turn, tracked, credential_use=credential_use)
        return headers, session_secrets

    body, content_type, encoding = _encode_login_body(config, values)
    if first_send:
        _register_pre_send_secrets(active_turn, tracked, credential_use=credential_use)
    login = replace(
        request,
        url=config['url'],
        method='POST',
        data=body,
        headers={'Content-Type': content_type},
        authorization_origin=row['origin'],
    )
    raw_response = send(login)
    if credential_http_status(raw_response) == 415:
        return _login_415_outcome(row, raw_response, encoding=encoding, content_type=content_type)
    challenge = _login_verification_challenge(row, raw_response)
    if challenge is not None and not isinstance(raw_response, RawResponseParts):
        return challenge
    response = credential_response_outcome(agent, path, row, raw_response, login_response=True)
    if isinstance(response, ToolHandlerOutcome):
        return response
    if not isinstance(response, RawResponseParts):
        raise ValueError('宿主登录未成功')
    try:
        headers = _session_headers(config, response)
    except (ValueError, KeyError, TypeError):
        if challenge is not None:
            return challenge
        return ToolHandlerOutcome('web_fetch', False, '登录响应缺少声明的有效会话信息，无法完成认证；未判定凭据错误。',
            error_code='CREDENTIAL_INPUT_UNAVAILABLE', result_envelope={'http_status': response.status})
    session_secrets = _extract_session_secrets(config, headers)
    new_session_values = (*session_secrets, *headers.values())
    tracked.extend(new_session_values)
    with _credential_mutation(agent, path) as admission:
        from .task_credential_schema import credential_ready_expired
        latest = read_json_object_report(path, context='credentials.session').payload
        now = time.time()
        if (latest.get('status') != 'ready' or latest.get('generation') != row['generation']
                or latest.get('verification_status') == 'rejected' or credential_ready_expired(latest, now=now)):
            raise ValueError('登录期间原凭据已失效')
        deadline = latest['ready_expires_at']
        session_deadline = now + config['session_seconds']
        latest.update(session_ciphertext=cipher.encrypt(json.dumps(headers).encode()).decode(),
                      session_expires_at=min(session_deadline, deadline) if deadline > 0 else session_deadline)
        _save_credential_row(admission, path, latest)
    if not _register_post_send_secrets(active_turn, new_session_values, credential_use=credential_use):
        sent_count = outbound_count_fn() if callable(outbound_count_fn) else 1
        return _post_login_withheld_outcome(
            row,
            response.status,
            outbound_requests=max(1, int(sent_count)),
            business_request_sent=business_request_sent,
        )
    return headers, session_secrets


def _extract_session_secrets(config: dict, headers: dict[str, str]) -> tuple[str, ...]:
    secrets = list(headers.values())
    if config['token_path']:
        secrets.append(headers[config['token_header']][len(config['token_prefix']):])
    if config['cookies']:
        cookies = SimpleCookie()
        cookies.load(headers['Cookie'])
        secrets.extend(cookie.value for cookie in cookies.values())
    return tuple(secrets)


def _post_login_withheld_outcome(
    row: dict,
    http_status: int,
    *,
    outbound_requests: int,
    business_request_sent: bool,
) -> ToolHandlerOutcome:
    """登录请求已发出但新发会话材料超出当前回合脱敏预算时，停止后续业务请求并扣留不可验证输出，保留真实网络副作用事实。"""
    effective_ref = str(row['credential_ref'])
    message = (
        f'登录请求已发送（HTTP {http_status}，outbound_requests={outbound_requests}），'
        '但当前回合凭据脱敏额度（256 项 / 512 KiB）已满，无法对新签发会话凭据提供完整回合脱敏保障；'
        '已扣留后续输出且未继续发送下一条业务请求。请开启新回合后复用同一凭据引用继续。'
    )
    return ToolHandlerOutcome(
        'web_fetch',
        False,
        message,
        error_code='CREDENTIAL_INPUT_UNAVAILABLE',
        failure_stage='execution',
        effect_outcome='failed',
        result_envelope={
            'credential_ref': effective_ref,
            'http_status': http_status,
            'outbound_requests': outbound_requests,
            'credential_error': {
                'stage': 'execution',
                'error_code': 'CREDENTIAL_INPUT_UNAVAILABLE',
                'reason': 'turn_redaction_budget_exhausted',
                'side_effect_state': 'failed',
                'login_request_sent': True,
                'business_request_sent': business_request_sent,
                'output_withheld': True,
            },
        },
        next_action=(
            f'本次已发出 {outbound_requests} 次认证相关 HTTP 请求（effect_outcome=failed，'
            f'business_request_sent={str(business_request_sent).lower()}）；禁止在当前回合原样重试或重发凭据卡，'
            f'请开启新回合复用 {effective_ref}。'
        ),
    )


def _encode_login_body(config: dict, values: dict) -> tuple[bytes, str, str]:
    """按声明的 body_encoding（缺省 json，可选 form）序列化同源登录字段，不猜字段或客户端加密。"""
    encoding = config.get('body_encoding', 'json')
    if encoding not in {'json', 'form'}:
        raise ValueError('登录请求体编码方式无效')
    payload = {key: values[field] for key, field in config['body_fields'].items()}
    if encoding == 'form':
        return urlencode(payload).encode('utf-8'), 'application/x-www-form-urlencoded', 'form'
    return json.dumps(payload).encode('utf-8'), 'application/json', 'json'


def _login_415_outcome(row: dict, response, *, encoding: str, content_type: str) -> ToolHandlerOutcome:
    """HTTP 415 保留登录请求已发与实际状态码，指引复用同一凭据引用改走浏览器或同步脚本，不判定密码错误也不重发卡。"""
    effective_ref = str(row['credential_ref'])
    error_code = (
        response.error_code
        if isinstance(response, ToolHandlerOutcome) and response.error_code
        else 'TOOL_INVALID_ARGUMENTS'
    )
    message = (
        f'登录请求已发送（POST Content-Type={content_type}，body_encoding={encoding}），'
        f'目标服务返回 HTTP 415（不支持该媒体类型，或要求网页前端加密/特定表单提交）；未判定凭据错误，禁止重发凭据卡或原样重放登录。'
        f'请复用同一凭据引用 {effective_ref} 改走受控浏览器 browser-lite fill(credential_ref="{effective_ref}", field=...) '
        f'或同步前台脚本 run_command(credential_ref="{effective_ref}") 完成登录后再读取受保护资源。'
    )
    return ToolHandlerOutcome(
        'web_fetch',
        False,
        message,
        error_code=error_code,
        effect_outcome='failed',
        result_envelope={
            'http_status': 415,
            'credential_ref': effective_ref,
            'login_attempt': {
                'sent': True,
                'http_status': 415,
                'body_encoding': encoding,
                'content_type': content_type,
                'side_effect_state': 'login_request_sent',
            },
            'fallback_consumers': ['browser_fill', 'run_command'],
        },
        next_action=(
            f'登录请求已发送且返回 HTTP 415（effect_outcome=failed）；禁止重复发卡或原样重试。'
            f'请改用同一凭据 {effective_ref} 调 browser-lite fill(credential_ref="{effective_ref}", field=...) '
            f'或同步 run_command(credential_ref="{effective_ref}")。'
        ),
    )


def _login_verification_challenge(row: dict, response) -> ToolHandlerOutcome | None:
    """识别结构化人工 MFA/CAPTCHA 验证响应，明确报告已到达阶段并阻止重复发送密码卡。"""
    status = credential_http_status(response)
    kind = ''
    if isinstance(response, RawResponseParts) and response.body:
        try:
            parsed = json.loads(response.body)
        except (ValueError, UnicodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get('captcha_required') is True or parsed.get('challenge_type') == 'captcha':
                kind = 'captcha'
            elif parsed.get('mfa_required') is True or parsed.get('challenge_type') in {'mfa', 'otp', 'totp'}:
                kind = 'mfa'
            elif parsed.get('verification_required') is True:
                kind = 'human_verification'
    elif status == 428:
        kind = 'mfa'
    if not kind:
        return None
    effective_ref = str(row['credential_ref'])
    envelope = {
        'credential_ref': effective_ref,
        'verification_challenge': {
            'required': True,
            'kind': kind,
            'stage': 'login_submitted',
            'login_request_sent': True,
        },
    }
    if status is not None:
        envelope['http_status'] = status
    return ToolHandlerOutcome(
        'web_fetch',
        False,
        (
            f'登录请求已提交，源站要求人工验证（{kind}）；原凭据 {effective_ref} 仍保留且未被判定错误，'
            '请向用户说明当前已到达人工验证阶段并请用户完成对应验证，禁止重新发送密码卡。'
        ),
        error_code='CREDENTIAL_INPUT_UNAVAILABLE',
        effect_outcome='failed',
        result_envelope=envelope,
        next_action=f'登录已提交但源站要求人工验证（{kind}）；请提示用户完成验证，禁止对 {effective_ref} 重新发密码卡。',
    )


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


# LLM: 复用验证模块的同一typed状态投影；只有明确401允许安全读取请求刷新，不解析正文或从网络异常推断失效。
# 函数用途: 判断原宿主响应是否明确401，拒绝浮点或模型字典冒充HTTP状态。
def _unauthorized(response):
    return credential_http_status(response) == 401
