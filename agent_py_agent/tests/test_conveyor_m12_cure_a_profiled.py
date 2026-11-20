from __future__ import annotations

import copy
import dataclasses
import inspect
import pickle

import pytest

from agent_py_agent.agent.agent_core.model.context_pressure import (
    ModelVisibleContextBudget,
    ModelVisibleContextSnapshot,
)
from agent_py_agent.agent.assets.descriptor import (
    AssetDescriptor,
    AssetDescriptorValidationError,
    AssetRefs,
    _Record,
)
from agent_py_agent.agent.capability._agents_contract_fields import AgentsContractError
from agent_py_agent.agent.common import fast_dataclass
from agent_py_agent.agent.conversation.models import ConversationThread, WakeSignal
from agent_py_agent.agent.memory_store.security import MemoryThreatFinding, MemoryThreatScanResult
from agent_py_agent.agent.settings.config_layers import (
    ConfigLayer,
    EffectiveConfig,
    compact_admin_forced,
)
from agent_py_agent.agent.settings.deployment_policy import (
    CachePolicy,
    CandidateCachePolicy,
    DeploymentPolicyCommitError,
    DeploymentPolicyError,
    FilesystemFallbackBudget,
    RuntimeGuardPolicy,
    TaskCachePolicy,
)
from agent_py_agent.agent.user_space.owner_policy import (
    EffectiveOwnerPolicy,
    LearningExclusionEntry,
    OwnerDiskUsage,
    OwnerPolicyBundle,
    OwnerPolicyBundleReport,
)


def _plain_guard_post_init(self):
    object.__setattr__(self, "tool_circuit_backoff_seconds", tuple(map(float, self.tool_circuit_backoff_seconds)))
    object.__setattr__(self, "provider_transient_auto_resume_delays_seconds",
                       tuple(map(float, self.provider_transient_auto_resume_delays_seconds)))
    object.__setattr__(self, "unknown_command_allowlist", tuple(self.unknown_command_allowlist))


def _plain_filesystem_budget_post_init(self):
    object.__setattr__(self, "max_wall_seconds", float(self.max_wall_seconds))


def _plain_asset_refs_post_init(self):
    for name in ("workflows", "scripts", "skills", "monitors"):
        object.__setattr__(self, name, tuple(getattr(self, name)))


def _plain_thread_post_init(self):
    if not self.workspace_root and self.cwd:
        object.__setattr__(self, "workspace_root", self.cwd)


_PLAIN_NORMALIZERS = {
    RuntimeGuardPolicy: _plain_guard_post_init,
    FilesystemFallbackBudget: _plain_filesystem_budget_post_init,
    AssetRefs: _plain_asset_refs_post_init,
    ConversationThread: _plain_thread_post_init,
}
_SNAPSHOT_ARGS = (400, 320, 90, 10, 65, 5, 10, "native", 90, "surface")
_OWNER_BUNDLE = OwnerPolicyBundle({}, {}, {}, {}, {}, {})
_OWNER_POLICY_ARGS = ("owner", "/owner", "workspace", (), "disabled", False, 1, 2, 3, 4, (), True, False, (), (), ())
_DTO_CASES = (
    (RuntimeGuardPolicy, (), {"tool_circuit_backoff_seconds": [1, 2.5], "unknown_command_allowlist": ["helper"]},
     {"provider_transient_auto_resume_delays_seconds": [4]}),
    (CandidateCachePolicy, (), {"max_paths": 3}, {"max_paths": 4}),
    (TaskCachePolicy, (), {}, {"max_entries": 3}),
    (CachePolicy, (), {}, {"resident_budget_bytes": 1024}),
    (FilesystemFallbackBudget, (), {"max_wall_seconds": 7}, {"max_wall_seconds": 9}),
    (ModelVisibleContextBudget, (400, 320, 90, 230), {}, {"current_tokens": 110}),
    (ModelVisibleContextSnapshot, _SNAPSHOT_ARGS, {}, {"producer": {"source": "provider"}}),
    (MemoryThreatFinding, ("rule", "category", "description", "match"), {}, {"match": "updated"}),
    (MemoryThreatScanResult, (True,), {}, {"findings": (MemoryThreatFinding("rule", "kind", "details", "match"),)}),
    (AssetRefs, (), {"workflows": ["weekly"]}, {"scripts": ["render"]}),
    (ConfigLayer, ("user", 1), {}, {"values": {"percent": 80}}),
    (EffectiveConfig, ({"percent": 80}, {}, ()), {}, {"values": {"percent": 90}}),
    (OwnerPolicyBundle, ({}, {}, {}, {}, {}, {}), {}, {"quota": {"limit": 3}}),
    (OwnerPolicyBundleReport, (_OWNER_BUNDLE,), {}, {"load_errors": ({"code": "unavailable"},)}),
    (OwnerDiskUsage, (7, {"workspace": 7}), {}, {"total_bytes": 10}),
    (EffectiveOwnerPolicy, _OWNER_POLICY_ARGS, {}, {"active_grants": ({"ref": "grant"},)}),
    (LearningExclusionEntry, ("conversation",), {}, {"reason": "manual"}),
    (ConversationThread, ("thread", "user"), {"cwd": "/workspace"}, {"workspace_root": "/preferred"}),
    (WakeSignal, ("signal", "thread"), {}, {"status": "handled"}),
)


