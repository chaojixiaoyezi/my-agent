# LLM: 临时Store隔离真实owner；正式搜索信封经原子线程账本恢复，不依赖携带正文或推断。
# 模块用途: 锁住跨轮工具展示、线程隔离、旧记录兼容与权限过滤。
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.conversation.models import MODEL_HIDDEN_THREAD_FIELDS, ConversationThread
from agent_py_agent.agent.conversation.tool_discovery import (
    normalized_loaded_tools,
    persist_discovered_tools,
    thread_loaded_tools,
)
from agent_py_agent.tests.test_compactcall_websocket import _setup


def test_thread_discovery_roundtrip_hidden_and_legacy():
    old = ConversationThread(thread_id="one", canonical_user_id="local")
    assert "loaded_tool_names" not in old.to_dict()
    loaded = replace(old, loaded_tool_names=("read_file", "read_artifact"))
    restored = ConversationThread.from_dict(loaded.to_dict())
    assert restored.loaded_tool_names == ("read_artifact", "read_file")
    assert "loaded_tool_names" in MODEL_HIDDEN_THREAD_FIELDS
    assert normalized_loaded_tools([None, 1, {}, "", " read_file "]) == ("read_file",)
    assert normalized_loaded_tools("read_file") == ()


def test_discovery_atomic_union_and_child_thread_identity(tmp_path, monkeypatch):
    agent, thread, _, records = _setup(tmp_path, monkeypatch, False)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    persist_discovered_tools(agent, params, records[0])
    agent.conversation_store.threads.update_atomic(thread.thread_id, lambda current: replace(current, compact_generation=9))
    record = {"tool": "tool_search", "tool_round": 2,
              "tool_result_envelope": {"tool_search": {"loaded_tool_names": ["read_artifact"]}}}
    persist_discovered_tools(agent, params, record)
    assert thread_loaded_tools(agent, params) == {"read_file", "read_artifact"}
    assert agent.conversation_store.threads.load(thread.thread_id).compact_generation == 9
    child = SimpleNamespace(task_attributes={"agent_thread_id": "absent-child", "conversation_thread_id": thread.thread_id})
    assert thread_loaded_tools(agent, child) == set()
    assert thread_loaded_tools(agent, SimpleNamespace(task_attributes={})) == set()
    snapshot = agent.tools.runtime_snapshot(allowed_tools=["list_files"])
    specs = agent.tools.model_visible_specs(allowed_tools=["list_files"], loaded_tool_names=thread_loaded_tools(agent, params),
                                          runtime_snapshot=snapshot)
    assert {spec.name for spec in specs} == {"list_files"}


def test_discovery_does_not_parse_prose_or_create_thread(tmp_path, monkeypatch):
    agent, thread, _, records = _setup(tmp_path, monkeypatch, False)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    persist_discovered_tools(agent, params, {"output": str(records[0])})
    persist_discovered_tools(agent, params, {"tool_result_envelope": {"tool_search": {"loaded_tool_names": [None, 1, {}]}}})
    assert thread_loaded_tools(agent, params) == set()
    missing = SimpleNamespace(task_attributes={"conversation_thread_id": "absent"})
    persist_discovered_tools(agent, missing, records[0])
    assert agent.conversation_store.threads.load("absent") is None


def test_compact_surface_does_not_import_other_thread_names():
    from agent_py_agent.agent.conversation.compact import _bound_loaded_tools
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        ConversationCompactModelSurface,
    )

    thread = SimpleNamespace(thread_id="child", loaded_tool_names=("read_file",))
    stale = ConversationCompactModelSurface(thread_id="parent", loaded_tool_names=("manage_models",))
    assert _bound_loaded_tools(stale, thread) == ("read_file",)
    assert _bound_loaded_tools(replace(stale, thread_id="child"), thread) == ("manage_models", "read_file")
