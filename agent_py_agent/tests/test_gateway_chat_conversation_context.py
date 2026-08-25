from __future__ import annotations

import json
import threading
import time
from functools import partial
from pathlib import Path
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
from agent_py_agent.agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import RunParams, _prepare_runtime_context
from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    _promote_conversation_task_for_work_tool,
)
from agent_py_agent.agent.common.audit_activation import AUDIT_ATTR, AUDIT_DEADLINE_ATTR
from agent_py_agent.agent.conversation.audit_lifecycle import (
    audit_scope_payload,
    project_audit_runtime_attributes,
    start_named_audit,
)
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
    CONVERSATION_WORKSPACE_TASK_ID_ATTR,
    CONVERSATION_WORKSPACE_TASK_STATUS_ATTR,
)
from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.agent.conversation.control_commands import (
    parse_conversation_control,
)
from agent_py_agent.agent.conversation.task_promotion import (
    complete_current_conversation_task,
    complete_named_audit_task_if_settled,
    conversation_workspace_execution_blocker,
    promote_current_conversation_task,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_errors import (
    ConversationPersistenceError,
    UserReplyUnavailableError,
)
from agent_py_agent.agent.gateway_parts.request_execution import (
    BufferedChunkStreamWriter,
    GatewayWorkspaceScopeError,
    _append_gateway_conversation_message,
    _gateway_conversation_context,
    _gateway_injections,
    _gateway_run_params,
    _gateway_run_task_attributes,
    _gateway_task_attributes,
    _GatewayAskRunContext,
    _GatewayConversationContext,
    _GatewayConversationLoadRequest,
    _GatewayRunParamsRequest,
    _GatewayWorkspaceSelection,
    _register_named_system_task,
    _run_gateway_ask,
    _update_response_from_result,
)
from agent_py_agent.agent.gateway_parts.request_worker import GatewayAskParams, submit_gateway_ask
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_indexes import latest_task_refs
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call


def _promote_work_tool(
    agent: SimpleAgent,
    params: RunParams,
    payload: dict[str, object],
):
    snapshot = agent.tools.runtime_snapshot(run_id=params.run_id)
    params.tool_runtime_snapshot = snapshot
    arguments = dict(payload)
    tool_name = str(arguments.pop("tool"))
    call = canonical_test_call(snapshot, tool_name, arguments)
    return _promote_conversation_task_for_work_tool(
        ToolCallRuntimeRequest(
            agent=agent,
            request=SimpleNamespace(params=params),
            call=call,
        )
    )


def test_gateway_chat_request_payload_carries_session_conversation(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
    assert payload["conversation"]["channel_user_id"] == "local-agent"


def test_gateway_cli_request_payload_carries_default_local_conversation(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
    assert payload["conversation"]["channel_user_id"] == "local-agent"


def test_gateway_thread_uses_validated_client_cwd_and_keeps_it_on_next_turn(tmp_path):
    service_root = tmp_path / "service"
    project_root = tmp_path / "project"
    project_root.mkdir()
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        service_root,
    )
    conversation = {
        "channel": "chat",
        "channel_conversation_id": "cwd-session",
        "channel_user_id": "local-agent",
        "canonical_user_id": "local-agent",
    }

    first = _conversation_context(
        agent,
        {
            "conversation": conversation,
            "workspace": {
                "cwd": str(project_root),
                "roots": [str(project_root)],
            },
        },
        "gw-cwd-first",
        "在当前目录开始",
    )
    second = _conversation_context(
        agent,
        {"conversation": conversation},
        "gw-cwd-second",
        "继续",
    )
    attrs = _gateway_task_attributes(second)

    assert first.cwd == str(project_root.resolve())
    assert second.cwd == str(project_root.resolve())
    assert second.runtime_workspace_roots == (str(project_root.resolve()),)
    assert attrs[CONVERSATION_EXECUTION_CWD_ATTR] == str(project_root.resolve())
    assert attrs[CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR] == [
        str(project_root.resolve())
    ]


def test_gateway_rejects_relative_client_cwd_before_conversation_turn(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path / "service",
    )

    with pytest.raises(GatewayWorkspaceScopeError, match="必须是绝对目录"):
        _conversation_context(
            agent,
            {
                "conversation": {
                    "channel": "chat",
                    "channel_conversation_id": "invalid-cwd",
                    "channel_user_id": "local-agent",
                    "canonical_user_id": "local-agent",
                },
                "workspace": {"cwd": "relative/project", "roots": []},
            },
            "gw-invalid-cwd",
            "不要在错误目录执行",
        )


def test_gateway_rejects_client_cwd_outside_declared_runtime_roots(tmp_path):
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path / "service",
    )

    with pytest.raises(GatewayWorkspaceScopeError, match="不在声明的 roots 内"):
        _conversation_context(
            agent,
            {
                "conversation": {
                    "channel": "chat",
                    "channel_conversation_id": "cwd-outside-roots",
                    "channel_user_id": "local-agent",
                    "canonical_user_id": "local-agent",
                },
                "workspace": {"cwd": str(project), "roots": [str(other)]},
            },
            "gw-cwd-outside-roots",
            "不要越过结构化 roots",
        )


def test_named_audit_projects_durable_task_deadline_into_current_run(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    command = parse_conversation_control("/audit 10m audit-smoke 逐条检查五路日志")
    assert command is not None and command.valid
    request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "session-audit",
            "channel_user_id": "local-agent",
            "canonical_user_id": "local-agent",
        },
    }
    conversation = _conversation_context(
        agent,
        request,
        "gw-audit",
        command.value,
    )
    link = start_named_audit(
        agent,
        agent.conversation_store,
        thread_id=conversation.thread_id,
        work_name=command.name,
        prompt=command.value,
        duration_seconds=int(command.duration_seconds or 0),
    )
    attrs = project_audit_runtime_attributes(
        {AUDIT_ATTR: True},
        audit_scope_payload(link),
        thread_id=conversation.thread_id,
        turn_request_id="gw-audit",
    )
    assert link.task_id.startswith("audit-")
    assert Path(link.task_path).parent.name == "audits"
    deadline = attrs[AUDIT_DEADLINE_ATTR]
    assert deadline == link.expires_at
    assert 590 <= deadline - link.created_at <= 610
    assert attrs["conversation_task_id"] == link.task_id
    assert attrs[CONVERSATION_TASK_TURN_ACTIVE_ATTR] is True
    assert attrs[CONVERSATION_TRANSIENT_WORKSPACE_ATTR] is True
    assert attrs["run_workspace"]["task_root"] == link.task_path


def test_gateway_chat_reuses_thread_but_does_not_auto_bind_task(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
    assert agent.conversation_store.thread_for_task("gw-second") is None
    section = _gateway_injections({"inject": []}, second)[0]
    assert "你好，我叫小叶子" in section
    assert "你好，小叶子。" in section
    assert "active_root_task_id" not in section


def test_interleaved_named_audit_prepare_history_keeps_exact_scope(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    conversation_spec = {
        "channel": "feishu",
        "channel_conversation_id": "oc-interleaved-audits",
        "channel_user_id": "ou-interleaved-owner",
        "canonical_user_id": "ou-interleaved-owner",
    }

    def prepare_request(name: str, request_id: str, prompt: str) -> tuple[dict, object]:
        request = {
            "id": request_id,
            "goal": prompt,
            "conversation": conversation_spec,
            "system_task": {
                "kind": "audit_prepare",
                "attributes": {
                    "conversation_audit_prepare": True,
                    "conversation_cancellation_scope": "foreground",
                    "conversation_work_kind": "audit",
                    "conversation_work_name": name,
                },
            },
        }
        initial = _gateway_conversation_context(
            _GatewayConversationLoadRequest(agent, request, request_id, prompt)
        )
        context = _GatewayAskRunContext(
            agent,
            request,
            tmp_path / f"{request_id}.json",
            tmp_path / f"{request_id}.response.json",
            request_id,
            lambda _chunk: None,
        )
        _register_named_system_task(context, initial, prompt)
        return request, initial

    request_a, conversation = prepare_request("审计A", "prepare-a-1", "A 的确认条件是 alpha")
    _append_gateway_conversation_message(
        agent,
        request_a,
        conversation,
        request_id="prepare-a-1",
        role="user",
        content="A 的确认条件是 alpha",
    )
    _append_gateway_conversation_message(
        agent,
        request_a,
        conversation,
        request_id="prepare-a-1",
        role="assistant",
        content="A 已记录 alpha",
    )

    request_b, _ = prepare_request("审计B", "prepare-b-1", "B 的确认条件是 beta")
    _append_gateway_conversation_message(
        agent,
        request_b,
        conversation,
        request_id="prepare-b-1",
        role="user",
        content="B 的确认条件是 beta",
    )
    _append_gateway_conversation_message(
        agent,
        request_b,
        conversation,
        request_id="prepare-b-1",
        role="assistant",
        content="B 已记录 beta",
    )
    _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        conversation,
        request_id="ordinary-between-audits",
        role="user",
        content="普通聊天仍应对两个准备轮可见",
    )

    request_a2, _ = prepare_request("审计A", "prepare-a-2", "继续完善 A")
    current_a = _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request_a2, "prepare-a-2", "继续完善 A")
    )
    history_a = "\n".join(content for _role, content in current_a.history)
    assert "A 的确认条件是 alpha" in history_a
    assert "A 已记录 alpha" in history_a
    assert "普通聊天仍应对两个准备轮可见" in history_a
    assert "B 的确认条件是 beta" not in history_a
    assert "B 已记录 beta" not in history_a

    rows = agent.conversation_store.recent_messages(conversation.thread_id, limit=20)
    row_a = next(row for row in rows if row.metadata.get("gateway_request_id") == "prepare-a-1")
    row_b = next(row for row in rows if row.metadata.get("gateway_request_id") == "prepare-b-1")
    assert row_a.metadata["conversation_task_id"] != row_b.metadata["conversation_task_id"]
    assert row_a.metadata["conversation_work_name"] == "审计A"
    assert row_b.metadata["conversation_work_name"] == "审计B"
    assert row_a.metadata["conversation_audit_prepare"] is True

    ordinary = _gateway_conversation_context(
        _GatewayConversationLoadRequest(
            agent,
            {"conversation": conversation_spec},
            "ordinary-after-audits",
            "我们聊聊刚才的准备",
        )
    )
    ordinary_history = "\n".join(content for _role, content in ordinary.history)
    assert "A 的确认条件是 alpha" in ordinary_history
    assert "B 的确认条件是 beta" in ordinary_history


