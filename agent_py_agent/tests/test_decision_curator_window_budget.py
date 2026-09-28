"""任务1 复现单测：curator 决策输入超模型窗口时的快速失败（invalid_input）与修复合同。

来源：my-agent-4 开发交流板任务 1。owner 的 data/decision/outcomes.jsonl 里 point=curator 有 7 条
status=error/reason=invalid_input、耗时 5–9ms（从未调用到模型）；根因是决策请求（state 里的整批材料
+ 题面）超过决策模型总输入窗口，被 typesafe_decision_wire.validate_typesafe_request_window 拒绝，
再由 decision_service 映射成 invalid_input。

本文件在**未修复**时红，修复后绿；变异验证：把 _decision_material 的候选引用共享改回逐题内联、
或去掉 _fit_items_to_window，相关用例会重新变红。

复现方法:
    cd <worktree> && PYTHONPATH=. python3 -m pytest agent_py_agent/tests/test_decision_curator_window_budget.py -q
"""

from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.backends.decision_protocol import (
    DecisionBinding,
    DecisionRequest,
    decision_json,
)
from agent_py_agent.agent.backends.typesafe_decision_wire import typesafe_payload
from agent_py_agent.agent.memory_archive.tokens import estimate_tokens
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorAuditInput,
    CuratorInputBatch,
    CuratorMessageInput,
)
from agent_py_agent.agent.memory_store.decision_curator import (
    _MAX_ANNOTATED_ITEMS,
    _decision_material,
)

WINDOW = 32768


def _audit(index: int) -> CuratorAuditInput:
    return CuratorAuditInput(
        event_id=f"event-{index:04d}", event_type="tool_call", created_at="2026-09-28T00:00:00+00:00",
        status="ok", operation_id="", tool_call_id=f"call-{index:04d}", tool_name="read_file",
        tool_success=True, error_code="", effect_outcome="confirmed", session_id="sess-1",
        thread_id="", request_id="req-1", task_id="task-1", run_id="run-1",
        content_hash="sha256:" + "a" * 64, source_ref="", artifact_ref="", artifact_hash="",
        artifact_size_bytes=0, preview="p" * 900,
    )


def _batch(count: int) -> CuratorInputBatch:
    return CuratorInputBatch(
        messages=(CuratorMessageInput(
            message_id="m-1", thread_id="t-1", role="user", channel="chat", created_at=0.0,
            full_content="c" * 500, content_preview="c" * 500, content_hash="sha256:" + "b" * 64,
            metadata={}),),
        audit_events=tuple(_audit(index) for index in range(count)),
    )


@pytest.fixture()
def oversized_batch() -> CuratorInputBatch:
    """构造一批其题面会把决策请求推过 32k 窗口的材料（模拟真机 :448 缩批后的规模）。"""
    return _batch(56)


def test_request_stays_within_window_with_existing_inline_criteria(oversized_batch):
    """题面保持既有内联候选合同（不改成引用），体积由整条来源的窗口裁剪控制。

    说明：早期版本把候选释义改成 `state.annotation_criteria` 引用以省字节，但那与既有测试
    把 criteria 当完整 dict 的合同冲突；改为保留内联形态，只做窗口兜底裁剪。
    """
    state, questions, sources, _revision = _decision_material(oversized_batch, window_tokens=32768)
    for question in questions.values():
        assert isinstance(question["criteria"], dict)
        assert {"not_needed", "need_data", "no_match", "abstain"} <= question["criteria"].keys()
        assert "required_refs" in question["criteria"]["need_data"]
    assert sources


def test_request_fits_model_window_without_dropping_material(oversized_batch):
    """修复后同一批材料的决策请求不再超过模型窗口；窗口够就一条不裁，不够只裁尾部整条。"""
    state, questions, sources, revision = _decision_material(oversized_batch, window_tokens=WINDOW)
    binding = DecisionBinding("curator", "owner-1", "op-1", "policy-1", revision)
    request = DecisionRequest(binding, state, questions)
    payload = typesafe_payload(request, "jev-latest")  # 超窗会在这里抛 DecisionInputError
    assert estimate_tokens(payload) <= WINDOW * 9 // 10
    available = min(len(oversized_batch.audit_events) + len(oversized_batch.messages), _MAX_ANNOTATED_ITEMS)
    assert 0 < len(sources) <= available


def test_window_budget_keeps_all_items_when_they_fit(oversized_batch):
    """窗口足够时不得因为预算计算而丢来源：预算只在真正超窗时才裁。"""
    wide_state, wide_questions, wide_sources, wide_revision = _decision_material(
        oversized_batch, window_tokens=200_000)
    available = min(len(oversized_batch.audit_events) + len(oversized_batch.messages), _MAX_ANNOTATED_ITEMS)
    assert len(wide_sources) == available
    assert estimate_tokens({"state": wide_state, "questions": wide_questions}) <= 200_000 * 9 // 10
    assert decision_json({"state": wide_state, "questions": wide_questions})
    assert wide_revision


def test_window_fit_drops_whole_tail_items_and_keeps_them_replayable(oversized_batch):
    """窗口不够时按整条来源从尾部裁：题面只剩前缀，被裁的来源不在题面里，留在游标之后等下轮。"""
    small = _decision_material(oversized_batch, window_tokens=4096)
    full = _decision_material(oversized_batch, window_tokens=WINDOW)
    small_state, small_questions, small_sources, small_revision = small
    _full_state, full_questions, full_sources, _full_revision = full

    assert 0 < len(small_sources) < len(full_sources)
    assert len(small_questions) == 2 * len(small_sources)
    # 保留的必须是同一条 items 顺序上的前缀（消息在前、审计在后），不能中间挖洞。
    assert list(small_sources) == list(full_sources)[:len(small_sources)]
    # 被裁的只是题面：它们不再被提问，但仍在 state.batch 的原始材料快照里（下一轮按游标重放靠它）。
    kept_ids = {source_id for _kind, source_id, _hash, _i, _refs in small_sources.values()}
    dropped_ids = {source_id for _kind, source_id, _hash, _i, _refs in full_sources.values()} - kept_ids
    batch_snapshot = json.dumps(small_state["batch"], ensure_ascii=False)
    questions_only = json.dumps(small_questions, ensure_ascii=False)
    assert dropped_ids
    assert all(source_id in batch_snapshot for source_id in dropped_ids)
    assert not any(source_id in questions_only for source_id in dropped_ids)
    # 题面身份引用仍指向 state 里同一批材料，供下一轮重放时按游标续上。
    assert decision_json({"state": small_state, "questions": small_questions})
    assert small_revision == _decision_material(oversized_batch, window_tokens=4096)[3]
