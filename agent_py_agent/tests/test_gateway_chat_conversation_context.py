from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ArchiveRunParams
from agent_py_agent.agent.agent_core.orchestration.create_policy import (
    add_current_conversation_attrs,
    create_run_params,
)
from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
    register_saved_run_task_ref,
    write_run_task_workspace_if_needed,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import RunParams, _prepare_runtime_context
from agent_py_agent.agent.agent_core.runtime.run_params import (
    run_params_with_materialized_delivery_contract,
)
from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.agent.conversation.task_promotion import (
    complete_current_conversation_task,
    promote_current_conversation_task,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_errors import ConversationPersistenceError
from agent_py_agent.agent.gateway_parts.request_execution import (
    _append_gateway_conversation_message,
    _gateway_conversation_context,
    _gateway_injections,
    _GatewayAskRunContext,
    _GatewayConversationLoadRequest,
    _root_user_prompt,
    _run_gateway_ask,
    _update_response_from_result,
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


def test_gateway_cli_request_payload_carries_default_local_conversation(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    paths = gateway_paths(agent)

    _request_id, request_path, _response_path = submit_gateway_ask(
        paths,
        params=GatewayAskParams(
            prompt="看一下后台情况",
            save=False,
            agent=agent,
        ),
    )

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert payload["source"] == "cli_gateway"
    assert payload["conversation"]["channel"] == "gateway-cli"
    assert payload["conversation"]["channel_conversation_id"] == "default"
    assert payload["conversation"]["channel_user_id"] == "local-cli"


def test_gateway_chat_reuses_thread_but_does_not_auto_bind_task(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "session-1",
            "channel_user_id": "local-cli",
            "canonical_user_id": "local-agent",
        }
    }

    first = _conversation_context(agent, request, "gw-first", "你好，我叫小叶子")
    assert first.thread_id
    assert first.active_task_id == ""
    assert agent.conversation_store.thread_for_task("gw-first") is None
    _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "chat", "message_id": "m1"}},
        first,
        request_id="gw-first",
        role="user",
        content="你好，我叫小叶子",
    )
    _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "chat"}},
        first,
        request_id="gw-first",
        role="assistant",
        content="你好，小叶子。",
    )
    second = _conversation_context(agent, request, "gw-second", "我叫什么？")
    assert second.thread_id == first.thread_id
    assert second.active_task_id == ""
    assert agent.conversation_store.thread_for_task("gw-second") is None
    section = _gateway_injections({"inject": []}, second)[0]
    assert "你好，我叫小叶子" in section
    assert "你好，小叶子。" in section
    assert "active_root_task_id" not in section


def test_gateway_run_persists_user_and_assistant_for_next_turn(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_chat1",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    first_request = {
        "prompt": "我喜欢简短回答",
        "save": False,
        "source": "http:feishu",
        "metadata": {"channel": "feishu", "message_id": "om_1"},
        "conversation": conversation,
    }
    _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            first_request,
            tmp_path / "req-1.json",
            tmp_path / "resp-1.json",
            "gw-1",
            lambda _chunk: None,
        )
    )
    second = _conversation_context(
        agent,
        {"conversation": conversation},
        "gw-2",
        "你还记得我的偏好吗？",
    )
    assert any(role == "user" and "简短回答" in content for role, content in second.history)
    assert any(role == "assistant" for role, _content in second.history)
    rows = agent.conversation_store.recent_messages(second.thread_id, limit=10)
    assert [(row.role, row.metadata["gateway_request_id"]) for row in rows] == [
        ("user", "gw-1"),
        ("assistant", "gw-1"),
    ]


