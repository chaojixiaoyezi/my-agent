"""W0 纯 DTO 与旧模型兼容合同；不启动模型、网络或真实服务。"""
from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import FrozenInstanceError, asdict, fields, is_dataclass, replace
from inspect import Parameter, signature
from typing import get_args, get_type_hints

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams, RuntimeLoopParams
from agent_py_agent.agent.conversation import models as conversation_models
from agent_py_agent.agent.workflows.models import WorkflowRunRecord


def dto(name):
    module_name = 'agent_py_agent.agent.conversation.business_contracts'
    assert importlib.util.find_spec(module_name) is not None, 'W0 business DTO module is missing'
    cls = getattr(importlib.import_module(module_name), name, None)
    assert cls is not None, f'W0 frozen DTO {name} is missing'
    assert is_dataclass(cls) and cls.__dataclass_params__.frozen
    return cls


def dto_values():
    return {
        'HostParentExecutionRef': dict(owner_id='owner', run_id='run', agent_run_id='agent-run',
                                       task_run_id='task-run'),
        'BusinessContextIntent': dict(owner_id='owner', requested_space_id=None,
                                      project_ref=None, binding_id=None),
        'BusinessExecutionContext': dict(owner_id='owner', space_id='space', project_ref='project',
            task_id='stask-canonical', space_revision=7, binding_id='binding',
            selected_method_refs=('method:1',), asset_version_refs=('asset:v1',),
            schema_version='business-execution-context.v1'),
        'BackgroundAgentIdentity': dict(agent_id='thread-canonical', managed_by_thread_id='manager',
            space_id='space', project_ref=None, identity_state='available', after_task='reuse',
            identity_revision=0),
        'TrustedTaskCompletion': dict(owner_id='owner', thread_id='thread', task_id='stask-canonical',
            run_id='run', attempt_id='attempt', terminal_status='completed', claim_ref='claim:1',
            completion_ref='completion:1', space_id=None, project_ref=None, source_kind='background',
            tool_rounds=0, result_refs=('artifact:1',), trajectory_digest='digest'),
        'WorkflowScope': dict(scope_mode='spaces', space_ids=('space',), scope_revision=3),
    }


@pytest.mark.parametrize('name', tuple(dto_values()))
def test_frozen_dto_fields_defaults_and_real_json_roundtrip(name):
    cls = dto(name)
    value = cls(**dto_values()[name])
    expected = set(dto_values()[name])
    if name == 'HostParentExecutionRef':
        expected.add('observed_attempt_id')
        assert value.observed_attempt_id is None
    if name == 'BusinessContextIntent':
        expected.update(('requested_method_refs', 'requested_asset_version_refs', 'schema_version'))
        assert value.requested_method_refs == value.requested_asset_version_refs == ()
        assert value.schema_version == 'business-context-intent.v1'
    assert {item.name for item in fields(value)} == expected
    assert cls.from_dict(json.loads(json.dumps(value.to_dict()))) == value
    assert cls.from_dict(asdict(value)) == value
    first_field = fields(value)[0].name
    with pytest.raises(FrozenInstanceError):
        setattr(value, first_field, 'changed')
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        value.untrusted_extra = True


@pytest.mark.parametrize('name,field_name', [
    ('BusinessContextIntent', 'requested_method_refs'),
    ('BusinessContextIntent', 'requested_asset_version_refs'),
    ('BusinessExecutionContext', 'selected_method_refs'),
    ('BusinessExecutionContext', 'asset_version_refs'),
    ('TrustedTaskCompletion', 'result_refs'),
    ('WorkflowScope', 'space_ids'),
])
def test_ref_arrays_are_copied_deeply_frozen_and_exported_independently(name, field_name):
    cls, values = dto(name), dto_values()[name]
    refs = ['ref:1']
    value = cls(**{**values, field_name: refs})
    refs.append('ref:2')
    assert getattr(value, field_name) == ('ref:1',)
    assert type(getattr(value, field_name)) is tuple
    exported = value.to_dict()
    exported[field_name].append('ref:3')
    assert getattr(value, field_name) == ('ref:1',)
    for bad in ('ref:1', b'ref:1', {'ref:1'}, {'key': 'ref:1'}, (['nested'],), ({'id': 'ref'},), (1,), (None,)):
        with pytest.raises((TypeError, ValueError)):
            cls(**{**values, field_name: bad})


