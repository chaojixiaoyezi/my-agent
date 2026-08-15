from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.models import GatewayRunContext


class _BackgroundCliBackend:
    name = "background-cli"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="后台主代理 CLI 汇报。", backend=self.name)


def test_background_main_agent_tick_runs_due_policy(tmp_path, capsys) -> None:
    from agent_py_agent.cli.background_main_agent import cmd_background_main_agent_tick

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _BackgroundCliBackend()
    agent.backend = backend
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "巡检", 'now': 11.0})
    agent.conversation_store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 10, 'route_channel': "internal", 'route_target': "thread-1", 'now': 12.0})
    args = SimpleNamespace(config=str(tmp_path / "config.yaml"), now=22.0, json=False)

    with patch("agent_py_agent.cli.background_main_agent.make_agent", return_value=agent):
        assert cmd_background_main_agent_tick(args) == 0

    output = capsys.readouterr().out
    messages = agent.conversation_store.recent_messages(thread.thread_id)
    assert "background-main-agent tick reports=1" in output
    assert messages[-1].content == "后台主代理 CLI 汇报。"
    assert "inspect_agent_tree" in backend.prompts[0]


def test_gateway_background_loop_runs_due_progress_policy(tmp_path) -> None:
    from agent_py_agent.cli.gateway_loops import _gateway_background_main_loop

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            gateway_request_poll_interval=1,
            gateway_heartbeat_interval=5,
        ),
        tmp_path,
    )
    backend = _BackgroundCliBackend()
    agent.backend = backend
    current = time.time()
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': current - 10})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "巡检", 'now': current - 9})
    agent.conversation_store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 1, 'route_channel': "internal", 'route_target': "thread-1", 'now': current - 2})
    context = GatewayRunContext(
        agent=agent,
        paths=SimpleNamespace(),
        config_path=tmp_path / "config.yaml",
    )
    stop_event = threading.Event()
    worker = threading.Thread(target=_gateway_background_main_loop, args=(context, stop_event), daemon=True)

    with patch("agent_py_agent.cli.gateway_loops.make_agent", return_value=agent):
        worker.start()
        deadline = time.time() + 2.0
        while (
            time.time() < deadline
            and not agent.conversation_store.recent_messages(thread.thread_id)
        ):
            time.sleep(0.05)
        stop_event.set()
        worker.join(timeout=2)

    messages = agent.conversation_store.recent_messages(thread.thread_id)
    assert backend.prompts
    assert messages[-1].content == "后台主代理 CLI 汇报。"


def test_background_main_agent_message_and_bind_task_commands(tmp_path, capsys) -> None:
    from agent_py_agent.cli.background_main_agent import (
        cmd_background_main_agent_bind_task,
        cmd_background_main_agent_message,
    )

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    message_args = SimpleNamespace(
        config=str(tmp_path / "config.yaml"),
        channel="internal",
        conversation_id="thread-1",
        user_id="user-1",
        canonical_user_id="user-1",
        content="请长期跟进这个任务。",
        run_background=False,
        now=100.0,
        json=True,
    )

    with patch("agent_py_agent.cli.background_main_agent.make_agent", return_value=agent):
        assert cmd_background_main_agent_message(message_args) == 0

    thread_payload = json.loads(capsys.readouterr().out)
    bind_args = SimpleNamespace(
        config=str(tmp_path / "config.yaml"),
        thread_id=thread_payload["thread_id"],
        task_id="task-1",
        goal="每分钟汇报一次",
        progress_interval_seconds=60,
        route_channel="internal",
        route_target="thread-1",
        now=101.0,
        json=True,
    )
    with patch("agent_py_agent.cli.background_main_agent.make_agent", return_value=agent):
        assert cmd_background_main_agent_bind_task(bind_args) == 0

    bind_payload = json.loads(capsys.readouterr().out)
    policy = agent.conversation_store.get_progress_policy(bind_payload["policy_id"])
    assert bind_payload["task_id"] == "task-1"
    assert policy is not None
    assert policy.next_due_at == 161.0


def test_background_main_agent_parser_registers_group() -> None:
    from agent_py_agent.cli.parser import build_parser

    parser = build_parser()
    args = parser.parse_args(["background-main-agent", "tick", "--now", "123", "--json"])
    status_args = parser.parse_args(["background-main-agent", "status", "--json"])

    assert args.command == "background-main-agent"
    assert args.background_command == "tick"
    assert args.now == 123.0
    assert args.json is True
    assert status_args.background_command == "status"
    assert status_args.json is True


