from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    render_tui_snapshot,
)
from agent_py_agent.cli.chat_parts.tui_events import TuiEvent
from agent_py_agent.cli.chat_parts.tui_interaction import TuiDraft, TuiInteractionState
from agent_py_agent.cli.chat_parts.tui_markdown import display_width_fragments
from agent_py_agent.cli.chat_parts.tui_runtime import (
    TuiRuntime,
    TuiTurnEventAdapter,
    TuiTurnSummary,
)

_TERMINAL_TEXT = st.text(
    alphabet=st.sampled_from(
        list("abcXYZ 012/._-\n\t")
        + ["你", "界", "é", "\u0301", "🙂", "👩", "\u200d", "🇨", "🇳", "א"]
    ),
    max_size=80,
)


class TuiInteractionStateMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.state = TuiInteractionState()
        self.expected_stash: TuiDraft | None = None
        self.expected_help = False
        self.expected_pasting = False

    @rule()
    def toggle_help(self) -> None:
        self.expected_help = not self.expected_help
        assert self.state.toggle_help() is self.expected_help

    @rule()
    def close_help(self) -> None:
        changed = self.state.close_help()
        assert changed is self.expected_help
        self.expected_help = False

    @rule(value=st.booleans())
    def set_pasting(self, value: bool) -> None:
        self.state.set_pasting(value)
        self.expected_pasting = value

    @rule(text=_TERMINAL_TEXT, cursor=st.integers(min_value=-20, max_value=120))
    def toggle_stash(self, text: str, cursor: int) -> None:
        draft = TuiDraft(text, cursor)
        restored = self.state.toggle_stash(draft)
        if text.strip():
            self.expected_stash = draft
            assert restored is None
        elif self.expected_stash is not None:
            assert restored == self.expected_stash
            self.expected_stash = None
        else:
            assert restored is None

    @rule()
    def consume_stash(self) -> None:
        assert self.state.consume_stash_after_submit() == self.expected_stash
        self.expected_stash = None

    @rule(text=_TERMINAL_TEXT)
    def paste_round_trip(self, text: str) -> None:
        visible = self.state.register_text_paste(text)
        draft = self.state.capture_draft(visible, len(visible))
        assert self.state.expand_draft(draft) == text
        assert 0 <= draft.cursor_position <= len(draft.text)

    @invariant()
    def visible_snapshot_matches_model(self) -> None:
        snapshot = self.state.snapshot()
        assert snapshot.has_stash is (self.expected_stash is not None)
        assert snapshot.help_open is self.expected_help
        assert snapshot.is_pasting is self.expected_pasting


TuiInteractionStateMachine.TestCase.settings = settings(
    max_examples=60,
    stateful_step_count=60,
    deadline=None,
)
TestTuiInteractionStateMachine = TuiInteractionStateMachine.TestCase


class TuiRuntimeStateMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.runtime = TuiRuntime("stateful-runtime")
        self.active_request_id: str | None = None
        self.active_adapter: TuiTurnEventAdapter | None = None
        self.expected_queued: dict[str, str] = {}
        self.expected_steers: dict[str, tuple[str, str]] = {}
        self.request_index = 0
        self.message_index = 0
        self.tool_index = 0
        self.compaction_index = 0
        self.external_seq = 0
        self.external_event_index = 0

    def _next_request_id(self, prefix: str) -> str:
        self.request_index += 1
        return f"{prefix}-{self.request_index}"

    def _next_message_id(self) -> str:
        self.message_index += 1
        return f"message-{self.message_index}"

    def _active_steer_ids(self) -> tuple[str, ...]:
        return tuple(
            message_id
            for message_id, (_text, request_id) in self.expected_steers.items()
            if request_id == self.active_request_id
        )

    def _semantic_snapshot(self) -> tuple[object, ...]:
        snapshot = self.runtime.store.snapshot()
        return (
            snapshot.stable_blocks,
            snapshot.active_blocks,
            snapshot.pending_steers,
            snapshot.queued_inputs,
            snapshot.permission,
            snapshot.status,
        )

    def _external_event(
        self,
        *,
        seq: int,
        event_id: str,
        block_id: str,
        text: str,
    ) -> TuiEvent:
        return TuiEvent(
            event_id=event_id,
            seq=seq,
            stream_id="stateful:external",
            block_id=block_id,
            kind="system_message",
            phase="completed",
            payload={"text": text},
            session_id="stateful-runtime",
            created_at=float(seq),
        )

    @rule(text=_TERMINAL_TEXT)
    def add_queued_prompt(self, text: str) -> None:
        request_id = self._next_request_id("queued")
        self.runtime.enqueue_prompt(request_id, text, queued=True)
        self.expected_queued[request_id] = text

    @precondition(lambda self: bool(self.expected_queued))
    @rule()
    def restore_oldest_queued_prompt(self) -> None:
        request_id = next(iter(self.expected_queued))
        self.runtime.restore_prompts((request_id,))
        self.expected_queued.pop(request_id)

    @precondition(lambda self: self.active_request_id is None and bool(self.expected_queued))
    @rule()
    def promote_oldest_queued_prompt(self) -> None:
        request_id = next(iter(self.expected_queued))
        adapter = self.runtime.begin_turn(request_id)
        self.expected_queued.pop(request_id)
        self.active_request_id = request_id
        self.active_adapter = adapter

    @precondition(lambda self: self.active_request_id is None)
    @rule(text=_TERMINAL_TEXT)
    def begin_direct_turn(self, text: str) -> None:
        request_id = self._next_request_id("direct")
        self.runtime.enqueue_prompt(request_id, text, queued=False)
        self.active_request_id = request_id
        self.active_adapter = self.runtime.begin_turn(request_id)

    @precondition(lambda self: self.active_adapter is not None)
    @rule()
    def repeat_begin_turn_is_idempotent(self) -> None:
        assert self.active_request_id is not None
        assert self.runtime.begin_turn(self.active_request_id) is self.active_adapter

    @precondition(lambda self: self.active_request_id is not None)
    @rule(text=_TERMINAL_TEXT)
    def add_active_turn_input(self, text: str) -> None:
        assert self.active_request_id is not None
        message_id = self._next_message_id()
        self.runtime.enqueue_active_turn_input(message_id, text)
        self.expected_steers[message_id] = (text, self.active_request_id)

    @precondition(lambda self: bool(self.expected_steers))
    @rule()
    def cancel_oldest_active_turn_input(self) -> None:
        message_id = next(iter(self.expected_steers))
        assert self.runtime.cancel_active_turn_input(message_id)
        assert not self.runtime.cancel_active_turn_input(message_id)
        self.expected_steers.pop(message_id)

    @precondition(lambda self: bool(self._active_steer_ids()))
    @rule()
    def promote_oldest_active_turn_input(self) -> None:
        assert self.active_adapter is not None
        assert self.active_request_id is not None
        message_id = self._active_steer_ids()[0]
        text, _request_id = self.expected_steers.pop(message_id)
        self.active_adapter.begin_active_turn_input((message_id, message_id, "unknown-message"))
        snapshot = self.runtime.store.snapshot()
        promoted = next(
            block
            for block in snapshot.stable_blocks
            if block.block_id == f"user:{self.active_request_id}:steer:{message_id}"
        )
        assert promoted.text == text
        before = self._semantic_snapshot()
        assert not self.runtime.promote_active_turn_inputs(
            (message_id,),
            request_id=self.active_request_id,
        )
        assert self._semantic_snapshot() == before

    @precondition(lambda self: self.active_request_id is not None)
    @rule()
    def unknown_active_turn_input_is_a_noop(self) -> None:
        assert self.active_request_id is not None
        before = self._semantic_snapshot()
        assert not self.runtime.promote_active_turn_inputs(
            ("missing-active-turn-message",),
            request_id=self.active_request_id,
        )
        assert self._semantic_snapshot() == before

    @precondition(lambda self: self.active_adapter is not None)
    @rule(text=_TERMINAL_TEXT)
    def stream_model_text(self, text: str) -> None:
        assert self.active_adapter is not None
        assert self.active_adapter.write_model(text) is bool(text.strip())

    @precondition(lambda self: self.active_adapter is not None)
    @rule(text=_TERMINAL_TEXT, duration=st.integers(min_value=0, max_value=30))
    def publish_thinking(self, text: str, duration: int) -> None:
        assert self.active_adapter is not None
        assert self.active_adapter.write_thinking(
            text,
            duration_seconds=float(duration),
        ) is bool(text.strip())

    @precondition(lambda self: self.active_adapter is not None)
    @rule(text=_TERMINAL_TEXT, ok=st.booleans())
    def publish_tool_lifecycle(self, text: str, ok: bool) -> None:
        assert self.active_adapter is not None
        assert self.active_request_id is not None
        self.tool_index += 1
        progress = {
            "tool": "run_command",
            "round": self.tool_index,
            "call_index": 0,
            "phase": "started",
            "detail": text,
        }
        block_id = f"tool:{self.active_request_id}:{self.tool_index}:0"
        self.active_adapter.write_progress(progress)
        self.active_adapter.write_progress(
            {**progress, "phase": "updated", "output": f"partial {text}"}
        )
        terminal = {
            **progress,
            "phase": "completed" if ok else "failed",
            "ok": ok,
            "output": f"result {text}",
        }
        self.active_adapter.write_progress(terminal)
        snapshot = self.runtime.store.snapshot()
        assert block_id not in {block.block_id for block in snapshot.active_blocks}
        assert sum(block.block_id == block_id for block in snapshot.stable_blocks) == 1

        self.active_adapter.write_progress(terminal)
        self.active_adapter.write_progress(
            {**progress, "phase": "updated", "output": "late progress"}
        )
        snapshot = self.runtime.store.snapshot()
        assert block_id not in {block.block_id for block in snapshot.active_blocks}
        assert sum(block.block_id == block_id for block in snapshot.stable_blocks) == 1

    @precondition(lambda self: self.active_adapter is not None)
    @rule(
        current=st.integers(min_value=0, max_value=300_000),
        window=st.integers(min_value=1, max_value=300_000),
        trigger=st.integers(min_value=0, max_value=300_000),
    )
    def publish_context_usage(self, current: int, window: int, trigger: int) -> None:
        assert self.active_adapter is not None
        usage = {
            "schema": "model_visible_context_usage.v1",
            "current_tokens": current,
            "context_window_tokens": window,
            "compact_trigger_tokens": trigger,
            "prompt_tokens": current // 5,
            "messages_tokens": current // 4,
            "runtime_guidance_tokens": current // 6,
            "tool_schema_tokens": current // 7,
            "estimated": True,
            "protocol": "native",
        }
        assert self.active_adapter.write_context_usage(usage)
        context_usage = self.runtime.store.snapshot().status.context_usage
        assert context_usage is not None
        assert context_usage.current_tokens == current
        assert context_usage.context_window_tokens == window
        assert context_usage.compact_trigger_tokens == trigger

    @precondition(lambda self: self.active_adapter is not None)
    @rule(
        before=st.integers(min_value=1, max_value=300_000),
        after=st.integers(min_value=0, max_value=300_000),
    )
    def publish_context_compaction(self, before: int, after: int) -> None:
        assert self.active_adapter is not None
        assert self.active_request_id is not None
        self.compaction_index += 1
        generation = self.compaction_index
        compaction = {
            "schema": "model_visible_context_compaction.v1",
            "generation": generation,
            "before_tokens": before,
            "after_tokens": after,
            "trigger_tokens": before,
            "dropped_pairs": generation,
            "preserved_pairs": generation % 4,
        }
        assert self.active_adapter.write_context_compaction(compaction)
        assert self.active_adapter.on_gateway_event(
            {
                "kind": "conversation_compacted",
                "compact_generation": generation,
            }
        )
        context_block_id = f"context-window:{self.active_request_id}:{generation}"
        durable_block_id = f"compact:{self.active_request_id}:{generation}"
        snapshot = self.runtime.store.snapshot()
        assert (
            sum(
                block.block_id in {context_block_id, durable_block_id}
                for block in snapshot.stable_blocks
            )
            == 2
        )

        assert self.active_adapter.write_context_compaction(compaction)
        assert self.active_adapter.on_gateway_event(
            {
                "kind": "conversation_compacted",
                "compact_generation": generation,
            }
        )
        snapshot = self.runtime.store.snapshot()
        assert (
            sum(
                block.block_id in {context_block_id, durable_block_id}
                for block in snapshot.stable_blocks
            )
            == 2
        )

    @precondition(lambda self: self.active_adapter is not None)
    @rule()
    def request_interrupt_is_idempotent(self) -> None:
        assert self.runtime.request_interrupt()
        before = self.runtime.store.snapshot()
        assert self.runtime.request_interrupt()
        assert self.runtime.store.snapshot() == before
        assert before.status.phase == "interrupting"

    @precondition(lambda self: self.active_adapter is not None)
    @rule(
        outcome=st.sampled_from(("completed", "failed", "interrupted")),
        text=_TERMINAL_TEXT,
    )
    def finalize_turn(self, outcome: str, text: str) -> None:
        assert self.active_adapter is not None
        assert self.active_request_id is not None
        summary = TuiTurnSummary(
            response_text=text if outcome == "completed" else "",
            ok=outcome == "completed",
            interrupted=outcome == "interrupted",
            error="typed failure" if outcome == "failed" else "",
            context_tokens=123,
            output_tokens=45,
            tool_rounds=self.tool_index,
        )
        adapter = self.active_adapter
        self.runtime.complete_turn(self.active_request_id, summary)
        snapshot = self.runtime.store.snapshot()
        expected_phase = {
            "completed": "idle",
            "failed": "failed",
            "interrupted": "interrupted",
        }[outcome]
        assert snapshot.status.phase == expected_phase
        before = snapshot
        adapter.finalize(summary)
        assert self.runtime.store.snapshot() == before
        self.active_request_id = None
        self.active_adapter = None

    @rule(text=_TERMINAL_TEXT)
    def exact_event_replay_is_a_noop(self, text: str) -> None:
        self.external_seq += 1
        self.external_event_index += 1
        event = self._external_event(
            seq=self.external_seq,
            event_id=f"external-{self.external_event_index}",
            block_id=f"external-block-{self.external_event_index}",
            text=text,
        )
        assert self.runtime.store.publish(event).status == "accepted"
        before = self.runtime.store.snapshot()
        assert self.runtime.store.publish(event).status == "duplicate"
        assert self.runtime.store.snapshot() == before

    @rule(text=_TERMINAL_TEXT)
    def out_of_order_event_cannot_mutate_visible_state(self, text: str) -> None:
        self.external_seq += 2
        self.external_event_index += 1
        accepted = self._external_event(
            seq=self.external_seq,
            event_id=f"external-{self.external_event_index}",
            block_id=f"external-block-{self.external_event_index}",
            text=text,
        )
        assert self.runtime.store.publish(accepted).status == "accepted"
        before = self._semantic_snapshot()
        self.external_event_index += 1
        late = self._external_event(
            seq=self.external_seq - 1,
            event_id=f"external-{self.external_event_index}",
            block_id=f"external-block-{self.external_event_index}",
            text=f"late {text}",
        )
        result = self.runtime.store.publish(late)
        assert result.status == "rejected"
        assert result.reason == "out_of_order_seq"
        assert self._semantic_snapshot() == before

    @rule(
        width=st.sampled_from((10, 16, 24, 40, 80, 120)),
        detailed=st.booleans(),
    )
    def render_at_width_is_pure(self, width: int, detailed: bool) -> None:
        before = self.runtime.store.snapshot()
        frame = render_tui_snapshot(
            before,
            TuiRenderContext(
                width=width,
                detailed_transcript=detailed,
                show_all=detailed,
                now=before.status.last_event_at + 1.0,
                status_started_at=before.status.started_at,
                status_last_event_at=before.status.last_event_at,
                context_tokens=before.status.context_tokens,
                context_usage=before.status.context_usage,
                output_tokens=before.status.output_tokens,
                has_active_tools=any(block.role == "tool" for block in before.active_blocks),
            ),
        )
        after = self.runtime.store.snapshot()
        assert before == after
        lines = (
            *frame.transcript_lines,
            *frame.overlay_lines,
            *frame.input_status_lines,
            frame.footer,
        )
        assert all(display_width_fragments(line) <= width for line in lines)

    @invariant()
    def runtime_and_view_identities_stay_consistent(self) -> None:
        snapshot = self.runtime.store.snapshot()
        stable_ids = tuple(block.block_id for block in snapshot.stable_blocks)
        active_ids = tuple(block.block_id for block in snapshot.active_blocks)
        pending_ids = tuple(item.message_id for item in snapshot.pending_steers)
        queue_ids = tuple(item.queue_id for item in snapshot.queued_inputs)

        assert len(stable_ids) == len(set(stable_ids))
        assert len(active_ids) == len(set(active_ids))
        assert set(stable_ids).isdisjoint(active_ids)
        assert len(pending_ids) == len(set(pending_ids))
        assert len(queue_ids) == len(set(queue_ids))
        assert {item.message_id: item.text for item in snapshot.pending_steers} == {
            message_id: value[0] for message_id, value in self.expected_steers.items()
        }
        assert self.runtime._pending_steers == {
            message_id: value[0] for message_id, value in self.expected_steers.items()
        }
        assert {item.queue_id: item.text for item in snapshot.queued_inputs} == {
            f"queue:{request_id}": text for request_id, text in self.expected_queued.items()
        }
        assert self.runtime._queued_prompts == {
            f"queue:{request_id}": text for request_id, text in self.expected_queued.items()
        }
        expected_turns = {self.active_request_id} if self.active_request_id is not None else set()
        assert set(self.runtime._turns) == expected_turns
        if self.active_request_id is None:
            assert snapshot.status.phase in {"idle", "failed", "interrupted"}
        else:
            assert snapshot.status.phase in {"running", "interrupting"}