def test_audit_prepare_projection_does_not_use_global_compact_or_sibling_artifacts() -> None:
    conversation = _GatewayConversationContext(
        thread_id="thread-audit-scope",
        compact_summary="旧 Audit 的 captured 规则",
        compact_operation_evidence={"events": [{"tool": "publish_audit_update"}]},
        recent_operation_evidence={"events": [{"tool": "publish_audit_update"}]},
        history=(("user", "当前 Audit 自己的普通上下文"),),
        recent_artifacts=({"path": "/tmp/old-audit-profile.md"},),
        workspace_task=_GatewayWorkspaceSelection(
            task_id="ordinary-task",
            status="completed",
            goal="旧普通任务",
            task_path="/tmp/ordinary-task",
        ),
        subagent_completions={
            "schema": "conversation-subagent-completions.v1",
            "items": [{"task_id": "ordinary-child", "completion_message": "旧子代理交付"}],
        },
        thread_goal={"name": "旧目标", "objective": "无关目标"},
    )
    request = {
        "system_task": {
            "kind": "audit_prepare",
            "attributes": {
                "conversation_audit_prepare": True,
                "conversation_work_kind": "audit",
                "conversation_work_name": "当前Audit",
            },
        },
        "conversation_audit_scope": {
            "audit_id": "audit-current",
            "name": "当前Audit",
            "status": "preparing",
            "pending_prompt": "当前要求",
        },
    }

    section = _gateway_injections(request, conversation)[0]
    assert "当前 Audit 自己的普通上下文" in section
    assert "旧 Audit 的 captured 规则" not in section
    assert "old-audit-profile.md" not in section
    assert "旧普通任务" not in section
    assert "旧子代理交付" not in section
    assert "无关目标" not in section


def _completion_followup_fixture(tmp_path: Path) -> tuple[SimpleAgent, dict, str]:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "child-completion-followup",
            "channel_user_id": "local-agent",
            "canonical_user_id": "local-agent",
        }
    }
    first = _conversation_context(agent, request, "gw-root", "派子代理调研")
    workspace = tmp_path / "root-workspace"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "root-task",
            "goal": "多子代理调研",
            "status": "interrupted",
            "task_path": str(workspace),
        }
    )
    agent.conversation_store.select_workspace_task(
        {"thread_id": first.thread_id, "task_id": "root-task"}
    )
    return agent, request, first.thread_id


def _append_completion_observation(
    agent: SimpleAgent,
    thread_id: str,
    *,
    task_id: str,
    parent_id: str,
    root_id: str,
    status: str,
    message: str,
    now: float,
) -> None:
    agent.conversation_store.append_observation(
        {
            "thread_id": thread_id,
            "event_type": "subagent_runner_finished",
            "source_agent_id": task_id,
            "parent_agent_id": parent_id,
            "root_task_id": root_id,
            "now": now,
            "metadata": {
                "task_id": task_id,
                "status": status,
                "turn_end_reason": "completed" if status == "DONE" else "error",
                "failure_type": "" if status == "DONE" else "provider_error",
                "completion_schema_version": "subagent-completion.v1",
                "completion_message": message,
                "final_report_ref": f"/workspace/{task_id}/final.md",
                "declared_output_refs": [f"/workspace/{task_id}/report.md"],
                "artifact_refs": [{"path": f"/workspace/{task_id}/artifact.json"}],
                "runner_result_json": f"/private/{task_id}/runner_result.json",
                "output_json": f"/private/{task_id}/output.json",
            },
        }
    )


def test_gateway_followup_receives_exact_root_child_completion_inputs(tmp_path):
    agent, request, thread_id = _completion_followup_fixture(tmp_path)
    append = partial(_append_completion_observation, agent, thread_id)
    append(
        task_id="child-1",
        parent_id="root-task",
        root_id="root-task",
        status="FAILED",
        message="旧失败结果",
        now=10.0,
    )
    append(
        task_id="child-2",
        parent_id="root-task",
        root_id="root-task",
        status="DONE",
        message="第二份调研已交付",
        now=15.0,
    )
    append(
        task_id="child-1",
        parent_id="root-task",
        root_id="root-task",
        status="DONE",
        message="第一份调研返工后已交付",
        now=20.0,
    )
    append(
        task_id="grandchild-1",
        parent_id="child-1",
        root_id="root-task",
        status="DONE",
        message="孙代理内容不能越级进入根会话",
        now=25.0,
    )
    append(
        task_id="other-child",
        parent_id="other-root",
        root_id="other-root",
        status="DONE",
        message="其它根任务内容不能串入",
        now=30.0,
    )

    followup = _conversation_context(agent, request, "gw-followup", "汇总子代理结果")
    completion_context = followup.subagent_completions
    section = _gateway_injections({"inject": []}, followup)[0]

    assert completion_context["schema"] == "conversation-subagent-completions.v1"
    assert completion_context["root_task_id"] == "root-task"
    assert completion_context["total"] == 2
    assert completion_context["omitted_count"] == 0
    items = {item["task_id"]: item for item in completion_context["items"]}
    assert items["child-1"]["status"] == "DONE"
    assert items["child-1"]["completion_message"] == "第一份调研返工后已交付"
    assert items["child-2"]["final_report_ref"] == "/workspace/child-2/final.md"
    assert items["child-2"]["artifact_refs"] == ["/workspace/child-2/artifact.json"]
    assert "Subagent Completion Inputs" in section
    assert "第一份调研返工后已交付" in section
    assert "第二份调研已交付" in section
    assert "旧失败结果" not in section
    assert "孙代理内容不能越级进入根会话" not in section
    assert "其它根任务内容不能串入" not in section
    assert "runner_result_json" not in section
    assert "output_json" not in section
    assert "/private/" not in section


def test_gateway_child_completion_inputs_are_bounded_with_explicit_omission(tmp_path):
    agent, request, thread_id = _completion_followup_fixture(tmp_path)
    for index in range(1, 16):
        _append_completion_observation(
            agent,
            thread_id,
            task_id=f"child-{index}",
            parent_id="root-task",
            root_id="root-task",
            status="DONE",
            message=f"第 {index} 份调研已交付",
            now=float(index),
        )
    bounded = _conversation_context(agent, request, "gw-bounded", "继续汇总")
    bounded_items = {
        item["task_id"]: item for item in bounded.subagent_completions["items"]
    }
    assert bounded.subagent_completions["total"] == 15
    assert bounded.subagent_completions["visible_count"] == 12
    assert bounded.subagent_completions["omitted_count"] == 3
    assert "child-3" not in bounded_items
    assert bounded_items["child-15"]["completion_message"] == "第 15 份调研已交付"


def test_gateway_run_persists_user_and_assistant_for_next_turn(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
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
    assert rows[-1].metadata["operation_verification"] == {
        "schema": "operation_verification.public.v1",
        "status": "none",
        "operation_count": 0,
        "omitted_operation_count": 0,
        "counts": {
            "succeeded": 0,
            "failed": 0,
            "not_started": 0,
            "unknown": 0,
            "cancelled": 0,
            "incomplete": 0,
            "unverified": 0,
        },
        "groups": [],
    }


def test_gateway_public_reply_redacts_structured_ids_but_keeps_internal_transcript(
    tmp_path, monkeypatch
):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_group_private_456",
        "channel_user_id": "ou_member_private_123",
        "canonical_user_id": "ou_member_private_123",
    }
    raw = (
        "这段话属于 oc_group_private_456，发言者 ou_member_private_123，"
        "内部请求 req_private_gateway_789，监控 audit-private-012。"
        "普通项目名 alpha 不变。"
    )
    seeded = _conversation_context(
        agent,
        {"conversation": conversation},
        "req-private-seed",
        "建立监控",
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": seeded.thread_id,
            "task_id": "audit-private-012",
            "goal": "检查测试来源",
            "status": "preparing",
            "work_kind": "audit",
            "work_name": "生产监控",
            "cancellation_scope": "detached",
        }
    )
    monkeypatch.setattr(
        agent,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            response=raw,
            runtime_status="ok",
            delivery_artifacts=[],
        ),
    )

    result = _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            {"prompt": "告诉我归属", "conversation": conversation},
            tmp_path / "req-private.json",
            tmp_path / "resp-private.json",
            "req_private_gateway_789",
            lambda _chunk: None,
        )
    )

    public = result.channel_delivery["content"]
    assert "oc_group_private_456" not in public
    assert "ou_member_private_123" not in public
    assert "req_private_gateway_789" not in public
    assert "audit-private-012" not in public
    assert "当前监控" in public
    assert "普通项目名 alpha 不变" in public
    thread = agent.conversation_store.resolve_thread(
        channel="feishu",
        channel_conversation_id="oc_group_private_456",
        channel_user_id="ou_member_private_123",
    )
    assert thread is not None
    rows = agent.conversation_store.recent_messages(thread.thread_id, limit=10)
    assert rows[-1].content == public
    assert "audit-private-012" not in rows[-1].content


def test_gateway_persists_same_redacted_runtime_tool_identifier_as_delivery(
    tmp_path,
    monkeypatch,
):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    conversation = {
        "channel": "cli",
        "channel_conversation_id": "cli_runtime_identifier_conversation",
        "channel_user_id": "cli_runtime_identifier_user",
        "canonical_user_id": "cli_runtime_identifier_user",
    }
    raw = "来源已经打开，内部读取编号 ws-private-runtime-012，可以继续。"
    monkeypatch.setattr(
        agent,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            response=raw,
            runtime_status="ok",
            delivery_artifacts=[],
            archive_tool_calls=[
                {
                    "tool": "watch_stream",
                    "parameters": {"action": "open"},
                    "output_externalized": False,
                    "output_preview": json.dumps(
                        {"ok": True, "watch_id": "ws-private-runtime-012"}
                    ),
                }
            ],
        ),
    )

    result = _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            {"prompt": "打开来源", "conversation": conversation},
            tmp_path / "req-runtime-id.json",
            tmp_path / "resp-runtime-id.json",
            "req_runtime_identifier_789",
            lambda _chunk: None,
        )
    )

    public = result.channel_delivery["content"]
    assert public == "来源已经打开，内部读取编号 当前来源读取，可以继续。"
    thread = agent.conversation_store.resolve_thread(
        channel="cli",
        channel_conversation_id="cli_runtime_identifier_conversation",
        channel_user_id="cli_runtime_identifier_user",
    )
    assert thread is not None
    rows = agent.conversation_store.recent_messages(thread.thread_id, limit=10)
    assert rows[-1].content == public
    assert "ws-private-runtime-012" not in rows[-1].content


def test_gateway_stream_commentary_uses_same_structured_identifier_redaction(tmp_path):
    chunk_path = tmp_path / "chunks.jsonl"
    writer = BufferedChunkStreamWriter(chunk_path)
    writer.set_identifier_redactions(
        (("oc_stream_private_456", "当前会话"), ("req_stream_private_789", "当前请求"))
    )
    writer.write_model("正在处理 oc_stream_private_456，对应 req_stream_private_789。")
    writer.write_progress({"phase": "started"}, "开始")
    writer.close()

    rows = [json.loads(line) for line in chunk_path.read_text(encoding="utf-8").splitlines()]
    commentary = next(row for row in rows if row.get("kind") == "assistant_commentary")
    assert commentary["text"] == "正在处理 当前会话，对应 当前请求。"


