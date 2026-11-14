"""业务领域的窄化门；actor只能由认证宿主适配，不从JSON、角色标签或allowed_actions恢复。

原插件grant、路径和审批门仍须由调用入口执行；这里的实时回调只进一步收窄范围。
治理正文和审批票据归原Workflow/interaction服务，本模块没有第二份政策或票据账。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..plugin_host.business_action_specs import business_action_spec
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import validate_object

_UNDECLARED_EFFECT = object()


class BusinessError(ValueError):
    def __init__(self, code: str, *, details: dict | None = None, effect_outcome: str | object = _UNDECLARED_EFFECT):
        declared = effect_outcome is not _UNDECLARED_EFFECT
        effect_outcome = effect_outcome if declared else 'not_started'
        if effect_outcome not in {'not_started', 'committed', 'unknown'}:
            raise ValueError('业务拒绝必须声明not_started、committed或unknown副作用事实')
        self.effect_outcome = effect_outcome
        self.effect_outcome_declared = declared
        self.error_code = self.code = code
        self.details = details or {}
        # 不把原参数、路径、票据或外部异常正文带入公开说明。
        super().__init__(code)


    @property
    def effect_outcome(self):
        return self._effect_outcome

    @effect_outcome.setter
    def effect_outcome(self, value):
        # 后端可在真实提交边界补充效果证明；默认拒绝不能冒充这份显式证明。
        self._effect_outcome = value
        self.effect_outcome_declared = True


@dataclass(frozen=True)
class BusinessActor:
    """一次宿主调用的非序列化适配值；回调每次查原授权，不缓存grant结果。

    service/root均由宿主组合根绑定，不能经模型参数提供。content_authorizer仅接原内容门；
    单纯持有business.control、扮演human或提交evidence_ref都不等于正式采用许可。
    """
    owner_id: str
    subject_id: str
    service: object = field(repr=False, compare=False)
    authorize: Callable[[str, dict], bool] = field(repr=False, compare=False)
    content_authorizer: Callable[[dict], bool] | None = field(default=None, kw_only=True, repr=False, compare=False)

    def __post_init__(self):
        if (type(self.owner_id) is not str or not self.owner_id or type(self.subject_id) is not str
                or not self.subject_id or not callable(self.authorize)):
            raise BusinessError('ACTOR_REQUIRED')
        if self.content_authorizer is not None and not callable(self.content_authorizer):
            raise BusinessError('ACTOR_REQUIRED')


def require_actor(actor: object, service=None) -> BusinessActor:
    if type(actor) is not BusinessActor:
        raise BusinessError('ACTOR_REQUIRED')
    bound = service if service is not None else actor.service
    if actor.service is not bound or actor.owner_id != getattr(bound, 'owner_id', None):
        raise BusinessError('SPACE_SCOPE_MISMATCH')
    return actor


def require_access(actor: object, action: str, *, service=None, record=None, **scope) -> BusinessActor:
    actor = require_actor(actor, service)
    business_action_spec(action)  # 未知动作不经字符串前缀猜权限。
    if record is not None:
        if record.get('owner_id') != actor.owner_id:
            raise BusinessError('SPACE_SCOPE_MISMATCH')
        for key in ('space_id', 'project_ref'):
            if key not in record:
                continue  # 空间本身没有project_ref；不能把其默认作品当成资产的真实作品范围。
            if key in scope and scope[key] is not None and record[key] != scope[key]:
                raise BusinessError('SPACE_SCOPE_MISMATCH')
            if key not in scope:
                scope[key] = record[key]
    facts = {'owner_id': actor.owner_id, **scope}
    if actor.authorize(action, facts) is not True:
        raise BusinessError('BUSINESS_ACCESS_DENIED')
    return actor


def validate_params(action: str, params: object) -> dict:
    try:
        spec = business_action_spec(action)
        return validate_object(params, spec.params_schema)
    except HostAPIError as error:
        raise BusinessError('INVALID_SCHEMA') from error


def validate_result(action: str, result: object) -> dict:
    try:
        return business_action_spec(action).validate_result(result)
    except HostAPIError as error:
        details = dict(getattr(error, 'details', {}) or {})
        details['validation_stage'] = 'domain_result'
        details['next_action'] = ('该动作的领域结果不满足所列字段约束；按原 request_id 核对结果，'
            '由结果生产方修正类型、协议枚举或长度，读取长内容使用原分页/引用入口。'
            '本错误不证明原写操作未执行，不要重新发起写操作。')
        raise BusinessError('DOMAIN_RESULT_INVALID', details=details) from error


def require_revision(expected: object, actual: int) -> None:
    if type(expected) is not int or expected < 0:
        raise BusinessError('INVALID_SCHEMA')
    if expected != actual:
        raise BusinessError('REVISION_CONFLICT')


def content_authorization_facts(owner_id, action, params, expected_revision):
    """采用类变更只凭同一动作、完整规范参数和目标版本的摘要授权。"""
    import hashlib
    import json
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return {**params, 'owner_id': owner_id, 'action': action, 'parameters_sha256': digest,
            'expected_revision': expected_revision}


def require_content_authorization(actor: BusinessActor, facts: dict) -> None:
    if actor.content_authorizer is None or actor.content_authorizer(dict(facts)) is not True:
        raise BusinessError('HUMAN_RESPONSE_REQUIRED')


def governance_call(service, action, params, *, actor, expected_revision=None, idempotency_key=None):
    """只消费原治理端口；原后端负责CAS、批准/票据与幂等，不在业务JSON存政策正文。"""
    service.authorize_call(action, params, actor=actor)
    backend = service.governance
    if backend is None:
        raise BusinessError('HOST_NOT_READY')
    if action == 'approvals.decide':
        require_revision(expected_revision, expected_revision)
        if not idempotency_key:
            raise BusinessError('INVALID_SCHEMA')
        # W13的受信票据消费者必须同时核真实人、installation/activation/owner、
        # interaction/action/revision/attempt/expiry和决定；本层不接human=true，也不签票。
        decider = getattr(backend, 'decide_approval', None)
        if not callable(decider):
            raise BusinessError('HUMAN_RESPONSE_REQUIRED')
        return validate_result(action, decider(dict(params), actor=actor,
            expected_revision=expected_revision, idempotency_key=idempotency_key))
    sid = params['space_id']
    if action == 'policies.get':
        return validate_result(action, backend.get_policy(sid, actor=actor))
    policy = params['policy']
    service.reference('workflow', policy['workflow_ref'], actor=actor, space_id=sid)
    for key, kind in (('automatic_release_scope_ref', 'release_scope'), ('budget_ref', 'budget')):
        if key in policy:
            service.reference(kind, policy[key], actor=actor, space_id=sid)
    preview = validate_result('policies.preview', backend.preview_policy(sid, dict(policy), actor=actor))
    if action == 'policies.preview':
        return preview
    require_revision(expected_revision, expected_revision)
    if not idempotency_key:
        raise BusinessError('INVALID_SCHEMA')
    authorization = params.get('authorization_ref')
    verifier = getattr(backend, 'authorize_policy_change', None)
    if preview['approval_required'] and (not callable(verifier) or verifier(
            sid, dict(policy), authorization, actor=actor, expected_revision=expected_revision) is not True):
        raise BusinessError('HUMAN_RESPONSE_REQUIRED')
    service.authorize_call(action, params, actor=actor)
    # callback须在原Workflow提交门内再次调用，防预览之后撤权或变宽；不可当授权票缓存。
    def commit_guard():
        service.authorize_call(action, params, actor=actor)
        if preview['approval_required'] and verifier(sid, dict(policy), authorization,
                actor=actor, expected_revision=expected_revision) is not True:
            raise BusinessError('HUMAN_RESPONSE_REQUIRED')
    result = backend.apply_policy(sid, dict(policy), actor=actor, expected_revision=expected_revision,
        idempotency_key=idempotency_key, authorization_ref=authorization, commit_guard=commit_guard)
    return validate_result(action, result)
