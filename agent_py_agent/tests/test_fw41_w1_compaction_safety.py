"""W1（CP-01 / PD-2）压缩核心安全回归测试：
1. active-turn 缓存分叉严格校验完整 4 元组（run_id, attempt_id, turn_id, call_id）与 tool_name，冲突或错位时退回独立摘要。
2. tool_ir_history 运行时事实按有界点时生命周期退休，保护保留工具组的治理快照、媒体与非替代增量义务。
3. 零进展压缩候选（candidate >= before）在提交/写检查点前以 COMPACT_NO_PROGRESS 中止，原历史与代次完好无损。
"""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
from agent_py_agent.agent.agent_core.tool_ir_history import (
    applied_compact_summary_item,
    drop_tool_call_pairs,
    summarized_runtime_fact_indexes,
)
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation.active_turn_compact import (
    ActiveTurnArchiveCompactRequest,
    ActiveTurnCacheFork,
    _ActiveTurnArchiveCompactPlan,
    _cache_fork_history,
    compact_carried_active_turn_archive,
    model_visible_active_turn_tool_calls,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    _commit_or_reject_trigger_fallback,
    _CompactCandidate,
    _CompactRunRequest,
    _RejectedCandidates,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import (
    CompactCapacityFacts,
    ConversationCompactError,
    compact_degradation_decision,
    compact_failure_progress_fields,
)
from agent_py_agent.agent.conversation.compact_progress import (
    COMPACT_CAPACITY_PROGRESS_FIELDS,
    normalize_conversation_compact_progress,
)
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.compact_tool_identity import compact_tool_refs
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import compact_semantic_summary
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)


def _tool_record(call, output_preview: str = "evidence", *, round_index: int = 1) -> dict[str, object]:
    return {
        **call.to_dict(),
        "tool": call.tool_name,
        "ok": True,
        "parameters": dict(call.arguments),
        "model_parameters": dict(call.arguments),
        "output_preview": output_preview,
        "tool_round": round_index,
    }


def _fork_from_ir(ir: tuple[object, ...]) -> ActiveTurnCacheFork:
    tool = {"name": "read_file", "description": "读取文件", "input_schema": {"type": "object", "properties": {}}}
    return ActiveTurnCacheFork(
        provider_prompt="继续任务",
        provider_history_messages=({"role": "user", "content": "前置消息"},),
        tool_ir_history=ir,
        tools=(tool,),
        system_instruction="宿主系统指令",
    )


def _fork_plan(agent, thread, records: tuple[dict[str, object], ...], ir: tuple[object, ...] = ()):
    refs = compact_tool_refs(list(records))
    return _ActiveTurnArchiveCompactPlan(
        thread=thread,
        binding=SimpleNamespace(thread=thread),
        policy=runtime_compact_policy(agent),
        visible_records=records,
        source_records=records,
        source_call_ids=tuple(ref["call_id"] for ref in refs),
        source_tool_refs=refs,
        retained_records=(),
        retained_call_ids=(),
        retained_tool_refs=(),
        projected_tokens_before=500,
        progress={},
        source_ir_history=ir,
        retained_ir_history=(),
    )


def _setup_carried_agent(
    tmp_path,
    monkeypatch,
    *,
    trigger_percent: int = 90,
    recovery_target_percent: int = 60,
    output_cap: int = 64,
):
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        prompt_files=[],
        max_tokens=output_cap,
        model_context_window_tokens=10_000,
        memory_compact_trigger_percent=trigger_percent,
        memory_compact_recovery_target_percent=recovery_target_percent,
    )
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    agent = SimpleAgent(config, tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "fw41-w1-owner",
        "channel": "test",
        "channel_conversation_id": "fw41-w1-thread",
        "channel_user_id": "fw41-w1-owner",
    })
    attrs = {"conversation_thread_id": thread.thread_id, CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True}
    monkeypatch.setattr(compact_semantic_summary, "summarize_live_tool_history", lambda *_, **__: "精简摘要结果")
    return agent, store, thread, attrs


