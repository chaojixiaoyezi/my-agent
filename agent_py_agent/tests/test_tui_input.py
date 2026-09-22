from __future__ import annotations

import asyncio
import threading
import time
from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.widgets import TextArea

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.cli.chat_parts import tui_keybindings
from agent_py_agent.cli.chat_parts.chat_style import CHAT_RESPONSE_STYLE_INJECT
from agent_py_agent.cli.chat_parts.plain_state import ChatJob
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
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


def test_plugin_namespace_completion_only_edits_without_path_scan(tmp_path, monkeypatch) -> None:
    from agent_py_agent.cli.chat_parts import tui_input

    scan = MagicMock(side_effect=AssertionError("插件命名空间不能扫描文件"))
    monkeypatch.setattr(tui_input, "_path_candidates", scan)
    completer = TuiInputCompleter(tmp_path)
    candidates = list(completer.get_completions(Document("/plug"), CompleteEvent()))
    assert {item.text for item in candidates} == {"/plugins", "/plugins@"}
    assert all(item.enter_action == "apply" for item in candidates)
    namespace = next(item for item in candidates if item.text == "/plugins@")
    assert namespace.append_space is False
    buffer = Buffer()
    buffer.set_document(Document("/plug"), bypass_readonly=True)
    buffer.complete_state = CompletionState(buffer.document, [namespace], complete_index=0)
    assert apply_selected_completion(buffer) is namespace
    assert buffer.text == "/plugins@"
    assert list(completer.get_completions(Document("/plugins@Demo"), CompleteEvent())) == []
    scan.assert_not_called()


@pytest.mark.parametrize("use_gateway", [False, True])
@pytest.mark.parametrize("mode", ["idle", "foreground", "background", "child"])
@pytest.mark.parametrize(("raw", "message"), [
    ('/plugins@Demo run --path "中文 a" -- -x | literal', "当前目录没有"),
    ("/plugins help install", "用法：/plugins install <source>"),
    ("/plugins help configure", "用法：/plugins configure"),
    ("/plugins list --bad", "未声明的选项"),
    ('/plugins install "未闭合', "引号尚未闭合"),
])
def test_plugin_submit_uses_real_dispatch_without_chat_guidance_or_stop(
    tmp_path, monkeypatch, use_gateway, mode, raw, message,
) -> None:
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.plugin_command_service import (
        execute_plugin_command,
        read_plugin_catalog,
    )
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    from agent_py_agent.cli.chat_parts import control_runtime, plugin_command_client, tui

    snapshot = read_plugin_catalog(OwnerIdentity.local_main(), channel="chat", conversation_id="session-1")

    def transport(port, owner, path, payload, *, timeout):
        assert path == "/client/plugins" and owner == OwnerIdentity.local_main()
        result = ({"ok": True, "catalog": snapshot.to_payload()} if payload["operation"] == "catalog"
                  else execute_plugin_command(snapshot, payload["command"], revision=payload["catalog_revision"]))
        return 200, result

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", transport)

    input_area = TextArea(multiline=True)
    input_area.text = raw
    output: list[str] = []
    blocked = MagicMock(side_effect=AssertionError("命令错误不能进入执行或普通输入"))
    monkeypatch.setattr(tui, "_cprint", output.append)
    monkeypatch.setattr(control_runtime, "execute_chat_control", blocked)
    for name in ("_tui_submit_active_turn_input", "_tui_enqueue_job",
                 "_tui_submit_agent_input", "_dispatch_agent_interrupt"):
        monkeypatch.setattr(tui_keybindings, name, blocked)
    monkeypatch.setattr(tui_keybindings, "_agent_navigation_snapshot", lambda _params:
                        SimpleNamespace(active_run_id="child-1" if mode == "child" else "", terminal=False))
    runtime = TuiRuntime("plugin-input")
    if mode == "background":
        runtime.update_background_activity(
            1, {"main_activity": {"task_id": "goal-task-1", "phase": "running"}},
        )
    background_before = runtime.has_active_background_task()
    paths = home_paths(tmp_path)
    owner = resolve_owner_home(tmp_path)
    agent = SimpleNamespace(config=AgentConfig(), home_paths=paths,
                            conversation_store=ConversationStore(owner.home_dir / "conversations", initialize=False),
                            effective_workspace_root=tmp_path)
    params = SimpleNamespace(
        input_area=input_area, interaction_state=TuiInteractionState(), tui_runtime=runtime,
        agent=agent, args=SimpleNamespace(memory_limit=5), runtime_inject=[], prompt_files=[],
        use_gateway=use_gateway, paths=SimpleNamespace(root=tmp_path), state_lock=threading.Lock(),
        is_running_ref=[mode == "foreground"], pending_jobs_ref=[0], running_prompt_ref=["正在核对"],
        running_request_id_ref=["goal-task-1" if mode == "background" else "req-1"],
        running_started_at_ref=[0.0], shutting_down_ref=[False], stop_event=threading.Event(),
        assistant_outputs=[], current_session_id="session-1",
        control_operation_reconciler=SimpleNamespace(enqueue=blocked),
        exit_armed_at_ref=[0.0], eof_armed_at_ref=[0.0],
        escape_armed_at_ref=[0.0], escape_armed_text_ref=[""],
        local_run_ref=[None],
    )
    event = SimpleNamespace(app=SimpleNamespace(exit=blocked, invalidate=lambda: None, create_background_task=asyncio.run))

    tui_keybindings._submit_input_area(event, params)

    assert len(output) == 1 and message in output[0]
    blocked.assert_not_called()
    assert not params.stop_event.is_set() and params.pending_jobs_ref == [0]
    assert params.local_run_ref == [None]
    assert params.is_running_ref == [mode == "foreground"]
    assert runtime.has_active_background_task() == background_before == (mode == "background")
    assert runtime.store.snapshot().queued_inputs == ()
    assert input_area.text == ""


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


