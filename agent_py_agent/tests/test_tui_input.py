from __future__ import annotations

import threading
import time
from queue import Queue
from types import SimpleNamespace

from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.document import Document
from prompt_toolkit.widgets import TextArea

from agent_py_agent.cli.chat_parts import tui_keybindings
from agent_py_agent.cli.chat_parts.chat_style import CHAT_RESPONSE_STYLE_INJECT
from agent_py_agent.cli.chat_parts.plain_state import ChatJob
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_input import (
    QUEUE_EDIT_PLACEHOLDER,
    TuiCompletion,
    TuiCompletionMenuControl,
    TuiInputCompleter,
    TuiQueuedPlaceholderProcessor,
    apply_selected_completion,
    move_completion_selection,
    move_input_cursor_by_wrapped_rows,
    restore_queued_prompts,
)
from agent_py_agent.cli.chat_parts.tui_interaction import TuiInteractionState
from agent_py_agent.cli.chat_parts.tui_keybindings import _normalize_bracketed_paste
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState


def _job(request_id: str, text: str) -> ChatJob:
    return ChatJob(
        user=text,
        show_prompt=False,
        inject=[],
        prompt_files=[],
        request_id=request_id,
        display_text=text,
    )


def test_restore_queued_prompts_drains_real_queue_and_merges_draft() -> None:
    runtime = TuiRuntime("queue-restore")
    runtime.enqueue_prompt("running", "one", queued=False)
    runtime.begin_turn("running")
    jobs: Queue[ChatJob] = Queue()
    for request_id, text in (("queued-1", "two"), ("queued-2", "three")):
        runtime.enqueue_prompt(request_id, text, queued=True)
        jobs.put(_job(request_id, text))
    pending = [2]

    restored = restore_queued_prompts(
        jobs=jobs,
        runtime=runtime,
        state_lock=threading.Lock(),
        pending_jobs_ref=pending,
        current_text="draft",
        current_cursor=2,
    )

    assert restored is not None
    assert restored.text == "two\nthree\ndraft"
    assert restored.cursor_position == len("two\nthree\n") + 2
    assert restored.request_ids == ("queued-1", "queued-2")
    assert jobs.empty()
    assert jobs.unfinished_tasks == 0
    assert pending == [0]
    assert runtime.store.snapshot().queued_inputs == ()


def test_escape_queue_restore_uses_same_canonical_queue_path() -> None:
    runtime = TuiRuntime("queue-escape")
    jobs: Queue[ChatJob] = Queue()
    runtime.enqueue_prompt("queued-1", "queued", queued=True)
    jobs.put(_job("queued-1", "queued"))
    input_area = TextArea(multiline=True)
    input_area.text = "draft"
    pending = [1]
    params = SimpleNamespace(
        input_area=input_area,
        jobs=jobs,
        tui_runtime=runtime,
        state_lock=threading.Lock(),
        pending_jobs_ref_for_enqueue=pending,
        interaction_state=TuiInteractionState(),
    )

    assert tui_keybindings._restore_editable_queue(params) is True

    assert input_area.text == "queued\ndraft"
    assert pending == [0]
    assert runtime.store.snapshot().queued_inputs == ()


def test_escape_stops_focused_child_and_ctrl_g_back_only_navigates(monkeypatch) -> None:
    input_area = TextArea(multiline=True)
    app = SimpleNamespace(invalidate_calls=0)
    app.invalidate = lambda: setattr(app, "invalidate_calls", app.invalidate_calls + 1)
    snapshot = SimpleNamespace(
        active_run_id="child-a",
        terminal=False,
    )
    back_calls: list[bool] = []
    navigation = SimpleNamespace(
        snapshot=lambda: snapshot,
        back=lambda: back_calls.append(True) or True,
    )
    params = SimpleNamespace(
        input_area=input_area,
        agent_navigation=navigation,
        exit_armed_at_ref=[0.0],
        eof_armed_at_ref=[0.0],
        escape_armed_at_ref=[0.0],
        escape_armed_text_ref=[""],
    )
    stopped: list[str] = []
    monkeypatch.setattr(
        tui_keybindings,
        "_dispatch_agent_interrupt",
        lambda _event, _params, run_id: stopped.append(run_id),
    )

    event = SimpleNamespace(app=app)
    tui_keybindings._handle_escape_keybinding(event, params)
    assert stopped == ["child-a"]
    assert back_calls == []

    tui_keybindings._handle_agent_back_keybinding(event, params)
    assert back_calls == [True]
    assert stopped == ["child-a"]