def test_gateway_model_history_keeps_long_message_tail_instead_of_ui_preview(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            conversation_history_message_max_chars=1200,
            conversation_history_max_chars=5000,
        ),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_long",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-long-1", "长内容")
    _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu", "message_id": "om-long"}},
        first,
        request_id="gw-long-1",
        role="assistant",
        content="开头" + "中间" * 2000 + "最终产物在 /output/final-report.md，暗号是青黛。",
    )

    followup = _conversation_context(agent, request, "gw-long-2", "产物在哪？")
    history_text = "\n".join(content for _role, content in followup.history)
    assert "中间内容已折叠" in history_text
    assert "/output/final-report.md" in history_text
    assert "暗号是青黛" in history_text


def test_delivery_protocol_is_projected_and_prior_artifact_is_reused_structurally(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_delivery",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-delivery-1", "生成一份周报")
    artifact = first.task_workspace or str(tmp_path / "owner" / "output" / "weekly.xlsx")
    raw = (
        '[MAIN_AGENT_DELIVERY_COMPLETE]\n{"ok":true,'
        f'"user_summary":"周报已整理好，文件是 {artifact.rsplit("/", 1)[-1]}。","artifacts":['
        f'{{"artifact_id":"weekly_report","kind":"xlsx","path":{json.dumps(artifact)},"ok":true}}]}}'
        "\n[/MAIN_AGENT_DELIVERY_COMPLETE]\n交付验收通过。"
    )
    projection = project_user_reply(raw)
    assert projection.content == f"周报已整理好，文件是 {artifact.rsplit('/', 1)[-1]}。"
    assert "MAIN_AGENT" not in projection.content
    assert artifact not in projection.content
    _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        first,
        request_id="gw-delivery-1",
        role="assistant",
        content=projection.content,
        delivery_artifacts=projection.artifacts,
    )

    followup = _conversation_context(agent, request, "gw-delivery-2", "发我")
    section = _gateway_injections({"inject": []}, followup)[0]

    assert followup.history[-1] == ("assistant", projection.content)
    assert followup.recent_artifacts[0]["artifact_id"] == "weekly_report"
    assert followup.recent_artifacts[0]["path"] == artifact
    assert "直接调用 send_message" in section
    assert "不要重新搜索、复制或制作一遍" in section
    assert "MAIN_AGENT_DELIVERY_COMPLETE" not in section


def test_gateway_response_uses_user_projection_instead_of_internal_result() -> None:
    raw = (
        '[MAIN_AGENT_DELIVERY_COMPLETE]\n{"user_summary":"报告已经整理好。","artifacts":['
        '{"artifact_id":"report","kind":"pdf","path":"/owner/private/report.pdf","ok":true}]}'
        "\n[/MAIN_AGENT_DELIVERY_COMPLETE]"
    )
    result = SimpleNamespace(
        response=raw,
        backend="fake",
        used_memories=0,
        tool_rounds=1,
        prompt="",
        prompt_token_estimate=10,
        runtime_injection_token_estimate=2,
        turn_token_estimate=12,
        cumulative_token_estimate=12,
        memory_resume_context_injected=False,
        memory_resume_context_query="",
        memory_resume_context_matches=0,
        memory_resume_context_token_estimate=0,
        memory_resume_context_error="",
        channel_delivery=project_user_reply(raw).to_dict(),
    )
    response: dict[str, object] = {}

    _update_response_from_result(response, result, {})

    assert response["response"] == "报告已经整理好。"
    assert "MAIN_AGENT" not in str(response["response"])
    assert response["channel_delivery"]["internal_signal"] is True
    assert "/owner/private" not in json.dumps(response, ensure_ascii=False)


def test_delivery_projection_preserves_completion_summary_but_sanitizes_host_paths() -> None:
    raw = (
        '[MAIN_AGENT_DELIVERY_COMPLETE]\n{"user_summary":'
        '"青岚系统已完成，25 项测试全部通过；报告位于 /root/private/report.pdf。",'
        '"artifacts":[{"artifact_id":"report","kind":"pdf",'
        '"path":"/root/private/report.pdf","ok":true}]}\n'
        "[/MAIN_AGENT_DELIVERY_COMPLETE]"
    )

    projection = project_user_reply(raw)

    assert "青岚系统已完成" in projection.content
    assert "25 项测试全部通过" in projection.content
    assert "report.pdf" in projection.content
    assert "/root/private" not in projection.content
    assert "MAIN_AGENT" not in projection.content


