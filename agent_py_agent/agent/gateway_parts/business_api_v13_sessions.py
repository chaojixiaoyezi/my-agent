"""#183 会话目录业务动作：只经 W1 规范 helper 读取当前认证 owner，不新增存储或权限。"""
from ..conversation.business_access import BusinessError
from ..plugin_host.business_action_specs import BUSINESS_LIST_DEFAULT_ROWS, BUSINESS_LIST_MAX_ROWS
from ..plugin_host.errors import HostAPIError
from .business_api_v13_contracts import V13Action

_SESSION_ITEM = {
    'type': 'object',
    'properties': {
        'conversation_id': {'type': 'string'},
        'thread_id': {'type': 'string'},
        'owner_id': {'type': 'string'},
        'channel': {'type': 'string'},
        'channel_user_id': {'type': 'string'},
        'title': {'type': 'string'},
        'preview': {'type': 'string', 'maxLength': 160},
        'created_at': {'type': 'number'},
        'updated_at': {'type': 'number'},
        'message_count': {'type': ['integer', 'null']},
        'in_flight': {'type': 'boolean'},
        'active_request_id': {'type': ['string', 'null']},
        'active_run_id': {'type': ['string', 'null']},
        'status': {'enum': ['running', 'queued', 'idle', 'failed']},
    },
    'required': ['conversation_id', 'thread_id', 'owner_id', 'channel', 'channel_user_id',
        'title', 'preview', 'created_at', 'updated_at', 'message_count', 'in_flight',
        'active_request_id', 'active_run_id', 'status'],
    'additionalProperties': False,
}

_PARAMS = {
    'type': 'object',
    'properties': {
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': BUSINESS_LIST_MAX_ROWS,
            'default': BUSINESS_LIST_DEFAULT_ROWS},
        'cursor': {'$ref': '#/$defs/Cursor'},
        'include_inflight': {'type': 'boolean', 'default': True},
    },
    'required': [],
    'additionalProperties': False,
}

_RESULT = {
    'type': 'object',
    'properties': {
        'owner_id': {'type': 'string', 'minLength': 1, 'maxLength': 512},
        'items': {'type': 'array', 'maxItems': BUSINESS_LIST_MAX_ROWS, 'items': _SESSION_ITEM},
        'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]},
        'coverage': {'$ref': '#/$defs/Coverage'},
        'inflight_coverage': {'type': 'string',
            'enum': ['complete', 'partial', 'skipped', 'unavailable']},
        'load_errors': {'type': 'array', 'maxItems': BUSINESS_LIST_MAX_ROWS,
            'items': {'type': 'object'}},
    },
    'required': ['owner_id', 'items', 'next_cursor', 'coverage', 'inflight_coverage', 'load_errors'],
    'additionalProperties': False,
}


# LLM: 只检查原生宿主是否绑定规范只读闭包，不打开 Store、不创建目录，也不签发权限。
# 函数用途: 判断当前宿主是否接入会话列表读取口。
def _ready(call):
    return callable(getattr(call.services.host, 'business_session_list', None))


# LLM: 读取前复核原上下文，只调用context.host的当前请求闭包，不回退缓存宿主；保留W1结果及既有错误码。
# 函数用途: 返回当前认证viewer在本owner内的会话列表业务投影。
def _list(call):
    from .business_api_v13 import v13_require_access

    v13_require_access(call)
    params = call.params
    try:
        body = call.context.host.business_session_list(limit=params['limit'], cursor=params.get('cursor'),
            include_inflight=params['include_inflight'])
    except ValueError as exc:
        if str(exc) == 'invalid session cursor':
            raise BusinessError('CURSOR_EXPIRED') from exc
        raise
    if body.get('ok') is not True:
        raise HostAPIError('HOST_NOT_READY', 503)
    return {key: body[key] for key in (
        'owner_id', 'items', 'next_cursor', 'coverage', 'inflight_coverage', 'load_errors')}


V13_ACTIONS = (V13Action('sessions.list', 'read', _PARAMS, _RESULT, _list, ready=_ready),)