def test_empty_enter_opens_selected_goal_editor_without_submitting_prompt(monkeypatch) -> None:
    import asyncio

    from agent_py_agent.cli.chat_parts import tui_goal_editor

    root = TuiRuntime("goal-enter-input")
    navigation = TuiAgentNavigationState(root)
    navigation.update_goal_rows(
        [
            {
                "goal_id": "goal-one",
                "name": "持续验证",
                "objective": "验证 TUI",
                "status": "active",
            }
        ]
    )
    assert navigation.move_selection(1) is True
    submitted: list[bool] = []
    monkeypatch.setattr(
        tui_keybindings,
        "_submit_input_area",
        lambda _event, _params: submitted.append(True),
    )
    invalidated: list[bool] = []
    opened = []

    async def open_editor(_app, editor_params):
        opened.append(editor_params.agent_navigation.selected_goal()["goal_id"])

    monkeypatch.setattr(tui_goal_editor, "open_goal_editor", open_editor)
    params = SimpleNamespace(
        input_area=TextArea(multiline=True),
        agent_navigation=navigation,
        exit_armed_at_ref=[0.0],
        eof_armed_at_ref=[0.0],
        escape_armed_at_ref=[0.0],
        escape_armed_text_ref=[""],
    )
    event = SimpleNamespace(
        app=SimpleNamespace(invalidate=lambda: invalidated.append(True), create_background_task=asyncio.run),
    )

    tui_keybindings._handle_enter_keybinding(event, params)

    assert submitted == []
    assert invalidated == []
    assert opened == ["goal-one"]
    snapshot = navigation.snapshot()
    assert snapshot.active_run_id == ""
    assert snapshot.expanded_goal_id == ""


def test_terminal_child_reject_clears_unsent_draft_before_parent_navigation() -> None:
    root_runtime = TuiRuntime("terminal-child-input")
    child_runtime = TuiRuntime("terminal-child-input:child-a")
    input_area = TextArea(multiline=True)
    input_area.text = "请继续补充终态子代理结果"
    invalidations: list[bool] = []
    navigation = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            active_run_id="child-a",
            terminal=True,
        ),
        active_runtime=lambda: child_runtime,
    )
    params = SimpleNamespace(
        input_area=input_area,
        interaction_state=TuiInteractionState(),
        agent_navigation=navigation,
        tui_runtime=root_runtime,
        exit_armed_at_ref=[0.0],
        eof_armed_at_ref=[0.0],
        escape_armed_at_ref=[0.0],
        escape_armed_text_ref=[""],
    )
    event = SimpleNamespace(
        app=SimpleNamespace(invalidate=lambda: invalidations.append(True)),
    )

    tui_keybindings._submit_input_area(event, params)

    assert input_area.text == ""
    assert child_runtime.notice() == (
        "这个子代理已经结束，当前页面只读；Ctrl+G 返回父代理"
    )
    assert root_runtime.store.snapshot().queued_inputs == ()
    assert invalidations == [True]


def test_escape_targets_active_manual_compact_control_message() -> None:
    runtime = TuiRuntime("manual-compact-escape")
    runtime.publish_manual_compact_started("compact-control-1")
    captured: list[object] = []

    class Reconciler:
        def enqueue(self, entry, *, on_persisted_before_dispatch=None) -> None:
            captured.append(entry)
            if on_persisted_before_dispatch is not None:
                on_persisted_before_dispatch(entry)

    app = SimpleNamespace(invalidate=lambda: None)
    params = SimpleNamespace(
        input_area=TextArea(multiline=True),
        agent_navigation=None,
        tui_runtime=runtime,
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_request_id_ref=[""],
        control_operation_reconciler=Reconciler(),
        escape_armed_at_ref=[0.0],
        escape_armed_text_ref=[""],
    )

    tui_keybindings._handle_escape_keybinding(SimpleNamespace(app=app), params)

    assert len(captured) == 1
    assert captured[0].command_kind == "stop"
    assert captured[0].expected_turn_id == ""
    assert captured[0].target_control_message_id == "compact-control-1"


