"""Tests for Workstream W6: Dynamic Curator routing, background models, headless lane, and model pricing."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import (
    ProviderConfigurationError,
    ProviderModelCapabilityError,
    ProviderRequestRejectedError,
)
from agent_py_agent.agent.capability.skill_learning import (
    SkillLearningMaterial,
    SkillLearningRuntime,
    SkillLearningService,
    SkillLearningSettings,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.core import (
    _curator_model_router,
    _curator_profile_route,
    _skill_learning_service,
)
from agent_py_agent.agent.llm_scale.model_pricing import (
    _WARNED_UNKNOWN_MODELS,
    ModelPrice,
    resolve_price,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
    _is_deterministic_thread_model_rejection,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    CURATOR_REPLAY_BREAKER_OPEN,
    CuratorModelRoute,
    CuratorModelRouting,
    CuratorOutputViolation,
    MemoryCuratorConfig,
    parse_curator_extraction,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.cli.gateway_lane_retry import BackgroundLaneRetry
from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor


@dataclass
class _FakeProfileConfig:
    model_name: str
    model_backend: str
    stream_enabled: bool = True


def test_curator_skips_profiles_lacking_specific_tool_choice(monkeypatch) -> None:
    """_curator_profile_route and _curator_model_router skip profiles lacking 'specific' tool choice."""
    fake_caps = SimpleNamespace(native_tool_choices=("auto",))
    fake_backend = SimpleNamespace(name="anthropic_compatible", model_request_capabilities=fake_caps)
    monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda *a, **k: fake_backend)

    profile_config = _FakeProfileConfig(
        model_name="MiniMax-M3.1-Flash",
        model_backend="anthropic_compatible",
    )
    route = _curator_profile_route("minimax-prof", profile_config)
    assert route is None

    out_reasons: dict[str, str] = {}
    route_with_reasons = _curator_profile_route("minimax-prof", profile_config, out_reasons=out_reasons)
    assert route_with_reasons is None
    assert out_reasons.get("minimax-prof") == "forced_tool_choice_not_supported"


def test_skill_learning_dynamic_backend_provider(tmp_path: Path) -> None:
    """SkillLearningService calls backend_provider dynamically when provided."""
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home)

    backend1 = SimpleNamespace(name="backend-1")
    backend2 = SimpleNamespace(name="backend-2")
    active_backend = backend1

    called_backends: list[str] = []

    def fake_call(backend, prompt, response_schema, timeout_seconds):
        del prompt, response_schema, timeout_seconds
        called_backends.append(getattr(backend, "name", "unknown"))
        return ModelResponse(
            text=json.dumps({
                "schema_version": "skill_learning_decision.v1",
                "action": "skipped",
                "skill_name": "noop_skill",
                "reason": "test_reason",
            }),
            backend=getattr(backend, "name", "unknown"),
        )

    runtime = SkillLearningRuntime(
        backend=backend1,
        snapshot_provider=lambda: SimpleNamespace(entries=()),
        backend_provider=lambda: active_backend,
    )
    service = SkillLearningService(store, SkillLearningSettings(timeout_seconds=5), runtime)

    from agent_py_agent.agent.capability import skill_learning as sl_module
    orig_call = sl_module.call_backend_with_timeout
    sl_module.call_backend_with_timeout = fake_call
    try:
        mat = SkillLearningMaterial(request={"request_key": "k1"})
        service._call_model(mat)
        assert called_backends == ["backend-1"]

        active_backend = backend2
        service._call_model(mat)
        assert called_backends == ["backend-1", "backend-2"]
    finally:
        sl_module.call_backend_with_timeout = orig_call


def test_skill_learning_service_wiring(tmp_path: Path, monkeypatch) -> None:
    """_skill_learning_service configures backend_provider in SkillLearningRuntime."""
    home = ensure_my_agent_home(tmp_path / "home")
    fake_agent = SimpleNamespace(
        home_paths=home,
        skills_service=SimpleNamespace(snapshot_for=lambda: None),
        memory_curator=SimpleNamespace(backend="initial_be"),
        config=SimpleNamespace(enable_self_learning=True, memory_curator_model_profile=""),
    )
    backend_obj = SimpleNamespace(name="dynamic_be")
    monkeypatch.setattr(
        "agent_py_agent.agent.core._curator_backend",
        lambda agent, profile: (backend_obj, "p", "m", ""),
    )
    service = _skill_learning_service(fake_agent, fake_agent.config)
    assert service is not None
    assert callable(service.runtime.backend_provider)
    resolved = service.runtime.backend_provider()
    assert resolved is None

    fake_agent.config.memory_curator_model_profile = "aux-prof"
    resolved2 = service.runtime.backend_provider()
    assert resolved2 is backend_obj


def test_deterministic_thread_model_rejection_detection() -> None:
    """_is_deterministic_thread_model_rejection distinguishes deterministic vs transient errors."""
    assert _is_deterministic_thread_model_rejection(
        ProviderModelCapabilityError("forced_tool_choice_not_supported")
    )
    assert _is_deterministic_thread_model_rejection(
        ProviderRequestRejectedError("Unauthorized", status_code=401)
    )
    assert _is_deterministic_thread_model_rejection(
        ProviderRequestRejectedError("Forbidden", status_code=403)
    )
    assert _is_deterministic_thread_model_rejection(
        ProviderRequestRejectedError("Bad Request", status_code=400)
    )
    assert _is_deterministic_thread_model_rejection(
        ProviderConfigurationError("config invalid")
    )

    wrapped = RuntimeError("wrapped error")
    wrapped.__cause__ = ProviderRequestRejectedError("auth fail", status_code=401)
    assert _is_deterministic_thread_model_rejection(wrapped)

    assert not _is_deterministic_thread_model_rejection(RuntimeError("provider unavailable"))
    assert not _is_deterministic_thread_model_rejection(ValueError("bad value"))
    assert not _is_deterministic_thread_model_rejection(
        ProviderRequestRejectedError("Rate limited", status_code=429)
    )
    assert not _is_deterministic_thread_model_rejection(
        ProviderRequestRejectedError("Internal server error", status_code=500)
    )


def test_curator_extract_for_route_immediate_fallback(tmp_path: Path) -> None:
    """_extract_for_route immediately falls back in-run on deterministic error, not on plain RuntimeError."""
    conv_store = ConversationStore(tmp_path / "conversations")
    thread_a = conv_store.threads.create(owner_id="owner", canonical_user_id="user", cwd=str(tmp_path))
    msg = conv_store.messages.append({
        "thread_id": thread_a.thread_id,
        "role": "user",
        "channel": "internal",
        "content": "hello world",
        "now": 10.0,
        "metadata": {},
    })

    valid_payload = json.dumps({
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [],
        "candidates": [{
            "candidate_type": "long_term_fact",
            "content": "fact from default",
            "subject_key": "fact.1",
            "scope": {"scope_type": "personal", "scope_key": "personal", "applies_when": None, "excludes_when": None},
            "origin": "user_explicit",
            "source_message_refs": [{"message_id": msg.message_id}],
            "source_tool_refs": [],
            "source_artifact_refs": [],
            "observed_at": None,
            "valid_from": None,
            "valid_until": None,
            "confidence": 0.9,
            "proposed_action": "add",
            "target_entry_id": None,
            "link_candidate_id": None,
            "conflicts_with": [],
            "promotion_target": "long_term",
        }],
        "processed_message_refs": [{"message_id": msg.message_id}],
        "processed_audit_refs": [],
        "unresolved_refs": [],
        "warnings": [],
        "next_cursor": {
            "per_thread_cursors": [{"thread_id": thread_a.thread_id, "message_id": msg.message_id}],
            "last_audit_event_id": None,
        },
    })

    default_be = SimpleNamespace(
        generate_structured=lambda prompt, response_schema: ModelResponse(text=valid_payload, backend="default"),
    )
    failing_be = SimpleNamespace()

    def raise_cap_error(prompt, response_schema):
        raise ProviderModelCapabilityError("forced_tool_choice_not_supported")

    failing_be.generate_structured = raise_cap_error

    default_route = CuratorModelRoute("p-def", default_be, "p-def", "m-def", is_default=True)
    thread_route = CuratorModelRoute("p-a", failing_be, "p-a", "m-a", is_default=False)
    routing = CuratorModelRouting(default_route, {"p-a": thread_route}, {thread_a.thread_id: "p-a"}, {})

    config = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, timeout_seconds=1, max_retries=0)
    service = MemoryCuratorService(config=config, dependencies=MemoryCuratorDependencies(
        backend=default_be, conversation_store=conv_store, audit_dir=tmp_path / "audit",
        state_store=MemoryCuratorStateStore(tmp_path / "memory" / "curator" / "state.json"),
        daily_store=DailyMemoryStore(tmp_path / "memory" / "daily"),
        candidate_service=CandidateService(tmp_path / "memory" / "candidates.jsonl"),
        run_log=CuratorRunLog(tmp_path / "memory" / "curator" / "runs"),
        identity=MemoryCuratorIdentity(provider="p-def", model="m-def"),
        model_router=lambda tids: routing,
    ))

    result = service.run(reason="admin")
    assert result.status == "succeeded"
    assert result.processed_messages == 1
    assert any(
        "curator_thread_model_failed:CURATOR_MODEL_FAILED" in w
        for w in service.run_log.list()[0].warnings
    )


def test_parse_curator_extraction_optional_field_normalization() -> None:
    """parse_curator_extraction defaults optional top-level warnings & link_candidate_id while enforcing strict schema."""
    valid_base = {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [{
            "event_type": "task",
            "summary": "did work",
            "actor": "user",
            "origin": "user_explicit",
            "message_refs": [],
            "tool_refs": [],
            "artifact_refs": [],
            "decisions": [],
            "lessons": [],
            "next_actions": [],
        }],
        "candidates": [{
            "candidate_type": "long_term_fact",
            "content": "fact",
            "subject_key": "fact.k",
            "scope": {
                "scope_type": "personal",
                "scope_key": "personal",
                "applies_when": None,
                "excludes_when": None,
            },
            "origin": "user_explicit",
            "confidence": 0.8,
            "proposed_action": "add",
            "promotion_target": "long_term",
            "source_message_refs": [],
            "source_tool_refs": [],
            "source_artifact_refs": [],
            "conflicts_with": [],
            "observed_at": None,
            "valid_from": None,
            "valid_until": None,
            "target_entry_id": None,
        }],
        "processed_message_refs": [],
        "processed_audit_refs": [],
        "unresolved_refs": [],
        "next_cursor": {"per_thread_cursors": [], "last_audit_event_id": None},
    }

    parsed = parse_curator_extraction(json.dumps(valid_base))
    assert parsed.warnings == ()
    assert parsed.daily_events[0].artifact_refs == ()
    assert parsed.daily_events[0].decisions == ()
    assert not parsed.candidates[0].link_candidate_id
    assert not parsed.candidates[0].valid_until
    assert not parsed.candidates[0].scope.applies_when
    assert parsed.candidates[0].conflicts_with == ()

    extra_candidate = json.loads(json.dumps(valid_base))
    extra_candidate["candidates"][0]["unknown_field"] = "bad"
    with pytest.raises(CuratorOutputViolation) as exc_info:
        parse_curator_extraction(json.dumps(extra_candidate))
    assert exc_info.value.violation_code == "unknown_property"
    assert "$.candidates[0]" in exc_info.value.violation_path

    missing_scope = json.loads(json.dumps(valid_base))
    del missing_scope["candidates"][0]["scope"]
    with pytest.raises(CuratorOutputViolation) as exc_info:
        parse_curator_extraction(json.dumps(missing_scope))
    assert exc_info.value.violation_code == "missing_property"
    assert "$.candidates[0].scope" in exc_info.value.violation_path


def test_background_agent_model_inheritance(tmp_path: Path, monkeypatch) -> None:
    """BackgroundAgentService inherits source model profile when non-empty, falls back on default."""
    conv_store = ConversationStore(tmp_path / "conversations")
    source_thread = conv_store.threads.create(
        owner_id="owner",
        canonical_user_id="user",
        cwd=str(tmp_path),
    )
    host = SimpleNamespace(
        conversation_store=conv_store,
        home_paths=SimpleNamespace(owner_id="owner", owner_home_dir=tmp_path),
        config=SimpleNamespace(),
    )
    actor = HostChannelActor("owner", source_thread.thread_id, "entry", "local", "local", "user")

    monkeypatch.setattr(
        "agent_py_agent.agent.settings.thread_model_selection.default_model_profile_id",
        lambda h: "default-model",
    )

    def fake_selected_config(h, profile_id):
        if profile_id in ("default-model", "source-model", "explicit-model"):
            return SimpleNamespace(model_name=profile_id)
        from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
        raise ModelProfileError("not_found", reason="profile_not_found")

    monkeypatch.setattr(
        "agent_py_agent.agent.settings.model_profiles.selected_model_config",
        fake_selected_config,
    )

    spaces = SimpleNamespace(
        get=lambda space_id, actor: {"space": {"space_id": space_id, "state": "active", "revision": 1}}
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.business_spaces.BusinessSpaceService",
        lambda h: spaces,
    )

    service = BackgroundAgentService(host)

    # 1. source has model_profile_id = "" -> default
    res1 = service.create(
        {"space_id": "space-1", "name": "worker1"},
        actor=actor,
        idempotency_key="k1",
    )
    thread1 = conv_store.threads.require(res1["agent"]["thread_id"])
    assert thread1.model_profile_id == "default-model"
    assert thread1.model_selection_source == "default"

    # 2. source has model_profile_id = "source-model" -> inherited
    source_thread2 = conv_store.threads.create(
        owner_id="owner",
        canonical_user_id="user",
        cwd=str(tmp_path),
    )
    conv_store.threads.update_atomic(
        source_thread2.thread_id,
        lambda t: replace(
            t,
            model_profile_id="source-model",
            model_selection_revision=t.model_selection_revision + 1,
            model_selection_source="explicit",
            model_selection_last_explicit_revision=t.model_selection_revision + 1,
        ),
    )
    actor2 = HostChannelActor("owner", source_thread2.thread_id, "entry", "local", "local", "user")
    res2 = service.create(
        {"space_id": "space-1", "name": "worker2"},
        actor=actor2,
        idempotency_key="k2",
    )
    thread2 = conv_store.threads.require(res2["agent"]["thread_id"])
    assert thread2.model_profile_id == "source-model"
    assert thread2.model_selection_source == "inherited"

    # 3. request specifies explicit model -> explicit
    res3 = service.create(
        {"space_id": "space-1", "name": "worker3", "model_profile_id": "explicit-model"},
        actor=actor2,
        idempotency_key="k3",
    )
    thread3 = conv_store.threads.require(res3["agent"]["thread_id"])
    assert thread3.model_profile_id == "explicit-model"
    assert thread3.model_selection_source == "explicit"

    # 4. source has deleted/invalid model -> falls back to default
    source_thread4 = conv_store.threads.create(
        owner_id="owner",
        canonical_user_id="user",
        cwd=str(tmp_path),
    )
    conv_store.threads.update_atomic(
        source_thread4.thread_id,
        lambda t: replace(
            t,
            model_profile_id="deleted-model",
            model_selection_revision=t.model_selection_revision + 1,
            model_selection_source="explicit",
            model_selection_last_explicit_revision=t.model_selection_revision + 1,
        ),
    )
    actor4 = HostChannelActor("owner", source_thread4.thread_id, "entry", "local", "local", "user")
    res4 = service.create(
        {"space_id": "space-1", "name": "worker4"},
        actor=actor4,
        idempotency_key="k4",
    )
    thread4 = conv_store.threads.require(res4["agent"]["thread_id"])
    assert thread4.model_profile_id == "default-model"
    assert thread4.model_selection_source == "default"


def test_gateway_loops_curator_breaker_backoff_and_headless_lane(tmp_path: Path) -> None:
    """_curator_has_pending_reason respects backoff and headless threads never park waiting for input."""
    owner_home = tmp_path / "home"
    curator_dir = owner_home / "memory" / "curator"
    curator_dir.mkdir(parents=True)
    state_file = curator_dir / "state.json"

    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_home),
    )
    supervisor = _BackgroundMainSupervisor.__new__(_BackgroundMainSupervisor)
    supervisor._lane_retry = BackgroundLaneRetry()

    # Active breaker cooldown with empty lease -> False
    state_file.write_text(json.dumps({
        "pending_reasons": ["admin"],
        "active_lease": "",
        "last_failure_code": CURATOR_REPLAY_BREAKER_OPEN,
        "last_failure_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    assert not supervisor._curator_has_pending_reason(agent)

    # Active lease -> True (urgent lease recovery)
    state_file.write_text(json.dumps({
        "pending_reasons": ["admin"],
        "active_lease": "lease-123",
        "last_failure_code": CURATOR_REPLAY_BREAKER_OPEN,
        "last_failure_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    assert supervisor._curator_has_pending_reason(agent)

    # Recent failure within cooldown -> False
    state_file.write_text(json.dumps({
        "pending_reasons": ["admin"],
        "active_lease": "",
        "last_failure_code": "CURATOR_SCHEMA_INVALID",
        "last_failure_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    assert not supervisor._curator_has_pending_reason(agent)

    # Old failure beyond cooldown -> True
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    state_file.write_text(json.dumps({
        "pending_reasons": ["admin"],
        "active_lease": "",
        "last_failure_code": "CURATOR_SCHEMA_INVALID",
        "last_failure_at": old_time,
    }), encoding="utf-8")
    assert supervisor._curator_has_pending_reason(agent)

    # Headless thread-bg-* never parked in waiting_for_user_input
    supervisor._record_thread_failure(
        "owner-1",
        "thread-bg-headless-01",
        ConversationCompactError("compact err", code="COMPACT_FAILED"),
    )
    key_headless = ("owner-1", "thread-bg-headless-01")
    failure_headless = supervisor._lane_retry._failures.get(key_headless)
    assert failure_headless is not None
    assert failure_headless.waiting_for_user_input is False

    # Normal user thread still gets waiting_for_user_input=True
    supervisor._record_thread_failure(
        "owner-1",
        "thread-user-01",
        ConversationCompactError("compact err", code="COMPACT_FAILED"),
    )
    key_user = ("owner-1", "thread-user-01")
    failure_user = supervisor._lane_retry._failures.get(key_user)
    assert failure_user is not None
    assert failure_user.waiting_for_user_input is True


def test_model_pricing_prefixes(caplog) -> None:
    """resolve_price matches gpt-6, gpt-5, gpt-4, and deepseek without warning."""
    for model in ("gpt-6.1-sol", "gpt-5-turbo", "gpt-4o", "deepseek-chat"):
        _WARNED_UNKNOWN_MODELS.discard(model)

    with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.llm_scale.model_pricing"):
        p_gpt6 = resolve_price("gpt-6.1-sol")
        p_gpt5 = resolve_price("gpt-5-preview")
        p_gpt4 = resolve_price("gpt-4.5")
        p_deepseek = resolve_price("deepseek-chat")

    assert p_gpt6 == ModelPrice(2.50, 10.0)
    assert p_gpt5 == ModelPrice(2.00, 8.0)
    assert p_gpt4 == ModelPrice(2.50, 10.0)
    assert p_deepseek == ModelPrice(0.27, 1.10)

    warnings = [r.getMessage() for r in caplog.records if "未知模型" in r.getMessage()]
    assert len(warnings) == 0
