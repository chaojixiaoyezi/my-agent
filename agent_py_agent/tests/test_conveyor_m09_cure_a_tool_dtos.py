"""M09: differential tests for the converted tool-turn DTOs and their G1 free-threading factories."""

from __future__ import annotations

import collections
import copy
import dataclasses
import hashlib
import inspect
import json
import pickle
import sys
import threading
import weakref
from types import FunctionType, MappingProxyType, MemberDescriptorType

import pytest

from agent_py_agent.agent.common import fast_dataclass
from agent_py_agent.agent.contracts import tool_approval
from agent_py_agent.agent.local_storage import (
    ToolOperationClaim,
    ToolOperationHolder,
    ToolOperationRecord,
)
from agent_py_agent.agent.tooling import input_schema, models, runtime_contracts
from agent_py_agent.agent.tooling import tool_input_completion as completion
from agent_py_agent.agent.tooling import tool_operation_coordinator as coordinator


def _model_spec_arguments():
    return {
        "name": " sample ",
        "description": " sample 工具 ",
        "input_schema": {"type": "object", "properties": {"标题": {"type": "string"}}},
    }


def _capability_arguments():
    return {
        "provider": " provider ",
        "endpoint": " endpoint ",
        "model": " model ",
        "stream": True,
        "native_supported": True,
        "evidence": " observed ",
    }


def _call_arguments():
    return {
        "call_id": " call ",
        "tool_name": " sample ",
        "arguments": {"nested": ["示例", 3]},
        "source_protocol": " TEXT ",
        "schema_hash": " sha256:schema ",
        "run_id": " run ",
        "turn_id": " turn ",
        "attempt_id": " attempt ",
    }


def _runtime_snapshot_arguments():
    runtime = models.ToolRuntime(
        models.ToolModelSpec(**_model_spec_arguments()),
        models.ToolRuntimePolicy(models.EffectResolverPolicy()),
        handler=None,
    )
    return {
        "run_id": "run",
        "runtimes": (runtime,),
        "available_tool_names": frozenset({"sample"}),
        "unavailable_tools": (),
        "allowed_tools": None,
    }


def _ok_outcome():
    return models.ToolHandlerOutcome("sample", True, "完成")


def _operation_request_arguments():
    return {
        "store": None,
        "store_required": True,
        "owner_id": "owner",
        "run_id": "run",
        "task_id": "task",
        "operation_id": "operation",
        "tool_name": "sample",
        "args_hash": "sha256:args",
        "idempotency_key": "key",
        "idempotency_scope": "operation",
        "idempotency_namespace": "sample",
        "timeout_seconds": 10,
        "invoke": _ok_outcome,
        "resource_scopes": ("logical:item:sample",),
    }


def _claim_attempt_arguments():
    record = ToolOperationRecord(
        owner_id="owner",
        run_id="run",
        task_id="task",
        operation_id="operation",
        tool="sample",
        args_hash="sha256:args",
        idempotency_key="key",
        idempotency_scope="operation",
        idempotency_namespace="sample",
        status="executing",
        holder_id="holder",
        holder_host="host",
        holder_pid=1,
        holder_process_start_token="start",
        generation=1,
        lease_expires_at=250.0,
    )
    return {
        "claim": ToolOperationClaim("execute", record),
        "holder": ToolOperationHolder("holder", "host", 1, "start"),
        "lease_expires_at": record.lease_expires_at,
    }


def _approval_request_arguments():
    return {
        "permission_id": " permission ",
        "request_id": " request ",
        "tool_name": " sample ",
        "round": -1,
        "call_index": 1,
        "title": "Tool use",
        "description": "示例",
        "binding": {
            "tool_name": " sample ",
            "run_id": " run ",
            "operation_id": " operation ",
            "idempotency_key": " key ",
            "args_hash": " sha256:args ",
        },
        "options": (
            {"id": "once", "label": "Allow", "decision": " APPROVED "},
            {"id": "cancel", "label": "Cancel", "decision": "cancelled"},
        ),
        "requested_at": 123.0,
    }


def _input_source_arguments():
    return {"path": "$.title", "source": "model", "source_ref": "call:sample"}


