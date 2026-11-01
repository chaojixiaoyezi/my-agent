"""只解析已分配的准确任务ID；无建任务、排队、模型调用或上下文持久化副作用。"""
from __future__ import annotations

from .business_access import BusinessError, require_access, require_actor
from .business_assets import BusinessAssetService
from .business_bindings import InterfaceBindingService
from .business_contracts import BusinessExecutionContext
from .business_spaces import _identifier


def resolve_business_context(*, actor: object, task_id: str, requested_space_id: str | None,
        project_ref: str | None, binding_id: str | None, requested_method_refs: tuple[str, ...] = (),
        requested_asset_version_refs: tuple[str, ...] = ()) -> BusinessExecutionContext:
    """仅供SessionTask的host-only factory首次调用；重放使用其原canonical context。

    W2负责将意图、准确stask ID和本结果同次原子写；这里不分配runtime ID，也不取latest。
    None默认选择与显式空间是不同意图；恢复时不能再次解析默认空间覆盖原冻结选择。
    """
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
    methods = []
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
    versions = BusinessAssetService(service).selected_versions(state, actor=actor, space_id=chosen,
        project_ref=project, requested=requested_asset_version_refs)
    require_access(actor, 'spaces.get', service=service, record=row)
    if service._read()['revision'] != state['revision']:
        raise BusinessError('REVISION_CONFLICT')
    return BusinessExecutionContext(owner_id=actor.owner_id, space_id=chosen, project_ref=project,
        task_id=task_id, space_revision=row['revision'], binding_id=binding_id,
        selected_method_refs=tuple(methods), asset_version_refs=versions, schema_version='business-context.v1')


def revalidate_business_context(*, actor, context):
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