def test_gateway_compacts_and_retries_internal_context_pressure_inline(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
            model_context_window_tokens=1_000_000,
        ),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_compact_retry",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    existing = _conversation_context(
        agent,
        {"conversation": conversation},
        "gw-seed",
        "开始",
    )
    for index in range(2):
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                existing,
                request_id=f"gw-old-{index}-{role}",
                role=role,
                content=f"旧消息 {index} {role}",
            )
    calls: list[object] = []
    carried_tool_record = {
        "tool": "create_subagents",
        "call_id": "call-create-once",
        "scoped_call_id": "scope:call-create-once",
        "ok": True,
        "status": "ok",
        "handler_executed": True,
        "parameters": {"tasks": [{"goal": "审计项目"}]},
    }
    carried_turn_input = {
        "schema_version": "active-turn-user-input.v1",
        "input_ids": ["guidance-1"],
        "text": "继续原任务，但先补边界测试。",
    }

    def pressure_then_answer(_prompt, *, params=None, **_kwargs):
        calls.append(params)
        if len(calls) == 1:
            return SimpleNamespace(
                response="[RUN_CONTEXT_PRESSURE]\nsource: preflight",
                runtime_status="context_overflow",
                delivery_artifacts=[],
                archive_tool_calls=[carried_tool_record],
                active_turn_user_inputs=[carried_turn_input],
            )
        return SimpleNamespace(
            response="压缩后继续得到的自然回复",
            runtime_status="ok",
            delivery_artifacts=[],
        )

    monkeypatch.setattr(agent, "run", pressure_then_answer)
    result = _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            {"prompt": "继续聊", "conversation": conversation},
            tmp_path / "req-compact.json",
            tmp_path / "resp-compact.json",
            "gw-compact",
            lambda _chunk: None,
        )
    )
    thread = agent.conversation_store.load_thread(existing.thread_id)
    rows = agent.conversation_store.recent_messages(existing.thread_id, limit=20)

    assert len(calls) == 2
    assert all(call.context_scope == "conversation" for call in calls)
    assert calls[0].carried_archive_tool_calls == []
    assert calls[1].carried_archive_tool_calls == [carried_tool_record]
    assert calls[1].carried_active_turn_user_inputs == [carried_turn_input]
    assert thread.compact_generation == 1
    assert result.response == "压缩后继续得到的自然回复"
    assert [row.content for row in rows if row.role == "assistant"][-1] == result.response
    assert all("RUN_CONTEXT_PRESSURE" not in row.content for row in rows)


def test_gateway_same_turn_can_cross_pressure_twice_without_recompacting_transcript(
    tmp_path,
    monkeypatch,
):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
            model_context_window_tokens=1_000_000,
        ),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_compact_same_turn_twice",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    existing = _conversation_context(
        agent,
        {"conversation": conversation},
        "gw-seed",
        "开始",
    )
    for index in range(2):
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                existing,
                request_id=f"gw-old-{index}-{role}",
                role=role,
                content=f"旧消息 {index} {role}",
            )
    calls: list[object] = []
    first_tool = {
        "tool": "read_file",
        "call_id": "call-read-1",
        "ok": True,
        "parameters": {"path": "input/one.md"},
    }
    second_tool = {
        "tool": "read_file",
        "call_id": "call-read-2",
        "ok": True,
        "parameters": {"path": "input/two.md"},
    }

    def pressure_twice_then_answer(_prompt, *, params=None, **_kwargs):
        calls.append(params)
        if len(calls) == 1:
            return SimpleNamespace(
                response="[RUN_CONTEXT_PRESSURE]",
                runtime_status="context_overflow",
                delivery_artifacts=[],
                archive_tool_calls=[first_tool],
                active_turn_user_inputs=[],
            )
        if len(calls) == 2:
            return SimpleNamespace(
                response="[RUN_CONTEXT_PRESSURE]",
                runtime_status="context_overflow",
                delivery_artifacts=[],
                archive_tool_calls=[first_tool, second_tool],
                active_turn_user_inputs=[],
            )
        return SimpleNamespace(
            response="连续两次压缩后仍沿原任务完成",
            runtime_status="ok",
            delivery_artifacts=[],
        )

    monkeypatch.setattr(agent, "run", pressure_twice_then_answer)
    result = _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            {"prompt": "继续长任务", "conversation": conversation},
            tmp_path / "req-compact-twice.json",
            tmp_path / "resp-compact-twice.json",
            "gw-compact-twice",
            lambda _chunk: None,
        )
    )
    thread = agent.conversation_store.load_thread(existing.thread_id)

    assert len(calls) == 3
    assert calls[1].carried_archive_tool_calls == [first_tool]
    assert calls[2].carried_archive_tool_calls == [first_tool, second_tool]
    assert thread is not None and thread.compact_generation == 1
    assert result.response == "连续两次压缩后仍沿原任务完成"


def test_gateway_same_turn_pressure_without_new_structured_progress_stops(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
            model_context_window_tokens=1_000_000,
        ),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_compact_no_progress",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    existing = _conversation_context(
        agent,
        {"conversation": conversation},
        "gw-seed",
        "开始",
    )
    for role in ("user", "assistant"):
        assert _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            existing,
            request_id=f"gw-old-{role}",
            role=role,
            content=f"旧消息 {role}",
        )
    tool_record = {
        "tool": "read_file",
        "call_id": "call-read-stable",
        "ok": True,
        "parameters": {"path": "input/one.md"},
    }
    calls = 0

    def pressure_without_progress(_prompt, *, params=None, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            response="[RUN_CONTEXT_PRESSURE]",
            runtime_status="context_overflow",
            delivery_artifacts=[],
            archive_tool_calls=[tool_record],
            active_turn_user_inputs=[],
        )

    monkeypatch.setattr(agent, "run", pressure_without_progress)
    with pytest.raises(ConversationPersistenceError, match="无法继续压缩"):
        _run_gateway_ask(
            _GatewayAskRunContext(
                agent,
                {"prompt": "继续长任务", "conversation": conversation},
                tmp_path / "req-compact-no-progress.json",
                tmp_path / "resp-compact-no-progress.json",
                "gw-compact-no-progress",
                lambda _chunk: None,
            )
        )

    assert calls == 2


def test_gateway_foreground_turn_holds_shared_conversation_execution_lane(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_single_lane",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    original_run = agent.run
    observed: dict[str, object] = {}

    def guarded_run(prompt, *, params=None, **kwargs):
        thread_id = str(params.task_attributes["conversation_thread_id"])
        claim = agent.conversation_store.load_background_run_claim(thread_id)
        observed["claim"] = claim
        observed["second_executor"] = agent.conversation_store.claim_background_run(
            {
                "thread_id": thread_id,
                "task_id": "same-task-background-wake",
                "reason": "scheduled_progress_report",
                "lease_seconds": 90,
            }
        )
        return original_run(prompt, params=params, **kwargs)

    monkeypatch.setattr(agent, "run", guarded_run)
    _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            {"prompt": "继续当前工作", "conversation": conversation},
            tmp_path / "req-lane.json",
            tmp_path / "resp-lane.json",
            "gw-lane",
            lambda _chunk: None,
        )
    )

    claim = observed["claim"]
    assert isinstance(claim, dict)
    assert claim["reason"] == "gateway_foreground_turn"
    assert claim["task_id"] == "gateway:gw-lane"
    assert observed["second_executor"] is None
    finished = agent.conversation_store.load_background_run_claim(claim["thread_id"])
    assert finished["status"] == "finished"
    assert finished["last_runtime_facts"] == {
        "execution_source": "gateway",
        "request_id": "gw-lane",
    }


def test_gateway_foreground_turn_waits_for_existing_conversation_lane(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_lane_wait",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    current = _conversation_context(
        agent,
        {"conversation": conversation},
        "gw-before-wait",
        "后台正在收口",
    )
    claim = agent.conversation_store.claim_background_run(
        {
            "thread_id": current.thread_id,
            "task_id": "task-background",
            "reason": "scheduled_progress_report",
            "lease_seconds": 90,
        }
    )
    assert claim is not None
    original_run = agent.run
    observed: dict[str, str] = {}

    def capture_fresh_history(prompt, *, params=None, **kwargs):
        observed["injection"] = "\n".join(str(item) for item in params.inject)
        return original_run(prompt, params=params, **kwargs)

    monkeypatch.setattr(agent, "run", capture_fresh_history)

    def release_background_lane() -> None:
        time.sleep(0.12)
        _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            current,
            request_id="background-before-foreground",
            role="assistant",
            content="后台上一轮刚刚写入的最终事实",
        )
        agent.conversation_store.finish_background_run(
            {
                "thread_id": current.thread_id,
                "claim_id": claim["claim_id"],
                "task_id": "task-background",
                "status": "finished",
            }
        )

    release = threading.Thread(target=release_background_lane)
    release.start()
    started = time.monotonic()
    try:
        result = _run_gateway_ask(
            _GatewayAskRunContext(
                agent,
                {"prompt": "等上一轮结束后继续", "conversation": conversation},
                tmp_path / "req-wait.json",
                tmp_path / "resp-wait.json",
                "gw-after-wait",
                lambda _chunk: None,
            )
        )
    finally:
        release.join(timeout=2)

    assert result.response
    assert time.monotonic() - started >= 0.1
    assert "后台上一轮刚刚写入的最终事实" in observed["injection"]
    latest = agent.conversation_store.load_background_run_claim(current.thread_id)
    assert latest["status"] == "finished"
    assert latest["task_id"] == "gateway:gw-after-wait"