def test_escape_stops_canonical_background_task_while_local_worker_is_idle() -> None:
    runtime = TuiRuntime("background-escape")
    runtime.update_background_activity(1, {"main_activity": {"phase": "waiting"}})
    captured: list[object] = []

    class Reconciler:
        def enqueue(self, entry, *, on_persisted_before_dispatch=None) -> None:
            del on_persisted_before_dispatch
            captured.append(entry)

    app = SimpleNamespace(invalidate_calls=0)
    app.invalidate = lambda: setattr(app, "invalidate_calls", app.invalidate_calls + 1)
    params = SimpleNamespace(
        input_area=TextArea(multiline=True),
        agent_navigation=None,
        tui_runtime=runtime,
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_request_id_ref=[""],
        control_operation_reconciler=Reconciler(),
        escape_armed_at_ref=[0.0],
        escape_armed_text_ref=[""],
    )

    tui_keybindings._handle_escape_keybinding(SimpleNamespace(app=app), params)

    assert len(captured) == 1
    assert captured[0].command_kind == "stop"
    assert captured[0].command_text == "/interrupt"
    assert captured[0].expected_turn_id == ""
    assert app.invalidate_calls == 1


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


@pytest.mark.parametrize("text", ["", " ", "   ", "\t", "\n", " \t\n ", "\u3000", "\u00a0", "@docs ", "@docs\n"])
def test_input_completer_blank_or_completed_token_never_scans_paths(tmp_path, monkeypatch, text) -> None:
    import asyncio

    from agent_py_agent.cli.chat_parts import tui_input

    def unexpected_scan(*args):
        pytest.fail("空白或已结束的引用不应触发目录补全")

    monkeypatch.setattr(tui_input, "_path_candidates", unexpected_scan)
    completer = TuiInputCompleter(tmp_path)
    document = Document(text)
    event = CompleteEvent(text_inserted=True)
    assert list(completer.get_completions(document, event)) == []

    async def collect():
        return [item async for item in completer.get_completions_async(document, event)]

    assert asyncio.run(collect()) == []


def test_input_completer_respects_cursor_and_keeps_explicit_empty_path_query(tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    completer = TuiInputCompleter(tmp_path)
    assert list(completer.get_completions(Document("   @docs", cursor_position=2), None)) == []
    for text in ("@", "看一下 @", "\n@d"):
        assert [item.text for item in completer.get_completions(Document(text), None)] == ["@docs/"]
    assert [item.text for item in completer.get_completions(Document("/prompt-file "), None)] == ["docs/"]


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


def test_empty_down_returns_detached_transcript_live_before_agent_selection() -> None:
    input_area = TextArea(multiline=True)
    end_calls: list[bool] = []
    selection_moves: list[int] = []
    redraws: list[bool] = []
    params = SimpleNamespace(
        input_area=input_area,
        transcript_area=SimpleNamespace(
            is_following=lambda: False,
            end=lambda: end_calls.append(True),
        ),
        transcript_follow_ref=[False],
        agent_navigation=SimpleNamespace(
            move_selection=lambda delta: selection_moves.append(delta) or True,
        ),
        exit_armed_at_ref=[1.0],
        eof_armed_at_ref=[1.0],
        escape_armed_at_ref=[1.0],
        escape_armed_text_ref=[""],
    )
    event = SimpleNamespace(
        arg=1,
        app=SimpleNamespace(invalidate=lambda: redraws.append(True)),
    )

    tui_keybindings._handle_down_keybinding(event, params)

    assert end_calls == [True]
    assert selection_moves == []
    assert params.transcript_follow_ref == [True]
    assert params.exit_armed_at_ref == [0.0]
    assert redraws == [True]


def test_empty_down_at_live_tail_still_selects_child_agent() -> None:
    input_area = TextArea(multiline=True)
    end_calls: list[bool] = []
    selection_moves: list[int] = []
    redraws: list[bool] = []
    params = SimpleNamespace(
        input_area=input_area,
        transcript_area=SimpleNamespace(
            is_following=lambda: True,
            end=lambda: end_calls.append(True),
        ),
        transcript_follow_ref=[True],
        agent_navigation=SimpleNamespace(
            move_selection=lambda delta: selection_moves.append(delta) or True,
        ),
        exit_armed_at_ref=[1.0],
        eof_armed_at_ref=[1.0],
        escape_armed_at_ref=[1.0],
        escape_armed_text_ref=[""],
    )
    event = SimpleNamespace(
        arg=1,
        app=SimpleNamespace(invalidate=lambda: redraws.append(True)),
    )

    tui_keybindings._handle_down_keybinding(event, params)

    assert end_calls == []
    assert selection_moves == [1]
    assert params.transcript_follow_ref == [True]
    assert params.exit_armed_at_ref == [0.0]
    assert redraws == [True]


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


def test_successful_submit_repins_transcript_before_enqueue(monkeypatch) -> None:
    input_area = TextArea(multiline=True)
    input_area.text = "继续检查真实结果"
    captured: dict[str, object] = {}
    transcript_area = SimpleNamespace(end_calls=0)
    transcript_area.end = lambda: setattr(
        transcript_area,
        "end_calls",
        transcript_area.end_calls + 1,
    )
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
        transcript_area=transcript_area,
        transcript_follow_ref=[False],
        interaction_state=TuiInteractionState(),
        exit_armed_at_ref=[0.0],
        eof_armed_at_ref=[0.0],
        escape_armed_at_ref=[0.0],
        escape_armed_text_ref=[""],
    )

    tui_keybindings._submit_input_area(
        SimpleNamespace(app=SimpleNamespace(exit=lambda: None)),
        params,
    )

    assert captured == {
        "text": "继续检查真实结果",
        "display_text": "继续检查真实结果",
    }
    assert transcript_area.end_calls == 1
    assert params.transcript_follow_ref == [True]


