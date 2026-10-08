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