def test_two_gateway_foreground_turns_share_one_lane_and_fresh_history(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_two_foreground_turns",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    original_run = agent.run
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    second_injection: list[str] = []
    errors: list[BaseException] = []

    def controlled_run(prompt, *, params=None, **kwargs):
        if prompt == "第一轮先运行":
            first_entered.set()
            assert release_first.wait(timeout=2)
        if prompt == "第二轮随后运行":
            second_injection.extend(str(item) for item in params.inject)
            second_entered.set()
        return original_run(prompt, params=params, **kwargs)

    monkeypatch.setattr(agent, "run", controlled_run)

    def invoke(prompt: str, request_id: str) -> None:
        try:
            _run_gateway_ask(
                _GatewayAskRunContext(
                    agent,
                    {"prompt": prompt, "conversation": conversation},
                    tmp_path / f"{request_id}.json",
                    tmp_path / f"{request_id}.response.json",
                    request_id,
                    lambda _chunk: None,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - thread failure is asserted below
            errors.append(exc)

    first = threading.Thread(target=invoke, args=("第一轮先运行", "gw-concurrent-1"))
    second = threading.Thread(target=invoke, args=("第二轮随后运行", "gw-concurrent-2"))
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    time.sleep(0.12)
    assert not second_entered.is_set()
    release_first.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert second_entered.is_set()
    assert "第一轮先运行" in "\n".join(second_injection)
    thread = _conversation_context(agent, {"conversation": conversation}, "inspect", "检查")
    rows = agent.conversation_store.recent_messages(thread.thread_id, limit=10)
    assert [row.metadata["gateway_request_id"] for row in rows] == [
        "gw-concurrent-1",
        "gw-concurrent-1",
        "gw-concurrent-2",
        "gw-concurrent-2",
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


def test_gateway_ordinary_history_excludes_detached_audit_deliveries(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_audit_background",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    context = _conversation_context(agent, request, "gw-create", "开始")
    agent.conversation_store.append_message(
        {
            "thread_id": context.thread_id,
            "role": "assistant",
            "content": "旧 Audit 的新受益人事件正文",
            "channel": "feishu",
            "metadata": {
                "task_id": "audit-old",
                "reason": "audit_finding",
                "background_delivery_reason": "audit_finding_report",
                "evidence_refs": ["audit://watch-old/candidate/1:0"],
            },
        }
    )
    agent.conversation_store.append_message(
        {
            "thread_id": context.thread_id,
            "role": "assistant",
            "content": "普通主代理回复仍应续接",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "gw-normal"},
        }
    )

    followup = _conversation_context(agent, request, "gw-follow", "继续聊天")
    history_text = "\n".join(content for _role, content in followup.history)
    stored_text = "\n".join(
        row.content for row in agent.conversation_store.recent_messages(context.thread_id, limit=0)
    )

    assert "旧 Audit 的新受益人事件正文" not in history_text
    assert "普通主代理回复仍应续接" in history_text
    assert "旧 Audit 的新受益人事件正文" in stored_text


def test_natural_reply_and_structured_artifact_are_reused_without_rerunning(tmp_path):
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
    artifact = str(tmp_path / "owner" / "output" / "weekly.xlsx")
    raw = f"周报已整理好，文件是 {artifact.rsplit('/', 1)[-1]}。"
    projection = project_user_reply(raw)
    artifact_refs = (
        {
            "artifact_id": "weekly_report",
            "kind": "xlsx",
            "path": artifact,
            "name": artifact.rsplit("/", 1)[-1],
            "ok": True,
        },
    )
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
        delivery_artifacts=artifact_refs,
    )

    followup = _conversation_context(agent, request, "gw-delivery-2", "发我")
    section = _gateway_injections({"inject": []}, followup)[0]

    assert followup.history[-1] == ("assistant", projection.content)
    assert followup.recent_artifacts[0]["artifact_id"] == "weekly_report"
    assert followup.recent_artifacts[0]["path"] == artifact
    assert "直接调用 send_message" in section
    assert "不要重新搜索、复制或制作一遍" in section
    assert "MAIN_AGENT" not in section


def test_gateway_response_does_not_fall_back_to_suppressed_internal_result() -> None:
    raw = "[RUN_TOOL_EVIDENCE_BLOCKED]\n/private/runtime/report.pdf"
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
        model_accounted_input_tokens=1000,
        model_output_tokens=100,
        model_total_tokens=1100,
        model_cached_input_tokens=700,
        model_cache_creation_input_tokens=50,
        model_provider_usage_call_count=1,
        model_estimated_usage_call_count=0,
        model_usage_breakdown={
            "schema": "model_usage_breakdown.v1",
            "provider": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "cache_read_input_tokens": 700,
                "cache_write_input_tokens": 50,
                "call_count": 1,
            },
            "estimated": {
                "input_tokens": 0,
                "output_tokens": 0,
                "call_count": 0,
            },
        },
        memory_resume_context_injected=False,
        memory_resume_context_query="",
        memory_resume_context_matches=0,
        memory_resume_context_token_estimate=0,
        memory_resume_context_error="",
        channel_delivery=project_user_reply(raw).to_dict(),
    )
    response: dict[str, object] = {}

    _update_response_from_result(response, result, {})

    assert response["response"] == ""
    assert "RUN_TOOL_EVIDENCE_BLOCKED" not in json.dumps(response, ensure_ascii=False)
    assert response["channel_delivery"]["internal_signal"] is True
    assert "/private/runtime" not in json.dumps(response, ensure_ascii=False)
    assert response["model_total_tokens"] == 1100

    from agent_py_agent.agent.gateway_parts.http_handlers import _public_result

    public = _public_result(response)
    assert public["model_accounted_input_tokens"] == 1000
    assert public["model_output_tokens"] == 100
    assert public["model_total_tokens"] == 1100
    assert public["model_cached_input_tokens"] == 700
    assert public["model_cache_creation_input_tokens"] == 50
    assert public["model_provider_usage_call_count"] == 1
    assert public["model_estimated_usage_call_count"] == 0
    assert public["model_usage_breakdown"]["provider"]["cache_read_input_tokens"] == 700


def test_gateway_public_path_sanitizer_preserves_completion_facts() -> None:
    from agent_py_agent.agent.conversation.channels import redact_host_absolute_paths

    raw = "青岚系统已完成，25 项测试全部通过；报告位于 /root/private/report.pdf。"
    content = redact_host_absolute_paths(project_user_reply(raw).content)

    assert "青岚系统已完成" in content
    assert "25 项测试全部通过" in content
    assert "report.pdf" in content
    assert "/root/private" not in content


def test_plain_reply_preserves_relative_paths_without_mangling_slashes() -> None:
    raw = (
        "产物在 tasks/demo/output/report.md，依赖位于 libs/agents/core.py；另见 ./notes/today.md。"
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


def test_delivery_projection_removes_fenced_tool_call_but_keeps_public_text() -> None:
    raw = (
        "明白，任务已接。\n\n"
        "```tool_call\n"
        'task_progress {target_dir: "reference_repos", items: [{"id":"tokei"}]}\n'
        "```\n\n"
        "我会读完源码后汇总。"
    )

    projection = project_user_reply(raw)

    assert projection.content == "明白，任务已接。\n\n我会读完源码后汇总。"
    assert projection.internal_signal is False
    assert projection.projection_status == "internal_protocol_removed"
    assert "task_progress" not in projection.content


def test_delivery_projection_removes_minimax_named_xml_tool_envelopes() -> None:
    raw = (
        "我先把几块工作分别推进。\n"
        "<task_progress>\n- [ ] 汇总资料\n</task_progress>\n"
        '<spawn_subagent>\n{"task_name":"资料核对"}\n</spawn_subagent>\n'
        "有可靠结果后我再一起说明。"
    )

    projection = project_user_reply(raw)

    assert projection.content == "我先把几块工作分别推进。\n有可靠结果后我再一起说明。"
    assert projection.internal_signal is False
    assert projection.projection_status == "internal_protocol_removed"
    assert "task_progress" not in projection.content
    assert "spawn_subagent" not in projection.content


def test_delivery_projection_drops_truncated_named_xml_tool_tail() -> None:
    raw = '我先开始核对。\n<spawn_subagent>\n{"task_name":"未闭合的内部调用"}'

    projection = project_user_reply(raw)

    assert projection.content == "我先开始核对。"
    assert projection.internal_signal is False
    assert projection.projection_status == "internal_protocol_removed"


def test_delivery_projection_strips_legacy_findings_ledger_block() -> None:
    raw = (
        "项目已经完成，全部测试通过。\n\n"
        "【逐条结论|本轮新增 1 条,已入结论账】\n"
        "- rf-private 内部结论(证据: internal-ref)"
    )

    projection = project_user_reply(raw)

    assert projection.content == "项目已经完成，全部测试通过。"
    assert projection.internal_signal is False
    assert projection.projection_status == "internal_protocol_removed"
    assert "rf-private" not in projection.content


def test_gateway_chat_history_isolated_by_real_conversation_id(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
    assert second.workspace_task is None


def test_gateway_conversation_turns_do_not_leak_into_owner_global_memory(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
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


def test_gateway_ordinary_chat_accepts_legacy_cleared_goal_tombstone(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_legacy_goal",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    current = _conversation_context(
        agent,
        {"conversation": conversation},
        "gw-before-upgrade",
        "旧版本目标",
    )
    goal = agent.conversation_store.create_goal(
        {"thread_id": current.thread_id, "objective": "旧版本目标"}
    )
    path = agent.conversation_store._goal_path(current.thread_id)
    payload = goal.to_dict()
    payload["status"] = "cleared"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_gateway_ask(
        _GatewayAskRunContext(
            agent,
            {"prompt": "继续普通聊天", "conversation": conversation},
            tmp_path / "req-legacy-goal.json",
            tmp_path / "resp-legacy-goal.json",
            "gw-after-upgrade",
            lambda _chunk: None,
        )
    )

    assert "继续普通聊天" in result.response
    assert agent.conversation_store.load_goal(current.thread_id) is None
    rows = agent.conversation_store.recent_messages(current.thread_id, limit=10)
    assert [(row.role, row.content) for row in rows] == [
        ("user", "继续普通聊天"),
        ("assistant", result.response),
    ]


def test_gateway_does_not_report_success_for_empty_unavailable_model_reply(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_empty_reply",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    monkeypatch.setattr(
        agent,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            response="",
            runtime_status="user_reply_unavailable",
            delivery_artifacts=[],
        ),
    )

    with pytest.raises(UserReplyUnavailableError):
        _run_gateway_ask(
            _GatewayAskRunContext(
                agent,
                {"prompt": "继续原任务", "conversation": conversation},
                tmp_path / "req-empty.json",
                tmp_path / "resp-empty.json",
                "gw-empty",
                lambda _chunk: None,
            )
        )

    thread = _conversation_context(
        agent,
        {"conversation": conversation},
        "gw-next",
        "还在吗？",
    )
    rows = agent.conversation_store.recent_messages(thread.thread_id, limit=10)
    assert [(row.role, row.content) for row in rows] == [("user", "继续原任务")]
    assert not (
        agent.conversation_store.root / "message_repairs" / "gw-empty-assistant.json"
    ).exists()


def test_gateway_does_not_report_done_for_any_empty_model_reply(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
        tmp_path,
    )
    conversation = {
        "channel": "feishu",
        "channel_conversation_id": "oc_empty_done",
        "channel_user_id": "ou_user1",
        "canonical_user_id": "ou_user1",
    }
    monkeypatch.setattr(
        agent,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            response="",
            runtime_status="ok",
            delivery_artifacts=[{"path": "output/report.md"}],
        ),
    )

    with pytest.raises(UserReplyUnavailableError):
        _run_gateway_ask(
            _GatewayAskRunContext(
                agent,
                {"prompt": "完成后告诉我", "conversation": conversation},
                tmp_path / "req-empty-done.json",
                tmp_path / "resp-empty-done.json",
                "gw-empty-done",
                lambda _chunk: None,
            )
        )


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
    operation_verification = {
        "schema": "operation_verification.v1",
        "scope": "current_request_only",
        "status": "succeeded",
        "operation_count": 1,
        "counts": {"succeeded": 1},
        "operations": [
            {
                "tool": "remember",
                "action": "add",
                "verification_status": "succeeded",
                "call_id": "private-call",
                "operation_id": "private-operation",
                "attempt_count": 1,
                "replayed": False,
            }
        ],
    }
    monkeypatch.setattr(
        agent,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            response="我记住了海棠。",
            runtime_status="ok",
            delivery_artifacts=[],
            operation_verification=operation_verification,
        ),
    )
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
    repair_payload = json.loads(repair_path.read_text(encoding="utf-8"))
    repair_verification = repair_payload["metadata"]["operation_verification"]
    assert repair_verification["groups"][0]["label"] == "remember/add"
    assert "private-call" not in json.dumps(repair_verification)

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
    assert rows[-1].metadata["operation_verification"] == repair_verification
    assert not repair_path.exists()


def test_gateway_authoritative_transcript_filters_legacy_dialogue_but_keeps_formal_fact(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    agent.memory.add("user", "串线令牌海棠", kind="dialogue")
    agent.memory.add(
        "user",
        "正式事实令牌青黛",
        kind="fact",
        attributes={
            "origin": "reviewed",
            "subject_key": "test.gateway.formal-fact",
            "scope_type": "personal",
            "scope_key": "personal",
        },
    )

    prepared = _prepare_runtime_context(
        agent,
        RuntimeContextRequest(
            user_prompt="令牌海棠 青黛",
            inject=[],
            resume_context=False,
            task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
        ),
    )

    assert [row.kind for row in prepared.memories] == ["fact"]
    assert prepared.memories[0].content == "正式事实令牌青黛"


def test_authoritative_transcript_overfetches_past_legacy_dialogue_for_formal_fact(tmp_path):
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
    agent.memory.add(
        "user",
        "海棠正式事实：项目代号青黛",
        kind="fact",
        attributes={
            "origin": "reviewed",
            "subject_key": "test.gateway.project-code",
            "scope_type": "personal",
            "scope_key": "personal",
        },
    )

    prepared = _prepare_runtime_context(
        agent,
        RuntimeContextRequest(
            user_prompt="海棠",
            inject=[],
            resume_context=False,
            task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
        ),
    )

    assert [row.kind for row in prepared.memories] == ["fact"]
    assert prepared.memories[0].content == "海棠正式事实：项目代号青黛"


def test_gateway_thread_does_not_expose_internal_run_candidates_to_model(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
    section = _gateway_injections({"inject": []}, second)[0]
    assert "Resumable Work Candidates" not in section
    assert "Recent Completed Work" not in section
    assert "task-old" not in section
    assert "旧任务" not in section
    assert "active_root_task_id" not in section


def test_current_turn_automatically_binds_sticky_workspace_without_overwriting_goal(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_task",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-chat", "普通聊天")
    workspace = Path(agent.home_paths.owner_home_dir) / "tasks" / "existing-task"
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
            "conversation_task_id": "task-old",
        },
    )
    agent._current_run_params = params
    try:
        selected = promote_current_conversation_task(agent)
        TaskProgressTool(agent).execute({"action": "update", "summary": "继续整理中"})
        # read 必须与 update 走同一账本解析(progress_ledger_id 按 task_path 指纹
        # 寻址,真机实证 2026-08-07 celery 复刻):显式字面 run_id 是跨任务逃生口,
        # 按字面 task-old 读指纹账本=读错位=空。故不传 run_id 且在 run 参数存活
        # 期内执行,验证 sticky 绑定后账本写读延续。
        progress = json.loads(TaskProgressTool(agent).execute({"action": "read"}).output)
        reused = promote_current_conversation_task(agent, goal="不应覆盖旧目标")
    finally:
        delattr(agent, "_current_run_params")

    assert selected is not None
    assert params.task_attributes["conversation_task_id"] == "task-old"
    assert params.task_attributes["run_workspace"]["task_root"] == str(workspace)
    assert progress["summary"] == "继续整理中"
    assert reused.goal == "整理季度报告"
    assert reused.task_path == str(workspace)


def test_live_background_claim_alone_blocks_second_task_executor(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_claimed",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-first", "开始长任务")
    agent.conversation_store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "task-claimed",
            "goal": "持续完成长任务",
            "status": "active",
        }
    )
    claim = agent.conversation_store.claim_background_run(
        {
            "thread_id": first.thread_id,
            "task_id": "task-claimed",
            "reason": "scheduled_progress_report",
            "lease_seconds": 90,
        }
    )
    assert claim is not None

    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        task_attributes={
            "conversation_thread_id": first.thread_id,
            "conversation_task_id": "task-claimed",
        },
    )
    agent._current_run_params = params
    try:
        selected = _promote_work_tool(agent, params, {"tool": "write_file"})
    finally:
        delattr(agent, "_current_run_params")

    assert selected.ok is False
    assert selected.error_code == "CONVERSATION_TASK_ALREADY_RUNNING"