def test_delivery_projection_preserves_relative_paths_without_mangling_slashes() -> None:
    raw = (
        '[MAIN_AGENT_DELIVERY_COMPLETE]\n{"user_summary":'
        '"产物在 tasks/demo/output/report.md，依赖位于 libs/agents/core.py；另见 ./notes/today.md。",'
        '"artifacts":[]}\n[/MAIN_AGENT_DELIVERY_COMPLETE]'
    )

    projection = project_user_reply(raw)

    assert "tasks/demo/output/report.md" in projection.content
    assert "libs/agents/core.py" in projection.content
    assert "./notes/today.md" in projection.content
    assert "tasksoutput" not in projection.content


def test_delivery_projection_removes_executed_tool_envelope_but_keeps_public_text() -> None:
    raw = (
        "我已经把任务转到后台。\n"
        '[TOOL_CALL]\n{"tool":"wait","seconds":120}\n[/TOOL_CALL]\n'
        "有新进展时会通知你。"
    )

    projection = project_user_reply(raw)

    assert projection.content == "我已经把任务转到后台。\n\n有新进展时会通知你。"
    assert "TOOL_CALL" not in projection.content


def test_delivery_projection_discards_summary_containing_internal_protocol() -> None:
    raw = (
        '[MAIN_AGENT_DELIVERY_COMPLETE]\n{"user_summary":'
        '"已完成 [TOOL_CALL] {\\"tool\\":\\"run_command\\"}",'
        '"artifacts":[{"artifact_id":"report","path":"/private/report.pdf","ok":true}]}\n'
        "[/MAIN_AGENT_DELIVERY_COMPLETE]"
    )

    projection = project_user_reply(raw)

    assert projection.content == ""
    assert "TOOL_CALL" not in projection.content
    assert projection.artifacts[0]["name"] == "report.pdf"


def test_gateway_chat_history_isolated_by_real_conversation_id(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "session-1",
            "channel_user_id": "local-cli",
            "canonical_user_id": "local-agent",
        }
    }
    first = _conversation_context(agent, request, "gw-first", "只属于会话一")
    _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "chat", "message_id": "m1"}},
        first,
        request_id="gw-first",
        role="user",
        content="只属于会话一",
    )
    other = {"conversation": {**request["conversation"], "channel_conversation_id": "session-2"}}
    second = _conversation_context(agent, other, "gw-second", "这是会话二")
    assert second.thread_id != first.thread_id
    assert second.history == ()


def test_gateway_conversation_turns_do_not_leak_into_owner_global_memory(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    first_conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_chat_a",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            {
                "prompt": "项目代号海棠",
                "save": True,
                "source": "http:feishu",
                "conversation": first_conversation,
            },
            tmp_path / "req-a.json",
            tmp_path / "resp-a.json",
            "gw-a",
            lambda _chunk: None,
        )
    )

    second = _conversation_context(
        agent,
        {
            "conversation": {
                **first_conversation,
                "channel_conversation_id": "oc_chat_b",
            }
        },
        "gw-b",
        "另一个聊天",
    )
    assert second.history == ()
    assert all("项目代号海棠" not in row.content for row in agent.memory.all())