def test_queued_placeholder_is_display_only() -> None:
    runtime = TuiRuntime("placeholder")
    runtime.enqueue_prompt("queued", "two", queued=True)
    processor = TuiQueuedPlaceholderProcessor(runtime)
    transformation = processor.apply_transformation(
        SimpleNamespace(
            lineno=0,
            document=Document(""),
            fragments=[("", "")],
        )
    )
    assert transformation.fragments[-1] == ("class:tui-placeholder", QUEUE_EDIT_PLACEHOLDER)
    assert Document("").text == ""


def test_input_completer_uses_structured_commands_and_one_level_paths(tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "draft.md").write_text("fixture", encoding="utf-8")
    completer = TuiInputCompleter(tmp_path)

    command = list(completer.get_completions(Document("/st"), None))
    assert [item.text for item in command] == ["/status", "/stop"]
    assert [item.display_text for item in command] == ["/status", "/stop"]
    paths = list(completer.get_completions(Document("@d"), None))
    assert [item.text for item in paths] == ["@docs/", "@draft.md"]
    prompt_file = list(completer.get_completions(Document("/prompt-file dr"), None))
    assert [item.text for item in prompt_file] == ["draft.md"]


def test_completion_menu_selects_first_without_editing_document() -> None:
    buffer = Buffer()
    original = Document("/", cursor_position=1)
    buffer.set_document(original, bypass_readonly=True)
    completions = [
        TuiCompletion(
            "/audit",
            start_position=-1,
            display="/audit",
            display_meta="Show Audit command help",
            kind="command",
        ),
        TuiCompletion(
            "/btw",
            start_position=-1,
            display="/btw",
            display_meta="Steer the current turn",
            kind="command",
        ),
    ]
    buffer.complete_state = CompletionState(original, completions=completions)
    control = TuiCompletionMenuControl(buffer)

    content = control.create_content(80, 6)

    assert buffer.text == "/"
    assert buffer.complete_state.complete_index == 0
    assert content.get_line(0)[0][0] == "class:tui-completion-selected"
    assert content.get_line(0)[0][1].startswith("  /audit")
    assert "bg:" not in content.get_line(0)[0][0]
    assert content.get_line(1)[0][0] == "class:tui-completion"


def test_completion_navigation_and_accept_keep_one_buffer_state() -> None:
    buffer = Buffer()
    original = Document("/", cursor_position=1)
    buffer.set_document(original, bypass_readonly=True)
    completions = [
        TuiCompletion(
            "/help",
            start_position=-1,
            display="/help",
            display_meta="Show help",
            kind="command",
            enter_action="submit",
            append_space=True,
        ),
        TuiCompletion(
            "/status",
            start_position=-1,
            display="/status",
            display_meta="Show status",
            kind="command",
            enter_action="submit",
            append_space=True,
        ),
    ]
    buffer.complete_state = CompletionState(original, completions=completions)

    assert move_completion_selection(buffer, -1) is completions[-1]
    assert buffer.text == "/"
    accepted = apply_selected_completion(buffer)

    assert accepted is completions[-1]
    assert buffer.text == "/status "
    assert buffer.complete_state is None


def test_up_down_moves_across_soft_wrapped_rows_before_history() -> None:
    buffer = Buffer()
    buffer.set_document(Document("abcdefghij", cursor_position=1), bypass_readonly=True)

    assert move_input_cursor_by_wrapped_rows(buffer, width=4, delta=1) is True
    assert buffer.cursor_position == 5
    assert move_input_cursor_by_wrapped_rows(buffer, width=4, delta=1) is True
    assert buffer.cursor_position == 9
    assert move_input_cursor_by_wrapped_rows(buffer, width=4, delta=1) is False
    assert move_input_cursor_by_wrapped_rows(buffer, width=4, delta=-1) is True
    assert buffer.cursor_position == 5


def test_wrapped_cursor_navigation_uses_terminal_width_for_cjk() -> None:
    buffer = Buffer()
    buffer.set_document(Document("甲乙丙丁", cursor_position=1), bypass_readonly=True)

    assert move_input_cursor_by_wrapped_rows(buffer, width=4, delta=1) is True
    assert buffer.cursor_position == 3
    assert move_input_cursor_by_wrapped_rows(buffer, width=4, delta=-1) is True
    assert buffer.cursor_position == 1


