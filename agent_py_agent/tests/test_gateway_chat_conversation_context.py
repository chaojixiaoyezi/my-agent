from __future__ import annotations

import json

from agent_py_agent.agent.agent_core._runtime_params import ArchiveRunParams
from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
    register_saved_run_task_ref,
    write_run_task_workspace_if_needed,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_execution import (
    _gateway_conversation_context,
    _gateway_injections,
    _root_user_prompt,
)
from agent_py_agent.agent.gateway_parts.request_worker import GatewayAskParams, submit_gateway_ask
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_indexes import latest_task_refs


def test_gateway_chat_request_payload_carries_session_conversation(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    paths = gateway_paths(agent)

    _request_id, request_path, _response_path = submit_gateway_ask(
        paths,
        params=GatewayAskParams(
            prompt="看一下后台情况",
            save=False,
            chat_session_id="session-1",
            agent=agent,
        ),
    )

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert payload["conversation"]["channel"] == "chat"
    assert payload["conversation"]["channel_conversation_id"] == "session-1"
    assert payload["conversation"]["channel_user_id"] == "local-cli"


def test_gateway_chat_followup_reuses_active_root_task_context(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "session-1",
            "channel_user_id": "local-cli",
            "canonical_user_id": "local-agent",
        }
    }

    first = _gateway_conversation_context(agent, request, "gw-first", "分析 all-agent 项目")
    assert first.thread_id
    assert first.active_task_id == ""
    assert agent.conversation_store.thread_for_task("gw-first").thread_id == first.thread_id

    workspace = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-03" / "analysis"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    register_saved_run_task_ref(
        agent,
        type("RunWorkspace", (), {"root": workspace, "work_dir": workspace / "work"})(),
        ArchiveRunParams(
            do_save=True,
            user_prompt="分析 all-agent 项目",
            final_response=None,
            archive_tool_calls=[],
            run_request_id="gw-first",
            run_id="gw-first",
            task_id="gw-first",
            source="gateway",
        ),
    )

    second = _gateway_conversation_context(agent, request, "gw-second", "后台还有人在跑吗")

    assert second.thread_id == first.thread_id
    assert second.active_task_id == "gw-first"
    assert second.task_workspace == str(workspace)
    assert agent.conversation_store.thread_for_task("gw-second") is None
    section = _gateway_injections({"inject": []}, second)[0]
    assert "active_root_task_id: gw-first" in section
    assert "不要把当前 gateway request id 当成新的 root" in section


def test_gateway_chat_followup_does_not_treat_completed_alias_as_active_task(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "session-1",
            "channel_user_id": "local-cli",
            "canonical_user_id": "local-agent",
        }
    }

    first = _gateway_conversation_context(agent, request, "gw-first", "分析 all-agent 项目")
    agent.conversation_store.update_task_status({"task_id": "gw-first", "status": "completed"})

    second = _gateway_conversation_context(agent, request, "gw-second", "继续吗")

    assert second.thread_id == first.thread_id
    assert second.active_task_id == ""
    assert agent.conversation_store.thread_for_task("gw-second").thread_id == first.thread_id


def test_gateway_followup_preserves_current_user_prompt_with_active_task_context(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "session-1",
            "channel_user_id": "local-cli",
            "canonical_user_id": "local-agent",
        }
    }
    first = _gateway_conversation_context(agent, request, "gw-first", "分析 all-agent 项目")
    assert first.thread_id
    workspace = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-03" / "analysis"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    register_saved_run_task_ref(
        agent,
        type("RunWorkspace", (), {"root": workspace, "work_dir": workspace / "work"})(),
        ArchiveRunParams(
            do_save=True,
            user_prompt="分析 all-agent 项目",
            final_response=None,
            archive_tool_calls=[],
            run_request_id="gw-first",
            run_id="gw-first",
            task_id="gw-first",
            source="gateway",
        ),
    )

    followup = _gateway_conversation_context(agent, request, "gw-chat", "不着急，我只是聊天，不布置任务，原任务继续。")

    assert followup.thread_id == first.thread_id
    assert followup.active_task_id == "gw-first"
    assert followup.task_workspace == str(workspace)
    assert agent.conversation_store.thread_for_task("gw-chat") is None
    assert _root_user_prompt("不着急，我只是聊天，不布置任务，原任务继续。", followup) == "不着急，我只是聊天，不布置任务，原任务继续。"
    section = _gateway_injections({"inject": []}, followup)[0]
    assert "active_root_task_id: gw-first" in section


def test_gateway_followup_archive_reuses_active_task_workspace(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    workspace = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-03" / "analysis"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()

    written = write_run_task_workspace_if_needed(
        agent,
        ArchiveRunParams(
            do_save=True,
            user_prompt="后台正常吗",
            final_response=None,
            archive_tool_calls=[],
            run_request_id="gw-second",
            run_id="gw-second",
            task_id="gw-second",
            source="gateway",
            task_attributes={
                "conversation_task_id": "gw-first",
                "run_workspace": {
                    "task_root": str(workspace),
                    "output_dir": str(workspace / "output"),
                    "work_dir": str(workspace / "work"),
                },
            },
        ),
    )

    assert written == str(workspace)
    assert not (workspace.parent / "后台正常吗-gw-second").exists()
    refs = latest_task_refs(agent.home_paths, owner_id=agent.home_paths.owner_id)
    assert any(ref["task_id"] == "gw-first" and ref["task_path"] == str(workspace) for ref in refs)