def test_wait_policy_alone_does_not_block_orchestration_tools(tmp_path):
    """wait 定时器 policy 不等于 live executor:管理/编排工具必须始终可用。

    真机 2026-08-09:父代理 wait 登记 policy 后,create_subagents/read_file 全被
    CONVERSATION_TASK_ALREADY_RUNNING 拦,子代理全死也无法重派,任务死锁。
    """
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_wait_policy",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-wait-policy", "开始长任务")
    agent.conversation_store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "task-waiting",
            "goal": "持续完成长任务",
            "status": "active",
        }
    )
    agent.conversation_store.set_progress_policy(
        {
            "thread_id": first.thread_id,
            "task_id": "task-waiting",
            "interval_seconds": 120,
            "metadata": {"kind": "subagent_progress_watch", "tool": "wait"},
        }
    )
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        task_attributes={
            "conversation_thread_id": first.thread_id,
            "conversation_task_id": "task-waiting",
        },
    )
    agent._current_run_params = params
    try:
        for tool in ("create_subagents", "cancel_subagents", "wait", "send_guidance", "read_file"):
            selected = _promote_work_tool(agent, params, {"tool": tool})
            assert selected is None, f"{tool} 不应被 wait policy 拦截: {selected}"
    finally:
        delattr(agent, "_current_run_params")


def test_wait_policy_alone_does_not_block_workspace_write(tmp_path):
    """wait 期间写类工具放行:policy 只证明「登记了唤醒」,不证明任何 run 在执行。

    2026-08-09 真机死锁实锤:把 enabled wait policy 当 running 源,会把父任务
    唤醒轮续接(claim 驱动轮 conversation_task_id 与 link 不一致时走 sticky 回落
    bind)拦成 CONVERSATION_TASK_BINDING_FAILED,父代理连 cancel policy 都救不了
    自己。running 的权威证据是 claim;policy 的 watch 目标(子代理)才算 running。
    """
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_wait_policy_write",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-wait-policy-write", "开始长任务")
    agent.conversation_store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "task-waiting-write",
            "goal": "持续完成长任务",
            "status": "active",
        }
    )
    agent.conversation_store.set_progress_policy(
        {
            "thread_id": first.thread_id,
            "task_id": "task-waiting-write",
            "interval_seconds": 120,
            "metadata": {"kind": "subagent_progress_watch", "tool": "wait"},
        }
    )
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        task_attributes={
            "conversation_thread_id": first.thread_id,
            "conversation_task_id": "task-waiting-write",
        },
    )
    agent._current_run_params = params
    try:
        for tool in ("write_file", "edit_file", "apply_patch"):
            selected = _promote_work_tool(agent, params, {"tool": tool})
            assert selected is None, f"{tool} 不应被 self-wait policy 拦截: {selected}"
    finally:
        delattr(agent, "_current_run_params")


def test_unreadable_background_execution_state_fails_closed(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_state_error",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-first", "开始长任务")
    agent.conversation_store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "task-state-error",
            "goal": "持续完成长任务",
            "status": "active",
        }
    )
    monkeypatch.setattr(
        agent.conversation_store,
        "load_background_run_claim_report",
        lambda _thread_id: ({"task_id": "task-state-error", "expires_at": "bad"}, None),
    )
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        task_attributes={
            "conversation_thread_id": first.thread_id,
            "conversation_task_id": "task-state-error",
        },
    )
    agent._current_run_params = params
    try:
        selected = _promote_work_tool(agent, params, {"tool": "write_file"})
    finally:
        delattr(agent, "_current_run_params")

    assert selected.ok is False
    assert selected.error_code == "CONVERSATION_TASK_STATE_UNAVAILABLE"


def test_background_promotion_reuses_link_workspace_without_synthetic_wake_directory(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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


def test_current_conversation_workspace_is_inherited_by_new_subagents(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
        task_attributes={
            "conversation_thread_id": conversation.thread_id,
            "conversation_task_id": "task-old",
        },
    )
    agent._current_run_params = params
    try:
        assert promote_current_conversation_task(agent) is not None
        child_attrs: dict[str, object] = {}
        add_current_conversation_attrs(child_attrs, agent)
    finally:
        delattr(agent, "_current_run_params")
    assert child_attrs["conversation_thread_id"] == conversation.thread_id
    assert child_attrs["conversation_task_id"] == "task-old"
    workspace = Path(params.task_attributes["run_workspace"]["task_root"])
    assert (
        json.loads((workspace / "work" / "state.json").read_text(encoding="utf-8"))["task_id"]
        == "task-old"
    )
    assert 'task_id: "task-old"' in (workspace / "work" / "task.yaml").read_text(encoding="utf-8")


def test_completed_run_stays_internal_and_does_not_become_a_model_task_menu(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
        CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
    }
    assert complete_current_conversation_task(agent, attrs, source="gateway") is True
    thread = agent.conversation_store.load_thread(first.thread_id)
    assert thread is not None and "task-done" not in thread.active_task_ids
    second = _conversation_context(agent, request, "gw-next", "聊点别的")
    section = _gateway_injections({"inject": []}, second)[0]
    assert "Recent Completed Work" not in section
    assert "task-done" not in section
    assert "做一份报告" not in section


def test_named_audit_turn_cannot_complete_before_its_typed_deadline(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_audit_duration",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-audit", "持续检查数据")
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "audit-live",
            "goal": "持续检查数据",
            "status": "active",
            "work_kind": "audit",
            "work_name": "持续巡检",
            "duration_seconds": 600,
        }
    )
    attrs = {
        "conversation_thread_id": conversation.thread_id,
        "conversation_task_id": "audit-live",
        "conversation_work_kind": "audit",
        CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
    }

    assert complete_current_conversation_task(agent, attrs, source="gateway") is False
    link = agent.conversation_store.task_links(conversation.thread_id)[0]
    assert link.status == "active"


def test_unregistered_audit_payload_cannot_mint_task_lineage(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_audit_lineage",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(
        agent,
        request,
        "gw-old-workspace",
        "持续检查数据",
    )
    attrs = _gateway_run_task_attributes(
        conversation,
        {
            "system_task": {
                "kind": "audit",
                "attributes": {
                    "audit_guarantee": True,
                    "conversation_work_kind": "audit",
                },
            }
        },
        "gw-audit-exact",
    )

    assert attrs is not None
    assert "conversation_task_id" not in attrs
    assert CONVERSATION_REQUEST_ID_ATTR not in attrs
    assert CONVERSATION_TASK_TURN_ACTIVE_ATTR not in attrs


def test_named_audit_terminal_turn_closes_only_its_exact_sources(
    tmp_path,
    monkeypatch,
):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_audit_complete",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(
        agent,
        request,
        "gw-audit-complete",
        "持续检查数据",
    )
    link = agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "audit-complete",
            "goal": "持续检查数据",
            "status": "active",
            "work_kind": "audit",
            "work_name": "完成巡检",
            "duration_seconds": 1,
        }
    )
    closed_tasks: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.task_promotion.time.time",
        lambda: link.expires_at + 1,
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.ingestion.audit_state.audit_task_activation_facts",
        lambda _agent, _link: {"ready": True},
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.named_work.close_named_audit_watches",
        lambda _agent, task_id, *, reason="named_audit_clear": (
            closed_tasks.append((task_id, reason)) or ("watch-exact",)
        ),
    )
    attrs = {
        "conversation_thread_id": conversation.thread_id,
        "conversation_task_id": "audit-complete",
        "conversation_work_kind": "audit",
        CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
    }

    assert complete_current_conversation_task(agent, attrs, source="gateway") is True
    assert closed_tasks == [("audit-complete", "audit_window_settled")]
    completed = agent.conversation_store.task_links(conversation.thread_id)[0]
    assert completed.status == "completed"


