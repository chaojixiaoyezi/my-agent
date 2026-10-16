"""Lifecycle contract probes. Synthetic registry records / managed cleanup, no OS policy claims."""
import os
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.agent.capability.agents_source_processes import process_source_configurations
from agent_py_agent.agent.tooling import pty_sessions as pty
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.agent.tooling.mcp_transport import MCPTransport
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.tests.test_agents_source_plugin_reconnect import plugin_client as plugin_client
from agent_py_agent.tests.test_agents_source_protection import protected as protected
from agent_py_agent.tests.test_agents_source_repository import choose
from agent_py_agent.tests.test_agents_source_repository import source as source


# LLM: 原断言抽为独立检查，仍通过真实choose入口确认不提交配置。
# 函数用途: 断言未确认资源会阻止切换来源。
def _assert_unconfirmed_switch(source, target):
    with pytest.raises(AgentsContractError, match='RESOURCE_UNCONFIRMED'):
        choose(source, target)
    assert not source.config_path.exists()


# LLM: 保留未知/已确认两类历史资源及容量剪枝前后断言，不能丢弃未确认责任。
# 函数用途: 验证剪枝后来源切换仍受旧资源责任约束。
@pytest.mark.parametrize('unknown', [False, True])
def test_pruning_never_forgets_unconfirmed_source_resource(source, unknown):
    registry = pty.PtySessionRegistry()
    scope = pty._pty_access_scope(source.owner_home, (source.owner_home,), protected_persona_root=source.owner_home)
    deps = process_source_configurations(source.owner_home)
    proc = SimpleNamespace(poll=lambda: 0)
    def history(name, is_unknown):
        return pty.PtySession(name, 'finished', proc, -1, 0, 0, scope,
                              closed=True, source_configurations=deps,
                              termination=ProcessTerminationReceipt('already_exited' if is_unknown else 'test-confirmed-tree', True, 0, 1))
    original = history('unconfirmed-history', unknown)
    registry._sessions[original.session_id] = original
    target = source.owner_home/'new-rules'
    target.write_text('NEW_SOURCE\n')
    try:
        if unknown:
            _assert_unconfirmed_switch(source, target)
        for i in range(pty._MAX_SESSION_COUNT):
            item = history('safe-' + str(i), False)
            registry._sessions[item.session_id] = item
        with registry._lock:
            pty._prune_finished_sessions(registry)
        if unknown:
            _assert_unconfirmed_switch(source, target)
        else:
            assert choose(source, target).revision == 1
    finally:
        registry.clear()


# LLM: 保留真实共享锁等待路径；超时必须是零写入PtyWriteError，不能仅以线程退出判断成功。
# 函数用途: 验证慢插件启动不能让PTY输入超过原期限后才继续写。
def test_slow_plugin_source_lock_is_in_pty_write_deadline(plugin_client, monkeypatch):
    client, _, _, _, _ = plugin_client
    original = MCPStdioClient._start_transport
    entered, release, done = [threading.Event() for _ in range(3)]
    errors, outcomes = [], []
    def slow_start(self):
        entered.set()
        assert release.wait(3)
        return original(self)
    monkeypatch.setattr(MCPStdioClient, '_start_transport', slow_start)
    monkeypatch.setattr(pty, '_WRITE_TIMEOUT_SECONDS', .05)
    registry = pty.PtySessionRegistry()
    readfd, writefd = os.pipe()
    scope = pty.PtyAccessScope('', '', None, None, None)
    session = pty.PtySession('ordinary', 'pipe-fixture', SimpleNamespace(poll=lambda: None), writefd, 0, 0, scope)
    registry._sessions[session.session_id] = session
    def launch():
        try:
            client.start()
        except Exception as exc:
            errors.append(exc)
    starter = threading.Thread(target=launch)
    writer = threading.Thread(target=_capture_write, args=(registry, session.session_id, outcomes, done))
    starter.start()
    try:
        assert entered.wait(2)
        writer.start()
        completed_within_budget = done.wait(.5)
    finally:
        _join_probe_threads(starter, writer, release)
        registry._sessions.clear()
        os.close(writefd)
        os.close(readfd)
    assert not errors
    assert not starter.is_alive() and not writer.is_alive()
    assert completed_within_budget, '50ms write budget did not apply during 500ms blocked PluginMCPClient.start; outcomes=' + repr(outcomes)
    assert len(outcomes) == 1 and isinstance(outcomes[0], pty.PtyWriteError), outcomes
    assert outcomes[0].code == 'TOOL_TIMEOUT' and outcomes[0].written == 0


def test_plugin_original_transport_can_observe_later_confirmed_cleanup(plugin_client):
    client, repo, target, _, _ = plugin_client
    client.start()
    calls = []
    state = {'confirmed': False}
    process = SimpleNamespace(pid=123456789, stdin=None, stdout=None, stderr=None)
    def cleanup():
        calls.append(state['confirmed'])
        return ProcessTerminationReceipt('test-original-managed-store', state['confirmed'], 0 if state['confirmed'] else None, 1)
    managed = SimpleNamespace(hosted=SimpleNamespace(process=process, record={'pid_birth_token':'test-birth'}), terminate=cleanup)
    transport = MCPTransport(process, 'review39', max_line_chars=1000, connect_timeout=1, managed=managed)
    transport.ready = True
    client._transport = transport
    before = repo.config_path.read_bytes()
    with pytest.raises(AgentsContractError, match='RESOURCE_UNCONFIRMED'):
        choose(repo, target)
    assert repo.config_path.read_bytes() == before
    state['confirmed'] = True
    assert managed.terminate().confirmed  # same managed identity now has a confirmed cleanup result
    error = None
    try:
        choose(repo, target)
    except AgentsContractError as exc:
        error = str(exc)
    assert repo.configuration().locator == str(target), {'error':error, 'cleanup_calls':calls, 'cached_confirmed':transport._receipt.confirmed}


# LLM: 与原线程回调相同，捕获结果/异常并总是通知完成，不改变被测写入路径。
# 函数用途: 记录一次真实PTY写调用的结果。
def _capture_write(registry, session_id, outcomes, done):
    try:
        outcomes.append(registry.write(session_id, b"X"))
    except Exception as exc:
        outcomes.append(exc)
    finally:
        done.set()


# LLM: 只用于测试finally；先释放故障锁再按原三秒预算回收线程。
# 函数用途: 清理故障注入线程，避免影响后续用例。
def _join_probe_threads(holder, writer, release):
    release.set()
    holder.join(3)
    if writer.ident is not None:
        writer.join(3)
