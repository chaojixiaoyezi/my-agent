"""托管 MCP 的真实临时进程组件验收；不启动 Gateway/TUI，不代替完整插件安装或真实模型测试。"""

import atexit
import json
import threading
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import plugin_catalog_digest
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.tooling import mcp_managed_process, mcp_transport
from agent_py_agent.agent.tooling.background_process_launch import BackgroundLaunchError
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPStdioClient
from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.agent.tooling.process_registry import _process_instance_terminated
from agent_py_agent.agent.tooling.process_session_cleanup import (
    ProcessSessionCleanup,
    ProcessSessionCleanupError,
)
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests.test_mcp_client import _ECHO_SERVER, _config
from agent_py_agent.tests.test_mcp_lifecycle import _run_thread
from agent_py_agent.tests.test_plugin_activation import revocation
from agent_py_agent.tests.test_plugin_activation_ref import plugin_reference


# LLM: 激活夹具不证明静态目录匹配；这里专门验证原进程、协议、排队和撤销，不使用产品外的任务执行路径冒充 TUI。
# 函数用途: 建立临时 MCP 客户端，所有资源由测试 finally 用原固定句柄清理。
def managed_client(tmp_path, *, script=_ECHO_SERVER, phase="active"):
    store, entry, reference, _ = plugin_reference(tmp_path, phase=phase)
    return MCPStdioClient(_config(script, cwd=str(tmp_path)), activation=reference), store, entry


def test_managed_handshake_binary_pipes_and_native_cleanup(tmp_path):
    script = "import sys\nsys.stderr.buffer.write('管道诊断\\n'.encode());sys.stderr.flush()\n" + _ECHO_SERVER
    client, _, _ = managed_client(tmp_path, script=script)
    try:
        transport = client.start()
        binding = transport.binding.managed
        assert binding is not None
        record = binding.hosted.record
        assert record["pid"] == binding.hosted.process.pid != record["child_pid"]
        assert record["activation_scope"]["activation_id"] == binding.activation.scope.activation_id
        assert [tool.name for tool in client.list_tools()] == ["echo", "add"]
        value = "中文🙂" * 10000
        assert client.call_tool("echo", {"text": value})["content"] == value
        assert "管道诊断" in "".join(transport.inbox.stderr._lines)
    finally:
        receipt = client.stop()
    assert isinstance(receipt, ProcessSessionCleanup) and receipt.confirmed
    assert receipt.record["session_id"] == record["session_id"]
    assert all(_process_instance_terminated(record[key], record[birth]) for key, birth in (
        ("pid", "pid_birth_token"), ("child_pid", "child_pid_birth_token")))


def test_preparing_allows_discovery_but_requires_publication_before_calls(tmp_path):
    client, store, prepared = managed_client(tmp_path, phase="preparing")
    published = []
    try:
        transport = client.start()
        assert len(client.list_tools()) == 2
        with pytest.raises(MCPError) as error:
            client.call_tool("echo", {"text": "not yet"})
        assert error.value.code == "PLUGIN_ACTIVATION_UNAVAILABLE" and client.is_running()
        with pytest.raises(PluginInstallationError, match="不可用"):
            client.publish_tools(transport, lambda: published.append(1))
        assert published == []
        active_request = PluginActivationRequest(prepared.activation.plan.operation_id, prepared.revision,
            replace(prepared.activation, phase="active", catalog_sha256=plugin_catalog_digest(prepared.manifest)))
        store.change_activation(active_request)
        assert client.call_tool("echo", {"text": "same connection"})["content"] == "same connection"
        client.publish_tools(transport, lambda: published.append(1))
        assert published == [1] and client.connection() is transport
    finally:
        assert client.stop().confirmed