def test_named_audit_waits_for_owner_event_receipt_before_structural_close(
    tmp_path,
    monkeypatch,
):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "ou_user1",
            "channel": "internal",
            "channel_conversation_id": "audit-owner-delivery",
            "channel_user_id": "ou_user1",
        }
    )
    link = store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-owner-delivery",
            "goal": "完成持续检查",
            "status": "active",
            "work_kind": "audit",
            "work_name": "owner-delivery",
            "duration_seconds": 1,
        }
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.task_promotion.time.time",
        lambda: float(link.expires_at or 0) + 1,
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.ingestion.audit_state.task_has_incomplete_watch",
        lambda _agent, _task_id: False,
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.ingestion.audit_state.audit_task_activation_facts",
        lambda _agent, _link: {"ready": True},
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.ingestion.source_worker.audit_task_has_unreported_findings",
        lambda _agent, _task_id: False,
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.named_work.close_named_audit_watches",
        lambda _agent, _task_id, *, reason="": (),
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_finding",
            "root_task_id": link.task_id,
            "evidence_refs": ["audit://watch-1/candidate/1:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "finding_id": "af-1",
                "requires_llm_report": True,
            },
        }
    )

    assert complete_named_audit_task_if_settled(agent, link.task_id) is False
    assert store.load_task_link(link.task_id).status == "active"

    store.mark_wake_signal_handled(signal.wake_signal_id)
    assert complete_named_audit_task_if_settled(agent, link.task_id) is True
    assert store.load_task_link(link.task_id).status == "completed"


def test_cancel_wins_completion_status_race(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_cancel_race",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-race", "完成一个长任务")
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-race",
            "goal": "完成一个长任务",
            "status": "active",
        }
    )
    attrs = {
        "conversation_thread_id": conversation.thread_id,
        "conversation_task_id": "task-race",
        CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
    }
    real_update = agent.conversation_store.update_task_status

    def cancel_before_completion(payload):
        if payload.get("status") == "completed":
            real_update(
                {
                    "task_id": "task-race",
                    "status": "cancelled",
                    "expected_status": "active",
                }
            )
        return real_update(payload)

    monkeypatch.setattr(agent.conversation_store, "update_task_status", cancel_before_completion)

    assert complete_current_conversation_task(agent, attrs, source="gateway") is False
    link = agent.conversation_store.task_links(conversation.thread_id)[0]
    assert link.status == "cancelled"


def test_pending_task_guidance_keeps_current_task_open(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_guidance_race",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-guidance", "完成一个长任务")
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-guidance",
            "goal": "完成一个长任务",
            "status": "active",
        }
    )
    agent.conversation_store.append_guidance(
        {
            "target_type": "task",
            "target_id": "task-guidance",
            "message": "把最终预算控制在四百元以内",
            "delivery": "current_task",
        }
    )
    attrs = {
        "conversation_thread_id": conversation.thread_id,
        "conversation_task_id": "task-guidance",
        CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
    }

    assert complete_current_conversation_task(agent, attrs, source="gateway") is False
    link = agent.conversation_store.task_links(conversation.thread_id)[0]
    assert link.status == "active"


def test_active_thread_goal_is_not_closed_by_one_delivery_complete_turn(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_goal_turn",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-goal", "持续整理项目资料")
    goal = agent.conversation_store.create_goal(
        {"thread_id": conversation.thread_id, "objective": "持续整理项目资料"}
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    attrs = {
        "conversation_thread_id": conversation.thread_id,
        "conversation_task_id": goal.task_id,
        CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
    }

    assert complete_current_conversation_task(agent, attrs, source="gateway") is False
    loaded_goal = agent.conversation_store.load_goal(conversation.thread_id)
    assert loaded_goal is not None and loaded_goal.status == "active"
    link = agent.conversation_store.task_links(conversation.thread_id)[0]
    assert link.task_id == goal.task_id and link.status == "active"


def test_multiple_named_goals_expose_only_management_facts_to_an_ordinary_turn(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_named_goals",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "普通聊天")
    for name, objective in (
        ("周报整理", "持续整理周报"),
        ("依赖升级", "持续检查依赖"),
    ):
        goal = agent.conversation_store.create_goal(
            {
                "thread_id": conversation.thread_id,
                "name": name,
                "objective": objective,
            }
        )
        agent.conversation_store.bind_task(
            {
                "thread_id": conversation.thread_id,
                "task_id": goal.task_id,
                "goal": objective,
                "status": "active",
                "work_kind": "goal",
                "work_name": name,
                "cancellation_scope": "detached",
            }
        )

    followup = _conversation_context(agent, request, "gw-chat", "我们聊点别的")
    injection = _gateway_injections({"inject": []}, followup)[0]

    assert followup.load_errors == ()
    assert followup.thread_goal is None
    assert "Persistent Goal" not in injection
    assert "Named Persistent Work" in injection
    assert '"name":"周报整理"' in injection
    assert '"name":"依赖升级"' in injection
    assert "持续整理周报" not in injection
    assert "持续检查依赖" not in injection
    assert "stop_named_work" in injection


def test_detached_named_work_never_occupies_the_foreground_sticky_workspace(
    tmp_path,
):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_detached_workspace",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "audit-1", "持续监测")
    audit_workspace = Path(agent.home_paths.owner_home_dir) / "audits" / "audit-1"
    (audit_workspace / "output").mkdir(parents=True)
    (audit_workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "audit-1",
            "goal": "持续监测",
            "status": "active",
            "task_path": str(audit_workspace),
            "work_kind": "audit",
            "work_name": "后台巡检",
            "cancellation_scope": "detached",
        }
    )
    agent.conversation_store.select_workspace_task(
        {"thread_id": conversation.thread_id, "task_id": "audit-1"}
    )
    agent.conversation_store.set_progress_policy(
        {
            "thread_id": conversation.thread_id,
            "task_id": "audit-1",
            "interval_seconds": 120,
        }
    )

    followup = _conversation_context(agent, request, "gw-chat", "同时整理另一份代码")
    attrs = _gateway_task_attributes(followup)
    assert followup.workspace_task is None
    assert attrs is not None
    assert attrs.get("conversation_task_id") is None

    params = RunParams(
        request_id="gw-chat",
        run_id="gw-chat",
        task_id="gw-chat",
        root_user_prompt="同时整理另一份代码",
        task_attributes=attrs,
    )
    agent._current_run_params = params
    try:
        assert conversation_workspace_execution_blocker(agent) is None
        selected = promote_current_conversation_task(agent)
    finally:
        delattr(agent, "_current_run_params")

    assert selected is not None
    assert selected.task_id == "gw-chat"
    assert selected.task_path != str(audit_workspace)
    audit = next(
        link
        for link in agent.conversation_store.task_links(conversation.thread_id)
        if link.task_id == "audit-1"
    )
    assert audit.status == "active"


def test_internal_child_links_never_appear_in_model_conversation_context(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
    section = _gateway_injections({"inject": []}, loaded)[0]
    assert "subagent-child" not in section
    assert "bg-main-child" not in section


@pytest.mark.parametrize("prior_status", ["completed", "interrupted"])
def test_next_turn_reuses_terminal_workspace_with_fresh_execution_automatically(
    tmp_path,
    prior_status,
):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_resume_completed",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "先完成第一步")
    workspace = Path(agent.home_paths.owner_home_dir) / "tasks" / "completed-task"
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
        {"task_id": "task-completed", "status": prior_status}
    )
    agent.conversation_store.select_workspace_task(
        {"thread_id": conversation.thread_id, "task_id": "task-completed"}
    )
    followup = _conversation_context(agent, request, "gw-followup", "继续做第二步")
    attrs = _gateway_task_attributes(followup)
    assert attrs is not None
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        root_user_prompt="继续做第二步",
        task_attributes=attrs,
    )
    agent._current_run_params = params
    agent._current_run_task_workspace = str(workspace)
    try:
        selected = promote_current_conversation_task(agent)
    finally:
        delattr(agent, "_current_run_params")

    assert selected is not None
    successor_id = params.task_attributes["conversation_task_id"]
    assert successor_id != "task-completed"
    if prior_status == "interrupted":
        # interrupted=暂停可恢复:手动继续仍续接原目录(暂停恢复语义保留)
        assert params.task_attributes["conversation_continued_from_task_id"] == "task-completed"
        assert params.task_attributes["run_workspace"]["task_root"] == str(workspace)
        assert agent._current_run_task_workspace == str(workspace)
    else:
        # completed=任务已完成:新消息是独立新任务,不再吸附旧身份与旧目录
        # (问题1 真机 2026-08-09:celery 完成后 click/jinja2/requests 等新任务
        # 消息全被吸进 celery 旧目录),新任务走独立目录、无续接标记。
        assert "conversation_continued_from_task_id" not in params.task_attributes
        assert params.task_attributes["run_workspace"]["task_root"] != str(workspace)
        assert agent._current_run_task_workspace != str(workspace)
    links = {
        link.task_id: (link.status, link.goal)
        for link in agent.conversation_store.task_links(conversation.thread_id)
    }
    assert links["task-completed"] == (prior_status, "校园交易网站第一步")
    assert links[successor_id] == ("active", "继续做第二步")


def test_terminal_workspace_successor_fails_closed_without_current_prompt(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_missing_current_prompt",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "旧任务")
    workspace = Path(agent.home_paths.owner_home_dir) / "tasks" / "completed-task"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-completed",
            "goal": "不得复制的旧目标",
            "status": "active",
            "task_path": str(workspace),
        }
    )
    agent.conversation_store.update_task_status(
        {"task_id": "task-completed", "status": "completed"}
    )
    agent.conversation_store.select_workspace_task(
        {"thread_id": conversation.thread_id, "task_id": "task-completed"}
    )
    followup = _conversation_context(agent, request, "gw-followup", "")
    attrs = _gateway_task_attributes(followup)
    assert attrs is not None
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        root_user_prompt="",
        task_attributes=attrs,
    )
    agent._current_run_params = params
    agent._current_run_task_workspace = str(workspace)
    try:
        selected = promote_current_conversation_task(agent)
    finally:
        delattr(agent, "_current_run_params")

    assert selected is None
    links = agent.conversation_store.task_links(conversation.thread_id)
    assert [(link.task_id, link.status, link.goal) for link in links] == [
        ("task-completed", "completed", "不得复制的旧目标")
    ]