def test_wrapped_cursor_treats_exact_line_fill_as_next_visual_row() -> None:
    buffer = Buffer()
    buffer.set_document(Document("abcd", cursor_position=4), bypass_readonly=True)

    assert move_input_cursor_by_wrapped_rows(buffer, width=4, delta=-1) is True
    assert buffer.cursor_position == 0
    assert move_input_cursor_by_wrapped_rows(buffer, width=4, delta=1) is True
    assert buffer.cursor_position == 4


def test_bracketed_paste_normalization_strips_terminal_controls() -> None:
    assert _normalize_bracketed_paste("a\r\nb\t\x1b[31mred\x1b[0m") == "a\nb    red"


def test_ctrl_v_replaces_selected_input_from_application_clipboard() -> None:
    from prompt_toolkit.clipboard import InMemoryClipboard

    buffer = Buffer(complete_while_typing=False)
    buffer.text = "旧正文"
    buffer.cursor_position = 0
    buffer.start_selection()
    buffer.cursor_position = len(buffer.text)
    input_area = SimpleNamespace(buffer=buffer)
    clipboard = InMemoryClipboard()
    clipboard.set_text("新\r\n正文")
    invalidations: list[bool] = []
    app = SimpleNamespace(clipboard=clipboard, invalidate=lambda: invalidations.append(True))
    params = SimpleNamespace(
        input_area=input_area,
        interaction_state=TuiInteractionState(),
        exit_armed_at_ref=[0.0],
        eof_armed_at_ref=[0.0],
        escape_armed_at_ref=[0.0],
        escape_armed_text_ref=[""],
    )

    tui_keybindings._handle_clipboard_paste(SimpleNamespace(app=app), params)

    assert buffer.text == "新\n正文"
    assert buffer.selection_state is None
    assert invalidations == [True]


def test_submit_expands_hidden_paste_but_keeps_placeholder_for_display(monkeypatch) -> None:
    interaction = TuiInteractionState()
    visible = interaction.register_text_paste("one\ntwo\nthree\nfour")
    input_area = TextArea(multiline=True)
    input_area.text = visible
    captured: dict[str, object] = {}
    monkeypatch.setattr(tui_keybindings, "_handle_command_params", lambda _params, _text: None)
    monkeypatch.setattr(tui_keybindings, "_tui_handle_command", lambda **_kwargs: False)
    monkeypatch.setattr(
        tui_keybindings,
        "_tui_submit_active_turn_input",
        lambda _params, _text, *, display_text: False,
    )
    monkeypatch.setattr(
        tui_keybindings,
        "_tui_enqueue_job",
        lambda _params, text, *, display_text=None: captured.update(
            text=text,
            display_text=display_text,
        ),
    )
    params = SimpleNamespace(
        input_area=input_area,
        interaction_state=interaction,
        exit_armed_at_ref=[0.0],
        eof_armed_at_ref=[0.0],
        escape_armed_at_ref=[0.0],
        escape_armed_text_ref=[""],
    )
    event = SimpleNamespace(app=SimpleNamespace(exit=lambda: None))

    tui_keybindings._submit_input_area(event, params)

    assert captured == {
        "text": "one\ntwo\nthree\nfour",
        "display_text": "[Pasted text #1 +3 lines]",
    }
    assert input_area.text == ""
    assert interaction.capture_draft("", 0).pasted_text_refs == ()


def test_gateway_btw_enters_durable_control_outbox_with_exact_turn() -> None:
    runtime = TuiRuntime("control-submit")
    captured: list[object] = []

    class Reconciler:
        def enqueue(self, entry) -> None:
            captured.append(entry)

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[True],
        running_request_id_ref=["gwreq-exact"],
        tui_runtime=runtime,
        control_operation_reconciler=Reconciler(),
    )

    assert tui_keybindings._tui_submit_control_operation(
        params,
        "/btw 先检查现有结果",
    )
    assert len(captured) == 1
    entry = captured[0]
    assert entry.command_kind == "steer"
    assert entry.command_text == "/btw 先检查现有结果"
    assert entry.expected_turn_id == "gwreq-exact"
    assert entry.message_id.startswith("control-")


def test_gateway_stop_is_not_sent_before_exact_turn_is_bound() -> None:
    runtime = TuiRuntime("control-submitting")

    class Reconciler:
        def enqueue(self, _entry) -> None:
            raise AssertionError("an unbound stop must not be persisted or sent")

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[True],
        running_request_id_ref=[""],
        tui_runtime=runtime,
        control_operation_reconciler=Reconciler(),
    )

    assert tui_keybindings._tui_submit_control_operation(params, "/stop")
    assert "No exact active turn" in runtime.notice()


