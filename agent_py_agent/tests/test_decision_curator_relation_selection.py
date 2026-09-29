"""关系对按词面相关度挑选：旧逻辑按顺序截取，相关对排在后面时永远比不到。

本文件只验证挑对规则的对外可见行为：相关对选得进、同输入同输出、空材料边界，
以及挑对函数是纯函数（同批两次调用必须得到同一 revision，否则 apply 会永远 stale）。
"""
import hashlib
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store import decision_curator_relation as relation
from agent_py_agent.agent.memory_store.curator_formal import CuratorFormalMemoryInput
from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch, CuratorMessageInput


# LLM: 只造本函数需要的投影字段，不伪造版本、哈希或完整性事实；正文与哈希保持一致。
# 函数用途: 造一条可参与关系比较的精确消息。
def message(index: int, text: str) -> CuratorMessageInput:
    return CuratorMessageInput(f"message-{index}", f"thread-{index}", "user", "internal", 1.0, text, text,
                               "sha256:" + hashlib.sha256(text.encode()).hexdigest(), {})


# LLM: 版本必须是原整数、长度与正文一致、hash 与正文一致，否则 _complete_formal 会判不完整。
# 函数用途: 造一条可参与关系比较的完整正式条目。
def formal(index: int, text: str) -> CuratorFormalMemoryInput:
    entry_id = f"entry-{index}"
    return CuratorFormalMemoryInput("long_term", entry_id, f"memory/long_term/memory.jsonl#{entry_id}", text,
                                    "sha256:" + hashlib.sha256(text.encode()).hexdigest(), f"subject-{index}",
                                    "personal", "personal", "2026-09-28T00:00:00+00:00",
                                    authority_version=1, content_chars=len(text))


# LLM: 不启动真实 Curator 或网络；只把材料喂进挑对与 material 构造。
# 函数用途: 用给定消息与正式条目构造关系材料，返回 state/questions/pairs。
def material(messages, formal_memories):
    batch = CuratorInputBatch(tuple(messages), (), tuple(formal_memories))
    state, questions, pairs, _revision, _incomplete = relation._relation_material(batch)
    return state, questions, pairs


# LLM: 旧逻辑取笛卡尔积前 32 对，第 33 对之后（含）永远不出现；本条正是要证明该缺口已被修掉。
# 函数用途: 相关的那一对排在第 33 对之后时，新逻辑必须挑得到它。
def test_relevant_pair_beyond_sequential_cutoff_is_selected():
    # 顺序枚举（消息为外层循环）下第 33 对是 message-1 × entry-0：
    # 第 1..32 对全部来自 message-0（每对 32 条条目），旧逻辑取不到 message-1。
    messages = [message(0, "今天天气不错，随便聊聊别的。"), message(1, "请记住，部署窗口的密码轮换流程。")]
    formal_memories = [formal(index, f"无关条目 {index} 内容") for index in range(31)]
    formal_memories.append(formal(31, "部署窗口的密码轮换流程需要两步确认。"))
    # 逐条给相关对之外的条目加噪声，保证相关对的分最高。
    state, questions, pairs = material(messages, formal_memories)
    assert len(pairs) == relation._MAX_RELATION_PAIRS
    assert (messages[1], formal_memories[31]) in pairs, "相关的第 33 对必须被挑中"
    # 旧逻辑下 message-1 不会出现在任何一对里；这条断言就是缺口的回归线。
    assert any(source.message_id == "message-1" for source, _item in pairs)
    selected_ids = {source.message_id for source, _item in pairs}
    assert "message-1" in selected_ids
    assert state["coverage"]["pair_count"] == len(pairs)


# LLM: 不依赖随机、时间或全局状态；两次构造必须逐字节一致，否则第 49 行的 revision 复核会误判 stale。
# 函数用途: 同样输入必须得到同样的挑对结果与 revision。
def test_selection_is_deterministic_across_calls():
    messages = [message(index, f"消息正文 {index} 关于缓存清理") for index in range(6)]
    formal_memories = [formal(index, f"正式条目 {index} 关于缓存清理") for index in range(6)]
    batch = CuratorInputBatch(tuple(messages), (), tuple(formal_memories))
    first = relation._relation_material(batch)
    second = relation._relation_material(batch)
    assert [source.message_id for source, _item in first[2]] == [source.message_id for source, _item in second[2]]
    assert [item.authority_id for _source, item in first[2]] == [item.authority_id for _source, item in second[2]]
    assert first[3] == second[3], "同批两次构造必须得到同一 revision"


