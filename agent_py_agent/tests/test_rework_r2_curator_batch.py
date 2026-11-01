"""Rework R1 / GPT-P1-02: the real Memory Curator promotes eligible candidates through promote_batch.

A real SimpleAgent is built so `_wire_memory_curator` supplies every curator dependency; only the curator
model backend is replaced by a scripted extraction. Candidate ledger flushes and vector snapshots are
observed on the original write paths, which still run unchanged.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store import candidates as ledger
from agent_py_agent.agent.memory_store.candidates import PROMOTION_BATCH_MAX_COUNT
from agent_py_agent.agent.memory_store.curator import MemoryCuratorService
from agent_py_agent.agent.retrieval.vector_store import VectorStore
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder
from agent_py_agent.tests.test_curator_observation_identity import _Backend, _output
from agent_py_agent.tests.test_curator_uncertain_outcome import _unknown
from agent_py_agent.tests.test_memory_promotion_v2 import _explicit

_BATCH_OPTIONS = {"reviewer": "memory-promotion-service", "automatic": True, "confirmed": False}


def _agent(tmp_path):
    return SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[]), tmp_path / "workspace")


def _thread(agent, conversation_id: str = "chat-1"):
    return agent.conversation_store.threads.get_or_create(dict(
        canonical_user_id="user-1", channel="internal", channel_conversation_id=conversation_id,
        channel_user_id="user-1", learning_source=ls.user_dialogue_projection()))


# 函数用途: 经真实候选服务登记带精确消息证据的用户明确事实，均为自动晋升合格的待审候选。
def _candidates(agent, thread, count: int, prefix: str = "r2"):
    return [_explicit(agent.memory_promotion, agent.conversation_store, thread, content=f"独立批次事实 {prefix} {index}",
                      subject_key=f"{prefix}.fact.{index}", promotion_target="") for index in range(count)]


# 函数用途: 由组合根注入的依赖构造策展服务，只把模型后端换成脚本化提取（不新增候选）。
def _curator(agent, rows) -> MemoryCuratorService:
    ref = rows[-1].source_message_refs[0]
    message = SimpleNamespace(message_id=ref["message_id"], thread_id=ref["thread_id"])
    dependencies = replace(agent.memory_curator.dependencies, backend=_Backend(_output(message, [])),
                           model_router=None, annotate_batch=None)
    return MemoryCuratorService(config=agent.memory_curator.config, dependencies=dependencies)


# 函数用途: 观察真实promote_batch调用、批次内候选账本落盘及向量快照；逐条promote一旦被调用即失败。
def _observe(agent, monkeypatch) -> SimpleNamespace:
    seen = SimpleNamespace(batches=[], writes=[], snapshots=[], active=[])
    write_original = ledger.write_private_text_file_atomic_unlocked
    snapshot_original = VectorStore._write_snapshot
    batch_original = agent.memory_promotion.promote_batch

    def write(target, text, **kwargs):
        result = write_original(target, text, **kwargs)
        if target == agent.memory_candidates.path and seen.active:
            seen.writes.append(len(seen.batches))
        return result

    def snapshot(store, items, identity, dim):
        seen.snapshots.append(len(items))
        return snapshot_original(store, items, identity, dim)

    def promote_batch(batch, **kwargs):
        seen.batches.append(([row.candidate_id for row in batch], kwargs))
        seen.active.append(True)
        try:
            return batch_original(batch, **kwargs)
        finally:
            seen.active.pop()

    def single(*_args, **_kwargs):
        pytest.fail("curator fell back to single-item promotion")

    monkeypatch.setattr(agent.memory, "_embedder", LocalHashingEmbedder(dim=8))
    monkeypatch.setattr(ledger, "write_private_text_file_atomic_unlocked", write)
    monkeypatch.setattr(VectorStore, "_write_snapshot", snapshot)
    monkeypatch.setattr(agent.memory_promotion, "promote_batch", promote_batch)
    monkeypatch.setattr(agent.memory_promotion, "promote", single)
    return seen


def test_composition_root_wires_promote_batch_for_automatic_promotion(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    callback = agent.memory_curator._promotion_batch_callback
    assert callback is not None and callback is agent.memory_curator.dependencies.promotion_batch_callback
    # 旧逐条回调仍保留给未注入批量入口的显式调用方。
    assert agent.memory_curator.promotion_callback is not None
    received = []
    monkeypatch.setattr(agent.memory_promotion, "promote_batch",
                        lambda batch, **kwargs: received.append((batch, kwargs)) or ("result",))
    batch = (object(),)
    assert callback(batch) == ("result",)
    assert received == [(batch, _BATCH_OPTIONS)]


@pytest.mark.parametrize("count", [1, 8])
def test_public_curator_run_promotes_backlog_through_one_batch(tmp_path, monkeypatch, count):
    agent = _agent(tmp_path)
    rows = _candidates(agent, _thread(agent), count)
    curator = _curator(agent, rows)
    seen = _observe(agent, monkeypatch)
    result = curator.run(reason="admin")
    assert result.status == "succeeded", result
    ids = [row.candidate_id for row in rows]
    assert seen.batches == [(ids, _BATCH_OPTIONS)]
    assert [receipt["candidate_id"] for receipt in result.promotion_results] == ids
    assert all(receipt["promoted"] and receipt["formal_memory_changed"] for receipt in result.promotion_results)
    assert result.formal_commits == count and f"auto_promoted:{count}" in result.warnings
    # 批准与终态各一次候选账本落盘，全批只发表一次向量快照。
    assert len(seen.writes) == 2
    assert seen.snapshots == [count]
    assert [agent.memory_candidates.get(candidate_id).status for candidate_id in ids] == ["promoted"] * count
    assert len(agent.memory.all()) == count


def test_more_than_32_candidates_are_promoted_in_ordered_bounded_chunks(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    rows = _candidates(agent, _thread(agent), PROMOTION_BATCH_MAX_COUNT + 8)
    curator = _curator(agent, rows)
    seen = _observe(agent, monkeypatch)
    ids = [row.candidate_id for row in rows]
    promoted, warnings, receipts = curator._promote_eligible([*ids, *ids[:3]])
    assert [batch for batch, _options in seen.batches] == [ids[:PROMOTION_BATCH_MAX_COUNT], ids[PROMOTION_BATCH_MAX_COUNT:]]
    assert [receipt["candidate_id"] for receipt in receipts] == ids
    assert promoted == len(ids) and warnings == ()
    assert Counter(seen.writes) == {1: 2, 2: 2}
    assert len(seen.snapshots) == 2
    assert len(agent.memory.all()) == len(ids)


def test_manual_only_and_learning_exclusions_are_filtered_before_the_batch(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    kept = _candidates(agent, _thread(agent), 3, "kept")
    excluded = _candidates(agent, _thread(agent, "chat-excluded"), 2, "excluded")
    curator = _curator(agent, kept)
    seen = _observe(agent, monkeypatch)
    order = [excluded[0], kept[0], kept[1], excluded[1], kept[2], kept[0]]
    requested = [row.candidate_id for row in order]
    policy = curator.config
    curator.config = replace(policy, auto_promotion_policy="manual_only")
    assert curator._promote_eligible(requested) == (0, (), ())
    curator.config = policy
    curator.learning_exclusions = lambda: (frozenset(), True)
    assert curator._promote_eligible(requested) == (0, ("learning_exclusions_unreadable",), ())
    assert seen.batches == []
    curator.learning_exclusions = lambda: (frozenset({"chat-excluded"}), False)
    promoted, _warnings, receipts = curator._promote_eligible(requested)
    expected = [row.candidate_id for row in kept]
    assert [batch for batch, _options in seen.batches] == [expected]
    assert promoted == 3 and [receipt["candidate_id"] for receipt in receipts] == expected
    assert all(agent.memory_candidates.get(row.candidate_id).status != "promoted" for row in excluded)
    assert len(agent.memory.all()) == 3


def test_public_run_partial_outcome_keeps_durable_obligation(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    rows = _candidates(agent, _thread(agent), 8)
    curator = _curator(agent, rows)
    seen = _observe(agent, monkeypatch)
    original = agent.memory_promotion._commit_target

    def commit(row, context):
        if row.candidate_id == rows[3].candidate_id:
            raise OSError("simulated target I/O error")
        return original(row, context)

    monkeypatch.setattr(agent.memory_promotion, "_commit_target", commit)
    result = curator.run(reason="admin")
    assert result.status == "succeeded", result
    assert len(seen.batches) == 1
    assert [receipt["promoted"] for receipt in result.promotion_results] == [True] * 3 + [False] + [True] * 4
    assert result.formal_commits == 7 and "auto_promoted:7" in result.warnings
    pending = agent.memory_candidates.get(rows[3].candidate_id)
    assert pending.status == "approved" and pending.commit_obligation and pending.obligation_retry_after
    assert result.promotion_results[3]["reason_code"] == "PROMOTION_IO_BLOCKED"
    assert not result.promotion_results[3]["formal_memory_changed"]
    assert len(agent.memory.all()) == 7


@pytest.mark.parametrize("kind", ["ordinary", "missing", "transport", "exit"])
def test_batch_errors_never_fall_back_to_single_item_promotion(tmp_path, monkeypatch, kind):
    agent = _agent(tmp_path)
    rows = _candidates(agent, _thread(agent), PROMOTION_BATCH_MAX_COUNT + 2)
    curator = _curator(agent, rows)
    _observe(agent, monkeypatch)
    observed = agent.memory_promotion.promote_batch
    error = RuntimeError("simulated batch failure") if kind == "ordinary" else _unknown(kind)
    sizes = []

    def promote_batch(batch, **kwargs):
        sizes.append(len(batch))
        if len(sizes) == 1:
            raise error
        return observed(batch, **kwargs)

    monkeypatch.setattr(agent.memory_promotion, "promote_batch", promote_batch)
    ids = [row.candidate_id for row in rows]
    if kind == "ordinary":
        # 普通失败只记警告并继续下一批；失败批次不逐条重试。
        promoted, warnings, receipts = curator._promote_eligible(ids)
        assert sizes == [PROMOTION_BATCH_MAX_COUNT, 2]
        assert warnings == ("CURATOR_AUTO_PROMOTION_FAILED",)
        assert promoted == 2 and [receipt["candidate_id"] for receipt in receipts] == ids[PROMOTION_BATCH_MAX_COUNT:]
        assert {agent.memory_candidates.get(cid).status for cid in ids[:PROMOTION_BATCH_MAX_COUNT]} == {"pending_review"}
    else:
        with pytest.raises(type(error)) as caught:
            curator._promote_eligible(ids)
        assert caught.value is error and sizes == [PROMOTION_BATCH_MAX_COUNT]
        assert {agent.memory_candidates.get(cid).status for cid in ids} == {"pending_review"}
