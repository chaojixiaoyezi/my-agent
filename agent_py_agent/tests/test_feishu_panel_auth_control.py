"""Tests for Feishu agent panel authorization, roster projection, and control facade."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.agent_control import (
    AgentControlError,
    enqueue_agent_stop,
    list_authorized_task_panel,
    read_agent_view,
    send_agent_guidance,
    stop_agent,
)
from agent_py_agent.agent.conversation.execution_threads import (
    EXECUTION_ORIGIN_KEY,
    IM_TASK_EXECUTION_KEY,
)
from agent_py_agent.agent.conversation.session_tasks import (
    SessionTaskDraft,
    SessionTaskStore,
    SessionTaskUpdate,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_gateway_json(port: int, path: str, payload: dict, agent: object) -> tuple[int, dict]:
    credentials = GatewayClientCredentials(agent.home_paths.root)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **credentials.headers(),
            "X-User-Id": str(payload.get("user_id") or ""),
            "X-Channel": str(payload.get("channel") or ""),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _setup_test_agent(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path / "workspace",
    )
    store = agent.conversation_store
    sender_thread = store.threads.get_or_create(
        {
            "canonical_user_id": "feishu-user-1",
            "channel": "feishu",
            "channel_conversation_id": "conv-sender-1",
            "channel_user_id": "feishu-user-1",
        }
    )
    scope = GatewayControlScope("feishu-user-1", "feishu", "conv-sender-1")
    return agent, store, sender_thread, scope


def test_delegated_session_task_authorization_success(tmp_path) -> None:
    agent, store, sender_thread, scope = _setup_test_agent(tmp_path)

    target_thread = store.threads.get_or_create(
        {
            "canonical_user_id": "feishu-user-1",
            "channel": "work_target",
            "channel_conversation_id": "work_conv_1",
            "channel_user_id": "feishu-user-1",
        }
    )

    st = store.session_tasks.create(
        SessionTaskDraft(
            sender_thread.thread_id,
            target_thread.thread_id,
            "delegated analysis task",
        )
    )
    store.session_tasks.advance(st.task_id, SessionTaskUpdate(status="accepted"))

    root_task_id = "root-delegated-1"
    store.tasks.bind(
        {
            "thread_id": target_thread.thread_id,
            "task_id": root_task_id,
            "goal": "delegated root goal",
            "status": "active",
            "now": time.time(),
        }
    )

    origin_identity = {
        "source_thread_id": sender_thread.thread_id,
        "kind": "delegated",
        "operation_key": "op-test-1",
        "binding": {
            "channel": "feishu",
            "conversation_id": "conv-sender-1",
            "actor_id": "feishu-user-1",
        },
    }
    execution_policy = {
        "mode": "delegated",
        "session_task_id": st.task_id,
        "root_task_id": root_task_id,
    }
    store.threads.update_atomic(
        target_thread.thread_id,
        lambda current: replace(
            current,
            metadata={
                **current.metadata,
                EXECUTION_ORIGIN_KEY: origin_identity,
                IM_TASK_EXECUTION_KEY: execution_policy,
            },
        ),
    )

    subagent = agent.subagents.create_run(
        goal="execute subtask in target branch",
        root_id=root_task_id,
        parent_id=root_task_id,
    )
    subagent = agent.subagents.lifecycle.prepare_runner_attempt(subagent.id)

    # 1. read_agent_view with session_task_id and bound_root_id
    view = read_agent_view(
        agent,
        scope=scope,
        run_id=subagent.id,
        session_task_id=st.task_id,
        bound_root_id=root_task_id,
    )
    assert view["ok"] is True
    assert view["agent"]["run_id"] == subagent.id

    # 2. send_agent_guidance
    guidance = send_agent_guidance(
        agent,
        scope=scope,
        run_id=subagent.id,
        message="check bounded index",
        message_id="msg-guidance-1",
        session_task_id=st.task_id,
        bound_root_id=root_task_id,
    )
    assert guidance["ok"] is True
    assert guidance["run_id"] == subagent.id

    # 3. stop_agent
    stopped = stop_agent(
        agent,
        scope=scope,
        run_id=subagent.id,
        operation_id="op-stop-1",
        session_task_id=st.task_id,
        bound_root_id=root_task_id,
    )
    assert stopped["ok"] is True
    assert stopped["status"] in {"cancelled", "preserved"}


def test_cross_conversation_forged_session_task_rejected(tmp_path) -> None:
    agent, store, sender_thread, scope = _setup_test_agent(tmp_path)

    other_thread = store.threads.get_or_create(
        {
            "canonical_user_id": "other-user",
            "channel": "feishu",
            "channel_conversation_id": "conv-other",
            "channel_user_id": "other-user",
        }
    )
    foreign_st = store.session_tasks.create(
        SessionTaskDraft(
            other_thread.thread_id,
            other_thread.thread_id,
            "foreign task",
        )
    )

    # Non-existent session_task_id -> 404 AGENT_TASK_NOT_FOUND
    with pytest.raises(AgentControlError) as exc_info:
        read_agent_view(
            agent,
            scope=scope,
            run_id="any-run",
            session_task_id="missing-task-id",
        )
    assert exc_info.value.status == 404
    assert exc_info.value.error_code == "AGENT_TASK_NOT_FOUND"

    # Foreign session_task_id belonging to other thread -> 403 AGENT_OUTSIDE_CONVERSATION
    with pytest.raises(AgentControlError) as exc_info:
        read_agent_view(
            agent,
            scope=scope,
            run_id="any-run",
            session_task_id=foreign_st.task_id,
        )
    assert exc_info.value.status == 403
    assert exc_info.value.error_code == "AGENT_OUTSIDE_CONVERSATION"

    # Foreign session_task_id in list_authorized_task_panel -> 403 AGENT_OUTSIDE_CONVERSATION
    with pytest.raises(AgentControlError) as exc_info:
        list_authorized_task_panel(
            agent,
            scope=scope,
            session_task_id=foreign_st.task_id,
        )
    assert exc_info.value.status == 403
    assert exc_info.value.error_code == "AGENT_OUTSIDE_CONVERSATION"


def test_queued_session_task_panel_projection_no_404(tmp_path) -> None:
    agent, store, sender_thread, scope = _setup_test_agent(tmp_path)

    target_thread = store.threads.get_or_create(
        {
            "canonical_user_id": "feishu-user-1",
            "channel": "work_target",
            "channel_conversation_id": "work_conv_queued",
            "channel_user_id": "feishu-user-1",
        }
    )
    st = store.session_tasks.create(
        SessionTaskDraft(
            sender_thread.thread_id,
            target_thread.thread_id,
            "queued coordinator task",
        )
    )

    origin_identity = {
        "source_thread_id": sender_thread.thread_id,
        "kind": "delegated",
        "operation_key": "op-test-queued",
        "binding": {
            "channel": "feishu",
            "conversation_id": "conv-sender-1",
            "actor_id": "feishu-user-1",
        },
    }
    execution_policy = {
        "mode": "delegated",
        "session_task_id": st.task_id,
        "root_task_id": "",
    }
    store.threads.update_atomic(
        target_thread.thread_id,
        lambda current: replace(
            current,
            metadata={
                **current.metadata,
                EXECUTION_ORIGIN_KEY: origin_identity,
                IM_TASK_EXECUTION_KEY: execution_policy,
            },
        ),
    )

    # Must succeed (ok: True) without 404
    panel = list_authorized_task_panel(
        agent,
        scope=scope,
        session_task_id=st.task_id,
    )
    assert panel["ok"] is True
    assert panel["schema_version"] == "feishu_task_panel.v1"
    assert panel["selected_task"]["session_task_id"] == st.task_id
    assert panel["selected_task"]["status"] == "queued"
    assert len(panel["nodes"]) == 1
    main_node = panel["nodes"][0]
    assert main_node["node_kind"] == "background_main"
    assert main_node["can_guide"] is False
    assert main_node["can_stop_branch"] is False
    assert panel["selected_agent_view"] is None


def test_foreground_historical_bound_root_id_after_advancement(tmp_path) -> None:
    agent, store, sender_thread, scope = _setup_test_agent(tmp_path)

    # Round 1 root
    root_1 = "task-root-round-1"
    store.tasks.bind(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_1,
            "goal": "round 1 goal",
            "status": "active",
            "now": time.time(),
        }
    )
    store.tasks.select_workspace_task(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_1,
            "now": time.time(),
        }
    )
    sub1 = agent.subagents.create_run(
        goal="subagent round 1",
        root_id=root_1,
        parent_id=root_1,
    )
    sub1 = agent.subagents.lifecycle.prepare_runner_attempt(sub1.id)

    # Round 2 advances sender_thread.workspace_task_id
    root_2 = "task-root-round-2"
    store.tasks.bind(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_2,
            "goal": "round 2 goal",
            "status": "active",
            "now": time.time(),
        }
    )
    store.tasks.select_workspace_task(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_2,
            "now": time.time(),
        }
    )

    # Verify that sub1 from round 1 can still be viewed using bound_root_id=root_1
    view = read_agent_view(
        agent,
        scope=scope,
        run_id=sub1.id,
        bound_root_id=root_1,
    )
    assert view["ok"] is True
    assert view["agent"]["run_id"] == sub1.id

    # Panel with bound_root_id=root_1 selects round 1
    panel = list_authorized_task_panel(
        agent,
        scope=scope,
        bound_root_id=root_1,
    )
    assert panel["ok"] is True
    assert panel["selected_task"]["bound_root_id"] == root_1
    node_ids = [n["node_id"] for n in panel["nodes"]]
    assert f"run:{sub1.id}" in node_ids


def test_persistent_team_identity_projected_as_background_main(tmp_path) -> None:
    agent, store, sender_thread, scope = _setup_test_agent(tmp_path)

    # Persistent /team target thread without IM_TASK_EXECUTION_KEY
    team_thread = store.threads.get_or_create(
        {
            "canonical_user_id": "feishu-user-1",
            "channel": "team_persistent",
            "channel_conversation_id": "team_conv_1",
            "channel_user_id": "feishu-user-1",
        }
    )
    team_root = "team-root-1"
    store.tasks.bind(
        {
            "thread_id": team_thread.thread_id,
            "task_id": team_root,
            "goal": "team coordinator root",
            "status": "active",
            "now": time.time(),
        }
    )
    store.tasks.select_workspace_task(
        {
            "thread_id": team_thread.thread_id,
            "task_id": team_root,
            "now": time.time(),
        }
    )

    st = store.session_tasks.create(
        SessionTaskDraft(
            sender_thread.thread_id,
            team_thread.thread_id,
            "team task",
        )
    )

    panel = list_authorized_task_panel(
        agent,
        scope=scope,
        session_task_id=st.task_id,
        bound_root_id=team_root,
    )
    assert panel["ok"] is True
    main_nodes = [n for n in panel["nodes"] if n["node_kind"] == "background_main"]
    assert len(main_nodes) == 1
    assert main_nodes[0]["can_guide"] is False
    assert main_nodes[0]["can_stop_branch"] is False


def test_enqueue_agent_stop_idempotency_and_already_terminal(tmp_path, monkeypatch) -> None:
    agent, store, sender_thread, scope = _setup_test_agent(tmp_path)

    root_id = "task-root-stop"
    store.tasks.bind(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_id,
            "goal": "stop test root",
            "status": "active",
            "now": time.time(),
        }
    )
    store.tasks.select_workspace_task(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_id,
            "now": time.time(),
        }
    )

    child = agent.subagents.create_run(
        goal="subagent for stop",
        root_id=root_id,
        parent_id=root_id,
    )
    child = agent.subagents.lifecycle.prepare_runner_attempt(child.id)

    # Case A: in-flight duplicate returns status='accepted' and already_active=True
    entered_cleanup = threading.Event()
    release_cleanup = threading.Event()

    from agent_py_agent.agent.conversation import agent_control as agent_control_mod

    original_cleanup = agent_control_mod.cleanup_subagent_tree

    def delayed_cleanup(batch, run_id):
        entered_cleanup.set()
        release_cleanup.wait(timeout=5.0)
        return original_cleanup(batch, run_id)

    monkeypatch.setattr(agent_control_mod, "cleanup_subagent_tree", delayed_cleanup)

    first = enqueue_agent_stop(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="op-stop-first",
    )
    assert first["status"] == "accepted"
    assert first["already_active"] is False

    assert entered_cleanup.wait(timeout=5.0)

    # In-flight duplicate call
    dup = enqueue_agent_stop(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="op-stop-dup",
    )
    assert dup["status"] == "accepted"
    assert dup["already_active"] is True

    # Allow cleanup to complete
    release_cleanup.set()
    time.sleep(0.1)

    # Case B: post-cleanup replay returns confirmed
    replay = enqueue_agent_stop(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="op-stop-first",
    )
    assert replay["status"] == "confirmed"
    assert replay["replayed"] is True
    assert replay["already_active"] is False

    # Case C: DONE/FAILED subagent stop returns status='already_terminal' without overwriting status
    child_done = agent.subagents.create_run(
        goal="subagent already done",
        root_id=root_id,
        parent_id=root_id,
    )
    child_done = agent.subagents.lifecycle.prepare_runner_attempt(child_done.id)
    child_done.status = "DONE"
    agent.subagents.save(child_done)

    term_stop = enqueue_agent_stop(
        agent,
        scope=scope,
        run_id=child_done.id,
        operation_id="op-stop-terminal",
    )
    assert term_stop["status"] == "already_terminal"
    assert term_stop["already_active"] is False
    # Verify subagent status remains DONE
    reloaded = agent.subagents.load(child_done.id)
    assert reloaded.status == "DONE"


def test_bounded_list_authorized_task_panel_never_calls_list_report(tmp_path, monkeypatch) -> None:
    agent, store, sender_thread, scope = _setup_test_agent(tmp_path)

    def guard_list_report(*args, **kwargs):
        raise AssertionError("SessionTaskStore.list_report called unexpectedly!")

    monkeypatch.setattr(SessionTaskStore, "list_report", guard_list_report)

    # Must succeed using only bounded page reads
    panel = list_authorized_task_panel(agent, scope=scope)
    assert panel["ok"] is True


def test_gateway_http_wire_roundtrip(tmp_path) -> None:
    agent, store, sender_thread, scope = _setup_test_agent(tmp_path)

    root_id = "task-root-wire"
    store.tasks.bind(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_id,
            "goal": "wire test root",
            "status": "active",
            "now": time.time(),
        }
    )
    store.tasks.select_workspace_task(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_id,
            "now": time.time(),
        }
    )
    child = agent.subagents.create_run(
        goal="wire subagent",
        root_id=root_id,
        parent_id=root_id,
    )
    child = agent.subagents.lifecycle.prepare_runner_attempt(child.id)

    port = _free_port()
    gateway_paths = GatewayPaths(
        root=tmp_path / "gw",
        pid=tmp_path / "gw" / "gateway.pid",
        adapter_pid=tmp_path / "gw" / "adapter.pid",
        state=tmp_path / "gw" / "state.json",
        heartbeat=tmp_path / "gw" / "heartbeat.json",
        stop_request=tmp_path / "gw" / "stop.request",
        log=tmp_path / "gw" / "gateway.log",
        inbox=tmp_path / "gw" / "pending",
        processing=tmp_path / "gw" / "processing",
        done=tmp_path / "gw" / "done",
        failed=tmp_path / "gw" / "failed",
        responses=tmp_path / "gw" / "responses",
        history=tmp_path / "gw" / "history.jsonl",
    )
    server = GatewayHTTPServer(
        port,
        gateway_paths,
        params=GatewayHTTPServerParams(agent=agent),
    )
    server.start()

    try:
        common_body = {
            "user_id": "feishu-user-1",
            "channel": "feishu",
            "conversation_id": "conv-sender-1",
        }

        # 1. POST /client/agent-view with mode='panel' -> HTTP 200
        code, panel_resp = _post_gateway_json(
            port,
            "/client/agent-view",
            {**common_body, "mode": "panel"},
            agent,
        )
        assert code == 200
        assert panel_resp["schema_version"] == "feishu_task_panel.v1"
        assert panel_resp["ok"] is True

        # 2. POST /client/agent-view with default mode -> HTTP 200
        code, view_resp = _post_gateway_json(
            port,
            "/client/agent-view",
            {**common_body, "run_id": child.id},
            agent,
        )
        assert code == 200
        assert view_resp["ok"] is True
        assert view_resp["agent"]["run_id"] == child.id

        # 3. POST /client/agent-guidance -> HTTP 202
        code, guide_resp = _post_gateway_json(
            port,
            "/client/agent-guidance",
            {
                **common_body,
                "run_id": child.id,
                "message": "test guidance wire",
                "message_id": "wire-msg-1",
            },
            agent,
        )
        assert code == 202
        assert guide_resp["ok"] is True

        # 4. POST /client/agent-stop -> HTTP 202 (accepted)
        code, stop_resp = _post_gateway_json(
            port,
            "/client/agent-stop",
            {
                **common_body,
                "run_id": child.id,
                "operation_id": "wire-stop-1",
            },
            agent,
        )
        assert code == 202
        assert stop_resp["status"] == "accepted"

    finally:
        server.stop()