def test_gateway_btw_enters_durable_control_outbox_with_exact_turn() -> None:
    runtime = TuiRuntime("control-submit")
    captured: list[object] = []

    class Reconciler:
        def enqueue(self, entry, *, on_persisted_before_dispatch=None) -> None:
            captured.append(entry)
            if on_persisted_before_dispatch is not None:
                on_persisted_before_dispatch(entry)

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
    pending = runtime.store.snapshot().pending_steers
    assert [(item.message_id, item.text) for item in pending] == [
        (entry.message_id, "先检查现有结果")
    ]


def test_gateway_goal_command_is_visible_only_after_durable_enqueue() -> None:
    runtime = TuiRuntime("control-goal-visible")
    captured: list[object] = []

    class Reconciler:
        def enqueue(self, entry, *, on_persisted_before_dispatch=None) -> None:
            captured.append(entry)
            assert on_persisted_before_dispatch is not None
            on_persisted_before_dispatch(entry)

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[False],
        running_request_id_ref=[""],
        tui_runtime=runtime,
        control_operation_reconciler=Reconciler(),
    )

    command = "/goal 1d 底座验证 验证所有 TUI 交互"
    assert tui_keybindings._tui_submit_control_operation(params, command)
    assert captured[0].command_kind == "goal"
    snapshot = runtime.store.snapshot()
    assert [
        block.text for block in snapshot.stable_blocks if block.role == "user"
    ] == [command]
    assert snapshot.queued_inputs == ()
    assert snapshot.pending_steers == ()

    # 同一持久消息的回调重放只保留一条显示块，不能重复执行或刷屏。
    assert runtime.publish_control_command_input(captured[0].message_id, command) is False


def test_expand_parser_accepts_documented_explicit_last() -> None:
    from agent_py_agent.cli.chat_parts.slash_commands import parse_expand_target

    assert parse_expand_target("/expand") == "last"
    assert parse_expand_target("/expand last") == "last"
    assert parse_expand_target("/expand 2") == "2"
    assert parse_expand_target("/expand unknown") is None


def test_gateway_manual_compact_starts_visible_progress_after_durable_enqueue() -> None:
    runtime = TuiRuntime("control-compact-submit")
    captured: list[object] = []

    class Reconciler:
        def enqueue(self, entry, *, on_persisted_before_dispatch=None) -> None:
            captured.append(entry)
            assert on_persisted_before_dispatch is not None
            on_persisted_before_dispatch(entry)

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[False],
        running_request_id_ref=[""],
        tui_runtime=runtime,
        control_operation_reconciler=Reconciler(),
    )

    assert tui_keybindings._tui_submit_control_operation(params, "/compact")
    assert captured[0].command_kind == "compact"
    active = runtime.store.snapshot().active_blocks
    assert len(active) == 1
    assert active[0].role == "compact"
    assert active[0].metadata["percent"] == 0
    assert active[0].metadata["indeterminate"] is True


def test_gateway_manual_compact_rejects_active_task_before_animation() -> None:
    for foreground_active, background_active in ((True, False), (False, True)):
        runtime = TuiRuntime(
            f"control-compact-active-{foreground_active}-{background_active}"
        )
        if background_active:
            runtime.update_background_activity(
                1,
                {"main_activity": {"phase": "waiting"}},
            )

        class Reconciler:
            def enqueue(self, _entry, *, on_persisted_before_dispatch=None) -> None:
                del on_persisted_before_dispatch
                raise AssertionError("active task must not persist manual compact")

        params = SimpleNamespace(
            use_gateway=True,
            state_lock=threading.Lock(),
            is_running_ref=[foreground_active],
            running_request_id_ref=["gwreq-active" if foreground_active else ""],
            tui_runtime=runtime,
            control_operation_reconciler=Reconciler(),
        )

        assert tui_keybindings._tui_submit_control_operation(params, "/compact")
        snapshot = runtime.store.snapshot()
        assert all(block.role != "compact" for block in snapshot.active_blocks)
        assert all(block.role != "compact" for block in snapshot.stable_blocks)
        assert any(
            block.role == "system" and "当前任务仍在运行" in block.text
            for block in snapshot.stable_blocks
        )