@pytest.mark.parametrize("queue", ["request_lock", "write_lock"])
def test_persisted_revoke_rejects_old_queue_without_writing_or_switching_connection(tmp_path, queue, monkeypatch):
    observed = tmp_path / "calls"
    script = _ECHO_SERVER.replace('            params = req.get("params") or {}',
        f'            open({str(observed)!r}, "a").write("called\\n")\n'
        '            params = req.get("params") or {}')
    client, store, entry = managed_client(tmp_path, script=script)
    transport = client.start()
    lock = getattr(transport, queue)
    entered = threading.Event()
    request = transport.request
    def queued(*args, **kwargs):
        entered.set()
        return request(*args, **kwargs)
    monkeypatch.setattr(transport, "request", queued)
    lock.acquire()
    try:
        thread, values, errors = _run_thread(lambda: client.call_tool("echo", {"text": "old"}))
        assert entered.wait(2)
        store.change_activation(revocation(entry))
        lock.release()
        thread.join(5)
        assert not thread.is_alive() and not values and len(errors) == 1
        assert isinstance(errors[0], MCPError) and errors[0].code == "PLUGIN_ACTIVATION_UNAVAILABLE"
        assert not observed.exists() and client._transport is transport
        assert transport.inbox.pending == set()
        # 仅关闭原连接不撤销持久代次；这里已经先提交 revoke，故重连也必须拒绝创建新进程。
        assert client.disconnect(transport=transport).confirmed
        before, _ = ProcessSessionStore(transport.binding.managed.hosted.store_root).list_records()
        with pytest.raises(BackgroundLaunchError):
            client.reconnect()
        after, _ = ProcessSessionStore(transport.binding.managed.hosted.store_root).list_records()
        assert [row["session_id"] for row in after] == [row["session_id"] for row in before]
    finally:
        if lock.locked():
            lock.release()
        client.stop()


def test_per_call_authority_rechecked_after_queue_without_closing_shared_connection(tmp_path, monkeypatch):
    client, _, _ = managed_client(tmp_path)
    transport = client.start()
    proxy = build_proxy_tool(client, "test", client.list_tools()[0], transport=transport)
    entered, checks = threading.Event(), []
    request = transport.request
    def queued(*args, **kwargs):
        entered.set()
        return request(*args, **kwargs)
    def refuse():
        checks.append(1)
        raise MCPError("本次任务已取消", code="MCP_CANCELLED")
    monkeypatch.setattr(transport, "request", queued)
    transport.request_lock.acquire()
    try:
        context = ToolInvocationContext(runtime_snapshot=None, execution_authority_check=refuse)
        thread, values, errors = _run_thread(lambda: proxy.execute_scoped({"text": "refused"}, context))
        assert entered.wait(2) and not checks
        transport.request_lock.release()
        thread.join(5)
        assert not thread.is_alive() and not errors and checks == [1]
        assert not values[0].ok and values[0].error_code == "CANCELLED"
        assert values[0].effect_outcome == "not_started"
        assert client.is_running() and not transport.inbox.closed.is_set()
        assert proxy.execute({"text": "another task"}).ok
    finally:
        if transport.request_lock.locked():
            transport.request_lock.release()
        assert client.stop().confirmed


def test_resource_lock_wait_can_observe_connection_revoke(tmp_path, monkeypatch):
    client, _, _ = managed_client(tmp_path)
    transport = client.start()
    managed = transport.binding.managed
    entered = threading.Event()
    original = mcp_managed_process.ManagedMCPProcess.transaction
    def transaction(self, check):
        entered.set()
        return original(self, check)
    monkeypatch.setattr(mcp_managed_process.ManagedMCPProcess, "transaction", transaction)
    try:
        with ProcessSessionStore(managed.hosted.store_root).transaction():
            thread, values, errors = _run_thread(lambda: client.call_tool("echo", {"text": "queued"}))
            assert entered.wait(2)
            transport.revoke()
            thread.join(2)
            assert not thread.is_alive() and not values and len(errors) == 1
            assert isinstance(errors[0], MCPError) and errors[0].code == "MCP_CONNECTION_CLOSED"
    finally:
        assert client.stop().confirmed


