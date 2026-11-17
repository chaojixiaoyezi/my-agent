"""Wave S：已启用插件在两轮对话之间被升级或退回后，Registry 里的旧客户端不再永久挡住 owner 的每一轮。

实测（克隆网关，local/main）：一轮对话为 waves-learn 0.1.0 建了客户端；之后 /plugins confirm 装上 0.2.0、
/plugins revert 退回 0.1.0，中间没有对话。管理端已核验清理并 release 旧代，随后又提交了新代激活，release 回执
不在 last_commit 上；旧客户端 stop 只能报 authority_missing，于是每一轮都抛 "plugin policy transition cleanup
unconfirmed"，连不用插件的对话也失败，直到停用全部插件。
锁定：会话记录已被消费且同一插件已持有另一程序激活时移交清理责任并为新代建客户端；记录仍在而停止未确认时照旧拒绝。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.agent.tooling import plugin_registration
from agent_py_agent.tests.test_plugin_registry import (
    released_client_registry,  # noqa: F401  (pytest fixture)
)


# 函数用途: 在原代 release 之后直接准备并发布下一代，中间不同步 Registry（对应升级/退回时没有对话的情形）。
def _next_generation(active, released):
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest, prepare_activation
    from agent_py_agent.agent.plugin_activation_record import PluginActivation
    from agent_py_agent.tests.test_plugin_activation import publication

    plan = replace(active.activation.plan, operation_id="enable-b", installation_revision=released.revision)
    prepared = prepare_activation(PluginActivationRequest("enable-b", released.revision,
                                                          PluginActivation(plan, "preparing")), (released,))
    return prepare_activation(publication(prepared), (prepared.installation,)).installation


# 函数用途: 旧代记录已被管理端消费、插件已换到新一代时，同步移交旧客户端并为新代建客户端，不抛错。
def test_consumed_record_and_newer_generation_hand_over_the_old_client(released_client_registry):  # noqa: F811
    registry, rows, client, active, _, released, created = released_client_registry
    current = _next_generation(active, released)
    rows[:] = [current]
    plugin_registration.synchronize_plugin_clients(registry)
    assert registry._mcp_clients == [created[-1]] and len(created) == 2
    assert created[-1].installation.activation_id == current.activation_id != active.activation_id
    assert client.stops == 1 and client.receipt.authority_missing and not client.receipt.confirmed


# 函数用途: 会话记录还在而停止未确认（可能仍在运行）时，即使已换代也保留责任、不启替代，原拒绝不变。
def test_unconfirmed_stop_with_newer_generation_still_keeps_responsibility(released_client_registry):  # noqa: F811
    from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanup

    registry, rows, client, active, _, released, created = released_client_registry
    client.receipt = ProcessSessionCleanup({"session_id": "bg-original"}, False)
    rows[:] = [_next_generation(active, released)]
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        plugin_registration.synchronize_plugin_clients(registry)
    assert registry._mcp_clients == [client] and len(created) == 1