def test_gateway_manual_compact_fast_terminal_cannot_overtake_start() -> None:
    runtime = TuiRuntime("control-compact-fast-terminal")

    class Reconciler:
        def enqueue(self, entry, *, on_persisted_before_dispatch=None) -> None:
            assert on_persisted_before_dispatch is not None
            on_persisted_before_dispatch(entry)
            runtime.publish_manual_compact_terminal(entry.message_id, succeeded=True)

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[False],
        running_request_id_ref=[""],
        tui_runtime=runtime,
        control_operation_reconciler=Reconciler(),
    )

    assert tui_keybindings._tui_submit_control_operation(params, "/compact")
    snapshot = runtime.store.snapshot()
    assert snapshot.active_blocks == ()
    assert all(
        item.get("code") != "COMPACT_TERMINAL_WITHOUT_START"
        for item in snapshot.diagnostics
    )


def test_gateway_manual_compact_noop_closes_without_failure(tmp_path) -> None:
    runtime = TuiRuntime("control-compact-noop")
    stop_event = threading.Event()
    stop_event.set()
    params = SimpleNamespace(
        agent=SimpleNamespace(),
        use_gateway=True,
        current_session_id="session-noop",
        paths=SimpleNamespace(root=tmp_path),
        stop_event=stop_event,
        tui_runtime=runtime,
        control_operation_reconciler=None,
    )
    reconciler = tui_keybindings._ensure_control_operation_reconciler(params)
    entry = SimpleNamespace(
        message_id="control-compact-noop",
        command_kind="compact",
    )
    runtime.publish_manual_compact_started(entry.message_id)

    reconciler.on_complete(
        entry,
        ConversationControlResult(
            "compact",
            True,
            "当前会话还没有可压缩的历史。",
        ),
    )

    snapshot = runtime.store.snapshot()
    assert snapshot.active_blocks == ()
    assert snapshot.status.compact_count == 0
    assert all(block.role != "compact" for block in snapshot.stable_blocks)
    assert any(
        block.role == "system" and "还没有可压缩的历史" in block.text
        for block in snapshot.stable_blocks
    )


def test_gateway_context_uses_current_tui_snapshot_without_control_request() -> None:
    runtime = TuiRuntime("control-context-local-snapshot")
    turn = runtime.begin_turn("gwreq-context")
    assert turn.write_context_usage(
        {
            "schema": "model_visible_context_usage.v1",
            "estimated": True,
            "context_window_tokens": 128_000,
            "compact_trigger_tokens": 115_200,
            "current_tokens": 42_100,
            "prompt_tokens": 8_700,
            "messages_tokens": 16_000,
            "runtime_guidance_tokens": 400,
            "tool_schema_tokens": 17_000,
            "protocol": "native",
        }
    )

    class Reconciler:
        def enqueue(self, _entry) -> None:
            raise AssertionError("/context must not create a second Gateway estimate")

    params = SimpleNamespace(
        use_gateway=True,
        state_lock=threading.Lock(),
        is_running_ref=[True],
        running_request_id_ref=["gwreq-context"],
        tui_runtime=runtime,
        agent_navigation=None,
        control_operation_reconciler=Reconciler(),
    )

    assert tui_keybindings._tui_submit_control_operation(params, "/context")
    snapshot = runtime.store.snapshot()
    assert snapshot.status.context_usage is not None
    reports = [block.text for block in snapshot.stable_blocks if block.role == "system"]
    assert len(reports) == 1
    assert "42,100 / 128,000 tokens（32.9%）" in reports[0]
    assert "对话与工具消息：16,000 tokens" in reports[0]


def test_gateway_memory_command_runs_outside_input_thread(monkeypatch) -> None:
    runtime = TuiRuntime("memory-command")
    targets: list[object] = []
    handled: list[object] = []
    invalidations: list[bool] = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            assert name == "my-agent-tui-memory-command"
            assert daemon is True
            targets.append(target)

        def start(self) -> None:
            return None

    monkeypatch.setattr(tui_keybindings.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        tui_keybindings,
        "_handle_command_params",
        lambda _params, text: ("command", text),
    )
    monkeypatch.setattr(
        tui_keybindings,
        "_tui_handle_command",
        lambda *, params: handled.append(params) or True,
    )
    params = SimpleNamespace(
        use_gateway=True,
        agent=SimpleNamespace(gateway_client_only=True),
        tui_runtime=runtime,
    )
    event = SimpleNamespace(
        app=SimpleNamespace(invalidate=lambda: invalidations.append(True)),
    )

    assert tui_keybindings._tui_submit_gateway_memory_command(
        event,
        params,
        "/remember 不阻塞输入",
    )
    assert handled == []
    assert "正在保存记忆" in runtime.notice()

    targets[0]()

    assert handled == [("command", "/remember 不阻塞输入")]
    assert invalidations == [True]


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
    assert "当前没有可精确绑定的运行回合" in runtime.notice()


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