def test_published_proxy_cannot_follow_same_activation_reconnect(tmp_path):
    client, _, _ = managed_client(tmp_path)
    try:
        original = client.start()
        proxy = build_proxy_tool(client, "test", client.list_tools()[0], transport=original)
        assert client.disconnect(transport=original).confirmed
        new = client.reconnect()
        assert new is not original
        assert not proxy.execute({"text": "old"}).ok
        assert client.call_tool("echo", {"text": "new"})["content"] == "new"
    finally:
        assert client.stop().confirmed


def test_managed_cleanup_commit_error_preserves_native_report_and_blocks_reconnect(tmp_path, monkeypatch):
    client, _, _ = managed_client(tmp_path)
    transport = client.start()
    managed = transport.binding.managed
    proxy = build_proxy_tool(client, "test", client.list_tools()[0], transport=transport)
    failure = ProcessSessionCleanupError(OSError("controlled commit failure"), managed.hosted.record, (), True)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(mcp_managed_process, "stop_process_session", lambda *a, **k: (_ for _ in ()).throw(failure))
            with pytest.raises(ProcessSessionCleanupError) as stopped:
                client.disconnect(transport=transport)
            assert stopped.value is failure and stopped.value.report["committed"]
            with pytest.raises(ProcessSessionCleanupError) as reconnect:
                client.reconnect()
            assert reconnect.value is failure and client._transport is transport
            patch.setattr(client, "call_tool", lambda *a, **k: (_ for _ in ()).throw(failure))
            outcome = proxy.execute({"text": "cleanup"})
            assert not outcome.ok and json.loads(outcome.output)["process_cleanup"] == failure.report
    finally:
        # 故障注入后的组件收尾，只处理测试创建的原句柄；不能据此改写被测清理失败。
        managed.terminate()
        mcp_transport.close_finished_streams(transport)
        atexit.unregister(client.stop)