# ---------------------------------------------------------------------------
# 1. F-CP-FORK-IDENTITY: _cache_fork_history 完整 4 元组匹配与回退
# ---------------------------------------------------------------------------


def test_cache_fork_history_requires_full_4tuple_identity_and_falls_back_on_collision(tmp_path, monkeypatch) -> None:
    agent, _store, thread, _attrs = _setup_carried_agent(tmp_path, monkeypatch)
    c0_turn0 = canonical_history_call(
        "read_file", {"path": "a.txt"}, call_id="call-shared",
        run_id="run-1", attempt_id="att-1", turn_id="turn-0",
    )
    c1_turn1 = canonical_history_call(
        "read_file", {"path": "b.txt"}, call_id="call-shared",
        run_id="run-1", attempt_id="att-1", turn_id="turn-1",
    )
    c_tail = canonical_history_call(
        "read_file", {"path": "tail.txt"}, call_id="call-tail",
        run_id="run-1", attempt_id="att-1", turn_id="turn-2",
    )
    r0 = canonical_history_result(c0_turn0, "DATA_TURN_0")
    r1 = canonical_history_result(c1_turn1, "DATA_TURN_1")
    r_tail = canonical_history_result(c_tail, "DATA_TAIL")
    applied = applied_compact_summary_item("前代摘要", 1, "cipher")

    ir = (
        UserTurn("任务输入"),
        AssistantTurn(tool_calls=[c0_turn0]),
        r0,
        AssistantTurn(tool_calls=[c1_turn1]),
        r1,
        AssistantTurn(tool_calls=[c_tail]),
        r_tail,
        applied,
    )
    fork = _fork_from_ir(ir)
    rec0 = _tool_record(c0_turn0, "DATA_TURN_0", round_index=1)

    # 只选 turn-0 同一裸 call_id：前缀截在 r0，不可泄漏同 call_id 的 turn-1 或 call-tail，并保留 applied 摘要。
    sliced = _cache_fork_history(fork, _fork_plan(agent, thread, (rec0,)))
    assert sliced == (UserTurn("任务输入"), AssistantTurn(tool_calls=[c0_turn0]), r0, applied)

    # 裸 call_id 相同但 turn_id / run_id / attempt_id 任一维不同：严禁错配，必须退回独立摘要 (None)。
    wrong_turn = canonical_history_call(
        "read_file", {"path": "a.txt"}, call_id="call-shared",
        run_id="run-1", attempt_id="att-1", turn_id="turn-other",
    )
    assert _cache_fork_history(fork, _fork_plan(agent, thread, (_tool_record(wrong_turn),))) is None

    wrong_run = canonical_history_call(
        "read_file", {"path": "a.txt"}, call_id="call-shared",
        run_id="run-2", attempt_id="att-1", turn_id="turn-0",
    )
    assert _cache_fork_history(fork, _fork_plan(agent, thread, (_tool_record(wrong_run),))) is None

    wrong_attempt = canonical_history_call(
        "read_file", {"path": "a.txt"}, call_id="call-shared",
        run_id="run-1", attempt_id="att-2", turn_id="turn-0",
    )
    assert _cache_fork_history(fork, _fork_plan(agent, thread, (_tool_record(wrong_attempt),))) is None

    # 工具名与权威来源记录不一致时必须退回 None。
    wrong_name_call = canonical_history_call(
        "grep_search", {"path": "a.txt"}, call_id="call-shared",
        run_id="run-1", attempt_id="att-1", turn_id="turn-0",
    )
    assert _cache_fork_history(fork, _fork_plan(agent, thread, (_tool_record(wrong_name_call),))) is None

    # fork IR 中若存在相同 4 元组重复配对或未配对重复调用，必须静默返回 None，不能外泄 COMPACT_TOOL_COVERAGE_UNKNOWN。
    dup_paired_ir = (
        UserTurn("任务输入"),
        AssistantTurn(tool_calls=[c0_turn0]),
        r0,
        AssistantTurn(tool_calls=[c0_turn0]),
        r0,
    )
    assert _cache_fork_history(_fork_from_ir(dup_paired_ir), _fork_plan(agent, thread, (rec0,))) is None

    unpaired_dup_ir = (
        UserTurn("任务输入"),
        AssistantTurn(tool_calls=[c0_turn0]),
        UserTurn("中断打断了结果"),
        AssistantTurn(tool_calls=[c0_turn0]),
        r0,
    )
    assert _cache_fork_history(_fork_from_ir(unpaired_dup_ir), _fork_plan(agent, thread, (rec0,))) is None

    # fork IR 内部 AssistantTurn 与 ToolResult tool_name 不一致时退回 None。
    mismatched_ir_result = (
        UserTurn("任务输入"),
        AssistantTurn(tool_calls=[c0_turn0]),
        canonical_history_result(
            replace(c0_turn0, tool_name="bash"),
            "DATA_TURN_0",
        ),
    )
    assert _cache_fork_history(_fork_from_ir(mismatched_ir_result), _fork_plan(agent, thread, (rec0,))) is None