def test_gateway_status_does_not_show_mutating_confirmation_notice() -> None:
    runtime = TuiRuntime("control-status-no-confirmation")
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

    assert tui_keybindings._tui_submit_control_operation(params, "/status")
    assert len(captured) == 1
    assert captured[0].command_kind == "status"
    assert runtime.notice() == ""


def test_gateway_stop_completion_is_stable_transcript_feedback(tmp_path) -> None:
    runtime = TuiRuntime("control-stop-stable-feedback")
    stop_event = threading.Event()
    stop_event.set()
    params = SimpleNamespace(
        agent=SimpleNamespace(),
        use_gateway=True,
        current_session_id="session-stop",
        paths=SimpleNamespace(root=tmp_path),
        stop_event=stop_event,
        tui_runtime=runtime,
        control_operation_reconciler=None,
    )
    reconciler = tui_keybindings._ensure_control_operation_reconciler(params)
    entry = SimpleNamespace(message_id="control-stop-one", command_kind="stop")

    reconciler.on_complete(
        entry,
        ConversationControlResult(
            "stop",
            True,
            "停止请求已接收，正在收口。",
        ),
    )

    assert any(
        block.role == "system" and "停止请求已接收" in block.text
        for block in runtime.store.snapshot().stable_blocks
    )


@pytest.mark.parametrize("foreground", [True, False])
def test_running_gateway_submit_uses_active_turn_receipt_instead_of_job_queue(foreground) -> None:
    runtime = TuiRuntime("active-turn-submit")
    target = "gwreq-active-submit" if foreground else "goal-task-background"
    if not foreground:
        runtime.update_background_activity(1, {"main_activity": {"task_id": target, "phase": "running"}})
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
        is_running_ref=[foreground],
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
    assert entry.expected_turn_id == target
    assert entry.execution_options.inject == (
        "遵守项目规则",
        CHAT_RESPONSE_STYLE_INJECT,
    )
    assert entry.execution_options.prompt_files == ("spec.md",)
    assert entry.execution_options.save is False
    assert entry.execution_options.resume_context is True
    assert entry.execution_options.tool_approval is True
    assert entry.execution_options.rich_transcript is True


def test_background_input_target_disappears_when_activity_ends() -> None:
    runtime = TuiRuntime("background-input-target")
    runtime.update_background_activity(1, {"main_activity": {"task_id": "goal-task", "phase": "running"}})
    assert runtime.background_input_target() == "goal-task"
    runtime.update_background_activity(0, {"main_activity": {"task_id": "goal-task", "phase": "done"}})
    assert runtime.background_input_target() == ""


def test_child_input_stays_queued_until_exact_provider_consumption(monkeypatch) -> None:
    runtime = TuiRuntime("child-input-queued")
    submitted: list[dict[str, object]] = []

    class Agent:
        def request_agent_guidance(self, session_id, **kwargs):
            submitted.append({"session_id": session_id, **kwargs})
            return {
                "ok": True,
                "delivery": "queued",
                "status": "pending",
                "operation_id": kwargs["message_id"],
            }

    invalidations: list[bool] = []
    app = SimpleNamespace(invalidate=lambda: invalidations.append(True))
    params = SimpleNamespace(
        agent=Agent(),
        current_session_id="session-child-input",
        stop_event=threading.Event(),
        tui_runtime=runtime,
        agent_navigation=None,
    )
    monkeypatch.setattr(
        tui_keybindings.threading.Thread,
        "start",
        lambda worker: worker.run(),
    )

    for text in ("第一条用户插话", "第二条用户插话"):
        tui_keybindings._tui_submit_agent_input(
            SimpleNamespace(app=app),
            params,
            run_id="child-a",
            text=text,
            display_text=text,
        )

    pending = runtime.store.snapshot()
    assert [item.text for item in pending.pending_steers] == [
        "第一条用户插话",
        "第二条用户插话",
    ]
    assert not [block for block in pending.stable_blocks if block.role == "user"]
    assert runtime.notice() == "已排队给当前子代理：第二条用户插话"
    assert [item["message"] for item in submitted] == [
        "第一条用户插话",
        "第二条用户插话",
    ]

    message_ids = [str(item["message_id"]) for item in submitted]
    request_id = "bg-agent:child-a:attempt-a"
    assert runtime.publish_background_transcript_events(
        [
            {
                "schema": "background_transcript_event.v1",
                "seq": 1,
                "task_id": "child-a",
                "request_id": request_id,
                "kind": "active_turn_input_consumed",
                "phase": "completed",
                "block_id": f"{request_id}:active-input:1",
                "payload": {"client_message_ids": message_ids},
            }
        ]
    ) == 1
    consumed = runtime.store.snapshot()
    assert consumed.pending_steers == ()
    assert [block.text for block in consumed.stable_blocks if block.role == "user"] == [
        "第一条用户插话",
        "第二条用户插话",
    ]
    assert invalidations


