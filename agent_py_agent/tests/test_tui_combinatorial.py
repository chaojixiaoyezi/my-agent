from __future__ import annotations

from itertools import combinations, product

import pytest

from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, render_tui_snapshot
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_markdown import display_width_fragments
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore

FACTORS = {
    "width": (24, 40, 80, 120),
    "content": ("ascii", "cjk", "emoji", "combining", "bidi", "controls"),
    "phase": ("idle", "running", "failed"),
    "tool": ("none", "command", "diff", "write"),
    "detail": (False, True),
    "queue": (0, 1, 3),
}

CONTENT = {
    "ascii": "plain text /path --flag",
    "cjk": "中文宽字符与标点，第二行",
    "emoji": "家庭 👩\u200d👩\u200d👧\u200d👦 国旗 🇨🇳",
    "combining": "Cafe\u0301 A\u0308",
    "bidi": "English אבג العربية 123",
    "controls": "literal\\x1b]52;c;AAAA\\x07 must stay text",
}


def _all_pair_obligations() -> set[tuple[str, object, str, object]]:
    obligations: set[tuple[str, object, str, object]] = set()
    names = tuple(FACTORS)
    for left, right in combinations(names, 2):
        obligations.update(
            (left, left_value, right, right_value)
            for left_value in FACTORS[left]
            for right_value in FACTORS[right]
        )
    return obligations


def _covered_pairs(case: dict[str, object]) -> set[tuple[str, object, str, object]]:
    return {
        (left, case[left], right, case[right])
        for left, right in combinations(tuple(FACTORS), 2)
    }


def _pairwise_cases() -> tuple[dict[str, object], ...]:
    names = tuple(FACTORS)
    candidates = [
        dict(zip(names, values))
        for values in product(*(FACTORS[name] for name in names))
    ]
    uncovered = _all_pair_obligations()
    selected: list[dict[str, object]] = []
    while uncovered:
        best = max(
            candidates,
            key=lambda case: len(_covered_pairs(case) & uncovered),
        )
        newly_covered = _covered_pairs(best) & uncovered
        if not newly_covered:
            raise AssertionError("pairwise generator cannot cover remaining obligations")
        selected.append(best)
        uncovered -= newly_covered
        candidates.remove(best)
    return tuple(selected)


PAIRWISE_CASES = _pairwise_cases()


def _case_id(case: dict[str, object]) -> str:
    return "-".join(str(case[name]) for name in FACTORS)


def _store_for_case(case: dict[str, object]) -> TuiStateStore:
    store = TuiStateStore()
    seq = TuiEventSequencer("pairwise", clock=lambda: 100.0)
    store.publish(
        seq.emit(
            "user_message",
            "completed",
            "user",
            {"text": CONTENT[str(case["content"])]},
        )
    )
    for index in range(int(case["queue"])):
        store.publish(
            seq.emit(
                "queue_added",
                "queued",
                f"queue:{index}",
                {"queue_id": f"queue:{index}", "text": f"queued {index} 中文"},
            )
        )
    phase = str(case["phase"])
    if phase in {"running", "failed"}:
        store.publish(seq.emit("turn_started", "started", "turn"))
    if phase == "running":
        store.publish(seq.emit("thinking_started", "started", "thinking"))
    elif phase == "failed":
        store.publish(seq.emit("turn_failed", "failed", "turn"))
    tool = str(case["tool"])
    if tool != "none":
        display = _tool_display(tool)
        store.publish(
            seq.emit(
                "tool_started",
                "started",
                "tool",
                {"tool": "run_command", "display": display},
            )
        )
        store.publish(
            seq.emit(
                "tool_completed",
                "completed",
                "tool",
                {"tool": "run_command", "ok": True, "display": display, "output": "ok"},
            )
        )
    return store


def _tool_display(kind: str) -> dict[str, object]:
    if kind == "diff":
        return {
            "kind": "diff",
            "path": "src/宽字符.ts",
            "lines_added": 1,
            "lines_removed": 1,
            "lines": [
                {"kind": "removed", "old_line": 1, "text": "old"},
                {"kind": "added", "new_line": 1, "text": "新🙂"},
            ],
        }
    if kind == "write":
        return {"kind": "write", "path": "docs/中文.md", "preview": "hello\n世界"}
    return {"kind": "command", "command": "printf 'ok'", "output": "ok"}


def test_pairwise_covering_array_covers_every_factor_pair() -> None:
    covered: set[tuple[str, object, str, object]] = set()
    for case in PAIRWISE_CASES:
        covered.update(_covered_pairs(case))
    assert covered == _all_pair_obligations()
    assert len(PAIRWISE_CASES) < 80


@pytest.mark.parametrize("case", PAIRWISE_CASES, ids=_case_id)
def test_pairwise_render_invariants(case: dict[str, object]) -> None:
    store = _store_for_case(case)
    before = store.snapshot()
    width = int(case["width"])
    frame = render_tui_snapshot(
        before,
        TuiRenderContext(
            width=width,
            detailed_transcript=bool(case["detail"]),
            show_all=bool(case["detail"]),
            now=110.0,
            status_started_at=100.0,
            status_last_event_at=100.0,
            notice="组合测试",
        ),
    )
    after = store.snapshot()

    assert before == after
    assert all(display_width_fragments(line) <= width for line in frame.transcript_lines)
    assert all(display_width_fragments(line) <= width for line in frame.overlay_lines)
    assert display_width_fragments(frame.footer) <= width
    stable_ids = {block.block_id for block in after.stable_blocks}
    active_ids = {block.block_id for block in after.active_blocks}
    assert len(stable_ids) == len(after.stable_blocks)
    assert len(active_ids) == len(after.active_blocks)
    assert stable_ids.isdisjoint(active_ids)


@pytest.mark.parametrize(
    ("width", "content", "tool"),
    tuple(product(FACTORS["width"], FACTORS["content"], FACTORS["tool"])),
)
def test_high_risk_width_unicode_tool_three_way(width: int, content: str, tool: str) -> None:
    case = {
        "width": width,
        "content": content,
        "phase": "idle",
        "tool": tool,
        "detail": True,
        "queue": 0,
    }
    frame = render_tui_snapshot(
        _store_for_case(case).snapshot(),
        TuiRenderContext(width=width, detailed_transcript=True, show_all=True),
    )
    assert all(display_width_fragments(line) <= width for line in frame.transcript_lines)