TuiRuntimeStateMachine.TestCase.settings = settings(
    max_examples=36,
    stateful_step_count=50,
    deadline=None,
    derandomize=True,
    database=None,
)
TestTuiRuntimeStateMachine = TuiRuntimeStateMachine.TestCase


@st.composite
def _chunkings(draw):
    text = draw(_TERMINAL_TEXT.filter(bool))
    if len(text) == 1:
        return text, [text]
    cut_points = draw(
        st.sets(
            st.integers(min_value=1, max_value=len(text) - 1),
            max_size=min(12, len(text) - 1),
        )
    )
    boundaries = [0, *sorted(cut_points), len(text)]
    return text, [text[start:end] for start, end in zip(boundaries, boundaries[1:])]


@settings(max_examples=80, deadline=None)
@given(_chunkings())
def test_stream_chunk_boundaries_do_not_change_terminal_semantics(case) -> None:
    text, chunks = case

    def run(parts: list[str]) -> tuple[tuple[str, str, str, str], ...]:
        runtime = TuiRuntime("metamorphic-stream")
        runtime.enqueue_prompt("request", "普通用户需求", queued=False)
        turn = runtime.begin_turn("request")
        for part in parts:
            turn.write_model(part)
        runtime.complete_turn("request", TuiTurnSummary(response_text=text))
        return tuple(
            (block.block_id, block.role, block.phase, block.text)
            for block in runtime.store.snapshot().stable_blocks
        )

    assert run([text]) == run(chunks)
