from __future__ import annotations

import pytest

from agent_py_agent.agent.conversation.compact_progress import (
    COMPACT_AUTHORITY_CONVERSATION,
    COMPACT_AUTHORITY_LEGACY,
    COMPACT_AUTHORITY_TURN_LOCAL,
    COMPACT_SOURCE_ACTIVE_TURN,
    COMPACT_SOURCE_LEGACY,
    COMPACT_SOURCE_TRANSCRIPT,
    COMPACT_SOURCE_TURN_LOCAL,
    CONVERSATION_COMPACT_PROGRESS_SCHEMA,
    normalize_conversation_compact_progress,
)


def _progress(**overrides: object) -> dict[str, object]:
    return {
        "schema": CONVERSATION_COMPACT_PROGRESS_SCHEMA,
        "phase": "progress",
        "stage": "summarizing",
        "percent": 35,
        "generation": 2,
        "operation_id": "compact:test",
        "before_tokens": 120_000,
        "after_tokens": 0,
        "trigger_tokens": 115_200,
        "source_messages": 80,
        **overrides,
    }


@pytest.mark.parametrize(
    ("source_kind", "commit_authority"),
    (
        (COMPACT_SOURCE_TRANSCRIPT, COMPACT_AUTHORITY_CONVERSATION),
        (COMPACT_SOURCE_ACTIVE_TURN, COMPACT_AUTHORITY_CONVERSATION),
        (COMPACT_SOURCE_TURN_LOCAL, COMPACT_AUTHORITY_TURN_LOCAL),
    ),
)
def test_compact_progress_accepts_only_canonical_source_authority_pairs(
    source_kind: str,
    commit_authority: str,
) -> None:
    value = normalize_conversation_compact_progress(
        _progress(source_kind=source_kind, commit_authority=commit_authority)
    )

    assert value["source_kind"] == source_kind
    assert value["commit_authority"] == commit_authority


def test_compact_progress_maps_fieldless_v1_event_to_explicit_legacy_projection() -> None:
    value = normalize_conversation_compact_progress(_progress())

    assert value["source_kind"] == COMPACT_SOURCE_LEGACY
    assert value["commit_authority"] == COMPACT_AUTHORITY_LEGACY


@pytest.mark.parametrize(
    "overrides",
    (
        {"source_kind": COMPACT_SOURCE_TRANSCRIPT},
        {"commit_authority": COMPACT_AUTHORITY_CONVERSATION},
        {
            "source_kind": COMPACT_SOURCE_TRANSCRIPT,
            "commit_authority": COMPACT_AUTHORITY_TURN_LOCAL,
        },
        {"source_kind": "unknown", "commit_authority": "unknown"},
    ),
)
def test_compact_progress_rejects_partial_or_contradictory_authority(
    overrides: dict[str, object],
) -> None:
    assert not normalize_conversation_compact_progress(_progress(**overrides))
