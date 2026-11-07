"""OpenAI 兼容协议的薄运输层；身份和执行仍沿原 Gateway owner/租约。"""
from __future__ import annotations

from contextlib import ExitStack

from ..plugin_host.errors import HostAPIError


def send_error(handler, status, code):
    """只返回固定结构码，绝不把后端异常文本、请求正文或凭据写回。"""
    category = {400: 'invalid_request_error', 401: 'authentication_error', 403: 'permission_error',
        404: 'not_found_error', 429: 'rate_limit_error'}.get(status, 'api_error')
    if status == 413:
        category = 'invalid_request_error'
    handler._send_json(status, {'error': {'message': code, 'type': category, 'code': code, 'param': None}})


def reject_untrusted(handler, middleware):
    from ..auth.middleware import _handler_peer_ip

    peer = _handler_peer_ip(handler)
    if middleware.check_trusted(handler.headers, peer):
        return False
    code = middleware.edge_identity_error(handler.headers)
    send_error(handler, 403 if code else 401, code or 'CAPABILITY_DENIED')
    return True


def _handle(handler, server, *, testing=False):
    from ..business_spaces import gap158_w1_openai_compat as gateway
    from ..plugin_host.schemas import validate_object
    from .business_api import _native_json, _native_source, owner_complete_host
    from .business_api_v13 import v13_action_spec
    from .control_service import resolve_gateway_scope_owner
    from .http_handlers import _gateway_control_scope, _request_channel

    try:
        if server is None or getattr(server, 'agent', None) is None:
            raise HostAPIError('HOST_NOT_READY', 503)
        if not _native_source(handler):
            return
        if not getattr(server.agent.config, 'openai_compat_gateway_enabled', False):
            send_error(handler, 404, 'RESOURCE_NOT_FOUND')
            return
        body = _native_json(handler)
        if testing:
            body = validate_object(body, v13_action_spec('llm.gateway.test').params_schema)
        else:
            gateway.validate_request(body)
        # 模型 body 的 user 只是兼容标签，不进入身份、owner 或会话解析。
        user, channel = _request_channel(handler)
        scope = _gateway_control_scope(handler, {}, user_id=user, channel=channel)
        owner = resolve_gateway_scope_owner(server.agent, scope)
        with ExitStack() as stack:
            agent = owner_complete_host(server, owner, stack)
            if not getattr(agent.config, 'openai_compat_gateway_enabled', False):
                send_error(handler, 404, 'RESOURCE_NOT_FOUND')
                return
            kwargs = {'admission_key': user, 'quota_config': server.agent.config}
            if testing:
                result = gateway.test_gateway(agent, body, **kwargs)
            else:
                result = gateway.execute(agent, body, handler.headers, **kwargs)
        if not testing and body.get('stream', False):
            _send_stream(handler, result)
        else:
            handler._send_json(200, result)
    except HostAPIError as error:
        send_error(handler, error.status, error.code)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return
    except Exception:
        send_error(handler, 500, 'HOST_NOT_READY')


def _send_stream(handler, result):
    from ..business_spaces.gap158_w1_openai_compat import stream_frames
    from .http_service import _is_client_disconnect_error

    handler.close_connection = True
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('X-My-Agent-Stream-Mode', 'buffered')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        for frame in stream_frames(result):
            handler.wfile.write(frame)
            handler.wfile.flush()
    except OSError as error:
        if not _is_client_disconnect_error(error):
            raise


def handle_openai_chat_completions(handler, server):
    _handle(handler, server)


def handle_llm_gateway_test(handler, server):
    _handle(handler, server, testing=True)
