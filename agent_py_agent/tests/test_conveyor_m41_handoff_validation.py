import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.backends.tool_ir import UserTurn
from agent_py_agent.agent.conversation import compact_request_budget as budget_module
from agent_py_agent.agent.conversation.active_turn_compact import (
    ActiveTurnArchiveCompactRequest,
    compact_carried_active_turn_archive,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.memory_archive.compact_semantic_summary import (
    LiveToolHistorySummaryRequest,
    summarize_live_tool_history,
    validate_handoff_candidate,
)
from agent_py_agent.agent.prompting_parts.cache_layout import (
    CacheStructuredPrompt,
    prompt_cache_layout,
)
from agent_py_agent.tests.test_native_tool_ir_compact_and_orphan_sweep import (
    _carried_record,
    _native_agent,
)

_PREFIX = """[compact-live-handoff.v1]
current_progress: Checked the current report.
user_constraints: Keep the original input.
completed: Reconciled the report rows.
failures: none
unresolved: """
_BLANK_NEXT_STEP = "\nnext_step:"
_INVALID_HANDOFF = (
    _PREFIX + "Check the totals. ".ljust(290 - len(_PREFIX) - len(_BLANK_NEXT_STEP), "x") + _BLANK_NEXT_STEP
)
_VALID_HANDOFF = _PREFIX + "Check the totals.\nnext_step: Compare the final two rows."


def _install_transport(monkeypatch, responses, *, attempts):
    budget = budget_module.CompactAttemptBudget(attempts, {})
    sent = []
    replies = iter(responses)

    def generate(request):
        sent.append(request)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return ModelResponse(reply, "fixture")

    monkeypatch.setattr(budget_module, "_new_compact_attempt_budget", lambda _agent: budget)
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response", generate)
    return SimpleNamespace(generate=generate), budget, sent


def _summarize(backend):
    return summarize_live_tool_history(LiveToolHistorySummaryRequest(
        history=[UserTurn("Continue the report.")],
        backend=backend,
        agent=SimpleNamespace(backend=backend, config=SimpleNamespace(model_context_window_tokens=128_000)),
        provider_prompt=CacheStructuredPrompt(
            "Stable prefix.", "Current task.", stable_user_prefix="Stable instructions.",
            canonical_user_turn="Continue the report.",
        ),
    ))


def test_290_character_handoff_with_empty_next_step_is_repaired_on_same_budget(monkeypatch):
    assert len(_INVALID_HANDOFF) == 290
    backend, budget, sent = _install_transport(monkeypatch, (_INVALID_HANDOFF, _VALID_HANDOFF), attempts=2)
    result = _summarize(backend)

    assert result.split("\n", 1)[1] == _VALID_HANDOFF
    assert budget.remaining == 0
    assert budget.consumed_by_stage == {"cached": 1, "auxiliary": 1}
    first, repair = sent
    initial_layout = prompt_cache_layout(first.prompt)
    repaired_layout = prompt_cache_layout(repair.prompt)
    assert initial_layout is not None and repaired_layout is not None
    assert initial_layout.stable_prefix == repaired_layout.stable_prefix
    assert initial_layout.stable_user_prefix == repaired_layout.stable_user_prefix
    assert initial_layout.canonical_user_turn == repaired_layout.canonical_user_turn
    assert repaired_layout.volatile_suffix.startswith(initial_layout.volatile_suffix)
    assert initial_layout.volatile_suffix != repaired_layout.volatile_suffix
    assert first.messages == repair.messages


def test_valid_short_handoff_is_accepted_unchanged_without_repair(monkeypatch):
    text = (
        "[compact-live-handoff.v1]\ncurrent_progress: Ready\nuser_constraints: none\n"
        "completed: none\nfailures: none\nunresolved: none\nnext_step:\n- Compare rows"
    )
    assert len(text) < 160
    backend, budget, sent = _install_transport(monkeypatch, (text,), attempts=2)

    assert validate_handoff_candidate(text) == (True, "")
    assert _summarize(backend).split("\n", 1)[1] == text
    assert len(sent) == 1 and budget.consumed_by_stage == {"cached": 1}


@pytest.mark.parametrize(("old", "replacement", "code"), [
    ("failures: none\n", "", "HANDOFF_MISSING_SECTION"),
    ("failures: none\n", "failures: \t\n", "HANDOFF_EMPTY_SECTION"),
    ("failures: none\n", "failures: none\nfailures: none\n", "HANDOFF_DUPLICATE_SECTION"),
    ("failures: none\nunresolved: Check the totals.", "unresolved: Check the totals.\nfailures: none",
     "HANDOFF_SECTION_ORDER"),
    ("\nfailures: none", " failures: none", "HANDOFF_INLINE_SECTION"),
])
def test_short_candidate_requires_six_nonempty_ordered_line_start_sections(old, replacement, code):
    assert validate_handoff_candidate(_VALID_HANDOFF.replace(old, replacement)) == (False, code)


def test_segmented_candidate_is_checked_before_acceptance(monkeypatch):
    backend, budget, sent = _install_transport(
        monkeypatch, (ProviderContextWindowError("simulated smaller window"), _INVALID_HANDOFF), attempts=2,
    )

    with pytest.raises(ConversationCompactError, match="HANDOFF_EMPTY_SECTION") as failure:
        _summarize(backend)

    assert failure.value.code == "COMPACT_EMPTY_SUMMARY"
    assert len(sent) == 2 and budget.consumed_by_stage == {"cached": 1, "segment": 1}


@pytest.mark.parametrize("attempts", [1, 2], ids=["no_repair_budget", "still_invalid_after_repair"])
def test_invalid_new_handoff_preserves_the_readable_legacy_checkpoint(tmp_path, monkeypatch, attempts):
    backend, budget, sent = _install_transport(monkeypatch, (_INVALID_HANDOFF, _INVALID_HANDOFF), attempts=attempts)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"channel": "test", "channel_conversation_id": "m41"})
    old_message = store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "Start report."})
    agent = _native_agent(tmp_path)
    agent.backend = backend
    agent.config.model_context_window_tokens = 128_000
    agent.config.memory_compact_trigger_percent = 90
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    ledger = agent.home_paths.owner_compact_dir / "conversations" / f"{thread.thread_id}.jsonl"
    ledger.parent.mkdir(parents=True)
    legacy = {
        "schema": "conversation_compact_checkpoint.v1",
        "checkpoint_id": "legacy-m41",
        "thread_id": thread.thread_id,
        "generation": 1,
        "previous_checkpoint_id": "",
        "source_kind": "transcript",
        "source_end_message_id": old_message.message_id,
        "summary": _INVALID_HANDOFF,
        "summary_sha256": hashlib.sha256(_INVALID_HANDOFF.encode()).hexdigest(),
        "created_at": 20.0,
        "operation_evidence": {},
    }
    original_ledger = (json.dumps(legacy) + "\n").encode()
    ledger.write_bytes(original_ledger)
    thread = store.threads.update_atomic(thread.thread_id, lambda current: replace(
        current, compact_checkpoint_id=legacy["checkpoint_id"], compact_generation=1, summary=_INVALID_HANDOFF,
    ))

    view = resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)
    assert view.summary == _INVALID_HANDOFF
    assert view.legacy_message_end_ids == (old_message.message_id,)
    with pytest.raises(ConversationCompactError, match="HANDOFF_EMPTY_SECTION") as failure:
        compact_carried_active_turn_archive(
            agent,
            store,
            thread,
            [_carried_record(index) for index in range(1, 6)],
            ActiveTurnArchiveCompactRequest(
                task_attributes={
                    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
                    "conversation_thread_id": thread.thread_id,
                },
                request_id="m41-new",
                attempt_id="m41-new-attempt",
                task_prompt="Continue the report.",
            ),
        )

    assert failure.value.code == "COMPACT_EMPTY_SUMMARY"
    assert len(sent) == attempts and budget.remaining == 0
    assert budget.consumed_by_stage == ({"cached": 1} if attempts == 1 else {"cached": 1, "auxiliary": 1})
    unchanged = store.threads.require(thread.thread_id)
    assert unchanged.compact_checkpoint_id == view.checkpoint_id
    assert unchanged.compact_generation == view.generation
    assert resolve_compact_summary_view(agent, unchanged, THREAD_COMPACT_SCOPE) == view
    assert ledger.read_bytes() == original_ledger