def test_progress_update_binds_current_turn_without_old_task_ceremony(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
        updated = TaskProgressTool(agent).execute({"action": "update", "summary": "开工"})
        old_select = TaskProgressTool(agent).execute({"action": "select", "task_id": "task-old"})
        old_start = TaskProgressTool(agent).execute({"action": "start", "new_task": True})
    finally:
        delattr(agent, "_current_run_params")

    assert updated.ok is True
    assert params.task_attributes["conversation_task_id"] == "gw-new"
    assert old_select.ok is False and old_select.error_code == "TOOL_INVALID_ARGUMENTS"
    assert old_start.ok is False and old_start.error_code == "TOOL_INVALID_ARGUMENTS"


def test_first_progress_update_automatically_binds_current_turn(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_first_task",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "帮我做第一个项目")
    params = RunParams(
        request_id="gw-first",
        run_id="gw-first",
        task_id="gw-first",
        root_user_prompt="帮我做第一个项目",
        task_attributes={"conversation_thread_id": conversation.thread_id},
    )
    agent._current_run_params = params
    try:
        started = TaskProgressTool(agent).execute({"action": "update", "summary": "开始第一个项目"})
    finally:
        delattr(agent, "_current_run_params")

    assert started.ok is True
    assert params.task_attributes["conversation_task_id"] == "gw-first"


@pytest.mark.parametrize("prior_status", ["completed", "interrupted"])
@pytest.mark.parametrize(
    "work_tool",
    ["read_file", "list_files", "search_text", "find_files", "write_file"],
)
def test_gateway_inherits_terminal_workspace_and_starts_fresh_execution_at_first_work_tool(
    tmp_path,
    prior_status,
    work_tool,
):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": f"oc_sticky_{prior_status}",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-first", "完成原项目")
    workspace = Path(agent.home_paths.owner_home_dir) / "tasks" / "original-project"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "task-original",
            "goal": "完成原项目",
            "status": "active",
            "task_path": str(workspace),
        }
    )
    agent.conversation_store.update_task_status(
        {"task_id": "task-original", "status": prior_status}
    )

    followup = _conversation_context(agent, request, "gw-followup", "这一句不参与机器判断")
    attrs = _gateway_task_attributes(followup)
    before = agent.conversation_store.task_links(first.thread_id)[0]

    assert attrs is not None
    if prior_status == "completed":
        # completed 是终态任务:不再作为隐式工作区返回,无状态提示、不预填
        # 身份与目录(问题1:新任务消息不再被吸进旧任务目录),新一轮工作走
        # 独立目录。
        assert CONVERSATION_WORKSPACE_TASK_ID_ATTR not in attrs
        assert "conversation_task_id" not in attrs
        assert "run_workspace" not in attrs
    else:
        # interrupted=暂停待恢复:预填身份供 promote 回落续接原目录
        assert attrs[CONVERSATION_WORKSPACE_TASK_ID_ATTR] == "task-original"
        assert attrs[CONVERSATION_WORKSPACE_TASK_STATUS_ATTR] == prior_status
        assert attrs["conversation_task_id"] == "task-original"
        assert attrs["run_workspace"]["task_root"] == str(workspace.resolve())
    assert CONVERSATION_TASK_TURN_ACTIVE_ATTR not in attrs
    assert before.status == prior_status
    assert complete_current_conversation_task(agent, attrs, source="gateway") is False
    assert (
        write_run_task_workspace_if_needed(
            agent,
            ArchiveRunParams(
                do_save=True,
                user_prompt="纯聊天",
                final_response=None,
                archive_tool_calls=[],
                run_request_id="gw-followup",
                run_id="gw-followup",
                task_id="gw-followup",
                source="gateway",
                task_attributes=attrs,
            ),
        )
        == ""
    )
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        root_user_prompt="这一句不参与机器判断",
        task_attributes=attrs,
    )
    agent._current_run_params = params
    try:
        promoted = _promote_work_tool(agent, params, {"tool": work_tool})
    finally:
        delattr(agent, "_current_run_params")

    assert promoted is None
    assert attrs[CONVERSATION_TASK_TURN_ACTIVE_ATTR] is True
    assert attrs["conversation_task_id"] == "gw-followup"
    links = {link.task_id: link for link in agent.conversation_store.task_links(first.thread_id)}
    thread = agent.conversation_store.load_thread(first.thread_id)
    assert links["task-original"].status == prior_status
    assert links["gw-followup"].status == "active"
    if prior_status == "interrupted":
        # 暂停恢复:新执行代数继续用原目录
        new_workspace = workspace
    else:
        # completed 新任务:独立目录,不再占用旧任务目录(问题1)
        new_workspace = Path(links["gw-followup"].task_path)
        assert new_workspace != workspace
    assert links["gw-followup"].task_path == str(new_workspace.resolve())
    assert thread is not None and thread.workspace_task_id == "gw-followup"
    state = json.loads((new_workspace / "work" / "state.json").read_text(encoding="utf-8"))
    identity = json.loads(
        (new_workspace / "work" / "run_workspace.json").read_text(encoding="utf-8")
    )
    assert state["task_id"] == "gw-followup" and state["status"] == "RUNNING"
    assert identity["task_id"] == "gw-followup" and identity["run_id"] == "gw-followup"
    assert 'task_id: "gw-followup"' in (new_workspace / "work" / "task.yaml").read_text(
        encoding="utf-8"
    )


def test_new_ordinary_turn_reuses_sticky_workspace_without_switch_command(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_sticky_new_task",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-first", "完成旧项目")
    old_workspace = Path(agent.home_paths.owner_home_dir) / "tasks" / "old-project"
    (old_workspace / "output").mkdir(parents=True)
    (old_workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "task-old",
            "goal": "旧项目",
            "status": "active",
            "task_path": str(old_workspace),
        }
    )
    agent.conversation_store.select_workspace_task(
        {"thread_id": first.thread_id, "task_id": "task-old"}
    )
    agent.conversation_store.update_task_status({"task_id": "task-old", "status": "completed"})
    followup = _conversation_context(agent, request, "gw-new", "普通用户说的一句话")
    attrs = _gateway_task_attributes(followup)
    params = RunParams(
        request_id="gw-new",
        run_id="gw-new",
        task_id="gw-new",
        root_user_prompt="普通用户说的一句话",
        task_attributes=attrs,
    )
    agent._current_run_params = params
    agent._current_run_task_workspace = str(old_workspace)
    try:
        started = TaskProgressTool(agent).execute(
            {"action": "update", "summary": "按当前用户消息开始本轮工作"}
        )
    finally:
        delattr(agent, "_current_run_params")

    thread = agent.conversation_store.load_thread(first.thread_id)
    links = {
        link.task_id: link.status for link in agent.conversation_store.task_links(first.thread_id)
    }
    assert started.ok is True
    assert thread is not None and thread.workspace_task_id == "gw-new"
    assert attrs["conversation_task_id"] == "gw-new"
    assert attrs[CONVERSATION_TASK_TURN_ACTIVE_ATTR] is True
    # completed 任务不吸附新消息:新任务走独立目录(问题1 真机 2026-08-09:
    # celery 完成后 click/jinja2 等新任务全被吸进 celery 旧目录)。
    assert attrs["run_workspace"]["task_root"] != str(old_workspace)
    assert links["task-old"] == "completed"
    assert links["gw-new"] == "active"


def test_sticky_workspace_superseded_falls_back_to_reusable_task(tmp_path):
    # sticky 指向 superseded 任务时,普通轮次回退复用最近的可复用任务目录,
    # 不因 sticky 失效而开新任务目录(真机 2026-08-08:用户连续消息被截断成
    # 新任务名,模型在新目录找不到旧产物,复刻任务停摆)。
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_sticky_superseded",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-first", "把 scrapy 用 Go 重写")
    workspace = Path(agent.home_paths.owner_home_dir) / "tasks" / "scrapy-go"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    for task_id in ("task-prev", "task-old"):
        agent.conversation_store.bind_task(
            {
                "thread_id": first.thread_id,
                "task_id": task_id,
                "goal": "scrapy 复刻",
                "status": "active" if task_id == "task-old" else "completed",
                "task_path": str(workspace),
            }
        )
    # sticky 指向 task-old,然后它被标记 superseded(sticky 仍指向它)
    agent.conversation_store.select_workspace_task(
        {"thread_id": first.thread_id, "task_id": "task-old"}
    )
    agent.conversation_store.update_task_status(
        {"task_id": "task-old", "status": "superseded"}
    )

    followup = _conversation_context(agent, request, "gw-new", "继续写解析器")
    attrs = _gateway_task_attributes(followup)

    # 全终态(superseded + completed)不再隐式继承任何旧任务身份/目录:
    # 新消息是独立新任务(问题1),后续 promote 走新目录;不再回退复用终态任务。
    assert attrs is not None
    assert CONVERSATION_WORKSPACE_TASK_ID_ATTR not in attrs
    assert "conversation_task_id" not in attrs
    assert "run_workspace" not in attrs


def test_multi_candidate_workspace_prefers_populated_output_over_latest_empty(tmp_path):
    # 多候选工作区(同会话多个可复用任务目录)时,普通消息延续有真实产物的任务,
    # 不选最近创建的 output 空壳目录(空壳=用户消息被截断成任务名,真机
    # 2026-08-08 scrapy 复刻停摆三连)。
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_multi_candidate",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    first = _conversation_context(agent, request, "gw-first", "把 scrapy 用 Go 重写")
    populated = Path(agent.home_paths.owner_home_dir) / "tasks" / "scrapy-go"
    (populated / "output").mkdir(parents=True)
    (populated / "work").mkdir()
    (populated / "output" / "main.go").write_text("package main", encoding="utf-8")
    shell = Path(agent.home_paths.owner_home_dir) / "tasks" / "output-空壳目录"
    (shell / "output").mkdir(parents=True)
    (shell / "work").mkdir()
    # 绑定顺序:先有产物的任务,后空壳任务(空壳为最近创建)
    for task_id, path in (("task-populated", populated), ("task-shell", shell)):
        agent.conversation_store.bind_task(
            {
                "thread_id": first.thread_id,
                "task_id": task_id,
                "goal": "scrapy 复刻",
                "status": "active",
                "task_path": str(path),
            }
        )

    followup = _conversation_context(agent, request, "gw-new", "继续写解析器")
    attrs = _gateway_task_attributes(followup)

    assert attrs is not None
    assert attrs["conversation_task_id"] == "task-populated"
    assert attrs["run_workspace"]["task_root"] == str(populated)


def test_interrupted_workspace_is_reused_at_first_work_tool_without_selection(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_resume_gate",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "完成原项目")
    workspace = Path(agent.home_paths.owner_home_dir) / "tasks" / "original-task"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-interrupted",
            "goal": "完成原项目",
            "status": "active",
            "task_path": str(workspace),
        }
    )
    agent.conversation_store.update_task_status(
        {"task_id": "task-interrupted", "status": "interrupted"}
    )
    agent.conversation_store.select_workspace_task(
        {"thread_id": conversation.thread_id, "task_id": "task-interrupted"}
    )
    followup = _conversation_context(agent, request, "gw-followup", "继续")
    section = _gateway_injections({"inject": []}, followup)[0]
    assert "task-interrupted" not in section
    assert "## Current Task Runtime" in section
    assert str(workspace) not in section
    assert "不要要求用户选择、开始、完成或关闭历史任务" in section
    attrs = _gateway_task_attributes(followup)
    assert attrs is not None
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        root_user_prompt="继续",
        task_attributes=attrs,
    )
    agent._current_run_params = params
    try:
        allowed = _promote_work_tool(agent, params, {"tool": "write_file"})
    finally:
        delattr(agent, "_current_run_params")

    assert allowed is None
    assert params.task_attributes["conversation_task_id"] == "gw-followup"
    assert params.task_attributes["conversation_continued_from_task_id"] == "task-interrupted"
    assert params.task_attributes["run_workspace"]["task_root"] == str(workspace)