def test_gateway_stop_without_foreground_turn_targets_background_task() -> None:
    runtime = TuiRuntime("control-background-task")
    captured: list[object] = []

    class Reconciler:
        def enqueue(self, entry) -> None:
            captured.append(entry)

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[False],
        running_request_id_ref=[""],
        tui_runtime=runtime,
        control_operation_reconciler=Reconciler(),
    )

    assert tui_keybindings._tui_submit_control_operation(params, "/stop")
    assert len(captured) == 1
    assert captured[0].command_kind == "stop"
    assert captured[0].expected_turn_id == ""


def test_running_gateway_submit_uses_active_turn_receipt_instead_of_job_queue() -> None:
    runtime = TuiRuntime("active-turn-submit")
    captured: dict[str, object] = {}

    class Agent:
        def request_active_turn_input(self, *_args, **_kwargs):
            raise AssertionError("transport belongs to the reconciler")

    class Reconciler:
        def enqueue(self, entry) -> None:
            captured["entry"] = entry

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[True],
        running_request_id_ref=["gwreq-active-submit"],
        runtime_inject=["遵守项目规则"],
        prompt_files=["spec.md"],
        args=SimpleNamespace(no_save=True, resume_context=True),
        agent=Agent(),
        current_session_id="session-active",
        tui_runtime=runtime,
        stop_event=threading.Event(),
        active_input_reconciler=Reconciler(),
    )

    assert tui_keybindings._tui_submit_active_turn_input(
        params,
        "继续原任务并改成 JSON",
        display_text="继续原任务并改成 JSON",
    ) is True
    snapshot = runtime.store.snapshot()
    assert [item.text for item in snapshot.pending_steers] == ["继续原任务并改成 JSON"]
    entry = captured["entry"]
    assert entry.text == "继续原任务并改成 JSON"
    assert entry.message_id.startswith("steer-")
    assert entry.expected_turn_id == "gwreq-active-submit"
    assert entry.execution_options.inject == (
        "遵守项目规则",
        CHAT_RESPONSE_STYLE_INJECT,
    )
    assert entry.execution_options.prompt_files == ("spec.md",)
    assert entry.execution_options.save is False
    assert entry.execution_options.resume_context is True
    assert entry.execution_options.tool_approval is True
    assert entry.execution_options.rich_transcript is True


def test_active_input_outbox_v1_migration_records_tui_execution_defaults() -> None:
    from agent_py_agent.cli.chat_parts.tui_input_delivery import (
        TuiActiveInputOutboxEntry,
    )

    entry = TuiActiveInputOutboxEntry.from_dict(
        {
            "message_id": "steer-legacy",
            "expected_turn_id": "gwreq-active",
            "text": "旧等待消息",
            "display_text": "旧等待消息",
        }
    )

    assert entry.options_migrated_from_v1 is True
    assert entry.execution_options.save is True
    assert entry.execution_options.tool_approval is True
    assert entry.execution_options.rich_transcript is True
    assert entry.to_dict()["options_migration"] == "v1_tui_defaults"


def test_unknown_active_turn_delivery_keeps_receipt_and_starts_same_id_reconciliation(
) -> None:
    runtime = TuiRuntime("active-turn-unknown")
    persisted: dict[str, object] = {}

    class Agent:
        def request_active_turn_input(self, *_args, **_kwargs):
            raise AssertionError("transport belongs to the reconciler")

    class Reconciler:
        def enqueue(self, entry) -> None:
            persisted["entry"] = entry
    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[True],
        running_request_id_ref=["gwreq-active-unknown"],
        runtime_inject=["原始注入"],
        prompt_files=["original.md"],
        args=SimpleNamespace(no_save=True, resume_context=True),
        agent=Agent(),
        current_session_id="session-active",
        tui_runtime=runtime,
        stop_event=threading.Event(),
        active_input_reconciler=Reconciler(),
    )

    assert tui_keybindings._tui_submit_active_turn_input(
        params,
        "网络慢时不能重复排队",
        display_text="网络慢时不能重复排队",
    ) is True
    pending = runtime.store.snapshot().pending_steers
    assert len(pending) == 1
    entry = persisted["entry"]
    assert entry.message_id == pending[0].message_id
    assert entry.expected_turn_id == "gwreq-active-unknown"


