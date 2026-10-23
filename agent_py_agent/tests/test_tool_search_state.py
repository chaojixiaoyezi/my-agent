# LLM: 工具发现只消费 typed 信封；顺序和活跃选择不产生权限，不读取其他 owner 状态。
# 模块用途: 验证兼容集合、有序发现恢复、线程历史及显式边界活跃选择。
"""工具发现的恢复投影不依赖执行循环、模型文字或其他 owner 状态。"""

from agent_py_agent.agent.tooling.tool_search_state import pending_carried_loaded_tool_names


# LLM: 10-08 前缀纪律：线程内加载过的工具只增不减——工具表来回抖动会让整段缓存前缀失效（.16 真机实测加载/撤回各一次全量未命中）。
# 函数用途: 带轮号的加载记录按线程累计；之后的轮没有再搜索也不撤回。
def test_structured_rounds_accumulate_loaded_tools_for_the_thread():
    records = [
        {"tool_round": 1, "tool_result_envelope": {"tool_search": {"loaded_tool_names": ["old"]}}},
        {"tool_round": 2, "tool_result_envelope": {"tool_search": {"loaded_tool_names": ["new", "new"]}}},
        {"tool_round": 2, "tool_result_envelope": {"tool_search": {"loaded_tool_names": ["second"]}}},
    ]
    assert pending_carried_loaded_tool_names(records) == {"old", "new", "second"}
    records.append({"tool_round": 3, "output": "继续使用 old new second"})
    assert pending_carried_loaded_tool_names(records) == {"old", "new", "second"}, "没有再搜索也不撤回"


def test_legacy_records_allow_only_exact_final_item_not_old_names_or_prose():
    search = {"tool_result_envelope": {"tool_search": {"loaded_tool_names": ["read_file"]}}}
    assert pending_carried_loaded_tool_names([search]) == {"read_file"}
    assert pending_carried_loaded_tool_names([search, {"output": "loaded_tool_names read_file"}]) == set()
    assert pending_carried_loaded_tool_names([]) == set()


def test_invalid_envelopes_do_not_load_tools():
    for envelope in (None, [], {"tool_search": "read_file"}, {"tool_search": {"loaded_tool_names": "read_file"}}):
        assert pending_carried_loaded_tool_names([{"tool_result_envelope": envelope}]) == set()


# LLM: 历史集合兼容入口保留；新顺序投影不得排序或重排首次真实发现。
# 函数用途: 验证结构化加载记录的有序去重和凭据追加。
def test_ordered_recovery_preserves_discovery_sequence():
    from agent_py_agent.agent.tooling import tool_search_state as state

    ordered = getattr(state, "ordered_carried_loaded_tool_names", None)
    assert callable(ordered)
    records = [
        {"tool_round": 1, "tool_result_envelope": {"tool_search": {"loaded_tool_names": ["z", "a", "z"]}}},
        {"tool_round": 2, "tool_result_envelope": {"tool_search": {"loaded_tool_names": ["b", "a"]}, "credential": {}}},
        {"tool_round": 3, "output": "scripts"},
    ]
    assert ordered(records) == ("z", "a", "b", "web_fetch")
    assert pending_carried_loaded_tool_names(records) == {"z", "a", "b", "web_fetch"}


# LLM: 活跃集合只供明确任务/Compact 边界选择，不定时撤回工具，不修改完整历史。
# 函数用途: 验证 pinned、最近使用、最近发现的优先级及稳定输出顺序。
def test_active_selection_keeps_pins_and_recent_without_mutating_history():
    from agent_py_agent.agent.tooling import tool_search_state as state

    resolve = getattr(state, "resolve_active_loaded_tool_names", None)
    assert callable(resolve)
    history = ("a", "b", "c", "d", "e", "f")
    assert resolve(history, recent_used_tool_names=("b",), pinned_tool_names=("a",), max_active_discovered=3) == ("a", "b", "f")
    assert resolve(history, pinned_tool_names=("a", "b"), max_active_discovered=1) == ("a", "b")
    assert resolve(history, max_active_discovered=0) == ()
    assert history == ("a", "b", "c", "d", "e", "f")


# LLM: 真实线程往返及原子更新保留独立完整历史；读取不得跨 parent/child 线程借状态。
# 函数用途: 验证新发现顺序跨重读保留，元数据不被别的字段更新覆盖。
def test_thread_discovery_retains_ordered_history_in_metadata(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.conversation.tool_discovery import (
        normalized_loaded_tools,
        persist_discovered_tools,
    )

    assert normalized_loaded_tools(["z", "a", "z", None, " b "]) == ("z", "a", "b")
    store = ConversationStore(tmp_path)
    thread = store.threads.get_or_create({"canonical_user_id": "local", "channel": "internal",
                                         "channel_conversation_id": "test-discovery", "channel_user_id": "local"})
    store.threads.update_atomic(thread.thread_id, lambda current: replace(current, metadata={"kept": 7}))
    agent = SimpleNamespace(conversation_store=store)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    for names in (["z", "a"], ["c", "a"]):
        persist_discovered_tools(agent, params, {"tool_round": 1, "tool_result_envelope": {"tool_search": {"loaded_tool_names": names}}})
    restored = store.threads.load(thread.thread_id)
    assert restored.loaded_tool_names == ("z", "a", "c")
    assert restored.metadata == {"kept": 7, "discovered_tool_history": ["z", "a", "c"]}


# LLM: 有序使用序列按旧到新表达真实使用时序，选择最近使用者但不重排最终发现前缀。
# 函数用途: 防止把最近发现错当最近使用。
def test_active_selection_respects_ordered_recent_use_not_discovery_age():
    from agent_py_agent.agent.tooling.tool_search_state import resolve_active_loaded_tool_names

    assert resolve_active_loaded_tool_names(("a", "b"), recent_used_tool_names=("b", "a"),
                                            max_active_discovered=1) == ("a",)