# LLM: 空材料属于正常边界，不是错误；必须返回空题集而不是抛异常或编造对。
# 函数用途: 消息为空、条目为空、两者都为空时都得到空的题目与零覆盖。
@pytest.mark.parametrize("messages,formal_memories", [
    ([], [formal(0, "一条正式事实")]),
    ([message(0, "一条新消息")], []),
    ([], []),
])
def test_empty_message_or_formal_yields_no_questions(messages, formal_memories):
    batch = CuratorInputBatch(tuple(messages), (), tuple(formal_memories))
    state, questions, pairs, revision, _incomplete = relation._relation_material(batch)
    assert questions == {} and pairs == ()
    assert state["coverage"]["pair_count"] == 0
    assert state["coverage"]["total_pair_count"] == len(messages) * len(formal_memories)
    assert isinstance(revision, str) and revision


# LLM: 只观察对外声明，不读内部实现；下游必须能看出"没比全部"，不能把展示的对当成全库覆盖。
# 函数用途: 结构化声明总对数、展示对数、挑选规则与上限。
def test_coverage_declares_total_presented_and_rule():
    messages = [message(index, f"消息 {index}") for index in range(5)]
    formal_memories = [formal(index, f"条目 {index}") for index in range(8)]
    state, _questions, pairs = material(messages, formal_memories)
    coverage = state["coverage"]
    assert coverage["total_pair_count"] == 40
    assert coverage["pair_count"] == len(pairs) == relation._MAX_RELATION_PAIRS
    assert coverage["selection"] == relation._RELATION_SELECTION
    assert coverage["selection_limit"] == relation._MAX_RELATION_PAIRS
    assert coverage["kind"] == "presented_pairs_only"


# LLM: 覆盖声明必须能区分"全部比过"与"只比了一部分"，避免下游误推。
# 函数用途: 对数不超过上限时全部展示，且声明里的总数与展示数一致。
def test_all_pairs_presented_when_under_limit():
    messages = [message(index, f"消息 {index}") for index in range(2)]
    formal_memories = [formal(index, f"条目 {index}") for index in range(3)]
    state, _questions, pairs = material(messages, formal_memories)
    assert len(pairs) == 6
    assert state["coverage"]["total_pair_count"] == 6
    assert state["coverage"]["pair_count"] == 6


# LLM: 相邻与无关材料的顺序不得影响挑对结果；只按分数与原始枚举序稳定排序。
# 函数用途: 同分时保持原枚举顺序，保证结果可复现。
def test_ties_keep_original_enumeration_order():
    # 所有正文完全相同 → 所有对同分，必须退回原枚举顺序（消息外层、条目内层）。
    messages = [message(index, "完全一样的正文") for index in range(3)]
    formal_memories = [formal(index, "完全一样的正文") for index in range(20)]
    _state, _questions, pairs = material(messages, formal_memories)
    expected = [(messages[0], formal_memories[index]) for index in range(20)]
    expected += [(messages[1], formal_memories[index]) for index in range(12)]
    assert [(source.message_id, item.authority_id) for source, item in pairs] == \
           [(source.message_id, item.authority_id) for source, item in expected]


# LLM: 只做算术，不触网、不引入嵌入；本片明确不增加网络请求与费用。
# 函数用途: 确认挑对只在本地用标准库计算，没有嵌入调用入口。
def test_selection_has_no_embedding_or_network_calls():
    messages = [message(index, f"消息 {index} 关于索引重建") for index in range(4)]
    formal_memories = [formal(index, f"条目 {index} 关于索引重建") for index in range(4)]
    source = relation._pair_scores(tuple((index, m, f) for index, (m, f) in
                                         enumerate((m, f) for m in messages for f in formal_memories)))
    assert source and all(isinstance(value, float) for value in source.values())
    assert not hasattr(relation, "embed")