def test_reconciliation_queues_once_only_after_explicit_rejection(monkeypatch, tmp_path) -> None:
    from agent_py_agent.cli.chat_client_context import (
        ActiveTurnInputDelivery,
        ActiveTurnInputResult,
    )

    runtime = TuiRuntime("active-turn-rejected")
    jobs: Queue[ChatJob] = Queue()

    class Agent:
        def request_active_turn_input(self, *_args, **_kwargs):
            return ActiveTurnInputResult(ActiveTurnInputDelivery.REJECTED)

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[True],
        running_request_id_ref=["gwreq-ended"],
        pending_jobs_ref_for_enqueue=[0],
        runtime_inject=["原始注入"],
        prompt_files=["original.md"],
        args=SimpleNamespace(no_save=True, resume_context=True),
        jobs=jobs,
        tui_runtime=runtime,
        current_session_id="session-active",
        stop_event=threading.Event(),
        agent=Agent(),
        paths=SimpleNamespace(root=tmp_path),
        active_input_reconciler=None,
    )
    monkeypatch.setattr(tui_keybindings, "ACTIVE_TURN_RETRY_INITIAL_SECONDS", 0.05)

    assert tui_keybindings._tui_submit_active_turn_input(
        params,
        "原回合结束后排队",
        display_text="原回合结束后排队",
    )
    deadline = time.time() + 2.0
    while jobs.empty() and time.time() < deadline:
        time.sleep(0.01)

    assert runtime.store.snapshot().pending_steers == ()
    assert jobs.qsize() == 1
    queued_job = jobs.get_nowait()
    assert queued_job.user == "原回合结束后排队"
    assert queued_job.inject == ["原始注入", CHAT_RESPONSE_STYLE_INJECT]
    assert queued_job.inject_complete is True
    assert queued_job.prompt_files == ["original.md"]
    assert queued_job.save is False
    assert queued_job.resume_context is True
    assert queued_job.tool_approval is True
    assert queued_job.rich_transcript is True
    assert params.pending_jobs_ref_for_enqueue == [1]
    params.stop_event.set()


def test_gateway_queued_active_input_attaches_exact_next_turn_without_resubmit(
    monkeypatch,
    tmp_path,
) -> None:
    from agent_py_agent.cli.chat_client_context import (
        ActiveTurnInputDelivery,
        ActiveTurnInputResult,
    )

    runtime = TuiRuntime("active-turn-server-queued")
    jobs: Queue[ChatJob] = Queue()

    class Agent:
        def request_active_turn_input(self, *_args, **_kwargs):
            return ActiveTurnInputResult(
                ActiveTurnInputDelivery.QUEUED,
                request_id="gwreq-next-turn",
                disposition="queued",
            )

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[True],
        running_request_id_ref=["gwreq-ending"],
        pending_jobs_ref_for_enqueue=[0],
        runtime_inject=[],
        prompt_files=[],
        args=SimpleNamespace(no_save=False, resume_context=None),
        jobs=jobs,
        tui_runtime=runtime,
        current_session_id="session-next-turn",
        stop_event=threading.Event(),
        agent=Agent(),
        paths=SimpleNamespace(root=tmp_path),
        active_input_reconciler=None,
    )
    monkeypatch.setattr(tui_keybindings, "ACTIVE_TURN_RETRY_INITIAL_SECONDS", 0.05)

    assert tui_keybindings._tui_submit_active_turn_input(
        params,
        "当前回合来不及就直接进入下一回合",
        display_text="当前回合来不及就直接进入下一回合",
    )
    deadline = time.time() + 2.0
    while jobs.empty() and time.time() < deadline:
        time.sleep(0.01)

    assert jobs.qsize() == 1
    queued_job = jobs.get_nowait()
    assert queued_job.request_id == "gwreq-next-turn"
    assert queued_job.gateway_request_id == "gwreq-next-turn"
    assert queued_job.client_message_id.startswith("steer-")
    assert queued_job.inject_complete is True
    assert runtime.store.snapshot().pending_steers == ()
    assert params.pending_jobs_ref_for_enqueue == [1]
    params.stop_event.set()


def test_ctrl_c_with_transcript_selection_copies_before_interrupt(monkeypatch) -> None:
    copied: list[str] = []
    runtime = TuiRuntime("selection-copy")
    app = SimpleNamespace(invalidate=lambda: None)
    params = SimpleNamespace(
        input_area=TextArea(multiline=True),
        transcript_area=SimpleNamespace(selected_text=lambda: "selected output"),
        tui_runtime=runtime,
    )
    monkeypatch.setattr(
        tui_keybindings,
        "_write_selection_clipboard",
        lambda _application, text: copied.append(text),
    )

    tui_keybindings._handle_ctrl_c_keybinding(SimpleNamespace(app=app), params)

    assert copied == ["selected output"]
    assert runtime.notice() == "Copied 15 chars"