def test_gateway_fails_closed_before_model_when_user_turn_cannot_persist(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    monkeypatch.setattr(
        agent.conversation_store,
        "append_message",
        lambda _request: (_ for _ in ()).throw(OSError("disk full")),
    )
    request = {
        "prompt": "这一轮不能丢",
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_fail",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        },
    }

    with pytest.raises(ConversationPersistenceError):
        _run_gateway_ask(
            _GatewayAskRunContext(
                agent,
                request,
                tmp_path / "req-fail.json",
                tmp_path / "resp-fail.json",
                "gw-fail",
                lambda _chunk: None,
            )
        )
    assert agent.memory.all() == []


def test_gateway_returns_answer_and_repairs_assistant_transcript_on_next_turn(
    tmp_path, monkeypatch
):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    original_append = agent.conversation_store.append_message
    calls = {"count": 0}

    def flaky_append(request):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("temporary assistant ledger failure")
        return original_append(request)

    monkeypatch.setattr(agent.conversation_store, "append_message", flaky_append)
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_repair",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    result = _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            {"prompt": "请记住海棠", "conversation": conversation},
            tmp_path / "req-repair.json",
            tmp_path / "resp-repair.json",
            "gw-repair",
            lambda _chunk: None,
        )
    )
    assert result.response
    assert result.conversation_persist_degraded is True
    repair_path = agent.conversation_store.root / "message_repairs" / "gw-repair-assistant.json"
    assert repair_path.exists()

    monkeypatch.setattr(agent.conversation_store, "append_message", original_append)
    next_turn = _conversation_context(
        agent,
        {"conversation": conversation},
        "gw-next",
        "刚才说了什么？",
    )
    rows = agent.conversation_store.recent_messages(next_turn.thread_id, limit=10)
    assert [(row.role, row.metadata["gateway_request_id"]) for row in rows] == [
        ("user", "gw-repair"),
        ("assistant", "gw-repair"),
    ]
    assert not repair_path.exists()


def test_gateway_authoritative_transcript_filters_legacy_dialogue_but_keeps_preferences(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    agent.memory.add("user", "串线令牌海棠", kind="dialogue")
    agent.memory.add("user", "偏好令牌青黛", kind="preference")

    prepared = _prepare_runtime_context(
        agent,
        RuntimeContextRequest(
            user_prompt="令牌海棠 青黛",
            inject=[],
            resume_context=False,
            task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
        ),
    )

    assert [row.kind for row in prepared.memories] == ["preference"]
    assert prepared.memories[0].content == "偏好令牌青黛"


def test_authoritative_transcript_overfetches_past_legacy_dialogue_for_preferences(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
            memory_top_k=5,
        ),
        tmp_path,
    )
    for index in range(5):
        agent.memory.add("user", f"海棠旧对话 {index}", kind="dialogue")
    agent.memory.add("user", "海棠偏好：回答简短", kind="preference")

    prepared = _prepare_runtime_context(
        agent,
        RuntimeContextRequest(
            user_prompt="海棠",
            inject=[],
            resume_context=False,
            task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
        ),
    )

    assert [row.kind for row in prepared.memories] == ["preference"]
    assert prepared.memories[0].content == "海棠偏好：回答简短"


def test_gateway_chat_does_not_inject_existing_active_task_without_task_ref(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "session-1",
            "channel_user_id": "local-cli",
            "canonical_user_id": "local-agent",
        }
    }

    first = _conversation_context(agent, request, "gw-first", "普通聊天")
    agent.conversation_store.bind_task(
        {"thread_id": first.thread_id, "task_id": "task-old", "goal": "旧任务", "status": "active"}
    )
    second = _conversation_context(agent, request, "gw-second", "滴滴滴")
    assert second.thread_id == first.thread_id
    assert second.active_task_id == ""
    assert _root_user_prompt("滴滴滴", second) == "滴滴滴"
    section = _gateway_injections({"inject": []}, second)[0]
    assert "Active Work Candidates" in section
    assert "旧任务" in section
    assert "active_root_task_id" not in section