@pytest.mark.parametrize('name', tuple(dto_values()))
def test_dto_rejects_scalar_coercions_missing_and_unknown_fields(name):
    cls, values = dto(name), dto_values()[name]
    for key, value in values.items():
        if isinstance(value, str):
            # 合同中的可空字段不能由当前样例值猜成必填；保留全部非字符串拒绝，并验证 None 的精确边界。
            bad_values = (True, 1, [], {})
            if type(None) in get_args(get_type_hints(cls)[key]):
                assert getattr(cls(**{**values, key: None}), key) is None
            else:
                bad_values = (None, *bad_values)
            for bad in bad_values:
                with pytest.raises((TypeError, ValueError)):
                    cls(**{**values, key: bad})
        elif value is None:
            for bad in (True, 1, [], {}):
                with pytest.raises((TypeError, ValueError)):
                    cls(**{**values, key: bad})
        elif type(value) is int:
            for bad in (True, 1.0, '1', -1):
                with pytest.raises((TypeError, ValueError)):
                    cls(**{**values, key: bad})
    with pytest.raises((TypeError, ValueError)):
        cls.from_dict({**values, 'trusted': True})
    required_key = next(iter(values))
    with pytest.raises((TypeError, ValueError)):
        cls.from_dict({key: value for key, value in values.items() if key != required_key})
    with pytest.raises((TypeError, ValueError)):
        cls.from_dict([])


@pytest.mark.parametrize('field_name,valid,invalid', [
    ('identity_state', ('available', 'suspended', 'retiring', 'retired'), ('AVAILABLE', 'running', 'done')),
    ('after_task', ('reuse', 'suspend', 'retire'), ('REUSE', 'retired', 'stop')),
])
def test_identity_enums_are_exact_and_organization_is_not_a_parent_run(field_name, valid, invalid):
    cls, values = dto('BackgroundAgentIdentity'), dto_values()['BackgroundAgentIdentity']
    for choice in valid:
        assert getattr(cls(**{**values, field_name: choice}), field_name) == choice
    for choice in invalid:
        with pytest.raises(ValueError):
            cls(**{**values, field_name: choice})
    assert not {'parent_run_id', 'model_profile_id'} & {item.name for item in fields(cls)}


def test_parent_reference_allows_no_observed_attempt_and_is_not_an_authorization_check():
    cls = dto('HostParentExecutionRef')
    parent = cls(**dto_values()['HostParentExecutionRef'])
    assert parent.observed_attempt_id is None
    assert replace(parent, observed_attempt_id='observed').observed_attempt_id == 'observed'
    with pytest.raises((TypeError, ValueError)):
        replace(parent, observed_attempt_id=123)


def test_intent_distinguishes_default_from_explicit_selection_without_runtime_facts():
    cls = dto('BusinessContextIntent')
    default = cls(**dto_values()['BusinessContextIntent'])
    assert default != replace(default, requested_space_id='space')
    assert not {'task_id', 'space_revision', 'execution_definition_ref'} & {item.name for item in fields(cls)}


def test_context_preserves_canonical_stask_and_legacy_unassigned_space():
    context = dto('BusinessExecutionContext')(**dto_values()['BusinessExecutionContext'])
    restored = type(context).from_dict(json.loads(json.dumps(context.to_dict())))
    params = RunParams(task_id='runtime-task', business_context=restored)
    assert params.task_id == 'runtime-task' and params.business_context.task_id == 'stask-canonical'
    assert replace(params, attempt_id='new-attempt').business_context is restored
    assert replace(context, space_id=None, space_revision=None).space_id is None
    assert context.space_revision == 7


@pytest.mark.parametrize('status', ('done', 'COMPLETED', 'failed', 'accepted', 'unknown'))
def test_completion_requires_exact_completed_status(status):
    cls = dto('TrustedTaskCompletion')
    with pytest.raises(ValueError):
        cls(**{**dto_values()['TrustedTaskCompletion'], 'terminal_status': status})