def test_ctrl_c_copies_input_selection_without_clearing_highlight(monkeypatch) -> None:
    copied: list[str] = []
    runtime = TuiRuntime("input-selection-copy")
    input_area = TextArea(multiline=True)
    input_area.text = "复制中文正文"
    input_area.buffer.cursor_position = 0
    input_area.buffer.start_selection()
    input_area.buffer.cursor_position = 4
    app = SimpleNamespace(invalidate=lambda: None)
    params = SimpleNamespace(
        input_area=input_area,
        transcript_area=SimpleNamespace(selected_text=lambda: "transcript"),
        tui_runtime=runtime,
    )
    monkeypatch.setattr(
        tui_keybindings,
        "_write_selection_clipboard",
        lambda _application, text: copied.append(text),
    )

    tui_keybindings._handle_ctrl_c_keybinding(SimpleNamespace(app=app), params)

    assert copied == ["复制中文"]
    assert input_area.buffer.selection_state is not None
    assert runtime.notice() == "Copied 4 chars"


def test_ctrl_t_toggles_todo_view_without_editing_input() -> None:
    redraws: list[bool] = []
    interaction = TuiInteractionState()
    input_area = TextArea(multiline=True)
    input_area.text = "保留当前输入"
    params = SimpleNamespace(
        input_area=input_area,
        interaction_state=interaction,
        exit_armed_at_ref=[1.0],
        eof_armed_at_ref=[1.0],
        escape_armed_at_ref=[1.0],
        escape_armed_text_ref=["保留当前输入"],
    )
    event = SimpleNamespace(app=SimpleNamespace(invalidate=lambda: redraws.append(True)))

    tui_keybindings._handle_ctrl_t_keybinding(event, params)

    assert interaction.snapshot().todos_expanded is True
    assert input_area.text == "保留当前输入"
    assert params.exit_armed_at_ref == [0.0]
    assert params.eof_armed_at_ref == [0.0]
    assert params.escape_armed_at_ref == [0.0]
    assert params.escape_armed_text_ref == [""]
    assert redraws == [True]


def test_f6_toggles_native_copy_and_tui_mouse_without_touching_input() -> None:
    redraws: list[bool] = []
    interaction = TuiInteractionState()
    runtime = TuiRuntime("mouse-mode")
    input_area = TextArea(multiline=True)
    input_area.text = "保留输入"
    params = SimpleNamespace(
        input_area=input_area,
        interaction_state=interaction,
        tui_runtime=runtime,
        agent_navigation=None,
        exit_armed_at_ref=[1.0],
        eof_armed_at_ref=[1.0],
        escape_armed_at_ref=[1.0],
        escape_armed_text_ref=["保留输入"],
    )
    event = SimpleNamespace(app=SimpleNamespace(invalidate=lambda: redraws.append(True)))

    tui_keybindings._handle_f6_mouse_keybinding(event, params)

    assert interaction.snapshot().mouse_capture_enabled is True
    assert "TUI 鼠标已开启" in runtime.notice()
    assert input_area.text == "保留输入"
    assert params.exit_armed_at_ref == [0.0]
    assert redraws == [True]

    tui_keybindings._handle_f6_mouse_keybinding(event, params)

    assert interaction.snapshot().mouse_capture_enabled is False
    assert "原生复制已开启" in runtime.notice()
    assert redraws == [True, True]


def test_input_mouse_up_auto_copies_settled_selection() -> None:
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from agent_py_agent.cli.chat_parts.tui_ui_setup import _install_input_copy_on_select

    buffer = Buffer()
    buffer.text = "鼠标复制"
    buffer.cursor_position = 0
    buffer.start_selection()
    buffer.cursor_position = len(buffer.text)
    calls: list[object] = []
    control = SimpleNamespace(mouse_handler=lambda event: calls.append(event) or None)
    input_area = SimpleNamespace(control=control, buffer=buffer)
    copied: list[str] = []
    _install_input_copy_on_select(input_area, copied.append)
    event = MouseEvent(
        Point(x=3, y=0),
        MouseEventType.MOUSE_UP,
        MouseButton.LEFT,
        frozenset(),
    )

    result = control.mouse_handler(event)

    assert result is None
    assert calls == [event]
    assert copied == ["鼠标复制"]
    assert buffer.selection_state is not None


