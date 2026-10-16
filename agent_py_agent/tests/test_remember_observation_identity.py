"""真实 remember 入口：证据身份绑定观察版本，不由 batch 位置或目标正文刷新。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.tests.test_memory_hardening import RuntimeGateLedgerRecord
from agent_py_agent.tests.test_memory_tool import _agent_with_current_user


# LLM: 使用临时owner的真实remember、候选、晋升与JSONL，不替换权限或证据校验器。
# 函数用途: 建立一个带正式版本的记忆目标。
def _target(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    result = ConfirmedRememberWorkflow(agent).execute({"content": "moneywise 项目使用 UTC 保存时间",
        "kind": "project", "origin": "user_explicit", "subject_key": "project.moneywise.timezone"})
    assert result.ok and json.loads(result.output)["active_memory_changed"]
    return agent, agent.memory.all()[0]


# LLM: 通过真实JSONL版本CAS模拟另一写者，不篡改候选或授权字段。
# 函数用途: 在观察与重放之间产生一个必须保留的新版本。
def _intervening_update(agent, record):
    agent.memory.apply_batch([{"action": "replace", "entry_id": record.entry_id,
        "content": "moneywise 项目已由另一条新消息改为北京时间",
        "expected_version": record.version}])
    return agent.memory.all()[0]


@pytest.mark.parametrize("action", ["replace", "remove"])
@pytest.mark.parametrize("mode", ["single", "batch", "paraphrase"])
# LLM: 只在真实候选持久化之后插入并发写；重放仍走完整remember，不能借换位置/目标正文取得覆盖权。
# 函数用途: 复现独立审查发现的同证据跨版本重放缺口。
def test_same_evidence_replay_preserves_intervening_version(tmp_path, monkeypatch, action, mode):
    agent, prior = _target(tmp_path)
    original = agent.memory_candidates.observe_many
    changed = []

    def observe_then_change(observations):
        saved = original(observations)
        if not changed:
            changed.append(_intervening_update(agent, prior))
        return saved

    operation = {"action": action, "entry_id": prior.entry_id, "origin": "user_explicit",
                 "content": "moneywise 项目按同一条旧消息改为新方案"}
    monkeypatch.setattr(agent.memory_candidates, "observe_many", observe_then_change)
    first = ConfirmedRememberWorkflow(agent).execute(operation)
    assert first.ok and not json.loads(first.output)["active_memory_changed"]
    pending = [row for row in agent.memory_candidates.list() if row.target_entry_id == prior.entry_id]
    assert len(pending) == 1 and pending[0].target_entry_version == prior.version
    before = agent.memory_candidates.path.read_bytes()
    payload = operation
    if mode == "paraphrase":
        payload = {**operation, "content": "moneywise 项目按那条旧消息执行另一个表述"}
    if mode == "batch":
        payload = {"action": "batch", "operations": [
            {"action": "add", "origin": "user_explicit", "content": "另一个独立事实",
             "subject_key": "project.other"}, operation]}
    replay = ConfirmedRememberWorkflow(agent).execute(payload)
    assert agent.memory.all() == changed, replay.output
    assert not replay.ok and replay.reported_error_code == "MEMORY_CANDIDATE_WRITE_FAILED"
    assert agent.memory_candidates.path.read_bytes() == before


# LLM: 新用户消息是新的权威证据；同一正文基于新版本重提不能被旧候选永久拦住。
# 函数用途: 保留真实用户明确更新记忆的正向入口。
def test_new_user_message_can_update_same_target_again(tmp_path):
    agent, prior = _target(tmp_path)
    operation = {"action": "replace", "entry_id": prior.entry_id, "origin": "user_explicit",
                 "content": "moneywise 项目使用新的长期方案"}
    first = ConfirmedRememberWorkflow(agent).execute(operation)
    assert first.ok and json.loads(first.output)["active_memory_changed"]
    intermediate = _intervening_update(agent, agent.memory.all()[0])
    params = agent._current_run_params
    params.request_id = "fresh-user-request"
    agent.conversation_store.messages.append({
        "thread_id": params.task_attributes["conversation_thread_id"], "role": "user",
        "content": operation["content"], "metadata": {"gateway_request_id": params.request_id}})
    second = ConfirmedRememberWorkflow(agent).execute(operation)
    assert second.ok and json.loads(second.output)["active_memory_changed"], second.output
    assert agent.memory.all()[0].version == intermediate.version + 1
    assert agent.memory.all()[0].content == operation["content"]


# LLM: 成功证据写入临时宿主的真实运行账本，引用经正式记忆验证器回查。
# 函数用途: 准备一个具有不同操作身份的成功工具结果。
def _tool_evidence(agent, suffix):
    params = agent._current_run_params
    agent.local_store.record_runtime_gate_ledger(RuntimeGateLedgerRecord(
        run_id=params.run_id, task_id=params.task_id, operation_id="op-" + suffix,
        tool="read_file", args_hash="sha256:test-" + suffix,
        idempotency_key="evidence-" + suffix, status="succeeded"))
    record = {"ok": True, "call_id": "call-" + suffix, "operation_id": "op-" + suffix,
              "run_id": params.run_id, "tool": "read_file", "artifact_ref": "artifact://" + suffix}
    loop = getattr(agent, "_current_tool_loop_params", None)
    records = list(getattr(loop, "archive_tool_calls", []) or [])
    agent._current_tool_loop_params = SimpleNamespace(archive_tool_calls=[*records, record])
    return record


# LLM: 同请求/位置/正文但新的已成功工具操作是独立证据，必须允许针对新正式版本合法更新。
# 函数用途: 防止修重放时把真正的新工具观察一并禁用。
def test_new_tool_evidence_allows_same_proposal_against_new_version(tmp_path):
    agent, prior = _target(tmp_path)
    first_ref = _tool_evidence(agent, "first")
    operation = {"action": "replace", "entry_id": prior.entry_id, "origin": "tool_verified",
                 "content": "moneywise 已验证的构建方案", "evidence_refs": [first_ref["call_id"]]}
    first = ConfirmedRememberWorkflow(agent).execute(operation)
    assert first.ok and json.loads(first.output)["active_memory_changed"], first.output
    intermediate = _intervening_update(agent, agent.memory.all()[0])
    fresh_ref = _tool_evidence(agent, "fresh")
    second = ConfirmedRememberWorkflow(agent).execute({**operation, "evidence_refs": [fresh_ref["call_id"]]})
    assert second.ok and json.loads(second.output)["active_memory_changed"], second.output
    current = agent.memory.all()[0]
    assert current.version == intermediate.version + 1 and current.content == operation["content"]
    candidates = [row for row in agent.memory_candidates.list() if row.target_entry_id == prior.entry_id]
    assert {row.target_entry_version for row in candidates} == {prior.version, intermediate.version}


@pytest.mark.parametrize("alias", ["call_id", "operation_id", "artifact_ref"])
# LLM: 一次工具操作的多个宿主引用只是别名，换别名不能取得新正式版本；同时覆盖已晋升后的旧证据重放。
# 函数用途: 验证工具证据规范化后幂等。
def test_same_tool_operation_alias_does_not_refresh_authority(tmp_path, alias):
    agent, prior = _target(tmp_path)
    ref = _tool_evidence(agent, "first")
    operation = {"action": "replace", "entry_id": prior.entry_id, "origin": "tool_verified",
                 "content": "moneywise 已验证的构建方案", "evidence_refs": [ref["call_id"]]}
    first = ConfirmedRememberWorkflow(agent).execute(operation)
    assert first.ok and json.loads(first.output)["active_memory_changed"], first.output
    intermediate = _intervening_update(agent, agent.memory.all()[0])
    replay = ConfirmedRememberWorkflow(agent).execute({"action": "batch", "operations": [
        {"action": "add", "origin": "user_explicit", "content": "另一件事实", "subject_key": "other"},
        {**operation, "evidence_refs": [ref[alias]]}]})
    assert agent.memory.all() == [intermediate], replay.output
    assert not replay.ok and replay.reported_error_code == "MEMORY_CANDIDATE_WRITE_FAILED"


@pytest.mark.parametrize("change", ["requested_kind", "target_kind", "subject", "scope_condition"])
# LLM: 目标元数据或模型提案分类改变也不构成新证据，仍由真实remember读取目标并做并发版本防护。
# 函数用途: 防止旧证据借kind、subject或scope变化重新取得覆盖权。
def test_same_evidence_cannot_refresh_version_through_domain_change(tmp_path, monkeypatch, change):
    agent, prior = _target(tmp_path)
    original = agent.memory_candidates.observe_many
    changed = []

    def observe_then_change(observations):
        saved = original(observations)
        if not changed:
            mutation = {"action": "replace", "entry_id": prior.entry_id,
                        "content": "中间的新正式方案", "expected_version": prior.version}
            if change == "target_kind":
                mutation["kind"] = "fact"
            if change == "subject":
                mutation["attributes"] = {"subject_key": "project.moneywise.changed_subject"}
            if change == "scope_condition":
                mutation["attributes"] = {"applies_when": "另一个明确条件"}
            agent.memory.apply_batch([mutation])
            changed.extend(agent.memory.all())
        return saved

    operation = {"action": "replace", "entry_id": prior.entry_id, "origin": "user_explicit",
                 "content": "来自旧证据的提议"}
    monkeypatch.setattr(agent.memory_candidates, "observe_many", observe_then_change)
    first = ConfirmedRememberWorkflow(agent).execute(operation)
    assert first.ok and not json.loads(first.output)["active_memory_changed"]
    before = agent.memory_candidates.path.read_bytes()
    replay = ConfirmedRememberWorkflow(agent).execute({**operation, **({"kind": "fact"} if change == "requested_kind" else {})})
    assert agent.memory.all() == changed, replay.output
    assert not replay.ok and replay.reported_error_code == "MEMORY_CANDIDATE_WRITE_FAILED"
    assert agent.memory_candidates.path.read_bytes() == before
