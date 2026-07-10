from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.observability.tracing import TraceContext
from agent_py_agent.agent.scale_downstream import ScaleAgentPool, ScaleMessage, _extract_message


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
            return SimpleNamespace(response="done", turn_token_estimate=7, prompt_token_estimate=0)

    class Adapter:
        def reply_message(self, message_id: str, response: str) -> bool:
            events.append(f"reply:{message_id}:{response}")
            return True

    pool = object.__new__(ScaleAgentPool)
    pool._workspaces = tmp_path
    pool._store = Store()
    pool._build_agent = lambda *_args: Agent()
    monkeypatch.setattr("agent_py_agent.agent.scale_downstream._adapter", lambda: Adapter())
    message = ScaleMessage("acme", "user", "u1", "hello", "m1")
    trace = TraceContext("1" * 32, "2" * 16)
    assert pool._run_locked(message, trace) == 7
    assert events == ["restore", "run", "commit", "reply:m1:done"]
    execution_dir = tmp_path / ".owner-executions"
    assert not execution_dir.exists() or not any(execution_dir.iterdir())
