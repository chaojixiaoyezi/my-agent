"""WF-03 Step Retry/Backoff & WF-10 Durable Step Watermarks / Checkpoints 单元与集成测试。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor, AssetRefs, IOContract
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import (
    StepExecutionRecord,
    WorkflowRunRecord,
    payload,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.agent.workflows.script_runner import _environment
from agent_py_agent.agent.workflows.step_retry import run_step_with_retry
from agent_py_agent.agent.workflows.step_watermarks import (
    commit_verified_watermark,
    load_workflow_step_watermark,
    reconcile_step_watermark,
    save_workflow_step_watermark,
)


def _make_step(
    step_id: str = "s1",
    mode: str = "deterministic_script",
    status: str = "running",
    **kwargs,
) -> StepExecutionRecord:
    defaults = {
        "step_id": step_id,
        "name": f"Step {step_id}",
        "mode": mode,
        "status": status,
        "started_at": time.time(),
        "ended_at": 0.0,
        "duration_ms": 0.0,
        "llm_involved": False,
        "escalation_result": {},
        "input_snapshot": {},
        "output_snapshot": {},
    }
    defaults.update(kwargs)
    return StepExecutionRecord(**defaults)


def _make_run(
    run_id: str = "run_1",
    workflow_id: str = "wf_1",
    owner_home: str = "/tmp",
    steps: tuple[StepExecutionRecord, ...] = (),
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=run_id,
        workflow_id=workflow_id,
        owner_id="owner_test",
        trigger_kind="manual",
        status="running",
        started_at=time.time(),
        ended_at=0.0,
        duration_ms=0.0,
        steps=steps,
    )


def _descriptor(workflow_id: str, name: str = "测试工作流") -> AssetDescriptor:
    return AssetDescriptor(
        id=workflow_id,
        kind="workflow",
        name=name,
        summary="测试工作流摘要",
        version="1.0.0",
        owner_id="owner_test",
        access_targets=(),
        data_sources=(),
        credential_refs=(),
        io_contract=IOContract(),
        refs=AssetRefs(),
    )


# ============================================================================
# WF-03: Step Retry & Backoff Tests
# ============================================================================

def test_retry_no_policy_executes_once():
    """无 retry_policy 时默认仅执行 1 次，不进行重试。"""
    step = _make_step()
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        step.status = "failed"
        step.error_code = "SOME_ERROR"

    run_step_with_retry(
        engine=None,
        step=step,
        spec={"step_id": "s1"},
        outputs={},
        definition={},
        run=None,
        execute_fn=execute,
    )

    assert call_count == 1
    assert step.attempt_count == 1
    assert step.max_attempts == 1
    assert step.status == "failed"


def test_retry_success_first_attempt():
    """首次尝试即成功时，不触发重试且正确标记 attempt。"""
    step = _make_step()
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        step.status = "succeeded"
        step.output_snapshot = {"result": "ok"}

    run_step_with_retry(
        engine=None,
        step=step,
        spec={"step_id": "s1", "retry_policy": {"max_attempts": 3, "delay_seconds": 1.0}},
        outputs={},
        definition={},
        run=None,
        execute_fn=execute,
    )

    assert call_count == 1
    assert step.attempt_count == 1
    assert step.max_attempts == 3
    assert step.status == "succeeded"
    assert "retry_history" not in step.escalation_result


def test_retry_transient_failure_eventual_success(tmp_path):
    """瞬态错误触发重试并在第 2 次成功，记录重试历史与退避。"""
    owner_home = str(tmp_path)
    store = WorkflowRunStore(owner_home)
    step = _make_step()
    run = _make_run(run_id="run_retry_ok", workflow_id="wf_test", owner_home=owner_home, steps=(step,))
    store.save_run(run)
    store.save_step_result(run.workflow_run_id, step)

    engine = SimpleNamespace(store=store, owner_home=owner_home)
    sleeps = []
    attempt_ids = []
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        attempt_ids.append(step.escalation_result.get("attempt_id"))
        if call_count == 1:
            step.status = "failed"
            step.error_code = "HTTP_503_SERVICE_UNAVAILABLE"
            step.error_message = "backend busy"
        else:
            step.status = "succeeded"
            step.output_snapshot = {"data": "done"}

    policy = {"max_attempts": 3, "delay_seconds": 2.0, "backoff_factor": 1.5}
    run_step_with_retry(
        engine=engine,
        step=step,
        spec={"step_id": "s1", "retry_policy": policy},
        outputs={},
        definition={},
        run=run,
        execute_fn=execute,
        sleep_fn=lambda s: sleeps.append(s),
    )

    assert call_count == 2
    assert step.attempt_count == 2
    assert step.max_attempts == 3
    assert step.status == "succeeded"
    assert sleeps == [2.0]
    assert len(attempt_ids) == 2
    assert attempt_ids[0] != attempt_ids[1]  # 每次尝试使用不同 attempt_id

    # 验证 retry_history 记录
    history = step.escalation_result.get("retry_history", [])
    assert len(history) == 1
    assert history[0]["attempt"] == 1
    assert history[0]["error_code"] == "HTTP_503_SERVICE_UNAVAILABLE"
    assert history[0]["next_delay_seconds"] == 2.0


def test_retry_exponential_backoff_exhaustion():
    """多次失败后耗尽重试次数，按指数退避递增等待并保留失败终态。"""
    step = _make_step()
    sleeps = []
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        step.status = "failed"
        step.error_code = "NETWORK_TIMEOUT"
        step.error_message = f"timeout {call_count}"

    policy = {"max_attempts": 4, "delay_seconds": 1.0, "backoff_factor": 2.0}
    run_step_with_retry(
        engine=None,
        step=step,
        spec={"step_id": "s1", "retry_policy": policy},
        outputs={},
        definition={},
        run=None,
        execute_fn=execute,
        sleep_fn=lambda s: sleeps.append(s),
    )

    assert call_count == 4
    assert step.attempt_count == 4
    assert step.max_attempts == 4
    assert step.status == "failed"
    # 等待时间: 1.0 * (2^0) = 1.0; 1.0 * (2^1) = 2.0; 1.0 * (2^2) = 4.0
    assert sleeps == [1.0, 2.0, 4.0]
    assert len(step.escalation_result["retry_history"]) == 3


def test_retry_non_retryable_exceptions_not_retried():
    """KeyboardInterrupt, SystemExit, ToolCancelled, InterruptedError 严禁重试。"""
    step = _make_step()
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        raise ToolCancelled("cancelled by user")

    with pytest.raises(ToolCancelled):
        run_step_with_retry(
            engine=None,
            step=step,
            spec={"step_id": "s1", "retry_policy": {"max_attempts": 3}},
            outputs={},
            definition={},
            run=None,
            execute_fn=execute,
        )

    assert call_count == 1
    assert step.attempt_count == 1


def test_retry_non_retryable_statuses_not_retried():
    """waiting_for_agent, paused_for_resume, skipped 不得触发重试。"""
    for terminal_status in ("waiting_for_agent", "paused_for_resume", "skipped"):
        step = _make_step()
        call_count = 0

        def execute():
            nonlocal call_count
            call_count += 1
            step.status = terminal_status

        run_step_with_retry(
            engine=None,
            step=step,
            spec={"step_id": "s1", "retry_policy": {"max_attempts": 3}},
            outputs={},
            definition={},
            run=None,
            execute_fn=execute,
        )

        assert call_count == 1
        assert step.status == terminal_status


def test_retryable_codes_filter():
    """显式指定 retryable_codes 时，非清单内错误码阻断重试。"""
    step = _make_step()
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        step.status = "failed"
        step.error_code = "SYNTAX_ERROR"

    policy = {"max_attempts": 3, "retryable_codes": ["TIMEOUT", "NETWORK_ERROR"]}
    run_step_with_retry(
        engine=None,
        step=step,
        spec={"step_id": "s1", "retry_policy": policy},
        outputs={},
        definition={},
        run=None,
        execute_fn=execute,
    )

    assert call_count == 1
    assert step.status == "failed"
    assert step.escalation_result.get("retry_suppressed_reason") == "ERROR_CODE_NOT_RETRYABLE"


def test_non_transient_error_suppressed_when_no_codes_specified():
    """未指定 retryable_codes 时，默认仅瞬态错误可重试，非瞬态错误被抑制。"""
    step = _make_step()
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        step.status = "failed"
        step.error_code = "PERMISSION_DENIED"

    policy = {"max_attempts": 3}
    run_step_with_retry(
        engine=None,
        step=step,
        spec={"step_id": "s1", "retry_policy": policy},
        outputs={},
        definition={},
        run=None,
        execute_fn=execute,
    )

    assert call_count == 1
    assert step.status == "failed"
    assert step.escalation_result.get("retry_suppressed_reason") == "NON_TRANSIENT_ERROR"


def test_side_effecting_uncertain_write_suppressed_without_idempotent():
    """副作用模式在写已启动且 outcome != not_started 时，若非幂等必须抑制重试 (WF-03 核心防线)。"""
    for mode in ("tool_call", "notification", "subworkflow", "http_request", "agent_task"):
        step = _make_step(mode=mode)
        step.escalation_result = {"effect_started": True, "effect_outcome": "running"}
        call_count = 0

        def execute():
            nonlocal call_count
            call_count += 1
            step.status = "failed"
            step.error_code = "TIMEOUT"

        policy = {"max_attempts": 3, "idempotent": False}
        run_step_with_retry(
            engine=None,
            step=step,
            spec={"step_id": "s1", "retry_policy": policy},
            outputs={},
            definition={},
            run=None,
            execute_fn=execute,
        )

        assert call_count == 1
        assert step.status == "failed"
        assert step.escalation_result.get("retry_suppressed_reason") == "UNCERTAIN_WRITE_NOT_IDEMPOTENT"


def test_side_effecting_uncertain_write_allowed_when_idempotent():
    """副作用模式若显式声明 idempotent=True，允许重试。"""
    step = _make_step(mode="tool_call")
    step.escalation_result = {"effect_started": True, "effect_outcome": "unknown"}
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            step.status = "failed"
            step.error_code = "TIMEOUT"
        else:
            step.status = "succeeded"

    policy = {"max_attempts": 3, "idempotent": True, "delay_seconds": 0.0}
    run_step_with_retry(
        engine=None,
        step=step,
        spec={"step_id": "s1", "retry_policy": policy},
        outputs={},
        definition={},
        run=None,
        execute_fn=execute,
        sleep_fn=lambda s: None,
    )

    assert call_count == 2
    assert step.status == "succeeded"


def test_side_effecting_allowed_when_effect_not_started():
    """副作用模式在写入明确未开始 (effect_outcome == 'not_started') 时，即使非幂等亦可重试。"""
    step = _make_step(mode="tool_call")
    step.escalation_result = {"effect_started": True, "effect_outcome": "not_started"}
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            step.status = "failed"
            step.error_code = "CONNECTION_REFUSED"
        else:
            step.status = "succeeded"

    policy = {"max_attempts": 3, "idempotent": False, "delay_seconds": 0.0}
    run_step_with_retry(
        engine=None,
        step=step,
        spec={"step_id": "s1", "retry_policy": policy},
        outputs={},
        definition={},
        run=None,
        execute_fn=execute,
        sleep_fn=lambda s: None,
    )

    assert call_count == 2
    assert step.status == "succeeded"


# ============================================================================
# WF-10: Durable Step Watermarks & Checkpoints Tests
# ============================================================================

def test_watermark_save_and_load_roundtrip(tmp_path):
    """跨运行持久化水位保存与加载往返，单调递增 revision。"""
    owner_home = tmp_path
    workflow_id = "wf_sync"
    step_id = "fetch_data"

    # 初始加载不存在
    assert load_workflow_step_watermark(owner_home, workflow_id, step_id) is None

    # 保存第 1 版
    cp1 = save_workflow_step_watermark(
        owner_home,
        workflow_id,
        step_id,
        {"cursor": 100, "page": 1},
        run_id="run_001",
        step_attempt_id="att_01",
        effect_id="eff_01",
    )
    assert cp1["schema_version"] == "workflow-step-watermark.v1"
    assert cp1["revision"] == 1
    assert cp1["watermark"] == {"cursor": 100, "page": 1}
    assert cp1["last_run_id"] == "run_001"
    assert cp1["effect_id"] == "eff_01"

    # 读取验证
    loaded1 = load_workflow_step_watermark(owner_home, workflow_id, step_id)
    assert loaded1 == cp1

    # 保存第 2 版，revision 单调递增为 2
    cp2 = save_workflow_step_watermark(
        owner_home,
        workflow_id,
        step_id,
        {"cursor": 250, "page": 2},
        run_id="run_002",
        step_attempt_id="att_02",
        effect_id="eff_02",
    )
    assert cp2["revision"] == 2
    assert cp2["watermark"]["cursor"] == 250

    loaded2 = load_workflow_step_watermark(owner_home, workflow_id, step_id)
    assert loaded2 == cp2


def test_watermark_commit_only_on_success(tmp_path):
    """水位仅在步骤状态为 succeeded 或 escalated_recovered 时提交，失败/暂停等不得推进。"""
    owner_home = str(tmp_path)
    engine = SimpleNamespace(owner_home=owner_home, store=None)
    run = _make_run(run_id="r1", workflow_id="wf_1", owner_home=owner_home)

    # 失败状态 -> 不提交
    failed_step = _make_step(status="failed", watermark={"cursor": 1})
    res_failed = commit_verified_watermark(
        engine=engine, run=run, step=failed_step, spec={"step_id": "s1"}
    )
    assert res_failed is None
    assert load_workflow_step_watermark(owner_home, "wf_1", "s1") is None

    # 暂停状态 -> 不提交
    paused_step = _make_step(status="paused_for_resume", watermark={"cursor": 1})
    res_paused = commit_verified_watermark(
        engine=engine, run=run, step=paused_step, spec={"step_id": "s1"}
    )
    assert res_paused is None
    assert load_workflow_step_watermark(owner_home, "wf_1", "s1") is None

    # 成功状态 -> 正常提交
    ok_step = _make_step(status="succeeded", watermark={"cursor": 42})
    res_ok = commit_verified_watermark(
        engine=engine, run=run, step=ok_step, spec={"step_id": "s1"}
    )
    assert res_ok is not None
    assert res_ok["watermark"] == {"cursor": 42}
    assert load_workflow_step_watermark(owner_home, "wf_1", "s1")["watermark"] == {"cursor": 42}


def test_watermark_candidate_extraction_sources(tmp_path):
    """测试三种候选水位来源的正确提取：1. step.watermark 2. output_snapshot['watermark'] 3. watermark_key。"""
    owner_home = str(tmp_path)
    engine = SimpleNamespace(owner_home=owner_home, store=None)
    run = _make_run(run_id="r1", workflow_id="wf_extract", owner_home=owner_home)

    # 1. 显式 step.watermark
    s1 = _make_step(step_id="step1", status="succeeded", watermark={"from": "step_wm"})
    res1 = commit_verified_watermark(engine=engine, run=run, step=s1, spec={"step_id": "step1"})
    assert res1["watermark"] == {"from": "step_wm"}

    # 2. output_snapshot['watermark']
    s2 = _make_step(step_id="step2", status="succeeded", output_snapshot={"watermark": {"from": "output_dict"}})
    res2 = commit_verified_watermark(engine=engine, run=run, step=s2, spec={"step_id": "step2"})
    assert res2["watermark"] == {"from": "output_dict"}

    # 3. watermark_key 配置提取
    s3 = _make_step(step_id="step3", status="succeeded", output_snapshot={"last_ts": "2026-11-10T12:00:00Z"})
    spec3 = {"step_id": "step3", "config": {"watermark_key": "last_ts"}}
    res3 = commit_verified_watermark(engine=engine, run=run, step=s3, spec=spec3)
    assert res3["watermark"] == {"cursor": "2026-11-10T12:00:00Z", "key": "last_ts"}


def test_timeout_after_write_reconciliation(tmp_path):
    """WF-03 + WF-10 联合场景：写后网络超时时通过已持久化水位直接对账成功，免于重复写。"""
    owner_home = str(tmp_path)
    workflow_id = "wf_reconcile"
    step_id = "post_payment"
    effect_id = "order_tx_998877"

    # 先持久化一次已完成的水位（模拟此前的尝试实际上已在后端写入并提交了水位，但返回客户端时发生超时）
    save_workflow_step_watermark(
        owner_home,
        workflow_id,
        step_id,
        {"output_snapshot": {"status": "charge_confirmed", "charge_id": "ch_123"}, "effect_id": effect_id},
        effect_id=effect_id,
    )

    # 验证独立 reconcile_step_watermark
    reconciled = reconcile_step_watermark(owner_home, workflow_id, step_id, effect_id)
    assert reconciled is not None
    assert reconciled["effect_id"] == effect_id

    # 模拟重试 runner 执行：本步虽然报超时失败，但由于通过 effect_id 对账命中，直接恢复为成功！
    step = _make_step(step_id=step_id, mode="tool_call")
    step.effect_id = effect_id
    run = _make_run(run_id="run_reconcile", workflow_id=workflow_id, owner_home=owner_home)
    engine = SimpleNamespace(owner_home=owner_home, store=None)

    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        step.status = "failed"
        step.error_code = "GATEWAY_TIMEOUT"
        step.error_message = "network timed out after write"

    policy = {"max_attempts": 3, "idempotent": False}
    run_step_with_retry(
        engine=engine,
        step=step,
        spec={"step_id": step_id, "retry_policy": policy},
        outputs={},
        definition={},
        run=run,
        execute_fn=execute,
    )

    assert call_count == 1  # 无须重复调用 execute_fn！
    assert step.status == "succeeded"
    assert step.escalation_result.get("reconciled_from_watermark") is True
    assert step.output_snapshot == {"status": "charge_confirmed", "charge_id": "ch_123"}


def test_script_runner_watermark_env_injection(tmp_path):
    """script_runner._environment 在存在 watermark 时正确注入 MY_AGENT_SCRIPT_WATERMARK_JSON。"""
    folder = tmp_path / "sandbox"
    folder.mkdir()
    metadata = {"metadata": {"script_input_protocol": "script-input.v1"}}

    # 无 watermark 时环境变量不包含
    env_clean = _environment(metadata, folder, "{}", sources={})
    assert "MY_AGENT_SCRIPT_WATERMARK_JSON" not in env_clean

    # 在 work_context 中传递 watermark
    env_wm = _environment(
        metadata,
        folder,
        "{}",
        sources={},
        work_context={"watermark": {"cursor": "row_100", "table": "users"}},
    )
    assert "MY_AGENT_SCRIPT_WATERMARK_JSON" in env_wm
    assert json.loads(env_wm["MY_AGENT_SCRIPT_WATERMARK_JSON"]) == {"cursor": "row_100", "table": "users"}

    # 在 input_json 中传递 watermark
    input_with_wm = json.dumps({"watermark": {"offset": 500}})
    env_in_wm = _environment(metadata, folder, input_with_wm, sources={})
    assert "MY_AGENT_SCRIPT_WATERMARK_JSON" in env_in_wm
    assert json.loads(env_in_wm["MY_AGENT_SCRIPT_WATERMARK_JSON"]) == {"offset": 500}


# ============================================================================
# Engine Full Integration Tests
# ============================================================================

def test_engine_full_integration_retry_and_watermarks(tmp_path):
    """端到端集成测试：WorkflowEngine 使用真实的 step_retry 与 step_watermarks。"""
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)

    step_executions = []

    def mock_tool_step(engine, run, workflow, step, spec, context, outputs):
        step_executions.append(step.step_id)
        if len(step_executions) == 1:
            step.status = "failed"
            step.error_code = "HTTP_504_GATEWAY_TIMEOUT"
            step.error_message = "transient upstream timeout"
        else:
            step.status = "succeeded"
            step.output_snapshot = {"processed_rows": 88, "last_id": "id_9999"}

    engine.tool_step_executor = mock_tool_step

    descriptor = _descriptor("wf_e2e_retry_wm", name="端到端重试与水位工作流")
    step_spec = WorkflowStepSpec(
        step_id="fetch_sync",
        name="同步拉取",
        mode="tool_call",
        retry_policy={"max_attempts": 3, "delay_seconds": 0.0, "idempotent": True},
        config={"watermark_key": "last_id"},
    )
    wf = WorkflowDefinition(descriptor, (step_spec,))
    WorkflowStore(owner_home).save_workflow(wf)

    # 运行工作流
    result = engine.run_workflow(wf)

    assert result.status == "succeeded"
    assert len(step_executions) == 2  # 第 1 次失败后重试，第 2 次成功
    executed_step = result.steps[0]
    assert executed_step.attempt_count == 2
    assert executed_step.status == "succeeded"
    assert executed_step.watermark == {"cursor": "id_9999", "key": "last_id"}

    # 验证跨运行持久化水位检查点已被写入磁盘
    persisted_wm = load_workflow_step_watermark(owner_home, "wf_e2e_retry_wm", "fetch_sync")
    assert persisted_wm is not None
    assert persisted_wm["watermark"] == {"cursor": "id_9999", "key": "last_id"}
    assert persisted_wm["revision"] == 1
    assert persisted_wm["last_run_id"] == result.workflow_run_id


def test_retry_cancellation_during_sleep():
    """在重试等待期间若收到取消信号，应立即抛出 ToolCancelled 中断。"""
    step = _make_step()
    token = CancellationToken()
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        step.status = "failed"
        step.error_code = "TIMEOUT"

    def cancel_sleep(seconds):
        token.cancel("cancelled during backoff")
        token.raise_if_cancelled()

    with bind_cancellation_token(token):
        with pytest.raises(ToolCancelled):
            run_step_with_retry(
                engine=None,
                step=step,
                spec={"step_id": "s1", "retry_policy": {"max_attempts": 3, "delay_seconds": 1.0}},
                outputs={},
                definition={},
                run=None,
                execute_fn=execute,
                sleep_fn=cancel_sleep,
            )

    assert call_count == 1


def test_retry_max_attempts_capped_by_engine():
    """重试次数应受 engine.max_step_retry_attempts 配置上限保护。"""
    step = _make_step()
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        step.status = "failed"
        step.error_code = "TIMEOUT"

    engine = SimpleNamespace(max_step_retry_attempts=2)
    # 策略请求 10 次，但引擎上限为 2
    policy = {"max_attempts": 10, "delay_seconds": 0.0}
    run_step_with_retry(
        engine=engine,
        step=step,
        spec={"step_id": "s1", "retry_policy": policy},
        outputs={},
        definition={},
        run=None,
        execute_fn=execute,
        sleep_fn=lambda s: None,
    )

    assert call_count == 2
    assert step.attempt_count == 2
    assert step.max_attempts == 2


def test_watermark_path_traversal_rejection(tmp_path):
    """水位读写对非法路径段与遍历攻击进行 safe_asset_id 严格防御。"""
    with pytest.raises(ValueError):
        save_workflow_step_watermark(tmp_path, "../malicious_wf", "step1", {"a": 1})

    with pytest.raises(ValueError):
        save_workflow_step_watermark(tmp_path, "wf1", "../traversal_step", {"a": 1})

    with pytest.raises(ValueError):
        load_workflow_step_watermark(tmp_path, "wf/slash", "step1")

    with pytest.raises(ValueError):
        load_workflow_step_watermark(tmp_path, "wf1", "step..dot")


def test_watermark_escalated_recovered_status_commits(tmp_path):
    """escalated_recovered 状态同样属于有效交付状态，正常提交水位。"""
    owner_home = str(tmp_path)
    engine = SimpleNamespace(owner_home=owner_home, store=None)
    run = _make_run(run_id="r_esc", workflow_id="wf_esc", owner_home=owner_home)

    step = _make_step(status="escalated_recovered", watermark={"state": "recovered_at_checkpoint"})
    res = commit_verified_watermark(engine=engine, run=run, step=step, spec={"step_id": "esc_step"})
    assert res is not None
    assert res["watermark"] == {"state": "recovered_at_checkpoint"}


def test_uncertain_write_suppressed_via_tool_receipt():
    """在 tool_receipt 报告非 not_started 副作用状态时，非幂等步骤严禁重试。"""
    step = _make_step(mode="tool_call")
    step.escalation_result = {
        "effect_started": True,
        "tool_receipt": {"effect_outcome": "pending_confirmation"},
    }
    call_count = 0

    def execute():
        nonlocal call_count
        call_count += 1
        step.status = "failed"
        step.error_code = "TIMEOUT"

    policy = {"max_attempts": 3, "idempotent": False}
    run_step_with_retry(
        engine=None,
        step=step,
        spec={"step_id": "s1", "retry_policy": policy},
        outputs={},
        definition={},
        run=None,
        execute_fn=execute,
    )

    assert call_count == 1
    assert step.status == "failed"
    assert step.escalation_result.get("retry_suppressed_reason") == "UNCERTAIN_WRITE_NOT_IDEMPOTENT"