def test_model_can_select_active_conversation_task_without_overwriting_goal_or_workspace(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_task",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-chat", "普通聊天")
    workspace = tmp_path / "existing-task"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-old",
            "goal": "整理季度报告",
            "status": "active",
            "task_path": str(workspace),
        }
    )
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        root_user_prompt="继续刚才的工作",
        task_attributes={
            "conversation_thread_id": conversation.thread_id,
            "conversation_lane": "chat",
        },
    )
    agent._current_run_params = params
    try:
        selected = TaskProgressTool(agent).execute(
            {"action": "select", "run_id": "task-old"}
        )
        TaskProgressTool(agent).execute(
            {"action": "update", "summary": "继续整理中"}
        )
        reused = promote_current_conversation_task(agent, goal="不应覆盖旧目标")
    finally:
        delattr(agent, "_current_run_params")

    assert selected.ok is True
    assert params.task_attributes["conversation_task_id"] == "task-old"
    assert params.task_attributes["run_workspace"]["task_root"] == str(workspace)
    progress = json.loads(TaskProgressTool(agent).execute({"action": "read", "run_id": "task-old"}).output)
    assert progress["summary"] == "继续整理中"
    assert reused.goal == "整理季度报告"
    assert reused.task_path == str(workspace)


def test_background_promotion_reuses_link_workspace_without_synthetic_wake_directory(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_background",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "做图书馆运营方案")
    workspace = tmp_path / "home" / "owners" / "users" / "ou_user1" / "tasks" / "图书馆运营方案"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-library",
            "goal": "做图书馆运营方案",
            "status": "active",
            "task_path": str(workspace),
        }
    )
    params = RunParams(
        request_id="bg-main-1",
        run_id="bg-main-1",
        task_id="task-library",
        root_user_prompt="定时唤醒：继续处理等待事项",
        task_attributes={
            "conversation_thread_id": conversation.thread_id,
            "conversation_task_id": "task-library",
            "conversation_lane": "task",
        },
    )
    agent._current_run_params = params
    try:
        reused = promote_current_conversation_task(agent, goal="定时唤醒：继续处理等待事项")
    finally:
        delattr(agent, "_current_run_params")

    assert reused is not None
    assert params.task_attributes["run_workspace"]["task_root"] == str(workspace)
    assert agent._current_run_task_workspace == str(workspace)
    stored = agent.conversation_store.active_task_links_report(conversation.thread_id)[0][0]
    assert stored.goal == "做图书馆运营方案"
    assert stored.task_path == str(workspace)
    assert not any("定时唤醒" in path.name for path in workspace.parent.iterdir())


def test_selected_conversation_task_is_inherited_by_new_subagents(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_selected",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-chat", "普通聊天")
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-old",
            "goal": "继续既有工作",
            "status": "active",
        }
    )
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        root_user_prompt="继续做",
        task_attributes={"conversation_thread_id": conversation.thread_id},
    )
    agent._current_run_params = params
    try:
        assert TaskProgressTool(agent).execute({"action": "select", "run_id": "task-old"}).ok
        child_attrs: dict[str, object] = {}
        add_current_conversation_attrs(child_attrs, agent)
    finally:
        delattr(agent, "_current_run_params")
    assert child_attrs["conversation_thread_id"] == conversation.thread_id
    assert child_attrs["conversation_task_id"] == "task-old"


def test_completed_conversation_task_disappears_from_chat_candidates(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_complete",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-work", "帮我做一份报告")
    agent.conversation_store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "task-done",
            "goal": "做一份报告",
            "status": "active",
        }
    )
    attrs = {
        "conversation_thread_id": first.thread_id,
        "conversation_task_id": "task-done",
        "conversation_lane": "task",
    }
    assert complete_current_conversation_task(agent, attrs, source="gateway") is True
    thread = agent.conversation_store.load_thread(first.thread_id)
    assert thread is not None and "task-done" not in thread.active_task_ids
    second = _conversation_context(agent, request, "gw-next", "聊点别的")
    assert all(task_id != "task-done" for task_id, _goal, _path in second.task_candidates)
    assert any(
        task_id == "task-done"
        for task_id, _goal, _path in second.completed_task_candidates
    )
    section = _gateway_injections({"inject": []}, second)[0]
    assert "Recent Completed Work" in section
    assert "任何文件操作前" in section