def _plain_dataclass(cls):
    namespace = {"__module__": cls.__module__}
    if cls in _PLAIN_NORMALIZERS:
        namespace["__post_init__"] = _PLAIN_NORMALIZERS[cls]
    return dataclasses.make_dataclass(
        cls.__name__, [(field.name, field.type, copy.copy(field)) for field in dataclasses.fields(cls)],
        namespace=namespace, frozen=True,
    )


def _state(instance):
    return tuple(getattr(instance, field.name) for field in dataclasses.fields(instance))


@pytest.mark.parametrize(("cls", "args", "kwargs", "changes"), _DTO_CASES, ids=[case[0].__name__ for case in _DTO_CASES])
def test_profiled_dataclass_matches_plain(cls, args, kwargs, changes):
    fast = cls(*args, **kwargs)
    assert "__slots__" in type(fast).__dict__
    assert not hasattr(fast, "__dict__")
    assert type(fast) in fast_dataclass.CONVERTED_CLASSES

    plain_cls = _plain_dataclass(cls)
    plain = plain_cls(*args, **kwargs)
    assert inspect.signature(cls) == inspect.signature(plain_cls)
    assert cls.__match_args__ == plain_cls.__match_args__
    assert _state(fast) == _state(plain)
    assert repr(fast) == repr(plain)
    assert fast == cls(*args, **kwargs)
    assert plain == plain_cls(*args, **kwargs)
    try:
        expected_hash = hash(plain)
    except TypeError as error:
        with pytest.raises(TypeError) as exc:
            hash(fast)
        assert str(exc.value) == str(error)
    else:
        assert hash(fast) == expected_hash

    for field in dataclasses.fields(cls):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(fast, field.name, None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            delattr(fast, field.name)
        if field.default_factory is dict and field.name not in kwargs:
            assert getattr(fast, field.name) is not getattr(cls(*args, **kwargs), field.name)

    for protocol in (2, pickle.HIGHEST_PROTOCOL):
        restored = pickle.loads(pickle.dumps(fast, protocol=protocol))
        assert type(restored) is cls
        assert restored == fast
        assert _state(restored) == _state(plain)

    expected = dataclasses.replace(plain, **changes)
    replaced = fast_dataclass.fast_replace(fast, **changes)
    assert _state(replaced) == _state(expected)
    assert replaced == dataclasses.replace(fast, **changes)
    assert replaced != fast
    assert _state(fast) == _state(plain)
    if hasattr(copy, "replace"):
        assert copy.replace(fast, **changes) == replaced

    invalid_changes = [{"unknown_field": True}]
    invalid_changes.extend({field.name: None} for field in dataclasses.fields(cls) if not field.init)
    for invalid in invalid_changes:
        with pytest.raises((ValueError, TypeError)) as base_error:
            dataclasses.replace(fast, **invalid)
        with pytest.raises(type(base_error.value)) as fast_error:
            fast_dataclass.fast_replace(fast, **invalid)
        assert str(fast_error.value) == str(base_error.value)


@pytest.mark.parametrize(
    ("cls", "base"),
    [(DeploymentPolicyError, ValueError), (DeploymentPolicyCommitError, DeploymentPolicyError),
     (AgentsContractError, ValueError), (AssetDescriptorValidationError, ValueError)],
)
def test_explicit_base_calls_match_loaded_mro(cls, base):
    pending = [cls]
    visited = set()
    while pending:
        child = pending.pop()
        if child in visited:
            continue
        visited.add(child)
        assert child.__mro__[child.__mro__.index(cls) + 1] is base
        pending.extend(child.__subclasses__())


@pytest.mark.parametrize("alias", (
    "force_compact_trigger_percent", "admin_force_compact_trigger_percent", "memory_compact_admin_forced",
))
def test_compact_admin_aliases_keep_strict_boolean_lookup(alias):
    class Config:
        pass

    for value, expected in ((True, True), (False, False), (1, False), (None, False)):
        config = Config()
        setattr(config, alias, value)
        assert compact_admin_forced(config) is expected
        assert compact_admin_forced({alias: value}) is expected


def test_runtime_guard_reads_declared_and_extended_fields():
    class ExtendedRuntimeGuardPolicy(RuntimeGuardPolicy):
        extra_threshold = 7

    guard = RuntimeGuardPolicy(repeat_fail_threshold=0, provider_transient_auto_resume_delays_seconds=[])
    assert guard.int_value("repeat_fail_threshold", 5) == 0
    assert guard.int_value("missing", 6) == 6
    assert guard.bool_value("terminal_block_enabled", True) is False
    assert guard.float_tuple_value("provider_transient_auto_resume_delays_seconds", (9.0,)) == ()

    extended = ExtendedRuntimeGuardPolicy(repeat_fail_threshold=0, provider_transient_auto_resume_delays_seconds=[])
    assert extended.int_value("extra_threshold") == 7
    assert extended.values == guard.values


def test_asset_descriptor_keeps_cooperative_decode_and_normalization():
    calls = []

    class ExtraNormalization(_Record):
        @classmethod
        def from_dict(cls, payload):
            calls.append("decode")
            return super().from_dict(payload)

        def __post_init__(self):
            calls.append("normalize")
            super().__post_init__()

    class ExtendedDescriptor(AssetDescriptor, ExtraNormalization):
        pass

    descriptor = ExtendedDescriptor.from_dict({"id": "portrait", "kind": "script", "name": "Render"})
    assert descriptor.name == "Render"
    assert calls == ["decode", "normalize"]


def test_conversation_records_keep_legacy_json_shape():
    thread = ConversationThread.from_dict({"thread_id": "thread", "canonical_user_id": "user", "cwd": "/workspace"})
    serialized = thread.to_dict()
    assert serialized["workspace_root"] == "/workspace"
    assert serialized["model_selection_source"] == "unknown"
    assert all(name not in serialized for name in (
        "conversation_methods", "loaded_tool_names", "learning_source", "last_user_message_id", "last_user_request_id",
    ))
    assert ConversationThread.from_dict(serialized) == thread

    wake = WakeSignal.from_dict({"wake_signal_id": "signal", "thread_id": "thread", "evidence_refs": ["event"]})
    assert wake.urgency == "urgent"
    assert wake.to_dict()["evidence_refs"] == ["event"]
    assert WakeSignal.from_dict(wake.to_dict()) == wake


def g1_case_runtime_guard_policy():
    return lambda: RuntimeGuardPolicy(repeat_fail_threshold=11)


def g1_case_candidate_cache_policy():
    return lambda: CandidateCachePolicy(max_paths=4)


def g1_case_task_cache_policy():
    return lambda: TaskCachePolicy(max_entries=8)


def g1_case_cache_policy():
    return lambda: CachePolicy(resident_budget_bytes=1024)


def g1_case_filesystem_fallback_budget():
    return lambda: FilesystemFallbackBudget(max_wall_seconds=8)


def g1_case_model_visible_context_budget():
    return lambda: ModelVisibleContextBudget(400, 320, 90, 230)


def g1_case_model_visible_context_snapshot():
    return lambda: ModelVisibleContextSnapshot(400, 320, 90, 10, 65, 5, 10, "native", 90, "surface")


def g1_case_memory_threat_finding():
    return lambda: MemoryThreatFinding("rule", "category", "description", "match")


def g1_case_memory_threat_scan_result():
    return lambda: MemoryThreatScanResult(True)


def g1_case_asset_refs():
    # Astra-R3 的 60 次前台构造都来自 topology._connect 中的默认实例。
    return lambda: AssetRefs()


def g1_case_config_layer():
    return lambda: ConfigLayer("user", 1, {"percent": 80})


def g1_case_effective_config():
    return lambda: EffectiveConfig({"percent": 80}, {}, ())


def g1_case_owner_policy_bundle():
    return lambda: OwnerPolicyBundle({}, {}, {}, {}, {}, {})


def g1_case_owner_policy_bundle_report():
    bundle = OwnerPolicyBundle({}, {}, {}, {}, {}, {})
    return lambda: OwnerPolicyBundleReport(bundle)


def g1_case_owner_disk_usage():
    return lambda: OwnerDiskUsage(7, {"workspace": 7})


def g1_case_effective_owner_policy():
    return lambda: EffectiveOwnerPolicy("owner", "/owner", "workspace", (), "disabled", False,
                                        1, 2, 3, 4, (), True, False, (), (), ())


def g1_case_learning_exclusion_entry():
    return lambda: LearningExclusionEntry("conversation")


def g1_case_conversation_thread():
    return lambda: ConversationThread("thread", "user", cwd="/workspace")


def g1_case_wake_signal():
    return lambda: WakeSignal("signal", "thread")
