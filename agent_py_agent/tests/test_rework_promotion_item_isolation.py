"""R4-P1-04：一条确定性失败的候选不能拖垮同批其它候选的正式晋升。

被用户遗忘的正文在正式库写入屏障处抛 ValueError("MEMORY_TOMBSTONED_CONTENT")；旧逻辑让它逃出逐条循环，整批向量、
候选终态和后续候选都被放弃，Curator 每个周期都整块失败。这里用真实候选服务、正式库和组合根 Curator 验证逐条隔离，
执行结果未知类异常仍原样中止整批。
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.common.owner_write_errors import OwnerManagementIngressUnavailable
from agent_py_agent.agent.common.request_execution_ports import RequestExecutionContextUnavailable
from agent_py_agent.agent.memory_store import promotion
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder
from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime
from agent_py_agent.tests.test_rework_r2_curator_batch import _agent, _curator, _thread

_OPTIONS = {"reviewer": "memory-promotion-service", "automatic": True}
_FORGOTTEN = "用户已要求遗忘的事实 X"
_KEPT = ("独立保留的事实 A", "独立保留的事实 B")


# LLM: 先经正式服务晋升 X，再按精确版本以用户遗忘删除，留下永久正文屏障；随后登记 A、X、B 三条自动合格候选。
# 函数用途: 构造中间一条撞上遗忘屏障、两侧都可正常提交的候选批次。
def _batch_around_forgotten(service, conversations, thread):
    seed = _explicit(service, conversations, thread, content=_FORGOTTEN, subject_key="isolation.seed")
    assert service.promote_batch([seed], **_OPTIONS)[0].promoted
    [record] = service.long_term.all()
    service.long_term.apply_batch([{"action": "remove", "entry_id": record.entry_id, "source": "user-forget",
                                    "expected_version": record.version}])
    assert service.long_term.all() == []
    contents = (_KEPT[0], _FORGOTTEN, _KEPT[1])
    return [_explicit(service, conversations, thread, content=content, subject_key=f"isolation.{index}")
            for index, content in enumerate(contents)]


# LLM: 只经隔离夹具登记互不冲突的用户明确事实，不改证据或自动资格。
# 函数用途: 为注入失败的用例准备三条可自动晋升的候选。
def _independent(service, conversations, thread):
    return [_explicit(service, conversations, thread, content=f"互不相关的事实 {index}",
                      subject_key=f"isolation.independent.{index}") for index in range(3)]


# LLM: 被遗忘正文只让本条带结构化屏障码失败并保留提交义务；两侧候选照常正式提交并收口终态。
# 函数用途: 证明遗忘屏障是逐条失败，不再中止整批。
def test_tombstoned_candidate_in_the_middle_does_not_block_its_batch(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    rows = _batch_around_forgotten(service, conversations, thread)
    results = service.promote_batch(rows, **_OPTIONS)
    assert [(result.promoted, result.reason_code) for result in results] == [
        (True, "PROMOTED"), (False, "MEMORY_TOMBSTONED_CONTENT"), (True, "PROMOTED")]
    first, forgotten, last = (service.candidates.get(row.candidate_id) for row in rows)
    assert [(row.status, row.commit_obligation) for row in (first, last)] == [("promoted", False)] * 2
    assert (forgotten.status, forgotten.commit_obligation) == ("approved", True)
    assert sorted(record.content for record in service.long_term.all()) == sorted(_KEPT)


# LLM: 组合根注入的批量回调与真实向量库；第二周期只剩 X 的逐条回执，不再出现整块失败警告。
# 函数用途: 证明 Curator 每周期都能提交其余候选，失败候选按结构化回执重试。
def test_curator_cycles_commit_around_a_tombstoned_candidate(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    monkeypatch.setattr(agent.memory, "_embedder", LocalHashingEmbedder(dim=8))
    rows = _batch_around_forgotten(agent.memory_promotion, agent.conversation_store, _thread(agent))
    curator = _curator(agent, rows)
    first = curator.run(reason="admin")
    assert first.status == "succeeded", first
    assert first.formal_commits == 2
    assert [receipt["candidate_id"] for receipt in first.promotion_results] == [row.candidate_id for row in rows]
    assert "CURATOR_AUTO_PROMOTION_FAILED" not in first.warnings
    formal = agent.memory.all()
    assert sorted(record.content for record in formal) == sorted(_KEPT)
    assert sorted(agent.memory._vector_store().ids()) == sorted(record.entry_id for record in formal)
    second = curator.run(reason="admin")
    assert second.status == "succeeded", second
    assert [(receipt["candidate_id"], receipt["promoted"], receipt["reason_code"])
            for receipt in second.promotion_results] == [(rows[1].candidate_id, False, "MEMORY_TOMBSTONED_CONTENT")]
    assert "CURATOR_AUTO_PROMOTION_FAILED" not in second.warnings


# LLM: 未分类的普通异常只映射稳定码，不把异常正文写进结果；失败项保留义务，其它项照常提交。
# 函数用途: 证明任意确定性逐条失败都不会让整批逃逸。
def test_unexpected_item_error_fails_only_that_item(tmp_path, monkeypatch):
    service, conversations, thread = _runtime(tmp_path)
    rows = _independent(service, conversations, thread)
    original = promotion._commit_admitted

    # LLM: 只让选定候选在正式提交前失败，其它候选原样走生产提交。
    # 函数用途: 为一条候选注入普通 RuntimeError。
    def commit(owner, candidate, review, **options):
        if candidate.candidate_id == rows[1].candidate_id:
            raise RuntimeError("simulated deterministic item failure")
        return original(owner, candidate, review, **options)

    monkeypatch.setattr(promotion, "_commit_admitted", commit)
    results = service.promote_batch(rows, **_OPTIONS)
    assert [(result.promoted, result.reason_code) for result in results] == [
        (True, "PROMOTED"), (False, "PROMOTION_ITEM_FAILED"), (True, "PROMOTED")]
    failed = service.candidates.get(rows[1].candidate_id)
    assert (failed.status, failed.commit_obligation) == ("approved", True)
    assert len(service.long_term.all()) == 2


# LLM: 普通异常兜底排在未知结果分支之后；与屏障同为 ValueError 的未知类也必须原对象中止整批。
# 函数用途: 证明逐条隔离不会吞掉执行结果未知。
@pytest.mark.parametrize("error", [
    RequestExecutionContextUnavailable("synthetic missing original source"),
    OwnerManagementIngressUnavailable("synthetic ingress unavailable"),
], ids=["runtime-error", "value-error"])
def test_uncertain_item_outcome_still_aborts_the_batch(tmp_path, monkeypatch, error):
    assert isinstance(error, promotion._UNCERTAIN_PROMOTION_ERRORS)
    service, conversations, thread = _runtime(tmp_path)
    rows = _independent(service, conversations, thread)
    original = promotion._commit_admitted

    # LLM: 只让选定候选抛出未知类异常，其它候选原样走生产提交。
    # 函数用途: 为一条候选注入执行结果未知。
    def commit(owner, candidate, review, **options):
        if candidate.candidate_id == rows[1].candidate_id:
            raise error
        return original(owner, candidate, review, **options)

    monkeypatch.setattr(promotion, "_commit_admitted", commit)
    with pytest.raises(type(error)) as caught:
        service.promote_batch(rows, **_OPTIONS)
    assert caught.value is error
