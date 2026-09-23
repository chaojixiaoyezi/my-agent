"""会话模型选择：同 owner 并行、跨 owner、重启和冻结执行片；不发模型请求。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import request_context
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    inherit_model_profile,
    inherited_model_config,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.settings.model_scope import active_thread_model_name, selected_model_scope
from agent_py_agent.agent.settings.thread_model_selection import (
    default_model_profile_id,
    execute_local_model_operation,
    thread_model_config,
    thread_model_profile_id,
)
from agent_py_agent.tests.test_model_profiles import Host, add


def host_with_store(tmp_path, owner="alice"):
    host = Host(tmp_path / "config", owner)
    host.conversation_store = ConversationStore(
        tmp_path / owner, model_default=lambda: default_model_profile_id(host),
    )
    return host


def test_same_owner_two_sessions_and_independent_im_owners(tmp_path):
    alice, bob, carol = [host_with_store(tmp_path, name) for name in ("alice", "bob", "carol")]
    a = add(alice, model_name="A")[0]
    b = add(alice, model_name="B")[0]
    c = add(bob, model_name="C")[0]
    d = add(carol, model_name="D")[0]
    rows = [(alice, "tui-a", a), (alice, "tui-b", b), (bob, "im-b", c), (carol, "im-c", d)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda row: execute_local_model_operation(
            row[0], row[1], "select", {"profile_id": row[2]},
        ), rows))
    for (host, session, key), result in zip(rows, results):
        assert result["selected"] == key
        assert result["default_selected"] == "default"
        assert execute_local_model_operation(host, session, "list", {})["selected"] == key
        assert thread_model_config(host, result["thread_id"]).model_name in "ABCD"
        assert "secret" not in json.dumps(result)
    assert results[0]["thread_id"] != results[1]["thread_id"]
    with pytest.raises(ValueError, match="不存在"):
        execute_local_model_operation(bob, "im-b", "select", {"profile_id": a})


def test_default_changes_only_new_threads_and_persisted_resume(tmp_path):
    host = host_with_store(tmp_path)
    a, b = add(host, model_name="A")[0], add(host, model_name="B")[0]
    execute_model_profile_operation(host, "set_default", {"profile_id": a})
    old = execute_local_model_operation(host, "old", "list", {})
    changed = execute_local_model_operation(host, "old", "set_default", {"profile_id": b})
    assert changed["selected"] == a and changed["default_selected"] == b
    fresh = execute_local_model_operation(host, "new", "list", {})
    assert fresh["selected"] == b
    host.conversation_store = ConversationStore(host.conversation_store.storage.root)
    assert thread_model_config(host, old["thread_id"]).model_name == "A"
    assert execute_local_model_operation(host, "old", "list", {})["selected"] == a


def test_two_windows_same_session_observe_selection(tmp_path):
    host = host_with_store(tmp_path)
    a = add(host, model_name="A")[0]
    first = execute_local_model_operation(host, "same", "list", {})
    second = execute_local_model_operation(host, "same", "select", {"profile_id": a})
    assert first["thread_id"] == second["thread_id"]
    assert execute_local_model_operation(host, "same", "list", {})["selected"] == a


def test_first_open_same_session_concurrently_creates_one_thread(tmp_path):
    host = host_with_store(tmp_path)
    gate = threading.Barrier(8)

    def open_window(_):
        gate.wait(timeout=5)
        return execute_local_model_operation(host, "new-session", "list", {})["thread_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(open_window, range(8)))
    assert len(set(ids)) == 1
    assert len(list(host.conversation_store.storage.threads_dir.glob("*.json"))) == 1


def test_work_slice_snapshot_and_default_nested_scope_are_stable(tmp_path, monkeypatch):
    host = host_with_store(tmp_path)
    monkeypatch.setattr("agent_py_agent.agent.backends.get_backend", lambda kind, cfg: SimpleNamespace(name=cfg.model_name))
    a, b = add(host, model_name="A")[0], add(host, model_name="B")[0]
    row = execute_local_model_operation(host, "s", "select", {"profile_id": a})
    thread_id = row["thread_id"]
    with selected_model_scope(host, thread_id=thread_id):
        assert host.config.model_name == "A"
        execute_local_model_operation(host, "s", "select", {"profile_id": b})
        assert active_thread_model_name(host, thread_id) == "A"
        with selected_model_scope(host, thread_id=thread_id):
            assert host.config.model_name == "A"
    with selected_model_scope(host, thread_id=thread_id):
        assert host.config.model_name == "B"
    assert active_thread_model_name(host, thread_id) == ""
    execute_local_model_operation(host, "s", "select", {"profile_id": "default"})
    with selected_model_scope(host, thread_id=thread_id):
        execute_model_profile_operation(host, "set_default", {"profile_id": b})
        with selected_model_scope(host):
            assert host.config.model_name == "deployment-model"


def test_selection_clears_stale_usage_without_modifying_history_or_compact(tmp_path):
    host = host_with_store(tmp_path)
    row = execute_local_model_operation(host, "s", "list", {})
    tid = row["thread_id"]
    host.conversation_store.threads.update_atomic(tid, lambda thread: replace(
        thread, summary="prior summary", compact_generation=3, compact_checkpoint_id="checkpoint",
        model_context_usage={"current_tokens": 123}, provider_context_observation={"input_tokens": 123},
    ))
    a = add(host, model_name="A")[0]
    execute_local_model_operation(host, "s", "select", {"profile_id": a})
    thread = host.conversation_store.threads.load(tid)
    assert thread.summary == "prior summary" and thread.compact_generation == 3
    assert thread.compact_checkpoint_id == "checkpoint"
    assert thread.model_context_usage == {} and thread.provider_context_observation == {}


def test_legacy_binding_is_atomic_and_other_owner_rejected(tmp_path):
    host = host_with_store(tmp_path)
    store = host.conversation_store
    store.threads.write(ConversationThread("legacy", "alice", owner_id="alice"))
    a = add(host, model_name="A")[0]
    execute_model_profile_operation(host, "set_default", {"profile_id": a})
    assert thread_model_profile_id(host, "legacy") == a
    thread_model_profile_id(host, "legacy", select="default")
    assert thread_model_profile_id(host, "legacy") == "default"
    store.threads.write(ConversationThread("foreign", "bob", owner_id="bob"))
    with pytest.raises(ValueError, match="其他用户"):
        thread_model_profile_id(host, "foreign", select=a)


def test_deleted_profile_is_error_not_owner_default_fallback(tmp_path):
    host = host_with_store(tmp_path)
    a = add(host, model_name="A")[0]
    row = execute_local_model_operation(host, "s", "select", {"profile_id": a})
    execute_model_profile_operation(host, "delete_model", {"profile_id": a})
    with pytest.raises(ValueError, match="已不存在"):
        thread_model_config(host, row["thread_id"])


def test_child_inherits_creation_selection_after_parent_switch(tmp_path, monkeypatch):
    host = host_with_store(tmp_path)
    monkeypatch.setattr("agent_py_agent.agent.backends.get_backend", lambda kind, cfg: SimpleNamespace(name=cfg.model_name))
    a, b = add(host, model_name="A")[0], add(host, model_name="B")[0]
    row = execute_local_model_operation(host, "s", "select", {"profile_id": a})
    attrs = {}
    with selected_model_scope(host, thread_id=row["thread_id"]):
        inherit_model_profile(attrs, host)
    execute_local_model_operation(host, "s", "select", {"profile_id": b})
    assert inherited_model_config(host, SimpleNamespace(attributes=attrs)).model_name == "A"
    default_attrs = {}
    inherit_model_profile(default_attrs, host)
    assert default_attrs == {"host_model_profile.v1": {"profile_id": "default"}}
    assert read_model_profiles(model_profiles_path(host.home_paths))["selected"] == "default"
    host.conversation_store.threads.write(ConversationThread(
        "child-thread", "alice", owner_id="alice", model_profile_id=b,
    ))
    task = SimpleNamespace(attributes=attrs, agent_thread_id="child-thread")
    assert inherited_model_config(host, task).model_name == "B"


def test_select_requires_thread_and_local_menu_requires_store(tmp_path):
    host = Host(tmp_path)
    with pytest.raises(ValueError, match="当前会话"):
        execute_model_profile_operation(host, "select", {"profile_id": "default"})
    with pytest.raises(ValueError, match="会话存储"):
        execute_local_model_operation(host, "s", "list", {})


def test_front_and_background_entrypoints_use_exact_thread(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import runtime
    from agent_py_agent.agent.gateway_parts import request_execution

    host = host_with_store(tmp_path)
    monkeypatch.setattr("agent_py_agent.agent.backends.get_backend", lambda kind, cfg: SimpleNamespace(name=cfg.model_name))
    a, b = add(host, model_name="A")[0], add(host, model_name="B")[0]
    row = execute_local_model_operation(host, "s", "select", {"profile_id": a})
    execute_model_profile_operation(host, "set_default", {"profile_id": b})
    thread = host.conversation_store.threads.load(row["thread_id"])
    monkeypatch.setattr(request_context, 'preflight_gateway_conversation', lambda inputs: SimpleNamespace(thread_id=thread.thread_id))
    monkeypatch.setattr(request_execution, "_require_gateway_conversation_ready", lambda *args: None)
    monkeypatch.setattr(request_execution, "_run_gateway_ask_with_model", lambda context, **_kwargs: context.agent.config.model_name)
    context = SimpleNamespace(agent=host, request={"prompt": "继续"}, request_id="req", on_chunk=None)
    assert request_execution._run_gateway_ask(context) == "A"
    monkeypatch.setattr(runtime, "_invoke_background_main_agent_with_model", lambda rt, *args: rt.agent.config.model_name)
    assert runtime._invoke_background_main_agent(SimpleNamespace(agent=host), thread, None, None, (None, None)) == "A"
    execute_local_model_operation(host, "s", "select", {"profile_id": b})
    # 已加载的旧 thread 参数不能压过 store 中的新选择。
    assert runtime._invoke_background_main_agent(SimpleNamespace(agent=host), thread, None, None, (None, None)) == "B"


def test_status_distinguishes_running_snapshot_and_next_selection(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.control_service import (
        GatewayControlScope,
        _status_model_name,
    )

    host = host_with_store(tmp_path)
    monkeypatch.setattr("agent_py_agent.agent.backends.get_backend", lambda kind, cfg: SimpleNamespace(name=cfg.model_name))
    a, b = add(host, model_name="A")[0], add(host, model_name="B")[0]
    row = execute_local_model_operation(host, "s", "select", {"profile_id": a})
    scope = GatewayControlScope(conversation_id="s", user_id="local-agent", channel="chat")
    with selected_model_scope(host, thread_id=row["thread_id"]):
        execute_local_model_operation(host, "s", "select", {"profile_id": b})
        assert _status_model_name(host, idle=False, scope=scope) == "A"
    assert _status_model_name(host, idle=True, scope=scope) == "B"