def test_reattached_child_replays_consumed_user_message_without_local_pending() -> None:
    runtime = TuiRuntime("child-input-replay")
    request_id = "bg-agent:child-a:attempt-a"

    assert runtime.publish_background_transcript_events(
        [
            {
                "schema": "background_transcript_event.v1",
                "seq": 1,
                "task_id": "child-a",
                "request_id": request_id,
                "kind": "active_turn_input_consumed",
                "phase": "completed",
                "block_id": f"{request_id}:active-input:1",
                "payload": {
                    "client_message_ids": ["agent-steer-replay"],
                    "messages": [
                        {
                            "message_id": "agent-steer-replay",
                            "text": "至少读取两个核心源码文件。",
                            "truncated": False,
                        }
                    ],
                },
            }
        ]
    ) == 1

    snapshot = runtime.store.snapshot()
    assert snapshot.pending_steers == ()
    assert [block.text for block in snapshot.stable_blocks if block.role == "user"] == [
        "至少读取两个核心源码文件。"
    ]


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
        lambda _application, text, *, notify: (copied.append(text), notify("正在复制…")),
    )

    tui_keybindings._handle_ctrl_c_keybinding(SimpleNamespace(app=app), params)

    assert copied == ["selected output"]
    assert runtime.notice() == "正在复制…"


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
        lambda _application, text, *, notify: (copied.append(text), notify("正在复制…")),
    )

    tui_keybindings._handle_ctrl_c_keybinding(SimpleNamespace(app=app), params)

    assert copied == ["复制中文"]
    assert input_area.buffer.selection_state is not None
    assert runtime.notice() == "正在复制…"


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


def test_ctrl_o_freezes_current_child_runtime_instead_of_root() -> None:
    root = TuiRuntime("ctrl-o-root")
    root.enqueue_prompt("root-prompt", "主代理正文", queued=False)
    navigation = TuiAgentNavigationState(root)
    navigation.update_rows(
        "",
        [
            {
                "run_id": "child-a",
                "parent_run_id": "",
                "name": "worker-a",
                "status": "RUNNING",
                "description": "短标题",
            }
        ],
    )
    navigation.move_selection(1)
    navigation.enter_selected()
    navigation.apply_agent_view(
        "child-a",
        {
            "ok": True,
            "agent": {
                "run_id": "child-a",
                "parent_run_id": "",
                "name": "worker-a",
                "status": "RUNNING",
                "description": "短标题",
                "goal": "子代理完整派工正文",
            },
            "terminal": False,
            "children": [],
            "task_progress_items": [],
            "transcript_events": [],
            "event_cursor": 0,
            "final_response": "",
        },
    )
    transcript_state = TuiTranscriptModeState()
    moved: list[bool] = []
    focused: list[object] = []
    modal_window = object()
    params = SimpleNamespace(
        tui_runtime=root,
        agent_navigation=navigation,
        transcript_state=transcript_state,
        transcript_area=SimpleNamespace(
            modal_control=SimpleNamespace(move_end=lambda: moved.append(True)),
            modal_window=modal_window,
        ),
    )
    event = SimpleNamespace(
        app=SimpleNamespace(
            layout=SimpleNamespace(focus=lambda target: focused.append(target)),
            invalidate=lambda: None,
        )
    )

    tui_keybindings._handle_ctrl_o_keybinding(event, params)

    frozen = transcript_state.snapshot_for_render(root.store.snapshot())
    assert transcript_state.snapshot().active is True
    assert any(
        block.role == "user" and block.text == "子代理完整派工正文"
        for block in frozen.stable_blocks
    )
    assert all("主代理正文" not in block.text for block in frozen.stable_blocks)
    assert moved == [True]
    assert focused == [modal_window]


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

    assert interaction.snapshot().mouse_capture_enabled is False
    assert "原生复制模式" in runtime.notice()
    assert input_area.text == "保留输入"
    assert params.exit_armed_at_ref == [0.0]
    assert redraws == [True]

    tui_keybindings._handle_f6_mouse_keybinding(event, params)

    assert interaction.snapshot().mouse_capture_enabled is True
    assert "滚轮模式" in runtime.notice()
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