@pytest.mark.parametrize('mode,ids', [
    ('owner_global', ()), ('unassigned', ()), ('spaces', ('a', 'b')),
])
def test_scope_valid_shapes_are_distinct_and_do_not_include_execution_definition(mode, ids):
    cls = dto('WorkflowScope')
    scope = cls(mode, ids, 0)
    assert (scope.scope_mode, scope.space_ids, scope.scope_revision) == (mode, ids, 0)
    assert 'execution_definition_ref' not in scope.to_dict()


@pytest.mark.parametrize('mode,ids', [
    ('spaces', ()), ('spaces', ('a', 'a')), ('owner_global', ('a',)),
    ('unassigned', ('a',)), ('global', ()), ('SPACES', ('a',)),
])
def test_scope_rejects_bad_modes_empty_spaces_and_duplicate_ids(mode, ids):
    with pytest.raises(ValueError):
        dto('WorkflowScope')(mode, ids, 0)


def test_runtime_context_defaults_keyword_only_and_no_implicit_attributes_recovery():
    assert 'business_context' in RunParams.__dataclass_fields__, 'RunParams lacks host-only context'
    assert signature(RunParams).parameters['business_context'].kind is Parameter.KEYWORD_ONLY
    assert RunParams().business_context is None
    assert RunParams(task_attributes={'business_context': {'owner_id': 'spoof'}}).business_context is None
    assert 'business_context' in RuntimeLoopParams.__dataclass_fields__
    assert signature(RuntimeLoopParams).parameters['business_context'].kind is Parameter.KEYWORD_ONLY
    assert RuntimeLoopParams('u', 'u', [], [], None, '').business_context is None


def test_conversation_context_is_optional_host_only_and_not_another_persistent_authority():
    cls = conversation_models.ConversationThread
    assert 'business_context' in cls.__dataclass_fields__, 'ConversationThread lacks host-only projection'
    context = dto('BusinessExecutionContext')(**dto_values()['BusinessExecutionContext'])
    legacy = cls('thread', 'owner')
    assert legacy.business_context is None
    assert 'business_context' not in legacy.to_dict()
    current = replace(legacy, business_context=context)
    assert current.business_context is context
    assert 'business_context' in conversation_models.MODEL_HIDDEN_THREAD_FIELDS
    # 完整业务上下文只归 canonical SessionTask；线程宿主投影不落盘、不从聊天式 JSON 恢复。
    assert 'business_context' not in current.to_dict()
    restored = cls.from_dict({**legacy.to_dict(), 'business_context': context.to_dict()})
    assert restored.business_context is None
    assert cls.from_dict(legacy.to_dict()).to_dict() == legacy.to_dict()


def test_workflow_extensions_keep_positional_defaults_and_real_json_roundtrip():
    names = ('business_context', 'scope_snapshot', 'scope_revision', 'revision')
    assert set(names) <= {item.name for item in fields(WorkflowRunRecord)}, 'Workflow extension fields missing'
    old = WorkflowRunRecord('wf-run', 'workflow', 'owner', 'manual', 'running', 1, 0, 0)
    for name in names:
        assert signature(WorkflowRunRecord).parameters[name].kind is Parameter.KEYWORD_ONLY
    assert (old.business_context, old.scope_snapshot, old.scope_revision, old.revision) == ({}, {}, 0, 0)
    independent = WorkflowRunRecord('other', 'workflow', 'owner', 'manual', 'running', 1, 0, 0)
    assert independent.business_context is not old.business_context
    assert independent.scope_snapshot is not old.scope_snapshot
    context = dto('BusinessExecutionContext')(**dto_values()['BusinessExecutionContext'])
    scope = dto('WorkflowScope')('spaces', ('space',), 11)
    run = replace(old, business_context=context.to_dict(), scope_snapshot=scope.to_dict(),
                  scope_revision=11, revision=4, execution_definition_ref='sha256:execution')
    restored = WorkflowRunRecord.from_dict(json.loads(json.dumps(run.to_dict())))
    assert restored == run
    assert restored.business_context['task_id'] == 'stask-canonical'
    assert (restored.business_context['space_revision'], restored.scope_revision,
            restored.revision, restored.execution_definition_ref) == (7, 11, 4, 'sha256:execution')
    restored.business_context['selected_method_refs'].append('changed')
    assert run.business_context['selected_method_refs'] == ['method:1']
    legacy_json = {key: value for key, value in old.to_dict().items() if key not in names}
    loaded = WorkflowRunRecord.from_dict(legacy_json)
    assert (loaded.business_context, loaded.scope_snapshot, loaded.scope_revision, loaded.revision) == ({}, {}, 0, 0)


