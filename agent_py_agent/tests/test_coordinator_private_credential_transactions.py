"""原冷C凭据/OAuth回调真实事务；只用临时owner、Echo与合成运输。"""

import http.client
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    input_dependency_path,
)
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
from agent_py_agent.agent.user_space import feishu_oauth as oauth
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_credential_callback_replay import _card


# LLM: 原Agent只在进入C前创建原任务/输入/签名和密钥；C中禁止任何新Agent或foreign pool，原HTTP/G1/lease均真实。
# 函数用途: 给本次隔离回调准备原卡及冷C设施，不启动W、真实模型或外部服务。
@contextmanager
def original_case(tmp_path, monkeypatch, *, operation="submit", kind="user", pending_peer=False):
    home = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    assert home != (Path.home() / ".my-agent").resolve()
    agent, path, row, body = _card(tmp_path, kind)
    if operation == "oauth_cancel":
        agent.config.feishu_app_id = "synthetic-app"
        agent.config.feishu_app_secret = "Synthetic-Deployment-Secret"
        capability = CapabilityConfig(
            feishu_oauth_redirect_uri="https://callback.example.invalid/feishu/oauth/callback"
        )
        monkeypatch.setattr(oauth, "deployment_capability_config", lambda _home: capability)
        oauth.request_oauth(agent)
        path = oauth.oauth_path(agent, row["actor_id"])
        row = json.loads(path.read_text())
        control = row["im_control"]
        body = {
            "reference": row["state"],
            "decision": "cancel",
            "message_id": control["message_id"],
            "chat_id": control["chat_id"],
        }
    else:
        body["decision"] = operation
    from agent_py_agent.agent.session_lock.service import UnlockService
    from agent_py_agent.agent.session_lock.store import SessionLockStore

    service = UnlockService(SessionLockStore(home / "session_lock/private_chat_locks.db"))
    service.set_password(row["actor_id"], "Synthetic-Private-Password-123")
    arm_input_dependency(agent, row["input_id"])
    if pending_peer:
        from agent_py_agent.agent.user_space.task_credentials import (
            CredentialCardOptions,
            credential_path,
            request_credential_card,
        )

        peer_ref, _ = request_credential_card(
            agent,
            url="https://peer.example.invalid",
            authentication="bearer",
            call_id="peer-input",
            options=CredentialCardOptions(access_scope="task"),
        )
        peer = json.loads(credential_path(agent.home_paths.owner_home_dir, peer_ref).read_text())
        arm_input_dependency(agent, peer["input_id"])
    if kind == "main":
        from agent_py_agent.agent.user_space.admin_channel_identity import (
            bind_admin_channel_identity,
        )

        bind_admin_channel_identity(home, "feishu", row["actor_id"])
    config = _config_with_owner(agent.config, OwnerIdentity.local_main())
    config = replace(config, conversation_workspace="")
    paths = gateway_paths_from_root(tmp_path / "gateway")
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import request_worker

    def forbidden(*_args, **_kwargs):
        raise AssertionError("C回调不能构造Agent/foreign pool")

    monkeypatch.setattr(SimpleAgent, "__init__", forbidden)
    monkeypatch.setattr(request_worker, "_owner_pool", forbidden)
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "private-C")
        control = GatewayControlPlane(
            config,
            home,
            paths,
            2,
            controller.c_run_id,
            agent_root=agent.root,
            workspace_roots=tuple(agent.workspace_roots),
        )
        runtime = GatewayDrainRuntime(control)
        server = GatewayHTTPServer(
            0, paths, params=GatewayHTTPServerParams(control_plane=control, drain_runtime=runtime)
        )
        try:
            server.start()
            yield agent, path, row, body, server, runtime
        finally:
            server.stop()