_DTO_ARGUMENTS = {
    models.TrustedParameterBinding: lambda: {"source_refs": (" context.title ",), "authority": " FILL_MISSING "},
    models.ToolModelHints: lambda: {"default_deferred": True, "deferred_summary": " summary "},
    models.ToolModelSpec: _model_spec_arguments,
    models.EffectResolverPolicy: lambda: {
        "default_effect": " READ_ONLY ",
        "by_parameter": ((" action ", (("save", " MUTATING "),)),),
    },
    models.ApprovalPolicy: lambda: {"mode": " ALWAYS ", "always_effects": ("mutating", "mutating")},
    models.SandboxPolicy: lambda: {"mode": " REQUIRED ", "contained_by_parameter": ((" action ", (" read ",)),)},
    models.IdempotencyPolicy: lambda: {"scope": " OPERATION "},
    models.TimeoutPolicy: lambda: {"seconds": -1},
    models.ConcurrencyPolicy: lambda: {"mode": "parallel_safe", "control_lane_actions": (" read ", "")},
    models.ResourceScopePolicy: lambda: {
        "parameter_names": (" item ",),
        "parameter_kinds": {" item ": " LOGICAL "},
        "resource_domains": {" item ": " record "},
    },
    models.OutputPolicy: lambda: {"trust": " EXTERNAL_DATA ", "redaction": " DEFAULT "},
    models.AvailabilityPolicy: lambda: {"mode": " ALWAYS "},
    models.ToolRuntimePolicy: lambda: {"effect_resolver": models.EffectResolverPolicy()},
    models.ToolRuntimeSnapshot: _runtime_snapshot_arguments,
    runtime_contracts.ToolChoice: lambda: {"mode": " SPECIFIC ", "tool_name": " sample "},
    runtime_contracts.ProviderToolCapability: _capability_arguments,
    runtime_contracts.ToolProtocolSnapshot: lambda: {
        "run_id": " run ",
        "source_protocol": " NATIVE ",
        "capability": runtime_contracts.ProviderToolCapability(**_capability_arguments()),
    },
    runtime_contracts.ToolCall: _call_arguments,
    runtime_contracts.ToolContentBlock: lambda: {"type": " JSON ", "data": {"b": 2, "a": "示例"}},
    runtime_contracts.ToolResultRef: lambda: {"kind": " artifact ", "ref": " artifact:example ", "size_bytes": -1},
    runtime_contracts.AppliedToolApproval: lambda: {"permission_id": " permission ", "decision": " APPROVED "},
    runtime_contracts.ToolFailureFacts: lambda: {"duration_ms": 1, "metadata": {"reason": "示例"}},
    runtime_contracts.ToolResult: lambda: {
        "call_id": " call ",
        "tool_name": " sample ",
        "status": " SUCCEEDED ",
        "content_blocks": [runtime_contracts.ToolContentBlock("text", text="示例")],
        "metadata": {"source": ["sample"]},
    },
    completion.ToolInputSource: _input_source_arguments,
    completion.ToolInputCompletionContext: lambda: {"call_id": "call", "trusted_context": {"title": "示例"}},
    completion.ToolInputCompletion: lambda: {
        "value": {"title": "示例"},
        "sources": (completion.ToolInputSource(**_input_source_arguments()),),
    },
    coordinator.ToolOperationExecutionRequest: _operation_request_arguments,
    coordinator._ToolOperationClaimAttempt: _claim_attempt_arguments,
    tool_approval.ToolApprovalRequest: _approval_request_arguments,
    tool_approval.ToolApprovalDecision: lambda: {
        "permission_id": " permission ",
        "decision": " APPROVED ",
        "feedback": " 示例 ",
        "decided_at": 123.0,
    },
}


def _dictionary_setter(name):
    def set_field(obj, value):
        object.__setattr__(obj, name, value)

    return set_field


def _plain_dataclass(cls, monkeypatch):
    namespace = {"__module__": __name__}
    post_init = cls.__dict__.get("__post_init__")
    if post_init is not None:
        # The reference keeps the plain dataclass constructor and field writes and runs the same normalization.
        scope = dict(post_init.__globals__)
        for name in post_init.__code__.co_names:
            descriptor = getattr(scope.get(name), "__self__", None)
            if isinstance(descriptor, MemberDescriptorType) and descriptor.__objclass__ is cls:
                scope[name] = _dictionary_setter(descriptor.__name__)
        namespace["__post_init__"] = FunctionType(post_init.__code__, scope)
    fields = [(item.name, item.type, copy.copy(item)) for item in dataclasses.fields(cls)]
    reference = dataclasses.make_dataclass(cls.__name__, fields, namespace=namespace, frozen=True)
    monkeypatch.setattr(sys.modules[__name__], cls.__name__, reference, raising=False)
    return reference