def test_internal_child_links_never_appear_as_user_task_candidates(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_hide_children",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "做一个项目")
    for task_id, status in (("subagent-child", "active"), ("bg-main-child", "completed")):
        agent.conversation_store.bind_task(
            {
                "thread_id": conversation.thread_id,
                "task_id": task_id,
                "goal": "内部任务",
                "status": "active",
            }
        )
        if status == "completed":
            agent.conversation_store.update_task_status({"task_id": task_id, "status": status})

    loaded = _conversation_context(agent, request, "gw-next", "继续")

    candidates = [*loaded.task_candidates, *loaded.completed_task_candidates]
    assert all(not task_id.startswith(("subagent-", "bg-main-")) for task_id, _goal, _path in candidates)


def test_model_can_reopen_completed_task_and_supersede_new_placeholder(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_resume_completed",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "先完成第一步")
    workspace = tmp_path / "completed-task"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-completed",
            "goal": "校园交易网站第一步",
            "status": "active",
            "task_path": str(workspace),
        }
    )
    agent.conversation_store.update_task_status(
        {"task_id": "task-completed", "status": "completed"}
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "gw-followup",
            "goal": "继续第二步",
            "status": "active",
            "task_path": str(tmp_path / "placeholder"),
        }
    )
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        root_user_prompt="继续做第二步",
        task_attributes={
            "conversation_thread_id": conversation.thread_id,
            "conversation_task_id": "gw-followup",
            "conversation_lane": "task",
            "run_workspace": {
                "task_root": str(tmp_path / "placeholder"),
                "output_dir": str(tmp_path / "placeholder" / "output"),
                "work_dir": str(tmp_path / "placeholder" / "work"),
            },
        },
    )
    agent._current_run_params = params
    agent._current_run_task_workspace = str(tmp_path / "placeholder")
    try:
        selected = TaskProgressTool(agent).execute(
            {"action": "select", "run_id": "task-completed"}
        )
    finally:
        delattr(agent, "_current_run_params")

    assert selected.ok is True
    assert params.task_attributes["conversation_task_id"] == "task-completed"
    assert params.task_attributes["run_workspace"]["task_root"] == str(workspace)
    assert params.task_attributes["conversation_rebase_from_task_root"] == str(tmp_path / "placeholder")
    assert agent._current_run_task_workspace == str(workspace)
    links = {
        link.task_id: link.status
        for link in agent.conversation_store.task_links(conversation.thread_id)
    }
    assert links["task-completed"] == "active"
    assert links["gw-followup"] == "superseded"


def test_progress_update_requires_structured_workspace_decision_when_candidates_exist(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_workspace_decision",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "先做一期")
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-old",
            "goal": "已完成的一期",
            "status": "active",
        }
    )
    agent.conversation_store.update_task_status({"task_id": "task-old", "status": "completed"})
    params = RunParams(
        request_id="gw-new",
        run_id="gw-new",
        task_id="gw-new",
        root_user_prompt="另做一个全新项目",
        task_attributes={"conversation_thread_id": conversation.thread_id},
    )
    agent._current_run_params = params
    try:
        blocked = TaskProgressTool(agent).execute({"action": "update", "summary": "开工"})
        started = TaskProgressTool(agent).execute({"action": "start"})
        updated = TaskProgressTool(agent).execute({"action": "update", "summary": "开工"})
    finally:
        delattr(agent, "_current_run_params")

    assert blocked.ok is False
    assert blocked.error_code == "CONVERSATION_WORKSPACE_DECISION_REQUIRED"
    assert "task-old" in blocked.output
    assert started.ok is True
    assert updated.ok is True
    assert params.task_attributes["conversation_task_id"] == "gw-new"


