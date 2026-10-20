# LLM: 临时Store隔离真实owner；正式搜索/凭据信封经同源投影及原子线程账本恢复，不依赖正文或推断。
# 模块用途: 锁住跨轮及凭据工具展示、后台首轮、前台不变、线程隔离与权限过滤。
from dataclasses import replace
from types import SimpleNamespace

import pytest

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


def test_discovery_failure_happens_after_native_ir(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service as service

    agent, thread, _, records = _setup(tmp_path, monkeypatch, False)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    events = []
    failure = OSError("synthetic thread write failure")

    def fail_update(*_args):
        events.append("discovery")
        raise failure

    monkeypatch.setattr(agent.conversation_store.threads, "update_atomic", fail_update)
    monkeypatch.setattr(service, "_record_tool_call_ir_if_native", lambda *_args: events.append("ir"))
    with pytest.raises(OSError) as caught:
        service._record_tool_post_archive(agent, SimpleNamespace(params=params), records[0], "safe-render")
    assert caught.value is failure
    assert events == ["ir", "discovery"]


def test_compact_cas_preserves_discovery_written_during_summary(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import compact_request_budget
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.tests.test_compactcall_websocket import _Probe

    agent, thread, _, records = _setup(tmp_path, monkeypatch, True)
    _Probe(monkeypatch, True, live=False)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    generate = compact_request_budget.generate_auxiliary_model_response

    def interleave(request):
        assert thread.loaded_tool_names == ()
        persist_discovered_tools(agent, params, records[0])
        return generate(request)

    monkeypatch.setattr(compact_request_budget, "generate_auxiliary_model_response", interleave)
    result = prepare_conversation_context(agent, agent.conversation_store, thread,
        options=ConversationCompactOptions(force=True))
    current = agent.conversation_store.threads.load(thread.thread_id)
    assert result.compacted and current.compact_generation == 1
    assert current.loaded_tool_names == ("read_file",)
    assert current.compact_checkpoint_id == result.thread.compact_checkpoint_id


@pytest.mark.parametrize("envelope_key", ["credential", "credential_discovery"])
@pytest.mark.parametrize("consumer", ["loop", "thread", "compact"])
def test_credential_envelopes_load_web_fetch_for_all_consumers(tmp_path, monkeypatch, envelope_key, consumer):
    from agent_py_agent.agent.agent_core._tool_loop_service import _load_discovered_tools
    from agent_py_agent.agent.tooling.tool_search_state import pending_carried_loaded_tool_names

    agent, thread, _, _ = _setup(tmp_path, monkeypatch, False)
    agent.tools.catalog_deferred_categories = ["web"]
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id}, loaded_tool_names=set())
    record = {"tool": "request_credentials", "tool_round": 1, "ok": True,
              "tool_result_envelope": {envelope_key: {}}}
    assert "web_fetch" not in {spec.name for spec in agent.tools.model_visible_specs()}
    if consumer == "loop":
        _load_discovered_tools(params, record)
        loaded = params.loaded_tool_names
    elif consumer == "thread":
        persist_discovered_tools(agent, params, record)
        loaded = thread_loaded_tools(agent, params)
        assert agent.conversation_store.threads.load(thread.thread_id).loaded_tool_names == ("web_fetch",)
    else:
        loaded = pending_carried_loaded_tool_names([record, {"tool_round": 2, "output": "synthetic"}])
    assert loaded == {"web_fetch"}
    assert "web_fetch" in {spec.name for spec in agent.tools.model_visible_specs(loaded_tool_names=loaded)}
    assert {spec.name for spec in agent.tools.model_visible_specs(
        loaded_tool_names=loaded, allowed_tools=["read_file"],
    )} == {"read_file"}
    assert record["tool_result_envelope"] == {envelope_key: {}}


