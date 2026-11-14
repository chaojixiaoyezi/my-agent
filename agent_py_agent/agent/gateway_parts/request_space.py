"""受理时冻结当前发送者的空间选择；执行重放只校验原快照。"""
from __future__ import annotations

REQUEST_SPACE_KEY = "host_session_space.v1"
_FIELDS = {"owner_id", "actor_id", "thread_id", "space_id", "revision", "source"}


class GatewayMemoryScopeError(ValueError):
    """已选择空间不可读时拒绝扩大召回；错误只携带结构码，不含业务正文。"""

    def __init__(self, code):
        self.error_code = code
        self.side_effect_state = 'not_started'
        action = ('请在原聊天核对发送者绑定后重新发送。' if code.startswith('CHANNEL_ACTOR_')
                  else '请核对当前空间仍启用且默认项目可读，再重新发送。')
        super().__init__(f'记忆范围解析失败（{code}）；本轮模型和工具尚未执行。{action}')


def gateway_memory_recall_scope(host, snapshot, *, channel_actor, request_id):
    """把已验真的受理空间解析成只读记忆范围，不签发任务或工具业务上下文。"""
    if snapshot is None:
        return None
    from ..conversation.business_access import BusinessError
    from ..conversation.host_actor import HostActorError, validate_space_recall_actor
    from ..memory_store.recall import MemoryRecallScope
    from ..plugin_host.errors import HostAPIError
    from .business_api import local_business_context

    if snapshot.get('owner_id') != str(host.home_paths.owner_id):
        raise GatewayMemoryScopeError('SPACE_SCOPE_MISMATCH')
    try:
        validate_space_recall_actor(host, snapshot, channel_actor, request_id)
        authority, services = local_business_context(host)
        actor = services.actor(authority)
        spaces = services.spaces
        state = spaces.authorize_call('spaces.get', {'space_id': snapshot['space_id']}, actor=actor)
        row = spaces._space(state, snapshot['space_id'], active=True)
        project = row['default_project_ref']
        if project is not None:
            ref = spaces.reference('project', project, actor=actor, space_id=row['space_id'])
            if ref['state'] != 'available':
                raise BusinessError('REFERENCE_NOT_FOUND')
        if spaces._read()['revision'] != state['revision']:
            raise BusinessError('REVISION_CONFLICT')
        # 空间无默认项目时仅保留owner内通用范围，不能退回所有project。
        return MemoryRecallScope.from_runtime(task_attributes={'project_id': project})
    except HostActorError as error:
        raise GatewayMemoryScopeError(error.error_code) from error
    except (BusinessError, HostAPIError) as error:
        raise GatewayMemoryScopeError(error.code) from error


def capture_request_space(home, thread, *, actor_id):
    from ..conversation.session_space import read_session_space

    selected = read_session_space(home, thread, actor_id=actor_id)
    if selected["space_id"] is None:
        return None
    return {"owner_id": str(home.owner_id), "actor_id": actor_id,
            "thread_id": thread.thread_id, **selected}


def request_space_for_execution(request, *, owner_id, thread_id, actor_id):
    """只接受原受理快照，空间切换不重写排队/恢复中的请求。"""
    snapshot = request.get(REQUEST_SPACE_KEY)
    if snapshot is None:
        return None
    if (not isinstance(snapshot, dict) or set(snapshot) != _FIELDS
            or snapshot.get("owner_id") != owner_id or snapshot.get("thread_id") != thread_id
            or snapshot.get("actor_id") != actor_id
            or not isinstance(snapshot.get("space_id"), str) or not snapshot["space_id"]
            or type(snapshot.get("revision")) is not int or snapshot["revision"] < 1
            or snapshot.get("source") != "thread"):
        raise ValueError("受理空间快照与当前用户或会话不一致")
    return dict(snapshot)