def test_input_right_click_copies_once_without_delegating_or_clearing_selection() -> None:
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from agent_py_agent.cli.chat_parts.tui_ui_setup import _install_input_copy_on_select

    buffer = Buffer()
    buffer.text = "输入框中文复制"
    buffer.cursor_position = 0
    buffer.start_selection()
    buffer.cursor_position = len(buffer.text)
    delegated: list[object] = []
    control = SimpleNamespace(mouse_handler=lambda event: delegated.append(event) or None)
    input_area = SimpleNamespace(control=control, buffer=buffer)
    copied: list[str] = []
    _install_input_copy_on_select(input_area, copied.append)

    down_result = control.mouse_handler(
        MouseEvent(Point(x=6, y=0), MouseEventType.MOUSE_DOWN, MouseButton.RIGHT, frozenset())
    )
    up_result = control.mouse_handler(
        MouseEvent(Point(x=6, y=0), MouseEventType.MOUSE_UP, MouseButton.RIGHT, frozenset())
    )

    assert down_result is None
    assert up_result is None
    assert delegated == []
    assert copied == ["输入框中文复制"]
    assert buffer.selection_state is not None


def test_input_mouse_selection_includes_release_character() -> None:
    from agent_py_agent.cli.chat_parts.tui_ui_setup import (
        _include_input_focus_character,
    )

    buffer = Buffer()
    buffer.text = "输入复制甲乙丙丁"
    buffer.cursor_position = 4
    buffer.start_selection()
    buffer.cursor_position = 7

    _include_input_focus_character(buffer, 4)

    assert tui_keybindings._selected_input_text(buffer) == "甲乙丙丁"


def test_osc52_clipboard_sequence_wraps_for_tmux() -> None:
    direct = tui_keybindings._osc52_sequence("YWJj", inside_tmux=False)
    wrapped = tui_keybindings._osc52_sequence("YWJj", inside_tmux=True)

    assert direct == "\x1b]52;c;YWJj\x07"
    assert wrapped == "\x1bPtmux;\x1b\x1b]52;c;YWJj\x07\x1b\\"


def test_tmux_clipboard_buffer_uses_write_through(monkeypatch) -> None:
    calls: list[tuple[list[str], str]] = []

    class Result:
        returncode = 0

    def fake_run(args, *, input, **_kwargs):
        calls.append((list(args), input))
        return Result()

    monkeypatch.delenv("LC_TERMINAL", raising=False)
    monkeypatch.setattr(tui_keybindings.subprocess, "run", fake_run)

    assert tui_keybindings._load_tmux_clipboard_buffer("甲乙")
    assert calls == [(["tmux", "load-buffer", "-w", "-"], "甲乙")]


