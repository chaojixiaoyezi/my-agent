"""原观察actor的请求局部传递；产物来源在读取字节/漂移诊断前核准，绝不从路径授予权限。"""
from contextvars import ContextVar
from functools import wraps

from ..plugin_host.errors import HostAPIError
from .business_access import BusinessError, require_access

_ACCESS = ContextVar('artifact_observation_access', default=None)


def artifact_query_access(query):
    @wraps(query)
    def wrapped(service, action, params, *, actor):
        token = _ACCESS.set((service, actor, action) if action in {'artifacts.get', 'artifacts.list'} else None)
        try:
            return query(service, action, params, actor=actor)
        finally:
            _ACCESS.reset(token)
    return wrapped


def check_artifact_access(facts, *, authority=None):
    from .business_observability import ObservationSourceError

    current = authority if authority is not None else _ACCESS.get()
    if current is None:
        raise ObservationSourceError('HOST_NOT_READY', details={'reason': 'ARTIFACT_AUTHORIZATION_UNAVAILABLE',
            'side_effect_state': 'none', 'next_action': '必须经原已认证观察或下载入口核验授权；不能直接按定位路径读内容。'})
    service, actor, action = current
    try:
        require_access(actor, action, service=service, owner_id=facts.owner_id, space_id=facts.space_id,
            project_ref=facts.project_ref, task_id=facts.task_id, thread_id=facts.thread_id,
            run_id=facts.run_id, attempt_id=facts.attempt_id)
    except (BusinessError, HostAPIError) as error:
        denied = isinstance(error, BusinessError) or error.status in {403, 404}
        if action == 'artifacts.list' and (isinstance(error, BusinessError) or isinstance(error, HostAPIError) and error.status == 404):
            return False
        code = ('BUSINESS_ACCESS_DENIED' if action == 'artifacts.list' else 'OBSERVATION_NOT_FOUND') if denied else 'HOST_NOT_READY'
        raise ObservationSourceError(code, details={'reason': 'ARTIFACT_NOT_VISIBLE' if denied else 'ARTIFACT_AUTHORIZATION_UNAVAILABLE',
            'authorization_code': error.code, 'error_type': type(error).__name__,
            'side_effect_state': 'none', 'next_action': '当前来源授权未通过，未读取产物内容；请在原授权范围内核验后查询。'}) from error
    return True