def test_active_turn_compact_falls_back_to_independent_summary_when_4tuple_diverges(tmp_path, monkeypatch) -> None:
    agent, store, thread, attrs = _setup_carried_agent(tmp_path, monkeypatch)
    archived_call = canonical_history_call(
        "read_file", {"path": "archived.txt"}, call_id="call-shared",
        run_id="run-1", attempt_id="att-1", turn_id="turn-old",
    )
    live_call = canonical_history_call(
        "read_file", {"path": "live.txt"}, call_id="call-shared",
        run_id="run-1", attempt_id="att-1", turn_id="turn-new",
    )
    records = [_tool_record(archived_call, "OLD_PAYLOAD", round_index=1)]

    agent.backend = HttpBackend(BackendOptions(
        api_base="https://example.invalid", api_key="fake-key", model_name="test-model",
        context_window_tokens=10_000, max_tokens=64,
    ))
    captured_requests: list[object] = []

    def fake_request_json(_payload, **_kw):
        return {
            "id": "resp-1",
            "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "独立摘要完成"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    # 恢复真实摘要函数路径以触发 cache_fork 判别并检查真实摘要输入。
    from agent_py_agent.agent.memory_archive.compact_semantic_summary import (
        summarize_live_tool_history as real_sum,
    )

    def spy_summarize(summary_req):
        captured_requests.append(summary_req)
        return real_sum(summary_req)

    monkeypatch.setattr(agent.backend, "request_json", fake_request_json)
    monkeypatch.setattr(compact_semantic_summary, "summarize_live_tool_history", spy_summarize)

    # fork 里只出现 turn-new 的同一裸 call_id，归档要压的是 turn-old：严禁分叉并错摘 turn-new。
    fork = _fork_from_ir((
        UserTurn("任务"),
        AssistantTurn(tool_calls=[live_call]),
        canonical_history_result(live_call, "NEW_PAYLOAD"),
    ))
    result = compact_carried_active_turn_archive(
        agent,
        store,
        thread,
        records,
        ActiveTurnArchiveCompactRequest(
            task_attributes=attrs,
            request_id="req-fork-fallback",
            attempt_id="att-req",
            task_prompt="核对证据",
            cache_fork=fork,
            projected_tokens_before=2_000,
            request_projector=lambda summary, retained, gen: ConversationCompactProjection(
                300, {"summary": summary, "retained": retained, "gen": gen},
            ),
        ),
    )
    assert result.compacted is True
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.tool_choice.reason == "active_turn_summary_no_prefix"
    assert req.provider_history_messages == ()
    assert "OLD_PAYLOAD" in str(req.history)
    assert "NEW_PAYLOAD" not in str(req.history)


# ---------------------------------------------------------------------------
# 2. F1a / F-CP-RETENTION: 保留晚期/早期工具组不再锁死数百轮具名点时快照，守住治理快照与增量义务
# ---------------------------------------------------------------------------


def test_retained_late_tool_group_retires_superseded_runtime_facts_snapshots() -> None:
    retained_patch = canonical_history_call(
        "apply_patch", {"path": "src/app.py"}, call_id="patch-retained",
        run_id="run-1", attempt_id="att-1", turn_id="turn-0",
    )
    history: list[object] = [
        UserTurn("执行长会话改造"),
        RuntimeFactsTurn("env-initial-governing", source="execution_facts"),
        AssistantTurn(tool_calls=[retained_patch]),
        canonical_history_result(retained_patch, "patched file ref"),
    ]
    retired_ids: set[str] = set()
    for round_idx in range(240):
        call = canonical_history_call(
            "read_file", {"path": f"file-{round_idx}.py"}, call_id=f"read-{round_idx}",
            run_id="run-1", attempt_id="att-1", turn_id=f"turn-{round_idx + 1}",
        )
        retired_ids.add(call.call_id)
        history.append(RuntimeFactsTurn(f"snap-{round_idx}", source="execution_facts"))
        history.append(AssistantTurn(tool_calls=[call]))
        history.append(canonical_history_result(call, f"ok-{round_idx}"))

    history.append(RuntimeFactsTurn("env-latest", source="execution_facts"))
    snapshot_before = list(history)

    retired_positions = {
        idx for idx, item in enumerate(history)
        if isinstance(item, AssistantTurn) or getattr(item, "call_id", None) in retired_ids
    }
    started = time.perf_counter()
    indexes_to_drop = summarized_runtime_fact_indexes(history, retired_positions)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0
    assert history == snapshot_before  # 只读计算不得修改原 history
    # 全部 240 个中间 superseded 快照被精准标记退休；第 1 条治理快照与最后 1 条最新快照保留。
    assert len(indexes_to_drop) == 240
    assert 1 not in indexes_to_drop
    assert (len(history) - 1) not in indexes_to_drop

    params = SimpleNamespace(tool_ir_history=list(history))
    dropped = drop_tool_call_pairs(params, retired_ids, drop_completed_tool_turns=True)
    assert dropped == 240
    remaining_facts = [item.text for item in params.tool_ir_history if isinstance(item, RuntimeFactsTurn)]
    assert remaining_facts == ["env-initial-governing", "env-latest"]


def test_runtime_fact_retirement_preserves_governing_snapshots_media_additive_and_untyped() -> None:
    c_ret = canonical_history_call("apply_patch", {"path": "a.py"}, call_id="c-ret", run_id="r", attempt_id="a", turn_id="t1")
    c_cov = canonical_history_call("read_file", {"path": "b.py"}, call_id="c-cov", run_id="r", attempt_id="a", turn_id="t2")

    history = [
        RuntimeFactsTurn("ws-pre-old", source="prompt.workspace"),
        RuntimeFactsTurn("ws-governing", source="prompt.workspace"),
        AssistantTurn(tool_calls=[c_ret]),
        canonical_history_result(c_ret, "retained patch"),
        RuntimeFactsTurn("ws-superseded", source="prompt.workspace"),
        RuntimeFactsTurn("wake-first", source="host.lifecycle_wake"),
        RuntimeFactsTurn("guidance-first", source="runtime.guidance"),
        RuntimeFactsTurn("img-fact", source="execution_facts", media=({"sha256": "a" * 64},)),
        RuntimeFactsTurn("anon-between", source=""),
        AssistantTurn(tool_calls=[c_cov]),
        canonical_history_result(c_cov, "covered read"),
        RuntimeFactsTurn("ws-post-barrier", source="prompt.workspace"),
        RuntimeFactsTurn("ws-latest", source="prompt.workspace"),
        RuntimeFactsTurn("wake-latest", source="host.lifecycle_wake"),
        RuntimeFactsTurn("guidance-latest", source="runtime.guidance"),
        RuntimeFactsTurn("exec-latest", source="execution_facts"),
        RuntimeFactsTurn("anon-latest", source=""),
    ]

    params = SimpleNamespace(tool_ir_history=list(history))
    assert drop_tool_call_pairs(params, {"c-cov"}, drop_completed_tool_turns=True) == 1
    kept_texts = [item.text for item in params.tool_ir_history if isinstance(item, RuntimeFactsTurn)]

    # ws-superseded 在 c-ret 与 c-cov 之间且非治理快照 -> 退休。
    assert "ws-superseded" not in kept_texts
    # ws-pre-old 被 ws-governing 覆盖、不治理 c-ret 且在 c-cov 之前 -> 退休。
    assert "ws-pre-old" not in kept_texts
    # c-ret 的治理快照、非替代宿主唤醒/指引、带媒体事实、匿名记录、barrier 之后未证明快照与最新快照全部保留。
    for expected in (
        "ws-governing",
        "wake-first",
        "guidance-first",
        "img-fact",
        "anon-between",
        "ws-post-barrier",
        "ws-latest",
        "wake-latest",
        "guidance-latest",
        "exec-latest",
        "anon-latest",
    ):
        assert expected in kept_texts


# ---------------------------------------------------------------------------
# 3. F1b: COMPACT_NO_PROGRESS 拒绝零进展提交且保护检查点/CAS/消息历史
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("candidate_tokens", [2_000, 2_500])
def test_active_turn_compact_aborts_before_commit_on_no_progress(tmp_path, monkeypatch, candidate_tokens: int) -> None:
    agent, store, thread, attrs = _setup_carried_agent(tmp_path, monkeypatch)
    calls = [
        canonical_history_call("read_file", {"path": f"doc-{i}.txt"}, call_id=f"call-{i}", run_id="r", attempt_id="a", turn_id=f"t-{i}")
        for i in range(3)
    ]
    records = [_tool_record(call, f"BODY_{i}", round_index=i + 1) for i, call in enumerate(calls)]
    events: list[dict[str, object]] = []

    with pytest.raises(ConversationCompactError) as exc_info:
        compact_carried_active_turn_archive(
            agent,
            store,
            thread,
            records,
            ActiveTurnArchiveCompactRequest(
                task_attributes=attrs,
                request_id="req-no-progress",
                attempt_id="att-1",
                task_prompt="核对文档",
                projected_tokens_before=2_000,
                progress_callback=events.append,
                request_projector=lambda summary, retained, gen: ConversationCompactProjection(
                    candidate_tokens, {"summary": summary, "retained": retained, "gen": gen},
                ),
            ),
        )

    err = exc_info.value
    assert err.code == "COMPACT_NO_PROGRESS"
    assert isinstance(err.capacity, CompactCapacityFacts)
    assert err.capacity.candidate_tokens == candidate_tokens
    assert err.capacity.candidates_tried == 1
    assert compact_degradation_decision(err, degrade_eligible=True) == (False, "COMPACT_NO_PROGRESS")

    # 线程、检查点与可见记录均不得被写脏。
    current = store.threads.require(thread.thread_id)
    assert current.compact_generation == 0
    assert current.compact_checkpoint_id == ""
    assert current.summary == ""
    assert current.compact_failure_code == "COMPACT_NO_PROGRESS"
    assert committed_compact_checkpoint_chain(agent, current) == ()
    assert model_visible_active_turn_tool_calls(agent, attrs, records) == records

    failed_events = [item for item in events if item.get("phase") == "failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["error_code"] == "COMPACT_NO_PROGRESS"
    fields_payload = compact_failure_progress_fields(err)
    expected_non_none = {field for field in COMPACT_CAPACITY_PROGRESS_FIELDS if field != "fixed_tokens"}
    assert expected_non_none <= set(fields_payload)
    assert fields_payload["error_code"] == "COMPACT_NO_PROGRESS"
    assert fields_payload["candidate_tokens"] == candidate_tokens
    normalized = normalize_conversation_compact_progress(failed_events[0])
    assert normalized["error_code"] == "COMPACT_NO_PROGRESS"
    assert normalized["candidate_tokens"] == candidate_tokens


@pytest.mark.parametrize("candidate_tokens", [4_500, 4_800])
def test_compact_trigger_fallback_aborts_before_commit_on_no_progress(tmp_path, monkeypatch, candidate_tokens: int) -> None:
    agent, store, thread, _attrs = _setup_carried_agent(
        tmp_path,
        monkeypatch,
        trigger_percent=50,
        recovery_target_percent=30,
        output_cap=2_000,
    )
    agent.backend = HttpBackend(BackendOptions(
        api_base="https://example.invalid", api_key="fake-key", model_name="test-model",
        context_window_tokens=10_000, max_tokens=2_000,
    ))
    for index in range(3):
        for role in ("user", "assistant"):
            store.messages.append({
                "thread_id": thread.thread_id,
                "role": role,
                "content": f"完整旧轮次 {index}",
                "metadata": {"conversation_request_id": f"prior-{index}"},
            })

    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "摘要正文")
    events: list[dict[str, object]] = []

    def project(view):
        # 压缩前 4500 (< ceiling=5000)，候选落在 (recovery_target=3000, ceiling=5000) 内，进入 trigger_fallback。
        return ConversationCompactProjection(candidate_tokens if view.is_candidate else 4_500, object())

    with pytest.raises(ConversationCompactError) as exc_info:
        prepare_conversation_context(
            agent,
            store,
            thread,
            options=ConversationCompactOptions(
                current_prompt="继续任务",
                force=True,
                progress_callback=events.append,
                request_projector=project,
            ),
        )

    err = exc_info.value
    assert err.code == "COMPACT_NO_PROGRESS"
    assert isinstance(err.capacity, CompactCapacityFacts)
    assert err.capacity.candidate_tokens == candidate_tokens
    assert err.capacity.input_ceiling_tokens == 5_000
    assert err.capacity.candidates_tried >= 1

    current = store.threads.require(thread.thread_id)
    assert current.compact_generation == 0
    assert current.compact_checkpoint_id == ""
    assert current.summary == ""
    assert current.compact_failure_code == "COMPACT_NO_PROGRESS"
    assert committed_compact_checkpoint_chain(agent, current) == ()
    failed_events = [item for item in events if item.get("phase") == "failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["error_code"] == "COMPACT_NO_PROGRESS"


def test_compact_trigger_fallback_helper_guards_exception_path_too(tmp_path, monkeypatch) -> None:
    agent, store, thread, _attrs = _setup_carried_agent(
        tmp_path, monkeypatch, trigger_percent=50, recovery_target_percent=30, output_cap=2_000,
    )
    policy = runtime_compact_policy(agent)
    request = _CompactRunRequest(
        agent=agent,
        store=store,
        thread=thread,
        current_prompt="继续",
        pending=(),
        policy=policy,
        projected_tokens=4_500,
        forced=True,
        attempted_at=time.time(),
        operation_id="op-no-progress",
    )
    candidate = _CompactCandidate(
        summary="新摘要",
        operation_evidence={},
        compact_rows=(),
        retained_tail=(),
        projected_tokens_after=4_500,
        request_projection=ConversationCompactProjection(4_500, object()),
    )
    with pytest.raises(ConversationCompactError) as exc_info:
        _commit_or_reject_trigger_fallback(request, candidate, _RejectedCandidates(request))

    assert exc_info.value.code == "COMPACT_NO_PROGRESS"
    current = store.threads.require(thread.thread_id)
    assert current.compact_generation == 0
    assert current.compact_failure_code == "COMPACT_NO_PROGRESS"


@pytest.mark.parametrize(
    ("before_tokens", "candidate_tokens"),
    [
        pytest.param(2_500, 2_500, id="equal_below_recovery_target"),
        pytest.param(2_000, 2_500, id="inflated_below_recovery_target"),
    ],
)
def test_ordinary_accept_branch_aborts_on_no_progress_below_recovery_target(
    tmp_path,
    monkeypatch,
    before_tokens: int,
    candidate_tokens: int,
) -> None:
    agent, store, thread, _attrs = _setup_carried_agent(
        tmp_path,
        monkeypatch,
        trigger_percent=50,
        recovery_target_percent=30,
        output_cap=2_000,
    )
    agent.backend = HttpBackend(BackendOptions(
        api_base="https://example.invalid", api_key="fake-key", model_name="test-model",
        context_window_tokens=10_000, max_tokens=2_000,
    ))
    for index in range(3):
        for role in ("user", "assistant"):
            store.messages.append({
                "thread_id": thread.thread_id,
                "role": role,
                "content": f"完整旧轮次 {index}",
                "metadata": {"conversation_request_id": f"prior-ordinary-{index}"},
            })

    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "摘要正文")
    events: list[dict[str, object]] = []

    def project(view):
        # candidate <= recovery_target(3000) 时会走普通接受分支；必须同样受 candidate < before 约束。
        return ConversationCompactProjection(candidate_tokens if view.is_candidate else before_tokens, object())

    with pytest.raises(ConversationCompactError) as exc_info:
        prepare_conversation_context(
            agent,
            store,
            thread,
            options=ConversationCompactOptions(
                current_prompt="继续任务",
                force=True,
                progress_callback=events.append,
                request_projector=project,
            ),
        )

    err = exc_info.value
    assert err.code == "COMPACT_NO_PROGRESS"
    assert isinstance(err.capacity, CompactCapacityFacts)
    assert err.capacity.candidate_tokens == candidate_tokens
    assert err.capacity.input_ceiling_tokens == 5_000
    assert err.capacity.candidates_tried >= 1
    assert compact_degradation_decision(err, degrade_eligible=True) == (False, "COMPACT_NO_PROGRESS")

    current = store.threads.require(thread.thread_id)
    assert current.compact_generation == 0
    assert current.compact_checkpoint_id == ""
    assert current.summary == ""
    assert current.compacted_through_message_id == ""
    assert current.compact_consecutive_failures == 1
    assert current.compact_failure_code == "COMPACT_NO_PROGRESS"
    assert committed_compact_checkpoint_chain(agent, current) == ()
    failed_events = [item for item in events if item.get("phase") == "failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["error_code"] == "COMPACT_NO_PROGRESS"


def test_ordinary_accept_branch_commits_when_shrinking_below_recovery_target(tmp_path, monkeypatch) -> None:
    agent, store, thread, _attrs = _setup_carried_agent(
        tmp_path,
        monkeypatch,
        trigger_percent=50,
        recovery_target_percent=30,
        output_cap=2_000,
    )
    agent.backend = HttpBackend(BackendOptions(
        api_base="https://example.invalid", api_key="fake-key", model_name="test-model",
        context_window_tokens=10_000, max_tokens=2_000,
    ))
    for index in range(3):
        for role in ("user", "assistant"):
            store.messages.append({
                "thread_id": thread.thread_id,
                "role": role,
                "content": f"完整旧轮次 {index}",
                "metadata": {"conversation_request_id": f"prior-shrink-{index}"},
            })

    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "精简摘要正文")

    def project(view):
        return ConversationCompactProjection(2_000 if view.is_candidate else 2_800, object())

    result = prepare_conversation_context(
        agent,
        store,
        thread,
        options=ConversationCompactOptions(
            current_prompt="继续任务",
            force=True,
            request_projector=project,
        ),
    )

    assert result.compacted is True
    assert result.projected_tokens == 2_000
    current = store.threads.require(thread.thread_id)
    assert current.compact_generation == 1
    assert current.compact_checkpoint_id != ""
    assert current.summary != ""
    assert current.compacted_through_message_id != ""
    assert current.compact_consecutive_failures == 0
    assert current.compact_failure_code == ""
    assert len(committed_compact_checkpoint_chain(agent, current)) == 1
