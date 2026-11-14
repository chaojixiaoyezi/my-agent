"""只解析已分配的准确任务ID；无建任务、排队、模型调用或上下文持久化副作用。"""
from __future__ import annotations

from .business_access import BusinessError, require_access, require_actor
from .business_assets import BusinessAssetService
from .business_bindings import InterfaceBindingService
from .business_contracts import BusinessExecutionContext, BusinessPackageVersion
from .business_spaces import _identifier


# LLM: 供SessionTask宿主factory首次解析准确task_id；只读核Actor、空间、绑定和引用并冻结选择，不建任务或调用模型；显式unassigned不采用唯一默认空间。
# 函数用途: 把本次派活的业务意图解析成可随任务保存的确定上下文。
def resolve_business_context(*, actor: object, task_id: str, requested_space_id: str | None,
        project_ref: str | None, binding_id: str | None, requested_method_refs: tuple[str, ...] = (),
        requested_asset_version_refs: tuple[str, ...] = (), unassigned: bool = False,
        host: object = None) -> BusinessExecutionContext:
    """仅供SessionTask的host-only factory首次调用；重放使用其原canonical context。

    W2负责将意图、准确stask ID和本结果同次原子写；这里不分配runtime ID，也不取latest。
    None默认选择与显式空间是不同意图；恢复时不能再次解析默认空间覆盖原冻结选择。
    """
    if unassigned:
        if requested_space_id is not None or binding_id is not None:
            raise BusinessError('SPACE_SCOPE_MISMATCH')
        return _unassigned_context(actor=actor, task_id=task_id, project_ref=project_ref,
            requested_method_refs=requested_method_refs, requested_asset_version_refs=requested_asset_version_refs,
            host=host)
    return _resolve_selection(actor=actor, task_id=task_id, requested_space_id=requested_space_id,
        project_ref=project_ref, binding_id=binding_id, requested_method_refs=requested_method_refs,
        requested_asset_version_refs=requested_asset_version_refs)


# LLM: 仅供工作流宿主工厂在原 WorkflowRunStore 创建运行前调用：准确 workflow_run_id 作逻辑 task_id（不是 SessionTask，
#   也不是编排父任务），来源由调用方与运行快照同次冻结；空间/项目/资产选择与访问、修订检查与 SessionTask 入口同一实现。
# 函数用途: 把一次原生工作流运行的业务意图解析成随运行记录冻结的上下文，不建任务或调用模型。
def resolve_workflow_run_context(*, actor: object, workflow_run_id: str, requested_space_id: str | None,
        project_ref: str | None) -> BusinessExecutionContext:
    """重放与恢复使用运行快照里的原上下文，不再调用本函数重新选默认空间。"""
    return _resolve_selection(actor=actor, task_id=workflow_run_id, requested_space_id=requested_space_id,
        project_ref=project_ref, binding_id=None, requested_method_refs=(), requested_asset_version_refs=())