@pytest.mark.parametrize("source,allowed_tools,background", [
    ("background_main_agent", None, True), ("user", None, False),
    ("background_main_agent", ["read_file"], False), ("background_main_agent", [], False),
])
def test_initial_loop_loaded_tools_preserve_foreground_and_explicit_limits(
    tmp_path, monkeypatch, source, allowed_tools, background,
):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.agent.conversation.background_tool_policy import (
        BACKGROUND_CONTINUATION_REQUIRED_TOOLS,
    )

    agent, thread, _, _ = _setup(tmp_path, monkeypatch, False)
    agent.tools.catalog_deferred_categories = ["web"]
    params = SimpleNamespace(source=source, allowed_tools=allowed_tools,
                             task_attributes={"conversation_thread_id": thread.thread_id})
    seed = SimpleNamespace(effective_contract_snapshot=None)
    reconstructed = SimpleNamespace(loaded_tool_names=set())
    initial = getattr(loop_support, "_initial_loop_loaded_tool_names", None)
    assert callable(initial), "初始加载名应由独立辅助函数统一计算"
    loaded = initial(agent, params, seed, reconstructed)
    assert ("web_fetch" in loaded) is background
    assert loaded == (set(BACKGROUND_CONTINUATION_REQUIRED_TOOLS) if background else set())
    visible = {spec.name for spec in agent.tools.model_visible_specs(
        loaded_tool_names=loaded, allowed_tools=allowed_tools,
    )}
    assert ("web_fetch" in visible) is background
    assert params.allowed_tools is allowed_tools
    assert reconstructed.loaded_tool_names == set()
    assert agent.conversation_store.threads.load(thread.thread_id).loaded_tool_names == ()


def test_initial_loop_loaded_tools_keep_reconstruction_open_actions_and_thread(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support

    agent, thread, _, records = _setup(tmp_path, monkeypatch, False)
    params = SimpleNamespace(source="user", allowed_tools=None,
                             task_attributes={"conversation_thread_id": thread.thread_id})
    persist_discovered_tools(agent, params, records[0])
    seed = SimpleNamespace(effective_contract_snapshot=SimpleNamespace(required_actions=(
        SimpleNamespace(status="open", allowed_tools=("read_artifact",)),
        SimpleNamespace(status="done", allowed_tools=("web_fetch",)),
    )))
    reconstructed = SimpleNamespace(loaded_tool_names={"search_text"})
    initial = getattr(loop_support, "_initial_loop_loaded_tool_names", None)
    assert callable(initial), "初始加载名应由独立辅助函数统一计算"
    assert initial(agent, params, seed, reconstructed) == {"read_file", "read_artifact", "search_text"}
    assert reconstructed.loaded_tool_names == {"search_text"}


@pytest.mark.parametrize("envelope", [
    None, {}, {"credential": []}, {"credential_discovery": "web_fetch"},
    {"credential": None, "credential_discovery": False},
])
def test_invalid_credential_envelopes_do_not_load_tools(envelope):
    from agent_py_agent.agent.tooling.tool_search_state import _carried_loaded_tool_names

    assert _carried_loaded_tool_names({"tool_result_envelope": envelope}) == set()
    assert _carried_loaded_tool_names({"output": str({"credential": {}})}) == set()


@pytest.mark.parametrize("search", [None, {}, {"loaded_tool_names": "read_file"},
                                         {"loaded_tool_names": [None, 1, " read_file ", ""]}])
def test_credential_loading_preserves_tool_search_normalization(search):
    from agent_py_agent.agent.tooling.tool_search_state import _carried_loaded_tool_names

    record = {"tool_result_envelope": {"credential": {}, "tool_search": search}}
    expected = {"web_fetch", "read_file"} if search and isinstance(search.get("loaded_tool_names"), list) else {"web_fetch"}
    assert _carried_loaded_tool_names(record) == expected


def test_shell_hints_direct_host_credentials_to_web_fetch():
    from agent_py_agent.agent.tooling.shell import _build_shell_tool_model_spec

    spec = _build_shell_tool_model_spec("full", 240, 16000)
    assert "需要宿主凭据（credential_ref）的 HTTP/API 请求必须使用 web_fetch，禁止用 curl/shell 拼接占位账号或探测受保护接口。" in spec.hints.avoid_when