def test_subagent_completion_cannot_close_parent_conversation_task(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_parent_active",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-parent", "帮我完成一个需要分工的项目")
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-parent",
            "goal": "完成整个项目",
            "status": "active",
        }
    )
    inherited_attrs = {
        "conversation_thread_id": conversation.thread_id,
        "conversation_task_id": "task-parent",
        "conversation_lane": "task",
    }

    assert (
        complete_current_conversation_task(
            agent,
            inherited_attrs,
            source="subagent_run_model_turn",
        )
        is False
    )
    thread = agent.conversation_store.load_thread(conversation.thread_id)
    assert thread is not None and "task-parent" in thread.active_task_ids
    links = agent.conversation_store.active_task_links_report(conversation.thread_id)[0]
    assert links[0].status == "active"


def test_subagent_completion_can_close_its_exact_own_conversation_link(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_child_own_link",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-parent", "完成一个需要分工的项目")
    child_id = "subagent-child-1"
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": child_id,
            "goal": "完成子模块",
            "status": "active",
        }
    )
    attrs = {
        "conversation_thread_id": conversation.thread_id,
        "conversation_task_id": child_id,
        "conversation_lane": "task",
    }

    assert complete_current_conversation_task(
        agent,
        attrs,
        source="subagent_run_model_turn",
        current_task_id=child_id,
    )
    links = agent.conversation_store.active_task_links_report(conversation.thread_id)[0]
    assert all(link.task_id != child_id for link in links)


def test_explicit_task_lane_and_task_ref_restore_workspace(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    chat_request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "session-1",
            "channel_user_id": "local-cli",
            "canonical_user_id": "local-agent",
        }
    }
    first = _conversation_context(agent, chat_request, "gw-chat", "普通聊天")
    assert first.thread_id
    workspace = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-03" / "analysis"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "task-old",
            "goal": "分析 all-agent 项目",
            "status": "active",
            "task_path": str(workspace),
        }
    )
    task_request = {
        "conversation": {**chat_request["conversation"], "lane": "task", "task_ref": "task-old"}
    }
    followup = _conversation_context(agent, task_request, "gw-task", "汇总这个任务")
    assert followup.thread_id == first.thread_id
    assert followup.lane == "task"
    assert followup.active_task_id == "task-old"
    assert followup.task_workspace == str(workspace)
    assert _root_user_prompt("汇总这个任务", followup) == "汇总这个任务"
    section = _gateway_injections({"inject": []}, followup)[0]
    assert "active_root_task_id: task-old" in section
    assert "分析 all-agent 项目" in section


def test_structured_task_tool_promotes_natural_language_chat_internally(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_chat1",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-work", "帮我整理这份报告")
    params = RunParams(
        request_id="gw-work",
        run_id="gw-work",
        task_id="gw-work",
        root_user_prompt="帮我整理这份报告",
        task_attributes={
            "conversation_thread_id": conversation.thread_id,
            "conversation_lane": "chat",
        },
    )
    agent._current_run_params = params
    try:
        link = promote_current_conversation_task(agent)
    finally:
        delattr(agent, "_current_run_params")
    assert link is not None and link.task_id == "gw-work"
    assert params.task_attributes["conversation_lane"] == "task"
    assert params.task_attributes["conversation_task_id"] == "gw-work"
    stored = agent.conversation_store.thread_for_task("gw-work")
    assert stored.thread_id == conversation.thread_id
    link = agent.conversation_store.active_task_links_report(conversation.thread_id)[0][0]
    assert link.task_path
    assert params.task_attributes["run_workspace"]["task_root"] == link.task_path
    assert agent._current_run_task_workspace == link.task_path


def test_explicit_special_mode_still_enters_task_lane(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_chat1",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-audit", "/audit 持续检查日志")
    assert conversation.lane == "task"
    assert conversation.active_task_id == "gw-audit"


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
                "conversation_lane": "task",
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