# LLM: 只读核Actor、空间、绑定和引用并冻结选择；两类调用方各自声明task_id的来源，这里不分配ID也不取latest。
# 函数用途: 由准确逻辑任务ID与已认证Actor解析空间、项目、方法与资产版本选择。
def _resolve_selection(*, actor, task_id, requested_space_id, project_ref, binding_id, requested_method_refs,
        requested_asset_version_refs):
    actor = require_actor(actor)
    service = actor.service
    _identifier(task_id)
    for value in (requested_method_refs, requested_asset_version_refs):
        if type(value) is not tuple or len(set(value)) != len(value):
            raise BusinessError('INVALID_SCHEMA')
        for ref in value:
            _identifier(ref)
    state = service.authorize_call('spaces.list', {}, actor=actor)
    chosen = requested_space_id
    if binding_id is not None:
        _identifier(binding_id)
        state = service.authorize_call('interfaces.resolve', {'binding_id': binding_id}, actor=actor, state=state)
        binding = state['bindings'][binding_id]
        InterfaceBindingService(service)._checked(binding, state, actor=actor, action='interfaces.resolve')
        chosen = binding['default_space_id'] if chosen is None else chosen
        if chosen not in binding['space_ids']:
            raise BusinessError('SPACE_SCOPE_MISMATCH')
    if chosen is None:
        candidates = []
        for space in state['spaces'].values():
            if space['state'] != 'active':
                continue
            try:
                require_access(actor, 'spaces.get', service=service, record=space)
            except BusinessError as error:
                if error.code != 'BUSINESS_ACCESS_DENIED':
                    raise
            else:
                candidates.append(space['space_id'])
        if len(candidates) != 1:
            raise BusinessError('CONTEXT_REQUIRED')
        chosen = candidates[0]
    _identifier(chosen)
    row = service._space(state, chosen, active=True)
    require_access(actor, 'spaces.get', service=service, record=row)
    project = row['default_project_ref'] if project_ref is None else project_ref
    if project is not None:
        service.reference('project', project, actor=actor, space_id=chosen)
    methods, packages = [], []
    if requested_method_refs:
        for ref in requested_method_refs:
            resolved = service.reference('method', ref, actor=actor, space_id=chosen)
            if resolved['state'] != 'available':
                raise BusinessError('REFERENCE_NOT_FOUND')
            methods.append(ref)
    else:
        for mount in row['mounts']:
            if not mount['enabled']:
                continue
            resolved = service.reference('package', mount['package_ref'], actor=actor, space_id=chosen)
            if resolved['state'] == 'available':
                methods.append(mount['package_ref'])
                packages.append(_package_version(mount['package_ref'], resolved))
    versions = BusinessAssetService(service).selected_versions(state, actor=actor, space_id=chosen,
        project_ref=project, requested=requested_asset_version_refs)
    require_access(actor, 'spaces.get', service=service, record=row)
    if service._read()['revision'] != state['revision']:
        raise BusinessError('REVISION_CONFLICT')
    return BusinessExecutionContext(owner_id=actor.owner_id, space_id=chosen, project_ref=project,
        task_id=task_id, space_revision=row['revision'], binding_id=binding_id,
        selected_method_refs=tuple(methods), asset_version_refs=versions, schema_version='business-context.v1',
        package_versions=tuple(packages), package_versions_state='frozen')


# LLM: 无归属显式选择只冻结Owner任务，不借唯一默认空间或空间资产扩大权限；仍过原执行/工具门。
# 函数用途: 为可信原生或业务Actor形成真正space_id=null的canonical任务上下文。
def _unassigned_context(*, actor, task_id, project_ref, requested_method_refs, requested_asset_version_refs, host):
    from .host_actor import HostChannelActor

    _identifier(task_id)
    if project_ref is not None or requested_method_refs or requested_asset_version_refs:
        raise BusinessError('CONTEXT_REQUIRED')
    if isinstance(actor, HostChannelActor):
        if (host is None or actor.owner_id != str(host.home_paths.owner_id)
                or host.conversation_store.threads.require(actor.source_thread_id).owner_id != actor.owner_id):
            raise BusinessError('ACTOR_REQUIRED')
    else:
        actor = require_actor(actor)
        require_access(actor, 'tasks.assign', service=actor.service, space_id=None)
    return BusinessExecutionContext(owner_id=actor.owner_id, space_id=None, project_ref=None, task_id=task_id,
        space_revision=None, binding_id=None, selected_method_refs=(), asset_version_refs=(),
        schema_version='business-context.v1', package_versions_state='frozen')


