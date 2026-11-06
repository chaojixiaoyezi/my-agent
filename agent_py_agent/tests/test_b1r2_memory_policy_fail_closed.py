"""B1-R2: an invalid owner policy disables this turn's formal recall only."""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.memory_store import jsonl
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope


# LLM: The real JSONL store lives entirely under pytest tmp_path; populate it before corrupting policy.
# 函数用途: 构造独立正式记忆库与每轮路由状态，不触碰真实用户数据。
@pytest.fixture
def recall_case(tmp_path):
    owner = tmp_path / "owner"
    path = owner / "memory" / "long_term" / "memory.jsonl"
    path.parent.mkdir(parents=True)
    memory = jsonl.JsonlMemory(path)
    record = jsonl.MemoryRecord(role="user", entry_id="fact-one", content="已确认的正式事实。", kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal",
                    "subject_key": "fact-one"})
    memory.add_record(record)
    agent = SimpleNamespace(memory=memory, memory_hot=SimpleNamespace(list=lambda: []),
        memory_lessons=SimpleNamespace(list=lambda: []),
        config=SimpleNamespace(memory_top_k=5, home_lesson_stale_caveat_days=7.0))
    routed = SimpleNamespace(receipts=[], findings=[], injected_sections=[])
    assert not memory._pending_access
    return agent, routed, [record], owner / "memory_policy.json"


# LLM: Call the production admission path; returned records alone may register final access.
# 函数用途: 通过真实正式召回入口执行一轮最小请求。
def _recall(agent, routed, records, *, skip=False):
    return loop_support._formal_memories_for_request(agent, SimpleNamespace(), routed,
        recall_scope=MemoryRecallScope.from_runtime(), long_term_memories=records, skip_formal_recall=skip)


@pytest.mark.parametrize("payload,error", [
    (b'{"schema_version": "memory-policy.v3"}', "ValueError"),
    (b'{"budget": {"recall_max_chars": "4000"}}', "ValueError"),
    (b'{"budget": {"recall_max_chars": 4000}', "JSONDecodeError"),
    (b'{"budget": []}', "ValueError"),
    (b'\xff', "UnicodeDecodeError"),
], ids=["v3_schema", "string_budget", "truncated_json", "non_dict_budget", "invalid_utf8"])
# LLM: Bad policy never falls back to a usable default, records access, repairs files, or repeats its finding.
# 函数用途: 覆盖坏政策类型和同轮告警去重，拒绝部分正式召回。
def test_bad_policy_skips_formal_recall_with_one_finding(recall_case, payload, error):
    agent, routed, records, policy_path = recall_case
    policy_path.write_bytes(payload)
    routed.injected_sections = ["stale formal injection"]
    for _ in range(2):
        assert _recall(agent, routed, records) == []
        assert routed.findings == [f"formal memory recall unavailable: {error}"]
        assert routed.injected_sections == []
        assert not agent.memory._pending_access
        assert policy_path.read_bytes() == payload


# LLM: Permission failures must use the same fail-closed finding without exposing exception text.
# 函数用途: 验证政策读取失败只影响本轮正式召回且告警不重复。
def test_policy_reader_oserror_skips_formal_recall(recall_case, monkeypatch):
    agent, routed, records, _ = recall_case
    reads = []

    # LLM: Simulate the policy reader itself, not filesystem mode bits that administrators may bypass.
    # 函数用途: 产生确定的读取权限错误。
    def denied():
        reads.append(True)
        raise PermissionError("PRIVATE-POLICY-PATH")

    monkeypatch.setattr(agent.memory, "memory_policy", denied)
    for _ in range(2):
        assert _recall(agent, routed, records) == []
        assert routed.findings == ["formal memory recall unavailable: PermissionError"]
        assert not agent.memory._pending_access
    assert reads == [True, True]
    assert "PRIVATE-POLICY-PATH" not in str(routed.findings)


# LLM: The valid-policy path still admits the existing record and confirms exactly its final access.
# 函数用途: 锁住正常政策下的召回结果与访问登记。
def test_valid_policy_preserves_selection_and_access(recall_case):
    agent, routed, records, policy_path = recall_case
    policy_path.write_text(json.dumps({"schema_version": "memory-policy.v2",
        "budget": {"recall_max_chars": 12000}}), encoding="utf-8")
    result = _recall(agent, routed, records)
    assert [record.entry_id for record in result] == [record.entry_id for record in records]
    assert set(agent.memory._pending_access) == {records[0].entry_id}
    assert routed.findings == []


# LLM: Only the per-turn caller is guarded; direct policy-budget consumers must still reject bad policy.
# 函数用途: 保持底层预算解析器的严格失败合同。
def test_budget_reader_still_raises_for_invalid_policy(recall_case):
    agent, _, _, policy_path = recall_case
    policy_path.write_text('{"schema_version": "memory-policy.v3"}', encoding="utf-8")
    with pytest.raises(ValueError, match="^MEMORY_POLICY_INVALID$"):
        loop_support._memory_context_budget(agent)


# LLM: The original skip gate must run before any policy read, including an invalid on-disk policy.
# 函数用途: 保留关闭正式召回时的提前返回行为。
def test_skip_formal_recall_does_not_read_policy(recall_case, monkeypatch):
    agent, routed, records, policy_path = recall_case
    policy_path.write_text('{"schema_version": "memory-policy.v3"}', encoding="utf-8")

    # LLM: Entering this probe means the skip short-circuit has regressed.
    # 函数用途: 禁止关闭召回时触发政策读取。
    def unexpected_read():
        raise AssertionError("skip_formal_recall must not read policy")

    monkeypatch.setattr(agent.memory, "memory_policy", unexpected_read)
    routed.injected_sections = ["stale formal injection"]
    assert _recall(agent, routed, records, skip=True) == []
    assert routed.injected_sections == [] and routed.findings == []
    assert not agent.memory._pending_access


# LLM: Unexpected programming errors must propagate rather than disappear under a broad Exception catch.
# 函数用途: 限定新保护只处理规格要求的政策读取与解析错误。
def test_unexpected_policy_error_is_not_swallowed(recall_case, monkeypatch):
    agent, routed, records, _ = recall_case

    # LLM: This exception is intentionally outside the fail-closed tuple.
    # 函数用途: 模拟不应被新保护吞掉的编程错误。
    def broken_reader():
        raise RuntimeError("policy reader bug")

    monkeypatch.setattr(agent.memory, "memory_policy", broken_reader)
    with pytest.raises(RuntimeError, match="policy reader bug"):
        _recall(agent, routed, records)
    assert routed.findings == [] and not agent.memory._pending_access
