from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import EchoBackend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.observability.tracing import TraceContext
from agent_py_agent.agent.scale_downstream import (
    ScaleAgentPool,
    ScaleMessage,
    _ExecutionPaths,
    _extract_message,
)
from agent_py_agent.agent.settings import AgentConfig


class _RecordingEchoBackend(EchoBackend):
    """记录每次原生 messages，验证历史走 provider 结构而不是动态 prompt。"""

    def __init__(self) -> None:
        self.message_calls: list[list[dict[str, object]]] = []
        self.prompt_calls: list[str] = []

    def generate(self, prompt: str, **kwargs):
        self.message_calls.append(list(kwargs.get("messages") or []))
        self.prompt_calls.append(prompt)
        return super().generate(prompt, **kwargs)


def test_extracts_private_feishu_message_and_owner() -> None:
    message = _extract_message(
        {
            "tenant": "acme",
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "content": '{"text":"你好"}',
                },
            },
        }
    )
    assert message.owner_kind == "user"
    assert message.owner_id == "ou_user"
    assert message.prompt == "你好"
    assert message.conversation_id == "oc_1"
    assert message.channel_user_id == "ou_user"


def test_extracts_topic_aware_group_conversation() -> None:
    message = _extract_message(
        {
            "tenant": "acme",
            "event": {
                "sender": {"sender_id": {"open_id": "ou_member"}},
                "message": {
                    "message_id": "om_topic",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                    "root_id": "om_root",
                    "content": '{"text":"继续这个话题"}',
                },
            },
        }
    )
    assert message.owner_kind == "group"
    assert message.owner_id == "oc_group"
    assert message.channel_user_id == "ou_member"
    assert message.conversation_id == "oc_group:thread:om_root"


def test_rejects_message_without_tenant_or_reply_target() -> None:
    with pytest.raises(ValueError):
        _extract_message({"event": {"message": {"content": '{"text":"x"}'}}})


def test_owner_snapshot_commits_before_external_reply(tmp_path, monkeypatch) -> None:
    events: list[str] = []

    class Store:
        @contextmanager
        def checkout(self, *_args):
            events.append("restore")
            yield
            events.append("commit")

    class Agent:
        def run(self, *_args, **_kwargs):
            events.append("run")
            return {"response": "done", "turn_token_estimate": 7, "prompt_token_estimate": 0}

    class Adapter:
        def reply_message(self, message_id: str, response: str) -> bool:
            events.append(f"reply:{message_id}:{response}")
            return True

    pool = object.__new__(ScaleAgentPool)
    pool._workspaces = tmp_path
    pool._store = Store()
    pool._build_agent = lambda *_args: Agent()
    pool._run_conversation_turn = lambda agent, *_args: agent.run()
    monkeypatch.setattr("agent_py_agent.agent.scale_downstream._adapter", lambda: Adapter())
    message = ScaleMessage("acme", "user", "u1", "hello", "m1")
    trace = TraceContext("1" * 32, "2" * 16)
    assert pool._run_locked(message, trace) == 7
    assert events == ["restore", "run", "commit", "reply:m1:done"]
    execution_dir = tmp_path / ".owner-executions"
    assert not execution_dir.exists() or not any(execution_dir.iterdir())


def test_direct_scale_turn_without_conversation_identity_fails_closed(tmp_path) -> None:
    pool = object.__new__(ScaleAgentPool)
    with pytest.raises(ValueError, match="conversation_id/channel_user_id"):
        pool._run_conversation_turn(
            SimpleNamespace(),
            ScaleMessage("acme", "user", "ou_user", "你好", "om_1"),
            TraceContext("1" * 32, "2" * 16),
        )


def test_scale_turn_reuses_gateway_transcript_and_isolates_other_conversation(tmp_path) -> None:
    home_root = tmp_path / "home"
    workspace = tmp_path / "workspace"
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(home_root),
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="ou_user",
            prompt_files=[],
        ),
        workspace,
    )
    backend = _RecordingEchoBackend()
    agent.backend = backend
    pool = object.__new__(ScaleAgentPool)
    first = ScaleMessage(
        "acme",
        "user",
        "ou_user",
        "请记住本会话代号是青黛",
        "om_1",
        "oc_chat:thread:om_root",
        "ou_user",
    )
    second = ScaleMessage(
        "acme",
        "user",
        "ou_user",
        "刚才的代号是什么？",
        "om_2",
        "oc_chat:thread:om_root",
        "ou_user",
    )
    other = ScaleMessage(
        "acme",
        "user",
        "ou_user",
        "这是另一个聊天",
        "om_3",
        "oc_other",
        "ou_user",
    )

    first_result = pool._run_conversation_turn(
        agent,
        first,
        TraceContext("1" * 32, "2" * 16),
    )
    second_result = pool._run_conversation_turn(
        agent,
        second,
        TraceContext("3" * 32, "4" * 16),
    )
    second_messages = backend.message_calls[-1]
    other_result = pool._run_conversation_turn(
        agent,
        other,
        TraceContext("5" * 32, "6" * 16),
    )
    other_messages = backend.message_calls[-1]

    assert first_result["response"]
    assert "请记住本会话代号是青黛" in str(second_messages)
    assert "这是 echo 后端的本地响应" in str(second_messages)
    assert "请记住本会话代号是青黛" not in str(other_messages)
    assert second_result["ok"] and other_result["ok"]
    assert all("# Conversation Transcript" not in prompt for prompt in backend.prompt_calls)
    calls_before_replay = len(backend.message_calls)
    assert pool._run_conversation_turn(agent, first, TraceContext("1" * 32, "2" * 16)) == first_result
    assert len(backend.message_calls) == calls_before_replay


def test_scale_group_members_share_group_conversation_without_becoming_owner(tmp_path) -> None:
    home_root = tmp_path / "home"
    workspace = tmp_path / "workspace"
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(home_root),
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="group",
            my_agent_owner_id="oc_group",
            prompt_files=[],
        ),
        workspace,
    )
    backend = _RecordingEchoBackend()
    agent.backend = backend
    pool = object.__new__(ScaleAgentPool)
    first = ScaleMessage(
        "acme", "group", "oc_group", "群里约定代号是远山", "om_1", "oc_group", "ou_a"
    )
    second = ScaleMessage(
        "acme", "group", "oc_group", "刚才群里约定了什么？", "om_2", "oc_group", "ou_b"
    )

    pool._run_conversation_turn(agent, first, TraceContext("7" * 32, "8" * 16))
    result = pool._run_conversation_turn(agent, second, TraceContext("9" * 32, "a" * 16))

    assert "群里约定代号是远山" in str(backend.message_calls[-1])
    assert result["ok"]
    assert all("# Conversation Transcript" not in prompt for prompt in backend.prompt_calls)
    assert len(list(agent.conversation_store.storage.threads_dir.glob("*.json"))) == 1
