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


# LLM: 线程加载字段保持宿主隐藏；新发现序列往返保留原顺序，旧空字段仍省略。
# 函数用途: 验证有序发现状态不会被线程反序列化按字母重排。
def test_thread_discovery_roundtrip_hidden_and_legacy():
    old = ConversationThread(thread_id="one", canonical_user_id="local")
    assert "loaded_tool_names" not in old.to_dict()
    loaded = replace(old, loaded_tool_names=("read_file", "read_artifact"))
    restored = ConversationThread.from_dict(loaded.to_dict())
    assert restored.loaded_tool_names == ("read_file", "read_artifact")
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
    ("background_main_agent", ["read_file"], True), ("background_main_agent", [], True),
])
def test_initial_loop_loaded_tools_preserve_foreground_and_explicit_limits(
    tmp_path, monkeypatch, source, allowed_tools, background,
):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    agent, thread, _, _ = _setup(tmp_path, monkeypatch, False)
    agent.tools.catalog_deferred_categories = ["web"]
    params = SimpleNamespace(source=source, allowed_tools=allowed_tools,
                             task_attributes={"conversation_thread_id": thread.thread_id})
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed_tools)
    seed = SimpleNamespace(effective_contract_snapshot=None, tool_runtime_snapshot=snapshot)
    reconstructed = SimpleNamespace(loaded_tool_names=set())
    initial = getattr(loop_support, "_initial_loop_loaded_tool_names", None)
    assert callable(initial), "初始加载名应由独立辅助函数统一计算"
    loaded = initial(agent, params, seed, reconstructed)
    # CTX-13/TOOL-05改变的是初始加载，不是后台执行许可上界；普通进度唤醒无需全量web Schema。
    focused = {"task_progress", "list_agents", "send_message", "read_file", "read_artifact", "tool_search", "list_tools"}
    expected = focused & snapshot.available_tool_names if background else set()
    assert loaded == expected
    assert "web_fetch" not in loaded
    visible = {spec.name for spec in agent.tools.model_visible_specs(
        loaded_tool_names=loaded, allowed_tools=allowed_tools,
    )}
    assert "web_fetch" not in visible
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


# LLM: 使用真实tool_search、loader、native转换与冻结参数，不能只测试registry的有序输入。
# 函数用途: 复现连续搜索时新Schema必须追加而非按字母插入。
def test_real_search_loader_preserves_append_only_native_wire(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core._tool_loop_service import _load_discovered_tools
    from agent_py_agent.agent.agent_core.native_tool_protocol import resolve_native_tools
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolProtocolSnapshot,
    )

    agent, thread, _, _ = _setup(tmp_path, monkeypatch, False)
    agent.tools.catalog_default_deferral_enabled = True
    p = ToolLoopExecuteParams(
        user_prompt="", memories=[], runtime_injections=[], prompt_files=[], tool_catalog_section="",
        tool_recommendations_section="", tool_context=[], effective_on_chunk=None, allowed_tools=None,
        write_boundary=None, task_attributes={"conversation_thread_id": thread.thread_id}, request_id="", run_id="run", task_id="",
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[], loaded_tool_names=set(),
        tool_runtime_snapshot=agent.tools.runtime_snapshot(), tool_protocol_snapshot=ToolProtocolSnapshot(
            "run", "native", ProviderToolCapability("test", "local://test", "test", False, True, "test")),
    )
    base = resolve_native_tools(agent, p)
    first = agent.tools.tools["tool_search"].execute({"names": ["workflows"]})
    _load_discovered_tools(p, {"tool_round": 1, "tool_result_envelope": first.result_envelope})
    a = resolve_native_tools(agent, p)
    second = agent.tools.tools["tool_search"].execute({"names": ["package_build"]})
    _load_discovered_tools(p, {"tool_round": 2, "tool_result_envelope": second.result_envelope})
    b = resolve_native_tools(agent, replace(p))
    assert a == [*base, next(tool for tool in a if tool["name"] == "workflows")]
    assert b == [*a, next(tool for tool in b if tool["name"] == "package_build")]
    persist_discovered_tools(agent, p, {"tool_round": 1, "tool_result_envelope": first.result_envelope})
    persist_discovered_tools(agent, p, {"tool_round": 2, "tool_result_envelope": second.result_envelope})
    next_turn = replace(p, live_archive_state={}, loaded_tool_names=set(p.loaded_tool_names))
    assert resolve_native_tools(agent, next_turn) == b
    _load_discovered_tools(p, {"tool_round": 3, "tool_result_envelope": first.result_envelope})
    assert resolve_native_tools(agent, p) == b
    narrowed = replace(p, allowed_tools=["tool_search", "workflows"])
    assert {tool["name"] for tool in resolve_native_tools(agent, narrowed)} == {"tool_search", "workflows"}
