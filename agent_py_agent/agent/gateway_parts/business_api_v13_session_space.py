"""原生会话空间选择动作；目标线程只来自本次已认证请求。"""
from .business_api_v13_contracts import V13Action

_PARAMS = {'type': 'object', 'properties': {}, 'additionalProperties': False}
_SET_PARAMS = {'type': 'object', 'properties': {'space_id': {'type': 'string', 'minLength': 1}},
               'required': ['space_id'], 'additionalProperties': False}
_RESULT = {'type': 'object', 'properties': {
    'space_id': {'type': ['string', 'null']}, 'revision': {'type': 'integer', 'minimum': 0},
    'source': {'enum': ['default', 'thread']}},
    'required': ['space_id', 'revision', 'source'], 'additionalProperties': False}


def _ready(call):
    return (callable(getattr(call.services.host, 'business_session_space', None))
            or getattr(call.services.host, 'conversation_store', None) is not None)


def _invoke(call):
    from .business_api_v13 import v13_require_access

    v13_require_access(call)
    native = getattr(call.context.host, 'business_session_space', None)
    if callable(native):
        return native(call.action, call.params, services=call.services,
            expected_revision=call.expected_revision, idempotency_key=call.idempotency_key)
    host, context = call.context.host, call.context
    from ..plugin_host.errors import HostAPIError

    source = context.session_space_source
    if not callable(source):
        raise HostAPIError('SESSION_SPACE_SOURCE_REQUIRED', 403,
            message='会话空间操作缺少当前已认证的Gateway主回合；请在原会话中调用。')
    thread, actor_id = source(call.action)
    store = host.conversation_store
    return session_space_operation(host.home_paths, store.threads, thread, actor_id=actor_id,
        action=call.action, params=call.params, services=call.services, context=context,
        expected_revision=call.expected_revision, idempotency_key=call.idempotency_key)


V13_ACTIONS = (
    V13Action('sessions.space.get', 'read', _PARAMS, _RESULT, _invoke, ready=_ready),
    V13Action('sessions.space.set', 'write', _SET_PARAMS, _RESULT, _invoke, ready=_ready,
              expected_revision_required=True),
)


def session_space_operation(home, threads, thread, *, actor_id, action, params, services, context,
                            expected_revision=None, idempotency_key=''):
    """HTTP与当前主会话模型共用同一选择账、权限复核及CAS；不借详情投影判断可用性。"""
    from ..conversation.business_access import BusinessError
    from ..conversation.session_space import read_session_space, set_session_space

    context.check(action, {'owner_id': home.owner_id, **params})
    if thread is not None and (thread.owner_id != home.owner_id or thread.owner_home != str(home.owner_home_dir)):
        raise BusinessError('RESOURCE_NOT_FOUND')
    if action == 'sessions.space.get':
        # 只读本人的选择引用，不读取旧空间；失效引用不能阻止用户切换到新空间。
        return read_session_space(home, thread, actor_id=actor_id)
    actor = services.actor(context)
    space_id = params['space_id']

    def authorize():
        context.check(action, {'owner_id': home.owner_id, 'space_id': space_id})
        state = services.spaces.authorize_call('spaces.get', {'space_id': space_id}, actor=actor)
        if state['spaces'][space_id]['state'] != 'active':
            raise BusinessError('SPACE_ARCHIVED')

    return set_session_space(home, threads, thread, actor_id=actor_id, space_id=space_id,
        expected_revision=expected_revision, idempotency_key=idempotency_key, authorize=authorize)