def _field_values(obj):
    return tuple(getattr(obj, item.name) for item in dataclasses.fields(obj))


def _hash_result(obj):
    try:
        return hash(obj)
    except TypeError as exc:
        return type(exc), str(exc)


@pytest.mark.parametrize("cls", _DTO_ARGUMENTS, ids=lambda cls: cls.__name__)
def test_dto_uses_fast_frozen_slots(cls):
    assert cls in fast_dataclass.CONVERTED_CLASSES
    assert "__slots__" in cls.__dict__


@pytest.mark.parametrize("cls", _DTO_ARGUMENTS, ids=lambda cls: cls.__name__)
def test_dto_matches_plain_dataclass(cls, monkeypatch):
    reference = _plain_dataclass(cls, monkeypatch)
    assert inspect.signature(cls) == inspect.signature(reference)
    assert cls.__match_args__ == reference.__match_args__

    arguments = _DTO_ARGUMENTS[cls]()
    obj, plain = cls(**arguments), reference(**arguments)
    assert _field_values(obj) == _field_values(plain)
    assert obj == cls(**arguments)
    assert plain == reference(**arguments)
    assert repr(obj) == repr(plain)
    assert _hash_result(obj) == _hash_result(plain)

    for instance in (obj, plain):
        for item in dataclasses.fields(instance):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(instance, item.name, None)
            with pytest.raises(dataclasses.FrozenInstanceError):
                delattr(instance, item.name)
        if cls is models.ToolRuntimeSnapshot:
            with pytest.raises(TypeError, match="cannot pickle 'mappingproxy' object"):
                pickle.dumps(instance)
        else:
            for protocol in (2, pickle.HIGHEST_PROTOCOL):
                restored = pickle.loads(pickle.dumps(instance, protocol))
                assert type(restored) is type(instance)
                assert restored == instance

    field = next(iter(arguments))
    changes = {field: arguments[field]}
    expected = _field_values(dataclasses.replace(plain, **changes))
    replacement_functions = [fast_dataclass.fast_replace, dataclasses.replace]
    if hasattr(copy, "replace"):
        replacement_functions.append(copy.replace)
    invalid_changes = [{"unknown_field": 1}]
    invalid_changes.extend({item.name: None} for item in dataclasses.fields(cls) if not item.init)

    for replace in replacement_functions:
        replaced = replace(obj, **changes)
        assert type(replaced) is cls
        assert _field_values(replaced) == expected
        for invalid in invalid_changes:
            with pytest.raises((TypeError, ValueError)) as expected_error:
                dataclasses.replace(obj, **invalid)
            with pytest.raises(expected_error.type) as actual_error:
                replace(obj, **invalid)
            assert str(actual_error.value) == str(expected_error.value)


