"""Tests for W7A: unified auxiliary model selector, 10m-30m-60m backoff, shared cooldown, and headless lane retry cap."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends.errors import (
    ModelNotConfiguredError,
    ProviderQuotaExhaustedError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from agent_py_agent.agent.conversation.compact_guard import (
    ConversationCompactError,
    is_compact_transient_provider_failure,
)
from agent_py_agent.agent.gateway_parts.model_profile_service import ModelProfileService
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_MODEL_NOT_CONFIGURED,
    curator_failure_retry_seconds,
)
from agent_py_agent.agent.memory_store.curator_state import (
    CuratorSuccessCommit,
    MemoryCuratorStateStore,
)
from agent_py_agent.agent.settings.curator_profile import (
    auxiliary_profile_cooldown_remaining,
    clear_auxiliary_profile_cooldown,
    effective_auxiliary_model_profile,
    record_auxiliary_profile_failure,
)
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.settings.thread_model_selection import (
    AUXILIARY_MODEL_OPERATIONS,
)
from agent_py_agent.cli.gateway_lane_retry import (
    _MAX_HEADLESS_CONSECUTIVE_RETRY_COUNT,
    BackgroundLaneRetry,
)
from agent_py_agent.cli.gateway_loops import _curator_run_allowed, _run_skill_learning

# ============================================================================
# Requirement 1: Default memory_curator_model_profile == "" -> 0 LLM calls
# ============================================================================

def test_empty_auxiliary_profile_disables_background_curator_and_skill_learning(tmp_path: Path):
    """When memory_curator_model_profile is empty, both curator and skill learning make 0 LLM calls."""
    config = SimpleNamespace(
        memory_curator_model_profile="",
        enable_self_learning=True,
        config_path=tmp_path / "agent_config.yaml",
    )
    fake_agent = SimpleNamespace(
        config=config,
        home_paths=SimpleNamespace(root=tmp_path, config_dir=tmp_path / "config"),
    )

    # Effective profile is empty
    assert effective_auxiliary_model_profile(config) == ""

    # Gateway loop precheck disallows curator immediately without building LLM
    fake_curator = SimpleNamespace(config=SimpleNamespace(enabled=True))
    allowed = _curator_run_allowed(fake_curator, fake_agent)
    assert allowed is False

    # Skill learning precheck returns immediately without calling LLM
    called = []
    fake_skill_service = SimpleNamespace(
        process_due=lambda: called.append("called"),
    )
    fake_agent.skill_learning_service = fake_skill_service
    _run_skill_learning(fake_agent, 100.0)
    assert called == []


# ============================================================================
# Requirement 2: /model auxiliary_select and auxiliary_off live toggle
# ============================================================================

def test_auxiliary_model_live_toggle_without_restart(tmp_path: Path):
    """auxiliary_select and auxiliary_off live update in-memory config and YAML."""
    config_path = tmp_path / "user_config.yaml"
    config_path.write_text("agent_name: test\n", encoding="utf-8")

    from agent_py_agent.agent.settings.config import AgentConfig

    config = AgentConfig(
        memory_curator_model_profile="",
        config_path=config_path,
    )
    home_paths = SimpleNamespace(
        root=tmp_path,
        config_dir=tmp_path,
        owner_provider="local",
        owner_kind="main",
        owner_id="main",
    )
    fake_host = SimpleNamespace(
        config=config,
        home_paths=home_paths,
        llm=SimpleNamespace(name="default_llm"),
    )

    # 1. Add a profile with a valid UUID
    prof_id = str(uuid4())
    profile_data = {
        "model_name": "aux-test-model",
        "model_backend": "openai_compatible",
        "api_base": "https://aux.example.test/v1",
        "api_key": "sk-aux-key",
    }
    execute_model_profile_operation(
        fake_host,
        "add",
        {"profile_id": prof_id, "profile": profile_data},
    )

    assert "auxiliary_select" in AUXILIARY_MODEL_OPERATIONS
    assert "auxiliary_off" in AUXILIARY_MODEL_OPERATIONS
    assert "auxiliary_list" in AUXILIARY_MODEL_OPERATIONS

    # 2. Select auxiliary profile
    service = ModelProfileService()
    res_select = service.execute_auxiliary_model_operation(
        fake_host, "auxiliary_select", {"profile_id": prof_id}
    )
    assert res_select.get("ok") is True
    assert res_select.get("selected") == prof_id
    assert fake_host.config.memory_curator_model_profile == prof_id
    assert effective_auxiliary_model_profile(fake_host.config) == prof_id

    # Verify YAML updated on disk
    yaml_text = config_path.read_text(encoding="utf-8")
    assert prof_id in yaml_text

    # 3. List auxiliary profiles
    res_list = service.execute_auxiliary_model_operation(
        fake_host, "auxiliary_list", {}
    )
    assert res_list.get("ok") is True
    assert res_list.get("selected") == prof_id

    # 4. Turn auxiliary off
    res_off = service.execute_auxiliary_model_operation(
        fake_host, "auxiliary_off", {}
    )
    assert res_off.get("ok") is True
    assert res_off.get("selected") == ""
    assert fake_host.config.memory_curator_model_profile == ""
    assert effective_auxiliary_model_profile(fake_host.config) == ""

    yaml_text = config_path.read_text(encoding="utf-8")
    assert "memory_curator_model_profile: ''" in yaml_text or 'memory_curator_model_profile: ""' in yaml_text


# ============================================================================
# Requirement 3: Curator 10m -> 30m -> 60m exponential backoff & auto-reset
# ============================================================================

def test_curator_exponential_backoff_progression_and_reset(tmp_path: Path):
    """curator_failure_retry_seconds progresses 600s -> 1800s -> 3600s cap and resets on success."""
    # consecutive_failures == 0: preserves original 2-arg behavior
    assert curator_failure_retry_seconds("SOME_ERROR", 300, consecutive_failures=0) == 300
    assert curator_failure_retry_seconds(CURATOR_MODEL_NOT_CONFIGURED, 300, consecutive_failures=0) == 3600

    # consecutive_failures >= 1: 10m (600s) -> 30m (1800s) -> 60m (3600s)
    assert curator_failure_retry_seconds("SOME_ERROR", 300, consecutive_failures=1) == 600
    assert curator_failure_retry_seconds("SOME_ERROR", 300, consecutive_failures=2) == 1800
    assert curator_failure_retry_seconds("SOME_ERROR", 300, consecutive_failures=3) == 3600
    assert curator_failure_retry_seconds("SOME_ERROR", 300, consecutive_failures=5) == 3600

    # State store tracking consecutive_failures
    store = MemoryCuratorStateStore(tmp_path / "state.json")
    state = store.load()
    assert state.consecutive_failures == 0

    # Failure 1
    acq1 = store.acquire(reason="session_close", config_revision="r1", lease_seconds=60)
    assert acq1 is not None
    _, lease1 = acq1
    f1 = store.commit_failure(lease_id=str(lease1["lease_id"]), failure_code="CURATOR_MODEL_FAILED")
    assert f1.consecutive_failures == 1
    assert store.load().consecutive_failures == 1

    # Failure 2
    acq2 = store.acquire(reason="session_close", config_revision="r1", lease_seconds=60)
    assert acq2 is not None
    _, lease2 = acq2
    f2 = store.commit_failure(lease_id=str(lease2["lease_id"]), failure_code="CURATOR_MODEL_FAILED")
    assert f2.consecutive_failures == 2
    assert store.load().consecutive_failures == 2

    # Failure 3
    acq3 = store.acquire(reason="session_close", config_revision="r1", lease_seconds=60)
    assert acq3 is not None
    _, lease3 = acq3
    f3 = store.commit_failure(lease_id=str(lease3["lease_id"]), failure_code="CURATOR_MODEL_FAILED")
    assert f3.consecutive_failures == 3

    # Success resets consecutive_failures to 0
    acq4 = store.acquire(reason="session_close", config_revision="r1", lease_seconds=60)
    assert acq4 is not None
    _, lease4 = acq4
    commit = CuratorSuccessCommit(
        lease_id=str(lease4["lease_id"]),
        run_id=str(lease4["run_id"]),
        reason="session_close",
        per_thread_cursors={},
        last_processed_audit_event_id="",
        processed_messages=1,
        processed_audit_events=0,
        candidate_count=0,
        daily_event_count=0,
    )
    s_state = store.commit_success(commit)
    assert s_state.consecutive_failures == 0
    assert store.load().consecutive_failures == 0


# ============================================================================
# Requirement 4: 20-owner shared auxiliary profile cooldown & auto-clear
# ============================================================================

def test_shared_auxiliary_profile_cooldown_across_owners():
    """Single profile failure cools down across 20 owners; success or /model clears it."""
    prof = "shared-aux-cluster-profile"
    clear_auxiliary_profile_cooldown(prof)

    # Initial state: no cooldown
    assert auxiliary_profile_cooldown_remaining(prof, now_monotonic=100.0) == 0.0

    # Owner 1 suffers a failure at t=100.0
    backoff = record_auxiliary_profile_failure(prof, "CURATOR_MODEL_FAILED", now_monotonic=100.0)
    assert backoff == 600

    # Owners 2..20 check cooldown at t=150.0 -> all see remaining > 0, 0 provider calls
    for _owner_idx in range(2, 21):
        rem = auxiliary_profile_cooldown_remaining(prof, now_monotonic=150.0)
        assert rem == 550.0

    # Strike 2 at t=750.0 (past 600s)
    backoff2 = record_auxiliary_profile_failure(prof, "CURATOR_MODEL_FAILED", now_monotonic=750.0)
    assert backoff2 == 1800
    assert auxiliary_profile_cooldown_remaining(prof, now_monotonic=800.0) == 1750.0

    # Once a probe succeeds after expiry, cooldown is cleared for all owners
    clear_auxiliary_profile_cooldown(prof)
    for _owner_idx in range(1, 21):
        assert auxiliary_profile_cooldown_remaining(prof, now_monotonic=800.0) == 0.0


# ============================================================================
# Requirement 5: Headless thread-bg-* lane retry cap (3) & latching
# ============================================================================

def test_headless_lane_retry_cap_and_latching():
    """Headless lanes retry attempts 1 and 2, latch waiting_for_user_input at attempt 3 until marker advances."""
    retry = BackgroundLaneRetry()
    owner = "local:main:main"
    thread_id = "thread-bg-curator-worker"
    exc = RuntimeError("Gateway timeout")

    # Attempt 1: wait_count=1, waiting_for_user_input=False
    retry.failed(owner, thread_id, exc, delay=5.0, wait_for_inbound_marker="m1", headless=True)
    row1 = retry._failures[(owner, thread_id)]
    assert row1.wait_count == 1
    assert row1.waiting_for_user_input is False

    # Attempt 2: wait_count=2, waiting_for_user_input=False
    retry.failed(owner, thread_id, exc, delay=5.0, wait_for_inbound_marker="m1", headless=True)
    row2 = retry._failures[(owner, thread_id)]
    assert row2.wait_count == 2
    assert row2.waiting_for_user_input is False

    # Attempt 3 (_MAX_HEADLESS_CONSECUTIVE_RETRY_COUNT = 3): waiting_for_user_input=True
    retry.failed(owner, thread_id, exc, delay=5.0, wait_for_inbound_marker="m1", headless=True)
    row3 = retry._failures[(owner, thread_id)]
    assert row3.wait_count == 3
    assert row3.waiting_for_user_input is True

    # While inbound_marker == "m1", ready() returns False
    assert retry.ready(owner, thread_id, model_ready=lambda: True, inbound_marker=lambda: "m1") is False

    # When inbound_marker advances to "m2", ready() unblocks!
    assert retry.ready(owner, thread_id, model_ready=lambda: True, inbound_marker=lambda: "m2") is True


# ============================================================================
# Requirement 6: compact_guard is_compact_transient_provider_failure alignment
# ============================================================================

def test_compact_transient_failure_alignment_and_quota_priority():
    """Verify ProviderTimeoutError and COMPACT_TRANSIENT_FAILURE treated as transient, quota failure treated as non-transient."""
    # 1. Quota check first: quota exhaustion returns False
    quota_err = ProviderQuotaExhaustedError("insufficient balance")
    wrapped_quota = ConversationCompactError("compact failed", code="COMPACT_TRANSIENT_FAILURE")
    wrapped_quota.__cause__ = quota_err
    assert is_compact_transient_provider_failure(wrapped_quota) is False
    assert is_compact_transient_provider_failure(quota_err) is False

    # 2. ProviderTimeoutError returns True
    timeout_err = ProviderTimeoutError("Request timed out after 30s")
    wrapped_timeout = ConversationCompactError("compact timeout", code="COMPACT_TRANSIENT_FAILURE")
    wrapped_timeout.__cause__ = timeout_err
    assert is_compact_transient_provider_failure(wrapped_timeout) is True
    assert is_compact_transient_provider_failure(timeout_err) is True

    # 3. ProviderTransientError returns True
    transient_err = ProviderTransientError("Connection reset by peer")
    wrapped_transient = ConversationCompactError("compact transient", code="COMPACT_TRANSIENT_FAILURE")
    wrapped_transient.__cause__ = transient_err
    assert is_compact_transient_provider_failure(wrapped_transient) is True

    # 4. Error code patterns
    assert is_compact_transient_provider_failure(
        ConversationCompactError("rate limit", code="COMPACT_RATE_LIMIT")
    ) is True
    assert is_compact_transient_provider_failure(
        ConversationCompactError("unavailable", code="COMPACT_UNAVAILABLE")
    ) is True

    # 5. Non-transient errors return False
    assert is_compact_transient_provider_failure(RuntimeError("syntax error in code")) is False
    assert is_compact_transient_provider_failure(ValueError("invalid arguments")) is False
