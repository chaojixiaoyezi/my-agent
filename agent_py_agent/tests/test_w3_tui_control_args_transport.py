"""TUI gateway transport keeps /dir, /fork and /restart arguments (live clone: /dir <path> showed the old dir,
/fork <message id> --title copied the whole thread under the default title), and /fork --at <N> forks
through the Nth user message as the command catalog promises."""
import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.workspace_context import execute_workspace_control
from agent_py_agent.cli.chat_parts.control_runtime import _command_text


def _facts(command):
    return (command.kind, command.valid, command.operation, command.value, command.name,
            getattr(command, "message_id_explicit", False), getattr(command, "fork_at", 0))


@pytest.mark.parametrize("text", [
    "/dir /private/tmp/project-a",
    "/dir /private/tmp/my project",
    "/dir reset",
    "/dir",
    "/fork",
    "/fork msg-a3531a614a014167",
    "/fork msg-a3531a614a014167 --title 分叉测试",
    '/fork msg-a3531a614a014167 --title "分叉 测试"',
    "/fork --from-message msg-a3531a614a014167 --title t1",
    "/fork --title 只改标题",
    "/fork --at 2",
    "/fork --at 2 --title 第二轮分叉",
    "/restart 维护窗口",
    "/restart",
])
def test_transport_text_round_trips_through_the_gateway_parser(text: str) -> None:
    command = parse_conversation_control(text)
    assert command is not None and command.valid
    transported = _command_text(command)
    again = parse_conversation_control(transported)
    assert again is not None
    assert _facts(again) == _facts(command), transported


def test_fork_cutoff_and_title_reach_the_gateway() -> None:
    transported = _command_text(parse_conversation_control("/fork msg-a3531a614a014167 --title 分叉测试"))
    assert transported.startswith("/fork msg-a3531a614a014167 --title ")
    again = parse_conversation_control(transported)
    assert (again.value, again.name, again.message_id_explicit) == ("msg-a3531a614a014167", "分叉测试", True)
    assert _command_text(parse_conversation_control("/dir /private/tmp/project-a")) == "/dir /private/tmp/project-a"


@pytest.mark.parametrize("raw", ["/fork --at", "/fork --at 0", "/fork --at x", "/fork --at -1",
                                 "/fork --at 2 msg-1", "/fork --at 2 --at 3", "/fork msg-1 --at 2"])
def test_fork_at_rejects_malformed_positions(raw: str) -> None:
    command = parse_conversation_control(raw)
    assert command is not None and command.kind == "fork" and not command.valid


def _turns(tmp_path, count: int):
    home = tmp_path / "home" / "owners" / "local" / "main"
    home.mkdir(parents=True)
    work = tmp_path / "project"
    work.mkdir()
    store = ConversationStore(home / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "local/main", "owner_home": str(home),
        "owner_id": "local/main", "cwd": str(work), "runtime_workspace_roots": (str(work),), "title": "源会话"})
    for turn in range(1, count + 1):
        for role in ("user", "assistant"):
            store.messages.append({"thread_id": thread.thread_id, "role": role,
                "content": f"{role}-{turn}", "metadata": {}, "now": 1234.5 + turn})
    return store, thread, home


def test_fork_at_keeps_history_through_the_nth_user_message(tmp_path) -> None:
    store, thread, home = _turns(tmp_path, 3)
    command = parse_conversation_control(_command_text(parse_conversation_control("/fork --at 2 --title 第二轮分叉")))
    result = execute_workspace_control(store, command, thread_id=thread.thread_id, owner_home=home)
    assert result.ok, result
    branch_id = result.details["new_thread_id"]
    rows, errors = store.messages.recent_report(branch_id, limit=0)
    assert not errors and [row.content for row in rows] == ["user-1", "assistant-1", "user-2", "assistant-2"]
    assert store.threads.require(branch_id).title == "第二轮分叉"
    source_rows, _ = store.messages.recent_report(thread.thread_id, limit=0)
    assert len(source_rows) == 6


def test_fork_at_past_the_last_user_message_creates_nothing(tmp_path) -> None:
    store, thread, home = _turns(tmp_path, 2)
    before = set(store.storage.threads_dir.glob("*.json"))
    result = execute_workspace_control(store, parse_conversation_control("/fork --at 3"),
                                       thread_id=thread.thread_id, owner_home=home)
    assert not result.ok and result.error_code == "FORK_MESSAGE_NOT_FOUND"
    assert set(store.storage.threads_dir.glob("*.json")) == before


def test_fork_at_counts_turns_not_mid_turn_guidance(tmp_path) -> None:
    # Live clone (2026-11-17): a message sent while turn 2 was still running is stored as active_turn_user_input
    # and answered by that turn's reply; counting it as the next user message cut the branch before the reply.
    store, thread, home = _turns(tmp_path, 0)
    for role, content, metadata in (("user", "user-1", {}), ("user", "guidance-1", {"kind": "active_turn_user_input"}),
                                    ("assistant", "assistant-1", {}), ("user", "user-2", {}),
                                    ("assistant", "assistant-2", {})):
        store.messages.append({"thread_id": thread.thread_id, "role": role, "content": content,
                               "metadata": metadata, "now": 1300.0})
    result = execute_workspace_control(store, parse_conversation_control("/fork --at 1"),
                                       thread_id=thread.thread_id, owner_home=home)
    assert result.ok, result
    rows, errors = store.messages.recent_report(result.details["new_thread_id"], limit=0)
    assert not errors and [row.content for row in rows] == ["user-1", "guidance-1", "assistant-1"]
    past_end = execute_workspace_control(store, parse_conversation_control("/fork --at 3"),
                                         thread_id=thread.thread_id, owner_home=home)
    assert not past_end.ok and past_end.error_code == "FORK_MESSAGE_NOT_FOUND"