def test_transport_construction_failure_cleans_unclaimed_managed_pipes(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import mcp_client
    client, _, _ = managed_client(tmp_path)
    received = []
    def fail(process, *args, **kwargs):
        received.append(kwargs["managed"])
        raise ValueError("controlled transport construction failure")
    monkeypatch.setattr(mcp_client, "MCPTransport", fail)
    with pytest.raises(ValueError):
        client.start()
    assert len(received) == 1
    managed = received[0]
    assert all(stream.closed for stream in (managed.hosted.process.stdin, managed.hosted.process.stdout, managed.hosted.process.stderr))
    record = ProcessSessionStore(managed.hosted.store_root).load(managed.hosted.record["session_id"]).record
    assert record["stop_requested"] and record["status"] == "killed"
    assert client.stop().confirmed


def test_unclaimed_launch_unknown_is_retained_by_stop_and_start(tmp_path, monkeypatch):
    client, _, _ = managed_client(tmp_path)
    failure = BackgroundLaunchError(OSError("injected unresolved launch"), {"session_id": "bg-original"}, False)
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(1)
        raise failure
    monkeypatch.setattr(mcp_managed_process.ManagedMCPProcess, "launch", fail)
    try:
        for action in (client.start, client.start, client.reconnect, client.stop):
            with pytest.raises(BackgroundLaunchError) as error:
                action()
            assert error.value is failure and not error.value.cleanup_confirmed
        assert attempts == [1]
    finally:
        atexit.unregister(client.stop)


def test_claimed_operation_revoked_before_send_records_failed_not_unknown(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_db.host_command_execution import execute_host_command
    from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.agent.tooling.executor import ToolExecutorRequest
    from agent_py_agent.agent.tooling.models import (
        ApprovalPolicy,
        ToolExposure,
        ToolRuntime,
        ToolRuntimeSnapshot,
    )
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, tool_arguments_hash

    observed = tmp_path / "claimed-calls"
    script = _ECHO_SERVER.replace('            params = req.get("params") or {}',
        f'            open({str(observed)!r}, "a").write("called\\n")\n'
        '            params = req.get("params") or {}')
    client, installations, entry = managed_client(tmp_path, script=script)
    transport = client.start()
    owner = transport.binding.managed.activation.owner()
    repo = RuntimeRepository(runtime_db_path(owner.home_dir))
    proxy = build_proxy_tool(client, "test", client.list_tools()[0], transport=transport, effect="mutating")
    proxy.runtime_policy = replace(proxy.runtime_policy, approval_policy=ApprovalPolicy("never"), mutates_workspace=False)
    arguments = {"text": "must not send"}
    request = HostCommandRequest(owner.owner_id, "actor", "chat", "thread", "request",
                                 proxy.model_spec.name, tool_arguments_hash(arguments).removeprefix("sha256:"))
    def prepare(binding):
        runtime = ToolRuntime(proxy.model_spec, proxy.runtime_policy, proxy, exposure=ToolExposure(model_visible=False))
        names = frozenset({proxy.model_spec.name})
        snapshot = ToolRuntimeSnapshot(binding.run_id, (runtime,), names, (), names)
        call = ToolCall("call", proxy.model_spec.name, arguments, "native", proxy.model_spec.schema_hash,
                        binding.run_id, "turn", binding.attempt_id, operation_id=request.operation_id)
        return ToolExecutorRequest(call, snapshot, owner.home_dir, operation_owner_id=owner.owner_id)
    entered = threading.Event()
    original_request = transport.request
    def queued(*args, **kwargs):
        entered.set()
        return original_request(*args, **kwargs)
    monkeypatch.setattr(transport, "request", queued)
    transport.request_lock.acquire()
    try:
        thread, values, errors = _run_thread(lambda: execute_host_command(repo, request, prepare))
        assert entered.wait(5)
        operation = repo.get_operation(request.operation_id)
        assert operation is not None and operation["status"] == "EXECUTING"
        installations.change_activation(revocation(entry))
        transport.request_lock.release()
        thread.join(8)
        assert not thread.is_alive() and not errors and values[0]["state"] == "failed"
        final = repo.get_operation(request.operation_id)
        assert final["status"] == "FAILED" and final["settled_at"] > 0
        assert json.loads(final["outcome_json"])["result"]["effect_outcome"] == "not_started"
        assert not observed.exists() and client.is_running()
    finally:
        if transport.request_lock.locked():
            transport.request_lock.release()
        assert client.stop().confirmed


def test_writer_started_failure_keeps_effect_unknown(tmp_path, monkeypatch):
    client, _, _ = managed_client(tmp_path)
    transport = client.start()
    failure = MCPError("controlled failure after writer start", code="MCP_TIMEOUT")
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(mcp_transport.MCPFrameWriter, "wait", fail)
    try:
        with pytest.raises(MCPError) as error:
            client.call_tool("echo", {"text": "possibly received"})
        assert error.value is failure and failure.effect_outcome == "unknown"
        assert not client.is_running()
    finally:
        assert client.stop().confirmed


def test_native_authority_exception_preserves_unstarted_fact_and_shared_connection(tmp_path):
    from agent_py_agent.agent.runtime_db.managed_operation_store import AuthorityContextMissing
    client, _, _ = managed_client(tmp_path)
    failure = AuthorityContextMissing("controlled authority loss")
    def refuse():
        raise failure
    try:
        client.start()
        with pytest.raises(MCPError) as caught:
            client.call_tool("echo", {"text": "must not send"}, authority_check=refuse)
        assert caught.value.__cause__ is failure and caught.value.effect_outcome == "not_started"
        assert caught.value.code == "MCP_EXECUTION_AUTHORITY_UNAVAILABLE"
        assert client.is_running()
        assert client.call_tool("echo", {"text": "unaffected"})["content"] == "unaffected"
    finally:
        assert client.stop().confirmed