def test_background_main_agent_observe_records_event_and_wake(tmp_path, capsys) -> None:
    from agent_py_agent.cli.background_main_agent import cmd_background_main_agent_observe

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "长期任务", 'now': 2.0})
    args = SimpleNamespace(
        config=str(tmp_path / "config.yaml"),
        thread_id="",
        task_id="task-1",
        event_type="runtime_alert",
        summary="子代理发现需要主代理处理的事件。",
        urgency="urgent",
        severity="high",
        source_agent_id="child-1",
        parent_agent_id="root-1",
        root_task_id="",
        evidence_ref=["tool://event/1"],
        requires_main_agent=True,
        requires_llm_report=True,
        wake=True,
        dedupe_key="task-1:event",
        now=3.0,
        json=True,
    )

    with patch("agent_py_agent.cli.background_main_agent.make_agent", return_value=agent):
        assert cmd_background_main_agent_observe(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["thread_id"] == thread.thread_id
    assert payload["wake_signal_id"].startswith("wake-")
    assert agent.conversation_store.pending_wake_signals()[0].summary == "子代理发现需要主代理处理的事件。"


def test_background_main_agent_service_wait_returns_when_wake_signal_pending(tmp_path) -> None:
    from agent_py_agent.cli.background_main_agent import _wait_for_service_interval

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    observation = agent.conversation_store.append_observation({'thread_id': thread.thread_id, 'event_type': "runtime_alert", 'summary': "立刻叫醒主代理。", 'urgency': "urgent", 'requires_main_agent': True, 'now': 2.0})
    agent.conversation_store.raise_wake_signal({'thread_id': thread.thread_id, 'observation': observation, 'now': 2.0})

    assert _wait_for_service_interval(agent, interval=30.0) == "wake_signal"


def test_background_main_agent_service_runs_bounded_cycles(tmp_path, capsys) -> None:
    from agent_py_agent.cli.background_main_agent import cmd_background_main_agent_service

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _BackgroundCliBackend()
    agent.backend = backend
    current = time.time()
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': current - 3})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "巡检", 'now': current - 2})
    agent.conversation_store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 1, 'route_channel': "internal", 'route_target': "thread-1", 'now': current - 2})
    args = SimpleNamespace(
        config=str(tmp_path / "config.yaml"),
        interval=0.0,
        max_cycles=1,
        stop_file="",
        json=False,
    )

    with patch("agent_py_agent.cli.background_main_agent.make_agent", return_value=agent):
        assert cmd_background_main_agent_service(args) == 0

    assert "background-main-agent service cycles=1 reports=1" in capsys.readouterr().out


def test_background_main_agent_status_summarizes_control_plane(tmp_path, capsys) -> None:
    from agent_py_agent.cli.background_main_agent import cmd_background_main_agent_status

    agent = _agent_with_status_control_plane(tmp_path)
    args = SimpleNamespace(config=str(tmp_path / "config.yaml"), json=True)

    with patch("agent_py_agent.cli.background_main_agent.make_agent", return_value=agent):
        assert cmd_background_main_agent_status(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["thread_count"] == 1
    assert payload["bound_task_count"] == 1
    assert payload["pending_wake_count"] == 1
    assert payload["unhandled_observation_count"] == 1
    assert payload["progress_policy_count"] == 1
    assert payload["collaboration"]["case_count"] == 1
    assert payload["agent_tree"]["schema_version"] == "agent_tree_status.v1"


def _agent_with_status_control_plane(tmp_path) -> SimpleAgent:
    from agent_py_agent.agent.collaboration import AgentCapability

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "长期协作", 'now': 2.0})
    observation = agent.conversation_store.append_observation({'thread_id': thread.thread_id, 'event_type': "runtime_alert", 'summary': "需要主代理处理。", 'urgency': "urgent", 'requires_main_agent': True, 'now': 3.0})
    agent.conversation_store.raise_wake_signal({'thread_id': thread.thread_id, 'observation': observation, 'now': 3.0})
    agent.conversation_store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'now': 4.0})
    agent.collaboration_store.register_agent(AgentCapability(agent_id="source-a", capabilities=("query",)))
    agent.collaboration_store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': "状态看板 case", 'created_by': "source-a", 'now': 5.0})
    return agent