def test_workflow_snapshot_fields_reject_non_json_and_bad_revision_types():
    assert 'business_context' in WorkflowRunRecord.__dataclass_fields__
    old = WorkflowRunRecord('wf-run', 'workflow', 'owner', 'manual', 'running', 1, 0, 0)
    for field_name in ('business_context', 'scope_snapshot'):
        for bad in (None, [], 'snapshot', {'bad': object()}, {'bad': float('nan')}):
            with pytest.raises((TypeError, ValueError)):
                replace(old, **{field_name: bad})
    for field_name in ('scope_revision', 'revision'):
        for bad in (None, True, 1.0, '1', -1):
            with pytest.raises((TypeError, ValueError)):
                replace(old, **{field_name: bad})


def test_workflow_snapshot_input_and_json_output_are_independent():
    assert 'business_context' in WorkflowRunRecord.__dataclass_fields__
    business_context = {'task_id': 'stask-canonical', 'selected_method_refs': ['method:1']}
    scope_snapshot = {'scope_mode': 'spaces', 'space_ids': ['space'], 'scope_revision': 3}
    run = WorkflowRunRecord('wf-run', 'workflow', 'owner', 'manual', 'running', 1, 0, 0,
                           business_context=business_context, scope_snapshot=scope_snapshot, scope_revision=3)
    business_context['selected_method_refs'].append('later')
    scope_snapshot['space_ids'].append('later')
    assert run.business_context['selected_method_refs'] == ['method:1']
    assert run.scope_snapshot['space_ids'] == ['space']
    encoded = run.to_dict()
    encoded['business_context']['task_id'] = 'changed'
    assert run.business_context['task_id'] == 'stask-canonical'


def test_host_only_context_fields_require_typed_values_without_using_task_attributes():
    context = dto('BusinessExecutionContext')(**dto_values()['BusinessExecutionContext'])
    for cls, args in ((conversation_models.ConversationThread, ('thread', 'owner')),
                      (RunParams, ()), (RuntimeLoopParams, ('u', 'u', [], [], None, ''))):
        value = cls(*args, business_context=context)
        assert value.business_context is context
        assert cls.__dataclass_fields__['business_context'].metadata['host_only'] is True
        for bad in (context.to_dict(), 'context', 1):
            with pytest.raises(TypeError):
                cls(*args, business_context=bad)


def test_frozen_dto_has_no_mutable_attribute_dictionary_and_is_hashable():
    for name, values in dto_values().items():
        value = dto(name)(**values)
        assert not hasattr(value, '__dict__')
        assert hash(value) == hash(type(value).from_dict(value.to_dict()))


def test_workflow_existing_run_store_persists_and_restores_new_snapshots(tmp_path):
    from agent_py_agent.agent.workflows.run_store import WorkflowRunStore

    context = dto('BusinessExecutionContext')(**dto_values()['BusinessExecutionContext'])
    scope = dto('WorkflowScope')('spaces', ('space',), 11)
    owner_home = tmp_path / 'owner'
    owner_home.mkdir()
    store = WorkflowRunStore(owner_home)
    run = WorkflowRunRecord('wf-run', 'workflow', 'owner', 'manual', 'running', 1, 0, 0,
        business_context=context.to_dict(), scope_snapshot=scope.to_dict(), scope_revision=11,
        revision=4, execution_definition_ref='sha256:execution')
    store.create_run(run, workflow_snapshot={'steps': [], 'definition': 'original'})
    restored = store.load_run(run.workflow_run_id)
    assert restored == run
    assert restored.business_context['task_id'] == 'stask-canonical'
    assert (restored.business_context['space_revision'], restored.scope_revision,
            restored.revision, restored.execution_definition_ref) == (7, 11, 4, 'sha256:execution')
    assert store.load_workflow_snapshot(run.workflow_run_id) == {'steps': [], 'definition': 'original'}
    assert restored.business_context is not run.business_context
    # W0 只提供/保存字段：原 Store 此处不冒充已实现 W3 锁内 CAS 递增。
    assert restored.revision == 4