def test_tmux_clipboard_buffer_keeps_iterm2_write_through(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return Result()

    monkeypatch.setenv("LC_TERMINAL", "iTerm2")
    monkeypatch.setattr(tui_keybindings.subprocess, "run", fake_run)

    assert tui_keybindings._load_tmux_clipboard_buffer("copy")
    assert calls == [["tmux", "load-buffer", "-w", "-"]]


def test_permission_y_n_shortcuts_use_typed_decisions(monkeypatch) -> None:
    runtime = TuiRuntime("permission-shortcuts")
    sequencer = TuiEventSequencer("permission-shortcuts", clock=lambda: 1.0)
    runtime.store.publish(
        sequencer.emit(
            "tool_started",
            "started",
            "tool:shortcut",
            {"tool": "run_command"},
        )
    )
    runtime.store.publish(
        sequencer.emit(
            "permission_requested",
            "waiting_permission",
            "tool:shortcut",
            {
                "permission_id": "approval-shortcut",
                "options": [
                    {"id": "allow", "label": "同意", "decision": "approved"},
                    {"id": "deny", "label": "拒绝", "decision": "denied"},
                ],
            },
        )
    )
    resolved: list[str] = []

    def move_selection(delta: int) -> bool:
        permission = runtime.store.snapshot().permission
        assert permission is not None
        selected_index = (permission.selected_index + delta) % len(permission.options)
        runtime.store.publish(
            sequencer.emit(
                "permission_selection_changed",
                "updated",
                "tool:shortcut",
                {
                    "permission_id": permission.permission_id,
                    "selected_index": selected_index,
                },
            )
        )
        return True

    monkeypatch.setattr(runtime, "move_permission_selection", move_selection)
    monkeypatch.setattr(
        runtime,
        "resolve_permission",
        lambda _permission_id, decision, *, feedback="": resolved.append(decision) or True,
    )
    app = SimpleNamespace(
        invalidate=lambda: None,
        layout=SimpleNamespace(focus=lambda _control: None),
    )
    params = SimpleNamespace(
        tui_runtime=runtime,
        permission_feedback_area=TextArea(multiline=True),
        input_area=TextArea(multiline=True),
        transcript_state=TuiTranscriptModeState(),
    )

    assert tui_keybindings._permission_active(params) is True
    assert tui_keybindings._transcript_scroll_active(params) is True

    tui_keybindings._resolve_permission_decision(
        SimpleNamespace(app=app),
        params,
        ("denied", "cancelled"),
    )
    tui_keybindings._resolve_permission_decision(
        SimpleNamespace(app=app),
        params,
        ("approved",),
    )

    assert resolved == ["denied", "approved"]

    params.transcript_state.enter(runtime.store.snapshot())
    params.transcript_state.open_search(0)
    assert tui_keybindings._transcript_scroll_active(params) is False


def test_native_clipboard_runs_pbcopy_when_local_macos(monkeypatch) -> None:
    calls: list[tuple[list[str], str]] = []

    class Result:
        returncode = 0

    def fake_run(args, *, input, **_kwargs):
        calls.append((list(args), input))
        return Result()

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr(tui_keybindings.sys, "platform", "darwin")
    monkeypatch.setattr(tui_keybindings.subprocess, "run", fake_run)

    assert tui_keybindings._run_clipboard_tool(["pbcopy"], "正文") is True
    tui_keybindings._copy_native_clipboard("正文")
    assert calls[1] == (["pbcopy"], "正文")


def test_native_clipboard_skipped_over_ssh(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(args, *, input, **_kwargs):
        calls.append(args[0])
        return type("R", (), {"returncode": 0})()

    monkeypatch.setenv("SSH_CONNECTION", "192.168.1.13 54321 10.0.0.1 22")
    monkeypatch.setattr(tui_keybindings.sys, "platform", "darwin")
    monkeypatch.setattr(tui_keybindings.subprocess, "run", fake_run)

    tui_keybindings._copy_native_clipboard("远端不写本机剪贴板")
    assert calls == []


def test_linux_clipboard_tool_probe_wayland_then_x11_and_caches(monkeypatch) -> None:
    monkeypatch.setattr(tui_keybindings, "_linux_clipboard_tool_cache", None)
    monkeypatch.setattr(
        tui_keybindings.shutil,
        "which",
        lambda name: "/usr/bin/" + name if name in {"wl-copy", "xsel"} else None,
    )

    assert tui_keybindings._linux_clipboard_tool() == ["wl-copy"]
    # 第二次命中缓存，不再探测
    monkeypatch.setattr(
        tui_keybindings.shutil,
        "which",
        lambda name: None,
    )
    assert tui_keybindings._linux_clipboard_tool() == ["wl-copy"]


def test_linux_clipboard_tool_falls_back_to_xclip(monkeypatch) -> None:
    monkeypatch.setattr(tui_keybindings, "_linux_clipboard_tool_cache", None)
    monkeypatch.setattr(
        tui_keybindings.shutil,
        "which",
        lambda name: "/usr/bin/xclip" if name == "xclip" else None,
    )

    assert tui_keybindings._linux_clipboard_tool() == ["xclip", "-selection", "clipboard"]


def test_write_selection_clipboard_starts_native_thread_when_local(monkeypatch) -> None:
    from prompt_toolkit.clipboard import InMemoryClipboard

    started: list[tuple[str, tuple]] = []
    output = SimpleNamespace(
        write_raw=lambda payload: None,
        flush=lambda: None,
    )
    app = SimpleNamespace(clipboard=InMemoryClipboard(), output=output)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("TMUX", raising=False)

    def fake_start(thread_self, **kwargs):
        started.append((thread_self._target.__name__, thread_self._args))
        return None

    monkeypatch.setattr(tui_keybindings.threading.Thread, "start", fake_start)

    tui_keybindings._write_selection_clipboard(app, "本地复制")

    assert [name for name, _ in started] == ["_copy_native_clipboard"]
    assert app.clipboard.get_data().text == "本地复制"