def test_background_child_archive_cannot_overwrite_parent_goal_workspace_or_index_title(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    conversation = _conversation_context(
        agent,
        {
            "conversation": {
                "channel": "feishu",
                "channel_conversation_id": "oc_archive",
                "channel_user_id": "ou_user1",
                "canonical_user_id": "ou_user1",
            }
        },
        "gw-parent",
        "做图书馆运营方案",
    )
    workspace = tmp_path / "home" / "owners" / "users" / "ou_user1" / "tasks" / "图书馆运营方案"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-library",
            "goal": "做图书馆运营方案",
            "status": "active",
            "task_path": str(workspace),
        }
    )

    written = write_run_task_workspace_if_needed(
        agent,
        ArchiveRunParams(
            do_save=True,
            user_prompt="[SubAgent Runner Task] 定时唤醒内部执行提示，不得覆盖父任务",
            final_response=None,
            archive_tool_calls=[],
            run_request_id="child-request",
            run_id="subagent-child",
            task_id="subagent-child",
            source="subagent_run_model_turn",
            task_attributes={
                "conversation_thread_id": conversation.thread_id,
                "conversation_task_id": "task-library",
                "conversation_lane": "task",
                "run_workspace": {
                    "task_root": str(workspace),
                    "output_dir": str(workspace / "output"),
                    "work_dir": str(workspace / "work"),
                },
            },
        ),
    )

    assert written == str(workspace)
    stored = agent.conversation_store.active_task_links_report(conversation.thread_id)[0][0]
    assert stored.goal == "做图书馆运营方案"
    assert stored.task_path == str(workspace)
    refs = latest_task_refs(agent.home_paths, owner_id=agent.home_paths.owner_id)
    parent = next(ref for ref in refs if ref["task_id"] == "task-library")
    assert parent["title"] == "做图书馆运营方案"
    assert parent["task_path"] == str(workspace)


def test_gateway_followup_subagent_lineage_uses_active_task_root(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)

    params = RunParams(
        request_id="gw-second",
        run_id="gw-second",
        task_id="gw-second",
        task_attributes={"conversation_task_id": "gw-first"},
    )
    agent._current_run_params = params
    try:
        create_params = create_run_params(
            agent,
            {"goal": "补齐 pi-main 分析", "allowed_tools": ["read_file"]},
            "补齐 pi-main 分析",
            ["read_file"],
        )
    finally:
        delattr(agent, "_current_run_params")

    assert create_params.parent_id == "gw-first"
    assert create_params.root_id == "gw-first"
    assert create_params.depth == 1


def test_gateway_subagent_records_originating_conversation_request(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    agent._current_run_params = RunParams(request_id="gw-current")
    try:
        create_params = create_run_params(
            agent,
            {"goal": "整理资料", "allowed_tools": ["read_file"]},
            "整理资料",
            ["read_file"],
        )
    finally:
        delattr(agent, "_current_run_params")

    assert create_params.attributes[CONVERSATION_REQUEST_ID_ATTR] == "gw-current"


def test_gateway_followup_delivery_contract_uses_active_task_goal(tmp_path):
    root = tmp_path / "all-agent"
    (root / "ECC-main").mkdir(parents=True)
    (root / "pi-main").mkdir()
    (root / "output" / "reports").mkdir(parents=True)
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), root)

    params = run_params_with_materialized_delivery_contract(
        agent,
        "刚才帮手完成了，现在请汇总草稿。",
        RunParams(
            root_user_prompt=(
                f"请阅读 {root} 下的项目源码，优先看 ECC-main、pi-main，"
                f"最后把报告写到 {root / 'output' / 'reports' / 'report.md'}。"
            ),
        ),
    )

    coverage = params.delivery_contract["target_coverage_contract"]
    assert [item["target_id"] for item in coverage["target_items"]] == ["ECC-main", "pi-main"]


def _conversation_context(agent: SimpleAgent, request: dict, request_id: str, prompt: str):
    return _gateway_conversation_context(_GatewayConversationLoadRequest(agent, request, request_id, prompt))