# LLM: 凭据只走原本机SDK回调HTTP的G1头；合成fields不记录到测试命令/日志，也不替换授权门。
# 函数用途: 向原冷C监听器提交一次原卡决定。
def post(server, row, body, *, actor=None, credential=True):
    connection = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=3)
    headers = {
        "Content-Type": "application/json",
        "X-Channel": "feishu",
        "X-User-Id": actor or row["actor_id"],
    }
    if credential:
        headers["X-Gateway-Token"] = server._local_client_credential
    try:
        connection.request("POST", "/client/private-interaction", json.dumps(body), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize(
    "kind,operation",
    [
        ("user", "submit"),
        ("user", "cancel"),
        ("user", "oauth_cancel"),
        ("group", "submit"),
        ("group", "cancel"),
    ],
)
def test_cold_c_original_private_card_settles_exact_input(tmp_path, monkeypatch, operation, kind):
    with original_case(tmp_path, monkeypatch, operation=operation, kind=kind) as (
        agent,
        path,
        row,
        body,
        server,
        runtime,
    ):
        status, result = post(server, row, body)
        assert status == 200 and result["ok"] is True, result
        saved = json.loads(path.read_text())
        assert saved["status"] == ("ready" if operation == "submit" else "cancelled")
        dependency = json.loads(input_dependency_path(agent, row["input_id"]).read_text())
        assert dependency["status"] == saved["status"]
        assert (
            row["input_id"]
            not in agent.conversation_store.tasks.load(
                dependency["scope"]["root_task_id"]
            ).input_dependency_refs
        )
        assert server.agent is None and runtime.control is server.control_plane
        assert "SYNTHETIC_FIRST" not in path.read_text()


@pytest.mark.parametrize("fault", ["credential", "actor", "message", "chat", "reference"])
def test_cold_c_rejects_untrusted_or_foreign_original_card_without_mutation(
    tmp_path, monkeypatch, fault
):
    with original_case(tmp_path, monkeypatch) as (agent, path, row, body, server, _runtime):
        before = path.read_bytes()
        kwargs = {}
        if fault == "credential":
            kwargs["credential"] = False
        elif fault == "actor":
            kwargs["actor"] = "ou_other"
        else:
            body[fault if fault != "message" else "message_id"] = "wrong"
        if fault == "chat":
            body["chat_id"] = body.pop("chat")
        status, result = post(server, row, body, **kwargs)
        assert status == 403
        if fault == "credential":
            assert result["error_code"] == "LOCAL_CREDENTIAL_REQUIRED"
        else:
            assert result["ok"] is False
        assert path.read_bytes() == before
        assert (
            json.loads(input_dependency_path(agent, row["input_id"]).read_text())["status"]
            == "pending"
        )


@pytest.mark.parametrize("operation", ["submit", "cancel", "oauth_cancel"])
def test_cold_c_existing_sql_lock_preserves_partial_commit_and_replays_exact_projection(
    tmp_path, monkeypatch, operation
):
    import sqlite3
    import time

    with original_case(tmp_path, monkeypatch, operation=operation, pending_peer=True) as (
        agent,
        path,
        row,
        body,
        server,
        _runtime,
    ):
        repo = agent.subagents.runtime_db
        connection = sqlite3.connect(repo.db_path, timeout=0)
        connection.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            status, result = post(server, row, body)
            assert status == 503 and result["error_code"] == "IM_INTERACTION_HOST_UNAVAILABLE"
            assert time.monotonic() - started < 1.0
            saved = path.read_bytes()
            dependency = json.loads(input_dependency_path(agent, row["input_id"]).read_text())
            assert dependency["status"] == ("ready" if operation == "submit" else "cancelled")
            wakes = agent.conversation_store.wakes.pending()
        finally:
            connection.rollback()
            connection.close()
        status, result = post(server, row, body)
        assert status == 200 and result["ok"] is True
        assert path.read_bytes() == saved
        assert [wake.wake_signal_id for wake in agent.conversation_store.wakes.pending()] == [
            wake.wake_signal_id for wake in wakes
        ]
        task_id = dependency["scope"]["root_task_id"]
        link = agent.conversation_store.tasks.load(task_id)
        assert row["input_id"] not in link.input_dependency_refs and link.input_dependency_refs
        deadlines = [
            json.loads(input_dependency_path(agent, key).read_text())["expires_at"]
            for key in link.input_dependency_refs
        ]
        with sqlite3.connect(repo.db_path) as check:
            actual = check.execute(
                "SELECT next_due_at FROM wake_queue WHERE root_task_id=? AND status != ?",
                (task_id, "woke"),
            ).fetchone()[0]
        assert actual == min(deadlines)


def test_existing_only_runtime_missing_does_not_create_or_initialize(tmp_path, monkeypatch):
    import sqlite3

    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import RuntimeSchemaMixin

    monkeypatch.setattr(
        RuntimeSchemaMixin,
        "_init_runtime_schema",
        lambda _self: pytest.fail("existing-only不能初始化"),
    )
    path = tmp_path / "missing" / "runtime.db"
    repo = RuntimeRepository(path, existing_only=True)
    with pytest.raises(sqlite3.OperationalError):
        repo.upsert_wake(root_task_id="original", next_due_at=1)
    assert not path.parent.exists()


def test_session_lock_read_only_is_original_status_without_schema_or_write(tmp_path, monkeypatch):
    import sqlite3

    from agent_py_agent.agent.session_lock.service import UnlockService
    from agent_py_agent.agent.session_lock.store import SessionLockStore

    path = tmp_path / "locks" / "lock.db"
    original = SessionLockStore(path)
    UnlockService(original).set_password("actor", "Synthetic-Private-Password-123")
    monkeypatch.setattr(SessionLockStore, "_init", lambda _self: pytest.fail("只读不能迁移"))
    readonly = SessionLockStore(path, read_only=True)
    assert UnlockService(readonly).status("actor").locked is False
    with pytest.raises(sqlite3.OperationalError):
        readonly.record_activity("another")
    missing = SessionLockStore(tmp_path / "missing" / "none.db", read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        missing.has_password("actor")
    assert not (tmp_path / "missing").exists()


def test_cold_c_stop_after_row_commit_keeps_input_pending_and_no_late_projection(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.user_space import task_credentials as tc

    with original_case(tmp_path, monkeypatch) as (agent, path, row, body, server, runtime):
        saved = []
        original = tc._save_credential_row

        def close_after_row(*args):
            result = original(*args)
            saved.append(path.read_bytes())
            runtime.stop_producers()
            return result

        monkeypatch.setattr(tc, "_save_credential_row", close_after_row)
        status, result = post(server, row, body)
        assert status == 503 and result["error_code"] == "IM_INTERACTION_HOST_UNAVAILABLE"
        assert path.read_bytes() == saved[0] and json.loads(saved[0])["status"] == "ready"
        assert (
            json.loads(input_dependency_path(agent, row["input_id"]).read_text())["status"]
            == "pending"
        )
        assert not agent.conversation_store.wakes.pending()
        assert (
            row["input_id"]
            in agent.conversation_store.tasks.load(
                row["scope"]["root_task_id"]
            ).input_dependency_refs
        )


def test_cold_c_copy_and_closed_original_source_never_gain_late_write(tmp_path, monkeypatch):
    import contextvars
    import copy

    from agent_py_agent.agent.gateway_parts import private_interaction_transaction as pi

    errors = []
    original = pi._PrivateInteractionTransaction.close

    def closed(source):
        with pytest.raises(pi.GlobalWriteError):
            copy.copy(source).perform(lambda: pytest.fail("复制源不能写"))
        context = contextvars.copy_context()
        original(source)
        with pytest.raises(pi.GlobalWriteError):
            context.run(source.perform, lambda: pytest.fail("退出后不能写"))
        errors.append(True)

    monkeypatch.setattr(pi._PrivateInteractionTransaction, "close", closed)
    with original_case(tmp_path, monkeypatch) as (_agent, _path, row, body, server, _runtime):
        assert post(server, row, body)[1]["ok"] is True
    assert errors == [True]


def test_cold_c_secret_stays_outside_request_command_transcript_and_audit(tmp_path, monkeypatch):
    with original_case(tmp_path, monkeypatch) as (agent, path, row, body, server, _runtime):
        assert post(server, row, body)[1]["ok"] is True
        secret = body["fields"]["secret"].encode()
        for root in (
            server.paths.root,
            agent.conversation_store.storage.root,
            agent.home_paths.owner_audit_dir,
        ):
            for candidate in root.rglob("*"):
                if candidate.is_file():
                    assert secret not in candidate.read_bytes()
        assert secret not in path.read_bytes()


def test_cold_c_corrupt_input_binding_cannot_settle_another_original_card(tmp_path, monkeypatch):
    with original_case(tmp_path, monkeypatch, pending_peer=True) as (
        agent,
        path,
        row,
        body,
        server,
        _runtime,
    ):
        task_id = row["scope"]["root_task_id"]
        peer = next(
            key
            for key in agent.conversation_store.tasks.load(task_id).input_dependency_refs
            if key != row["input_id"]
        )
        changed = json.loads(path.read_text())
        changed["input_id"] = peer
        path.write_text(json.dumps(changed))
        peer_path = input_dependency_path(agent, peer)
        peer_before = peer_path.read_bytes()
        original_before = input_dependency_path(agent, row["input_id"]).read_bytes()
        row_before = path.read_bytes()
        link_before = agent.conversation_store.tasks.load(task_id)
        status, result = post(server, row, body)
        assert status == 403 and result["ok"] is False
        assert peer_path.read_bytes() == peer_before
        assert input_dependency_path(agent, row["input_id"]).read_bytes() == original_before
        assert path.read_bytes() == row_before
        assert agent.conversation_store.tasks.load(task_id) == link_before
        assert not agent.conversation_store.wakes.pending()