def test_exact_mutation_path_reselects_completed_conversation_workspace(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_exact_mutation",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "完成原项目")
    original = tmp_path / "original-task"
    (original / "output" / "project").mkdir(parents=True)
    (original / "work").mkdir()
    target = original / "output" / "project" / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-completed",
            "goal": "完成原项目",
            "status": "active",
            "task_path": str(original),
        }
    )
    agent.conversation_store.update_task_status(
        {"task_id": "task-completed", "status": "completed"}
    )
    placeholder = tmp_path / "placeholder"
    (placeholder / "output").mkdir(parents=True)
    (placeholder / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "gw-followup",
            "goal": "误开的占位任务",
            "status": "active",
            "task_path": str(placeholder),
        }
    )
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        root_user_prompt="这一句不参与机器判断",
        task_attributes={
            "conversation_thread_id": conversation.thread_id,
            "conversation_task_id": "gw-followup",
            "run_workspace": {
                "task_root": str(placeholder),
                "output_dir": str(placeholder / "output"),
                "work_dir": str(placeholder / "work"),
            },
        },
    )
    agent._current_run_params = params
    agent._current_run_task_workspace = str(placeholder)
    try:
        result = _promote_work_tool(
            agent,
            params,
            {
                "tool": "edit_file",
                "path": str(target),
                "old_string": "value = 1",
                "new_string": "value = 2",
            },
        )
    finally:
        delattr(agent, "_current_run_params")

    assert result is None
    successor_id = params.task_attributes["conversation_task_id"]
    assert successor_id not in {"task-completed", "gw-followup"}
    assert params.task_attributes["conversation_continued_from_task_id"] == "task-completed"
    assert params.task_attributes["run_workspace"]["task_root"] == str(original)
    assert params.task_attributes["conversation_rebase_from_task_root"] == str(placeholder)
    links = {
        link.task_id: link.status
        for link in agent.conversation_store.task_links(conversation.thread_id)
    }
    assert links == {
        "task-completed": "completed",
        "gw-followup": "superseded",
        successor_id: "active",
    }


def test_owner_relative_task_mutation_reselects_exact_conversation_workspace(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_owner_relative_mutation",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-first", "完成原项目")
    owner_home = Path(agent.home_paths.owner_home_dir)
    original = owner_home / "tasks" / "2026-07-24" / "original-task"
    (original / "output" / "project").mkdir(parents=True)
    (original / "work").mkdir()
    target = original / "output" / "project" / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-completed",
            "goal": "完成原项目",
            "status": "active",
            "task_path": str(original),
        }
    )
    agent.conversation_store.update_task_status(
        {"task_id": "task-completed", "status": "completed"}
    )
    placeholder = owner_home / "tasks" / "2026-07-25" / "placeholder"
    (placeholder / "output").mkdir(parents=True)
    (placeholder / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "gw-followup",
            "goal": "误开的占位任务",
            "status": "active",
            "task_path": str(placeholder),
        }
    )
    params = RunParams(
        request_id="gw-followup",
        run_id="gw-followup",
        task_id="gw-followup",
        root_user_prompt="这一句不参与机器判断",
        task_attributes={
            "conversation_thread_id": conversation.thread_id,
            "conversation_task_id": "gw-followup",
            "run_workspace": {
                "task_root": str(placeholder),
                "output_dir": str(placeholder / "output"),
                "work_dir": str(placeholder / "work"),
            },
        },
    )
    agent._current_run_params = params
    agent._current_run_task_workspace = str(placeholder)
    try:
        result = _promote_work_tool(
            agent,
            params,
            {
                "tool": "edit_file",
                "path": str(target.relative_to(owner_home)),
                "old_string": "value = 1",
                "new_string": "value = 2",
            },
        )
    finally:
        delattr(agent, "_current_run_params")

    assert result is None
    successor_id = params.task_attributes["conversation_task_id"]
    assert successor_id not in {"task-completed", "gw-followup"}
    assert params.task_attributes["conversation_continued_from_task_id"] == "task-completed"
    assert params.task_attributes["run_workspace"]["task_root"] == str(original)
    links = {
        link.task_id: link.status
        for link in agent.conversation_store.task_links(conversation.thread_id)
    }
    assert links == {
        "task-completed": "completed",
        "gw-followup": "superseded",
        successor_id: "active",
    }


def test_exact_mutation_path_rebases_child_without_superseding_parent_task(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    request = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_child_rebase",
            "channel_user_id": "ou_user1",
            "canonical_user_id": "ou_user1",
        }
    }
    conversation = _conversation_context(agent, request, "gw-parent", "继续现有项目并分工")
    original = tmp_path / "original-task"
    (original / "output" / "project").mkdir(parents=True)
    (original / "work").mkdir()
    target = original / "output" / "project" / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "task-original",
            "goal": "原项目",
            "status": "active",
            "task_path": str(original),
        }
    )
    parent = tmp_path / "new-parent-task"
    (parent / "output").mkdir(parents=True)
    (parent / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": conversation.thread_id,
            "task_id": "gw-parent",
            "goal": "本轮主任务",
            "status": "active",
            "task_path": str(parent),
        }
    )
    child_workspace = tmp_path / "child-runner"
    (child_workspace / "output").mkdir(parents=True)
    (child_workspace / "work").mkdir()
    attrs = {
        "conversation_thread_id": conversation.thread_id,
        "conversation_task_id": "gw-parent",
        "run_workspace": {
            "task_root": str(child_workspace),
            "output_dir": str(child_workspace / "output"),
            "work_dir": str(child_workspace / "work"),
        },
    }
    params = RunParams(
        request_id="subagent-child",
        run_id="subagent-child",
        task_id="gw-parent",
        root_user_prompt="子代理只按结构化路径工作",
        task_attributes=attrs,
    )
    agent._current_run_params = params
    agent._current_run_task_workspace = str(child_workspace)
    previous = set_current_subagent_context(
        agent,
        run_id="subagent-child",
        task_attributes=attrs,
    )
    try:
        result = _promote_work_tool(
            agent,
            params,
            {
                "tool": "edit_file",
                "path": str(target),
                "old_string": "value = 1",
                "new_string": "value = 2",
            },
        )
    finally:
        restore_current_subagent_context(agent, previous)
        delattr(agent, "_current_run_params")

    assert result is None
    assert attrs["conversation_task_id"] == "gw-parent"
    assert attrs["run_workspace"]["task_root"] == str(original)
    assert attrs["conversation_rebase_from_task_root"] == str(child_workspace)
    assert attrs["conversation_subagent_workspace_rebase"] == {
        "task_id": "task-original",
        "task_root": str(original),
    }
    links = {
        link.task_id: link.status
        for link in agent.conversation_store.task_links(conversation.thread_id)
    }
    assert links == {"task-original": "active", "gw-parent": "active"}


def test_subagent_completion_cannot_close_parent_conversation_task(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
    }

    assert complete_current_conversation_task(
        agent,
        attrs,
        source="subagent_run_model_turn",
        current_task_id=child_id,
    )
    links = agent.conversation_store.active_task_links_report(conversation.thread_id)[0]
    assert all(link.task_id != child_id for link in links)


def test_structured_task_tool_promotes_natural_language_chat_internally(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
        },
    )
    agent._current_run_params = params
    try:
        link = promote_current_conversation_task(agent)
    finally:
        delattr(agent, "_current_run_params")
    assert link is not None and link.task_id == "gw-work"
    assert params.task_attributes["conversation_task_id"] == "gw-work"
    stored = agent.conversation_store.thread_for_task("gw-work")
    assert stored.thread_id == conversation.thread_id
    link = agent.conversation_store.active_task_links_report(conversation.thread_id)[0][0]
    assert link.task_path
    assert params.task_attributes["run_workspace"]["task_root"] == link.task_path
    assert agent._current_run_task_workspace == link.task_path


def test_gateway_followup_archive_reuses_active_task_workspace(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    workspace = (
        tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-03" / "analysis"
    )
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


def test_background_child_archive_cannot_overwrite_parent_goal_workspace_or_index_title(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )

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


def test_gateway_subagent_relative_outputs_use_validated_client_cwd(tmp_path):
    service_root = tmp_path / "service"
    client_root = tmp_path / "client-project"
    task_root = tmp_path / "home" / "task"
    client_root.mkdir()
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        service_root,
    )
    agent._current_run_task_workspace = str(task_root)
    agent._current_run_params = RunParams(
        request_id="gw-cwd-child",
        run_id="gw-cwd-child",
        task_id="gw-cwd-child",
        task_attributes={
            "conversation_thread_id": "thread-cwd-child",
            "conversation_task_id": "gw-cwd-child",
            "conversation_execution_cwd": str(client_root),
            "conversation_runtime_workspace_roots": [str(client_root)],
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            },
        },
    )
    try:
        create_params = create_run_params(
            agent,
            {
                "goal": "实现 bbb 的页面",
                "output_files": ["bbb/index.html"],
                "allowed_tools": ["write_file"],
            },
            "实现 bbb 的页面",
            ["write_file"],
        )
    finally:
        delattr(agent, "_current_run_params")

    assert create_params.attributes["output_refs"] == [
        str((client_root / "bbb" / "index.html").resolve())
    ]


def test_gateway_subagent_inherits_validated_client_workspace_without_output_files(
    tmp_path,
):
    service_root = tmp_path / "service"
    client_root = tmp_path / "client-project"
    task_root = tmp_path / "home" / "task"
    client_root.mkdir()
    (client_root / "source.txt").write_text("original", encoding="utf-8")
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        service_root,
    )
    agent._current_run_task_workspace = str(task_root)
    agent._current_run_params = RunParams(
        request_id="gw-cwd-inherit-child",
        run_id="gw-cwd-inherit-child",
        task_id="gw-cwd-inherit-child",
        task_attributes={
            "conversation_thread_id": "thread-cwd-inherit-child",
            "conversation_task_id": "gw-cwd-inherit-child",
            CONVERSATION_EXECUTION_CWD_ATTR: str(client_root),
            CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR: [str(client_root)],
        },
    )
    try:
        create_params = create_run_params(
            agent,
            {"goal": "读取现有项目并在项目内实现功能", "allowed_tools": ["read_file", "write_file"]},
            "读取现有项目并在项目内实现功能",
            ["read_file", "write_file"],
        )
        task = agent.subagents.create_run(params=create_params)
        context = agent.subagents.runner_context.write_execution_context(task.id)
    finally:
        delattr(agent, "_current_run_params")

    client_root_text = str(client_root.resolve())
    assert create_params.extra_write_roots == [client_root_text]
    assert task.attributes["workspace_root"] == client_root_text
    assert task.attributes["workspace_roots"] == [client_root_text]
    assert client_root_text in task.allowed_write_roots
    assert context.context_bundle["workspace_refs"]["owner_workspace_dir"] == client_root_text
    assert context.write_boundary["execution_cwd"] == client_root_text
    assert client_root_text in context.write_boundary["allowed_write_roots"]


def test_gateway_subagent_records_originating_conversation_request(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
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


def _conversation_context(agent: SimpleAgent, request: dict, request_id: str, prompt: str):
    return _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, request_id, prompt)
    )
