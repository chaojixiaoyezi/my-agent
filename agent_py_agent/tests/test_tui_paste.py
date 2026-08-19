from __future__ import annotations

from agent_py_agent.cli.chat_parts.tui_paste import (
    collapse_tui_paste,
    expand_tui_paste_refs,
)


def test_short_paste_stays_inline_without_hidden_reference() -> None:
    visible, reference = collapse_tui_paste("one\ntwo", paste_id=1)

    assert visible == "one\ntwo"
    assert reference is None


def test_long_or_tall_paste_collapses_with_reference_and_expands() -> None:
    original = "one\ntwo\nthree\nfour"
    visible, reference = collapse_tui_paste(original, paste_id=7)

    assert visible == "[Pasted text #7 +3 lines]"
    assert reference is not None
    assert expand_tui_paste_refs(f"before {visible} after", (reference,)) == (
        f"before {original} after"
    )


def test_pasted_placeholder_like_content_is_not_recursively_expanded() -> None:
    nested = "literal [Pasted text #2 +1 lines] inside"
    visible, reference = collapse_tui_paste(
        nested,
        paste_id=1,
        threshold=1,
    )

    assert reference is not None
    assert expand_tui_paste_refs(visible, (reference,)) == nested