def test_tmux_clipboard_buffer_uses_documented_set_buffer_write_through(monkeypatch) -> None:
    calls: list[tuple[list[str], object]] = []

    class Result:
        returncode = 0

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs.get("input")))
        return Result()

    monkeypatch.delenv("LC_TERMINAL", raising=False)
    monkeypatch.setattr(tui_keybindings.subprocess, "run", fake_run)

    assert tui_keybindings._load_tmux_clipboard_buffer("甲乙")
    assert calls == [(["tmux", "set-buffer", "-w", "--", "甲乙"], None)]


def test_tmux_clipboard_buffer_uses_stdin_only_for_iterm2(monkeypatch) -> None:
    calls: list[tuple[list[str], object]] = []

    class Result:
        returncode = 0

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs.get("input")))
        return Result()

    monkeypatch.setenv("LC_TERMINAL", "iTerm2")
    monkeypatch.setattr(tui_keybindings.subprocess, "run", fake_run)

    assert tui_keybindings._load_tmux_clipboard_buffer("copy")
    assert calls == [(["tmux", "load-buffer", "-"], "copy")]


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
    assert tui_keybindings._copy_native_clipboard("正文") is True
    assert calls[1] == (["pbcopy"], "正文")


def test_native_clipboard_skipped_over_ssh(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(args, *, input, **_kwargs):
        calls.append(args[0])
        return type("R", (), {"returncode": 0})()

    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.13 54321 10.0.0.1 22")
    monkeypatch.setattr(tui_keybindings.sys, "platform", "darwin")
    monkeypatch.setattr(tui_keybindings.subprocess, "run", fake_run)

    tui_keybindings._copy_native_clipboard("远端不写本机剪贴板")
    assert calls == []


def test_linux_clipboard_caches_only_successful_command(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(tui_keybindings, "_linux_clipboard_tool_cache", None)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("DISPLAY", ":test")
    monkeypatch.setattr(tui_keybindings, "_run_clipboard_tool",
                        lambda args, text: calls.append(args[0]) is None and args[0] == "xsel")
    assert tui_keybindings._copy_linux_clipboard("第一次") is True
    assert calls == ["wl-copy", "xclip", "xsel"]
    calls.clear()
    assert tui_keybindings._copy_linux_clipboard("第二次") is True
    assert calls == ["xsel"]


def test_linux_clipboard_invalid_cache_does_not_block_other_display(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(tui_keybindings, "_linux_clipboard_tool_cache", ["wl-copy"])
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("DISPLAY", ":test")
    monkeypatch.setattr(tui_keybindings, "_run_clipboard_tool",
                        lambda args, text: calls.append(args[0]) is None)
    assert tui_keybindings._copy_linux_clipboard("正文") is True
    assert calls == ["xclip"]


def test_linux_clipboard_failure_is_not_cached(monkeypatch) -> None:
    monkeypatch.setattr(tui_keybindings, "_linux_clipboard_tool_cache", ["wl-copy"])
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("DISPLAY", ":test")
    monkeypatch.setattr(tui_keybindings, "_run_clipboard_tool", lambda args, text: False)
    assert tui_keybindings._copy_linux_clipboard("正文") is False
    assert tui_keybindings._linux_clipboard_tool_cache is None


def test_write_selection_clipboard_starts_one_ordered_worker_when_local(monkeypatch) -> None:
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

    assert [name for name, _ in started] == ["_run"]
    assert app._my_agent_clipboard_projector._pending.native is True
    assert app._my_agent_clipboard_projector._pending.tmux is False
    assert app.clipboard.get_data().text == "本地复制"


def test_large_selection_uses_native_and_tmux_without_oversized_osc(monkeypatch) -> None:
    from prompt_toolkit.clipboard import InMemoryClipboard

    started = []
    raw = []
    app = SimpleNamespace(clipboard=InMemoryClipboard(), output=SimpleNamespace(write_raw=raw.append))
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setenv("TMUX", "test")
    monkeypatch.setattr(tui_keybindings.threading.Thread, "start",
                        lambda thread: started.append((thread._target.__name__, thread._args)))
    text = "长文本复制" * 9000
    tui_keybindings._write_selection_clipboard(app, text)
    assert [name for name, _ in started] == ["_run"]
    pending = app._my_agent_clipboard_projector._pending
    assert pending.native is True and pending.tmux is True
    assert pending.text == text and pending.terminal_copy() is False
    assert app.clipboard.get_data().text == text
    assert raw == []


def test_large_tmux_selection_uses_stdin_instead_of_argv(monkeypatch) -> None:
    calls = []
    monkeypatch.delenv("LC_TERMINAL", raising=False)
    monkeypatch.setattr(tui_keybindings.subprocess, "run",
                        lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    text = "长选区" * 50000
    assert tui_keybindings._load_tmux_clipboard_buffer(text)
    assert calls[0][0] == ["tmux", "load-buffer", "-"]
    assert calls[0][1]["input"] == text
