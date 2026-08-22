from __future__ import annotations

from agent_py_agent.cli.chat_parts.tui_interaction import (
    TuiDraft,
    TuiInteractionState,
)


def test_stash_is_single_slot_and_submit_consumes_it() -> None:
    redraws: list[bool] = []
    state = TuiInteractionState(lambda: redraws.append(True))

    assert state.toggle_stash(TuiDraft("draft text", 5)) is None
    assert state.snapshot().has_stash is True
    assert state.consume_stash_after_submit() == TuiDraft("draft text", 5)
    assert state.snapshot().has_stash is False
    assert len(redraws) == 2


def test_empty_input_pops_stash_without_submitting_it() -> None:
    state = TuiInteractionState()
    state.toggle_stash(TuiDraft("多行\n草稿", 3))

    restored = state.toggle_stash(TuiDraft("", 0))

    assert restored == TuiDraft("多行\n草稿", 3)
    assert state.snapshot().has_stash is False


def test_history_search_matches_newest_unique_and_tracks_failure() -> None:
    state = TuiInteractionState()
    state.start_history_search(
        TuiDraft("unfinished", 4),
        ["new prompt alpha", "older alpha", "new prompt alpha", "other"],
    )

    first = state.update_history_query("alpha")
    second = state.next_history_match()
    exhausted = state.next_history_match()

    assert first == TuiDraft("new prompt alpha", len("new prompt "))
    assert second == TuiDraft("older alpha", len("older "))
    assert exhausted is None
    assert state.snapshot().history_failed_match is True
    assert state.accept_history_search() == second
    assert state.snapshot().history_search_active is False


def test_history_search_cancel_restores_original_and_execute_requires_match() -> None:
    state = TuiInteractionState()
    original = TuiDraft("current draft", 3)
    state.start_history_search(original, ["one", "two"])
    assert state.update_history_query("missing") is None
    assert state.execute_history_search() is None

    state.start_history_search(original, ["one", "two"])
    state.update_history_query("one")
    assert state.cancel_history_search() == original


def test_pasting_is_visible_without_entering_history_state() -> None:
    state = TuiInteractionState()
    state.set_pasting(True)
    assert state.snapshot().is_pasting is True
    assert state.snapshot().history_search_active is False
    state.set_pasting(False)
    assert state.snapshot().is_pasting is False


def test_help_toggle_is_explicit_and_idempotently_closes() -> None:
    redraws: list[bool] = []
    state = TuiInteractionState(lambda: redraws.append(True))

    assert state.toggle_help() is True
    assert state.snapshot().help_open is True
    assert state.close_help() is True
    assert state.close_help() is False
    assert state.snapshot().help_open is False
    assert len(redraws) == 2


def test_todo_expansion_toggle_is_display_only_and_redraws() -> None:
    redraws: list[bool] = []
    state = TuiInteractionState(lambda: redraws.append(True))

    assert state.snapshot().todos_expanded is False
    assert state.toggle_todos() is True
    assert state.snapshot().todos_expanded is True
    assert state.toggle_todos() is False
    assert state.snapshot().todos_expanded is False
    assert len(redraws) == 2


def test_long_paste_refs_survive_stash_and_expand_only_on_submit() -> None:
    state = TuiInteractionState()
    visible = state.register_text_paste("one\ntwo\nthree\nfour")
    draft = state.capture_draft(visible, len(visible))

    assert visible == "[Pasted text #1 +3 lines]"
    assert state.expand_draft(draft) == "one\ntwo\nthree\nfour"
    state.toggle_stash(draft)
    state.install_draft(TuiDraft("", 0))
    restored = state.toggle_stash(TuiDraft("", 0))

    assert restored == draft
    assert restored is not None
    state.install_draft(restored)
    assert state.expand_draft(state.capture_draft(restored.text, restored.cursor_position)) == (
        "one\ntwo\nthree\nfour"
    )