def test_presentation_clone_keeps_snapshot_index_and_schema_hash():
    snapshot = models.ToolRuntimeSnapshot(**_runtime_snapshot_arguments())
    presented = snapshot.with_presentation(loaded_tool_names=("sample",))
    assert presented.loaded_tool_names == ("sample",)
    assert snapshot.loaded_tool_names == ()
    assert presented.snapshot_hash == snapshot.snapshot_hash
    assert presented._runtime_by_name is snapshot._runtime_by_name
    assert presented.runtime("sample") is snapshot.runtimes[0]
    spec = presented.runtime("sample").model_spec
    encoded = json.dumps(spec.input_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert spec.schema_hash == "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_tool_result_and_approval_keep_wire_format():
    call = runtime_contracts.ToolCall(**_call_arguments())
    block = runtime_contracts.ToolContentBlock("json", data={"z": 2, "a": "示例"})
    result = runtime_contracts.ToolResult(call.call_id, call.tool_name, "succeeded", (block,))
    assert result.output == json.dumps(block.data, ensure_ascii=False, sort_keys=True)

    approval = tool_approval.build_tool_approval_request(
        call, request_id="request", round_number=0, call_index=0, description="示例",
    )
    identity = json.dumps(
        {"call_id": call.call_id, **approval.binding}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    assert approval.permission_id == "approval:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    assert tool_approval.ToolApprovalRequest.from_mapping(approval.to_dict()) == approval


def test_runtime_snapshot_keeps_weak_references_for_the_registry_projection_cache():
    # registry._deferred_tool_projection caches snapshots through weakref.ref.
    snapshot = models.ToolRuntimeSnapshot(**_runtime_snapshot_arguments())
    presented = snapshot.with_presentation(loaded_tool_names=("sample",))
    for item in (snapshot, presented):
        assert weakref.ref(item)() is item


# Copies on the tool-turn path go through input_schema.clone_json_value instead of copy.deepcopy.

class _Copyable:
    def __init__(self, value):
        self.value = value

    def __deepcopy__(self, memo):
        return _Copyable(("copied", self.value))

    def __repr__(self):
        return f"_Copyable({self.value!r})"


class _Text(str):
    pass


class _Rows(list):
    pass


def _copy_shape(copied, original):
    """List types, scalar values, identity with the original and aliasing of a copy, in visit order."""
    order = {}
    tokens = []

    def walk(node, source):
        kind = type(node)
        if kind in (str, int, float, bool, type(None)):
            tokens.append(("scalar", kind.__name__, repr(node), node is source))
            return
        if id(node) in order:
            tokens.append(("again", order[id(node)], node is source))
            return
        order[id(node)] = len(order)
        tokens.append(("node", kind.__name__, node is source))
        if isinstance(node, dict):
            for (key, item), (source_key, source_item) in zip(node.items(), source.items(), strict=True):
                tokens.append(("key", repr(key), key is source_key))
                walk(item, source_item)
        elif isinstance(node, (list, tuple)):
            tokens.append(("length", len(node)))
            for item, source_item in zip(node, source, strict=True):
                walk(item, source_item)
        else:
            tokens.append(("value", repr(node)))

    walk(copied, original)
    return tokens


def _json_corpus():
    shared_list = [1, "two"]
    shared_dict = {"inner": shared_list}
    cyclic_list = []
    cyclic_list.append(cyclic_list)
    cyclic_dict = {}
    cyclic_dict["self"] = cyclic_dict
    holder = []
    tuple_cycle = (holder, "tail")
    holder.append(tuple_cycle)
    scalar_tuple = ("a", 1, None)
    return {
        "scalars": ["text", "示例", 0, -1, 2 ** 70, 1.5, float("nan"), float("-inf"), True, False, None],
        "nested": {"a": {"b": [{"c": "d"}, [], {}]}, "e": []},
        "scalar_keys": {1: "int", 2.5: "float", False: "bool", None: "none", "s": "str"},
        "shared": {"first": shared_dict, "second": shared_dict, "pair": [shared_list, shared_list]},
        "cyclic_list": cyclic_list,
        "cyclic_dict": cyclic_dict,
        "scalar_tuple": scalar_tuple,
        "repeated_scalar_tuple": [scalar_tuple, scalar_tuple],
        "mutable_tuple": ([1], {"a": (2, [3])}),
        "tuple_cycle": tuple_cycle,
        "empty": [{}, [], ()],
    }


_FALLBACK_VALUES = {
    "dict_subclass": lambda: collections.OrderedDict(a=[1]),
    "list_subclass": lambda: {"rows": _Rows([1])},
    "str_subclass": lambda: [_Text("abc")],
    "enum_member": lambda: {"stage": models.ToolFailureStage.PROTOCOL},
    "set": lambda: {"tags": {"a", "b"}},
    "frozenset": lambda: (frozenset({"a"}),),
    "bytes": lambda: {"raw": b"x"},
    "tuple_key": lambda: {("a", 1): [1]},
    "custom_deepcopy": lambda: {"item": _Copyable([1])},
}


def _record_deepcopy(monkeypatch):
    calls = []
    real = copy.deepcopy

    def recording(value, memo=None):
        calls.append(type(value))
        return real(value, memo)

    monkeypatch.setattr(input_schema, "deepcopy", recording)
    return calls


@pytest.mark.parametrize("value", ["text", 7, 1.5, True, None], ids=repr)
def test_clone_json_value_returns_scalars_as_is(value, monkeypatch):
    calls = _record_deepcopy(monkeypatch)
    assert input_schema.clone_json_value(value) is value
    assert calls == []


@pytest.mark.parametrize("name", sorted(_json_corpus()))
def test_clone_json_value_matches_deepcopy_without_calling_it(name, monkeypatch):
    calls = _record_deepcopy(monkeypatch)
    value = _json_corpus()[name]
    cloned = input_schema.clone_json_value(value)
    assert calls == []
    assert _copy_shape(cloned, value) == _copy_shape(copy.deepcopy(value), value)


def test_clone_json_value_keeps_aliasing_across_the_whole_value(monkeypatch):
    calls = _record_deepcopy(monkeypatch)
    value = _json_corpus()
    cloned = input_schema.clone_json_value(value)
    assert calls == []
    assert _copy_shape(cloned, value) == _copy_shape(copy.deepcopy(value), value)


@pytest.mark.parametrize("name", sorted(_FALLBACK_VALUES))
def test_clone_json_value_hands_other_types_to_deepcopy(name, monkeypatch):
    calls = _record_deepcopy(monkeypatch)
    value = _FALLBACK_VALUES[name]()
    cloned = input_schema.clone_json_value(value)
    assert calls == [type(value)]
    assert _copy_shape(cloned, value) == _copy_shape(copy.deepcopy(value), value)


@pytest.mark.parametrize(
    "make", [lambda: {"view": MappingProxyType({})}, lambda: [threading.Lock()]], ids=["mappingproxy", "lock"],
)
def test_clone_json_value_raises_what_deepcopy_raises(make):
    value = make()
    with pytest.raises(Exception) as expected:
        copy.deepcopy(value)
    with pytest.raises(expected.type) as actual:
        input_schema.clone_json_value(value)
    assert str(actual.value) == str(expected.value)


def _production_metadata():
    # Shape of executor.py result metadata: nested dicts and lists with host-built tuples.
    return {
        "action_decision": {
            "status": "allow", "reason_codes": ["OK"], "approval_request": {},
            "evidence": {"paths": ("a", "b"), "matched": [{"rule": "r1", "args": ("x", 1)}]},
            "sandbox_plan": {"mode": "inherit", "roots": ("/tmp/a",)},
            "resolved_effect": "read_only", "resource_scopes": ["logical:item:sample"],
        },
        "input_sources": [{"path": "$.title", "source": "model", "source_ref": "call:sample"}],
        "host_error": {},
        "handler_details": {"rows": [[1, 2.5, None, True], [3, 4.5, "y", False]], "count": 2},
    }


def _completion_runtime():
    spec = models.ToolModelSpec("complete", "completion sample", {
        "type": "object",
        "properties": {"query": {"type": "string"}, "title": {"type": "string"}, "filters": {"type": "object"}},
    })
    policy = models.ToolRuntimePolicy(
        models.EffectResolverPolicy(),
        input_policy=models.ToolInputPolicy(
            safe_parameter_defaults=(("filters", {"kinds": ["a", "b"]}),),
            trusted_parameter_bindings=(("title", models.TrustedParameterBinding(("registry.title",))),),
        ),
    )
    return models.ToolRuntime(spec, policy, handler=None)


def _deepcopy_calls(action):
    """Count calls of copy.deepcopy made while action runs in this thread."""
    target = copy.deepcopy.__code__
    calls = []

    def profile(frame, event, _arg):
        if event == "call" and frame.f_code is target:
            calls.append(event)

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        action()
    finally:
        sys.setprofile(previous)
    return len(calls)


def test_tool_turn_copies_do_not_call_deepcopy_for_json_shaped_values():
    runtime = _completion_runtime()
    context = completion.ToolInputCompletionContext(call_id="call", trusted_context={"registry": {"title": "T"}})
    produced = {}

    def tool_turn():
        produced["completion"] = completion.complete_tool_arguments({"query": "q"}, runtime, context)
        call = runtime_contracts.ToolCall(**{**_call_arguments(), "input_migration": {"from": 1, "fields": ["q"]}})
        block = runtime_contracts.ToolContentBlock("json", data={"rows": [[1, "a"], [2, None]], "pair": (1, 2)})
        result = runtime_contracts.ToolResult(
            call.call_id, call.tool_name, "succeeded", (block,), metadata=_production_metadata(),
        )
        produced["spec"] = models.ToolModelSpec(**_model_spec_arguments())
        produced["dicts"] = (call.to_dict(), block.to_dict(), result.to_dict())
        produced["projected"] = result.with_live_prompt_projection("projected")

    assert _deepcopy_calls(tool_turn) == 0
    assert produced["completion"].value == {"query": "q", "filters": {"kinds": ["a", "b"]}, "title": "T"}
    assert produced["projected"].metadata["action_decision"] == _production_metadata()["action_decision"]
    # A value outside the JSON-shaped types still takes copy.deepcopy.
    assert _deepcopy_calls(lambda: runtime_contracts.ToolResult("call", "tool", "succeeded", metadata={"s": {1}})) > 0


def _carried_call_arguments():
    arguments = _call_arguments()
    call = runtime_contracts.ToolCall(**arguments)
    arguments.update(operation_id=call.operation_id, idempotency_key=call.idempotency_key)
    return arguments


def _g1_factory(cls, make_arguments=None):
    make_arguments = make_arguments or _DTO_ARGUMENTS[cls]

    def factory():
        arguments = None

        def construct():
            # Each worker builds its own arguments during warm-up, as a request thread owns its turn input.
            nonlocal arguments
            if arguments is None:
                arguments = make_arguments()
            return cls(**arguments)

        return construct

    return factory


g1_trusted_parameter_binding = _g1_factory(models.TrustedParameterBinding)
g1_tool_model_hints = _g1_factory(models.ToolModelHints)
g1_tool_model_spec = _g1_factory(models.ToolModelSpec)
g1_effect_resolver_policy = _g1_factory(models.EffectResolverPolicy)
g1_approval_policy = _g1_factory(models.ApprovalPolicy)
g1_sandbox_policy = _g1_factory(models.SandboxPolicy)
g1_idempotency_policy = _g1_factory(models.IdempotencyPolicy)
g1_timeout_policy = _g1_factory(models.TimeoutPolicy)
g1_concurrency_policy = _g1_factory(models.ConcurrencyPolicy)
g1_resource_scope_policy = _g1_factory(models.ResourceScopePolicy)
g1_output_policy = _g1_factory(models.OutputPolicy)
g1_availability_policy = _g1_factory(models.AvailabilityPolicy)
g1_tool_runtime_policy = _g1_factory(models.ToolRuntimePolicy)
g1_tool_runtime_snapshot = _g1_factory(models.ToolRuntimeSnapshot)
g1_tool_choice = _g1_factory(runtime_contracts.ToolChoice)
g1_provider_tool_capability = _g1_factory(runtime_contracts.ProviderToolCapability)
g1_tool_protocol_snapshot = _g1_factory(runtime_contracts.ToolProtocolSnapshot)
# A call that already carries its operation identity, as rebuilt from history.
g1_tool_call = _g1_factory(runtime_contracts.ToolCall, _carried_call_arguments)
# A new model-turn call, which derives its operation identity and idempotency key.
g1_tool_call_derived_identity = _g1_factory(runtime_contracts.ToolCall, _call_arguments)
g1_tool_content_block = _g1_factory(runtime_contracts.ToolContentBlock, lambda: {"type": "text", "text": "示例"})
g1_tool_result_ref = _g1_factory(runtime_contracts.ToolResultRef)
g1_applied_tool_approval = _g1_factory(runtime_contracts.AppliedToolApproval)
g1_tool_failure_facts = _g1_factory(runtime_contracts.ToolFailureFacts)
g1_tool_result = _g1_factory(runtime_contracts.ToolResult)
# Executor results carry decision evidence, sandbox plans and handler details with host-built tuples.
g1_tool_result_production_metadata = _g1_factory(runtime_contracts.ToolResult, lambda: {
    **_DTO_ARGUMENTS[runtime_contracts.ToolResult](), "metadata": _production_metadata()})
g1_tool_input_source = _g1_factory(completion.ToolInputSource)
g1_tool_input_completion_context = _g1_factory(completion.ToolInputCompletionContext)
g1_tool_input_completion = _g1_factory(completion.ToolInputCompletion)
g1_tool_operation_execution_request = _g1_factory(coordinator.ToolOperationExecutionRequest)
g1_tool_operation_claim_attempt = _g1_factory(coordinator._ToolOperationClaimAttempt)
g1_tool_approval_request = _g1_factory(tool_approval.ToolApprovalRequest)
g1_tool_approval_decision = _g1_factory(tool_approval.ToolApprovalDecision)
