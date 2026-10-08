"""真实 Gateway 队列、原生工具循环、临时安装和 Compact 提交；只替换模型传输，不接生产。"""
from __future__ import annotations

from agent_py_agent.agent.agent_core import _tool_loop_service, tool_ir_history
from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn
from agent_py_agent.agent.conversation.compact_checkpoint import (
    CompactCheckpointRequest,
    write_compact_checkpoint,
)
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import gateway_paths
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_package import content_bundle
from agent_py_agent.tests.test_host_notices import _ask, _thread_id


def _gateway_with_package(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_plugins=True, prompt_files=[],
                                    my_agent_home=str(tmp_path / "home"), gateway_per_user_owner_scoping=False), tmp_path / "ws")
    package = inspect_plugin_package(content_bundle(files={"CAPABILITY.md": b"GATEWAY_METHOD_ENTRY",
                                                           "methods/detail.md": b"GATEWAY_RESOURCE_BODY"},
                                                     change=lambda row: row.update(plugin_id="carry-gateway")))
    install = PluginInstallStore(resolve_owner_home(agent.home_paths.root))
    row = install.install(PluginInstallRequest(package, "install", 0)).installation
    activation = PluginContentActivation("enable", "carry-gateway", row.package_sha256, row.revision, row.settings_revision)
    install.change_activation(PluginActivationRequest("enable", row.revision, activation))
    paths = gateway_paths(agent)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    return agent, paths


# LLM: 合成摘要也必须用真实 writer 和相同 Store 的 CAS，不能只写不存在的指针，更不能绕过 loader。
# 函数用途: 在第一轮原消息上提交一个合法检查点，供下一轮走完整恢复链。
def _compact_gateway_thread(agent, tid):
    store = agent.conversation_store
    thread = store.threads.require(tid)
    rows, end_offset, errors = store.messages.page_after_offset_report(tid, after=0, limit=0)
    assert rows and not errors
    checkpoint = write_compact_checkpoint(agent, CompactCheckpointRequest(
        thread=thread, summary="合成摘要", operation_evidence={}, compact_rows=rows, retained_tail=(),
        source_end_byte_offset=end_offset, projected_tokens_before=0, projected_tokens_after=0,
        policy=runtime_compact_policy(agent), forced=True))
    commit = ConversationCompactCommit("合成摘要", {}, checkpoint, rows[-1].message_id, end_offset, len(rows), 0)
    store.threads.update_compact_state(tid, commit=commit, expected_generation=thread.compact_generation)


def test_three_gateway_turns_read_compact_restore_once_with_real_tools(tmp_path, monkeypatch):
    agent, paths = _gateway_with_package(tmp_path)
    requests, deliveries = [], []
    original = tool_ir_history.record_runtime_facts_turn_ir

    def observe(params, text, *, source):
        result = original(params, text, source=source)
        if source == "conversation_method_carry" and result:
            deliveries.append((params.request_id, text))
        return result

    def generate(request):
        requests.append(request)
        if len(requests) in (1, 2):
            arguments = {"action": "get", "package_id": "carry-gateway"}
            if len(requests) == 2:
                arguments["resource_path"] = "methods/detail.md"
            return ModelResponse(text="", backend="fixture", tool_use_blocks=[
                {"id": f"method-{len(requests)}", "name": "skill_search", "input": arguments}])
        return ModelResponse(text="继续完成业务", backend="fixture")

    monkeypatch.setattr(tool_ir_history, "record_runtime_facts_turn_ir", observe)
    monkeypatch.setattr(_tool_loop_service, "generate_model_response", generate)
    first_id, first, _chunks = _ask(tmp_path, agent, paths, "第一轮读方法")
    assert first["ok"] and len(requests) == 3, first
    store, tid = agent.conversation_store, _thread_id(agent)
    thread = store.threads.require(tid)
    row, = thread.conversation_methods
    assert row["package_id"] == "carry-gateway" and row["resource_paths"] == ["methods/detail.md"]
    assert deliveries == []
    assert all("（本会话在用）" not in request.prompt for request in requests), "同run中途读取不能改提示目录"
    _compact_gateway_thread(agent, tid)
    second_id, second, _chunks = _ask(tmp_path, agent, paths, "接着做")
    assert second["ok"] and len(requests) == 4, second
    turn = requests[-1]
    carry = [item for item in turn.params.tool_ir_history if isinstance(item, RuntimeFactsTurn)
             and item.source == "conversation_method_carry"]
    assert len(carry) == 1 and len(deliveries) == 1
    text = carry[0].text
    assert "GATEWAY_METHOD_ENTRY" in text and "GATEWAY_RESOURCE_BODY" not in text
    assert '"resource_path":"methods/detail.md"' in text and '"expected_activation_id"' in text
    assert "（本会话在用）" in turn.prompt and deliveries[0][0] == second_id != first_id
    assert store.threads.require(tid).conversation_methods[0]["carried_generation"] == 1
    _third_id, third, _chunks = _ask(tmp_path, agent, paths, "Continue with the next step")
    assert third["ok"] and len(requests) == 5, third
    assert len(deliveries) == 1, "同压缩代次的下一轮不得再次注入"