# LLM: 供原执行器在使用canonical上下文时复核当前业务授权；只读校验Owner、空间或无归属约束及绑定/项目，不重算冻结选择，也不授予工具执行权限。
# 函数用途: 确认旧任务保存的业务归属在当前权限下仍然允许使用。
def revalidate_business_context(*, actor, context, package_ref=None):
    """只复核当前权限，不按新默认值迁移或重算旧任务的冻结选择。

    W5/原executor须从canonical任务把两项显式注入ActionPolicyRequest；这里不读模型字典、run_id或latest。
    原工具的执行/路径/审批权限随后仍须通过，当前空间view许可本身不授予任何工具权限。
    """
    actor = require_actor(actor)
    if type(context) is not BusinessExecutionContext:
        raise BusinessError('CONTEXT_REQUIRED')
    if context.owner_id != actor.owner_id:
        raise BusinessError('SPACE_SCOPE_MISMATCH')
    service = actor.service
    if context.space_id is None:
        if (context.space_revision is not None or context.binding_id is not None or context.project_ref is not None
                or context.selected_method_refs or context.asset_version_refs):
            raise BusinessError('SPACE_SCOPE_MISMATCH')
        require_access(actor, 'tasks.assign', service=service, space_id=None)
        return
    state = service.authorize_call('spaces.get', {'space_id': context.space_id}, actor=actor)
    service._space(state, context.space_id, active=True)
    if context.binding_id is not None:
        service.authorize_call('interfaces.resolve', {'binding_id': context.binding_id}, actor=actor, state=state)
        binding = state['bindings'][context.binding_id]
        if context.space_id not in binding['space_ids']:
            raise BusinessError('SPACE_SCOPE_MISMATCH')
        InterfaceBindingService(service)._checked(binding, state, actor=actor, action='interfaces.resolve')
    if context.project_ref is not None:
        project = service.reference('project', context.project_ref, actor=actor, space_id=context.space_id)
        if project['state'] != 'available':
            raise BusinessError('REFERENCE_NOT_FOUND')
    if package_ref is not None:
        _revalidate_package_versions(service, actor, context, package_ref)


def _package_version(ref, row):
    version = row.get('version_ref')
    if not isinstance(version, str) or not version:
        raise BusinessError('PACKAGE_VERSION_UNAVAILABLE', details={
            'package_ref': ref, 'field': 'version_ref', 'effect_outcome': 'not_started',
            'next_action': '核对原Owner包引用解析器的版本来源；取得原版本后创建新任务，不按最新包补旧任务。'})
    return BusinessPackageVersion(ref, version, row.get('activation_id'))


def _revalidate_package_versions(service, actor, context, package_ref):
    # 旧记录无法证明包版本，但不能据此拒绝取消、查旧产物或无关方法；沿原权限门，
    # 不补读最新版本、不提升工具权限，也不把原记录投影为已冻结。
    if context.package_versions_state == 'legacy_unproven':
        return
    for frozen in context.package_versions:
        if frozen.package_ref not in (package_ref, 'capability:' + package_ref):
            continue
        try:
            current = service.reference('package', frozen.package_ref, actor=actor, space_id=context.space_id)
        except BusinessError as error:
            raise BusinessError('PACKAGE_VERSION_UNAVAILABLE', details={
                'package_ref': frozen.package_ref, 'source_error_code': error.code,
                'expected_version_ref': frozen.version_ref, 'effect_outcome': 'not_started',
                'next_action': '核对原Owner该包的安装与授权；不支持读历史代次，需采用当前版本请创建新任务，勿原样重试。'}) from error
        if current.get('state') != 'available':
            raise BusinessError('PACKAGE_VERSION_UNAVAILABLE', details={
                'package_ref': frozen.package_ref, 'actual_state': current.get('state'),
                'expected_version_ref': frozen.version_ref, 'effect_outcome': 'not_started',
                'next_action': '原包已停用或不可用；先核对撤销原因，不补读最新包；需采用新安装时创建新任务。'})
        if (current.get('version_ref') != frozen.version_ref or frozen.activation_id is not None
                and current.get('activation_id') != frozen.activation_id):
            raise BusinessError('PACKAGE_VERSION_MISMATCH', details={
                'package_ref': frozen.package_ref, 'expected_version_ref': frozen.version_ref,
                'actual_version_ref': current.get('version_ref'),
                'expected_activation_id': frozen.activation_id,
                'actual_activation_id': current.get('activation_id'),
                'effect_outcome': 'not_started',
                'next_action': '当前包版本或激活身份已变，且读取服务不支持历史代次；保留旧任务，核对需求后创建绑定当前版本的新任务，勿原样重试。'})


def business_execution_policy_scope(host):
    """只运送当前runner已采用的typed上下文和原宿主Actor；不从ToolCall或历史正文重建权限。"""
    current = getattr(host, '_current_run_params', None)
    frozen = getattr(current, 'business_context', None)
    if frozen is None:
        return {}
    if type(frozen) is not BusinessExecutionContext:
        raise BusinessError('CONTEXT_REQUIRED')
    from ..gateway_parts.business_api import local_business_context

    authority, services = local_business_context(host, model=True)
    return {'business_context': frozen, 'business_actor': services.actor(authority)}
