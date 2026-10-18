# LLM: 使用main正式候选服务和真实原C/P3/claim；假供应商只控制等待，不确认或代写任何Skill。
# 模块用途: 验证学习消费精确FIFO，未知保留原请求，撤权后不能落草稿/终账或删除源。
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.capability.skill_learning import SkillLearningService
from agent_py_agent.agent.capability.skill_learning_proposals import list_learning_proposals
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
from agent_py_agent.agent.gateway_parts.io import (
    gateway_turn_transition,
    read_json_file,
    update_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.tests.test_gateway_background_curator_transactions import (
    curator_case,
    run_original,
)
from agent_py_agent.tests.test_skill_learning import NAME, _ctx, _output, _runtime


# LLM: 显式换回产品Service，旧publisher测试的人工确认子类不参与本片验收。
# 函数用途: 准备隔离正式候选服务，不自动批准任何输出。
def learning_case(tmp_path):
    context = _runtime(tmp_path, _output())
    service = SkillLearningService(context.store, context.service.settings, context.service.runtime)
    assert service.enqueue_from_finalize(_ctx()) == "queued"
    return context, service


def test_expected_fifo_mismatch_does_not_touch_neighbor(tmp_path):
    context, service = learning_case(tmp_path)
    [path] = context.store.pending_requests()
    before = path.read_bytes()
    result = service.run_pending(expected_request_path=path.with_name("another.json"))
    assert result.status == "busy" and len(context.backend.prompts) == 0
    assert path.read_bytes() == before
    assert not context.store.registry_path.exists() and not context.store.ledger_path.exists()
    assert list_learning_proposals(context.store) == []


def test_expected_fifo_creates_only_main_proposal(tmp_path):
    context, service = learning_case(tmp_path)
    [path] = context.store.pending_requests()
    result = service.run_pending(expected_request_path=path)
    assert result.status == "proposed" and len(context.backend.prompts) == 1
    assert len(list_learning_proposals(context.store)) == 1
    assert not context.store.skill_path(NAME).exists()


@pytest.mark.parametrize("stage", ["model", "snapshot"])
@pytest.mark.parametrize("kind", ["outcome", "context", "tool", "bounded"])
def test_actual_unknown_never_rewrites_attempt_or_finishes_learning(tmp_path, monkeypatch, stage, kind):
    context, service = learning_case(tmp_path)
    [path] = context.store.pending_requests()
    error = {
        "outcome": RequestExecutionOutcomeUncertainError(
            {"id": "request-a", "execution_attempt_id": "attempt-a", "lease_epoch": 1, "lease_owner": "attempt-a"}, "learning"),
        "context": RequestExecutionContextUnavailable("original source missing"),
        "tool": ToolWriteContextUnavailable("original claim missing"),
        "bounded": BoundedCallExitUnconfirmedError("call-a", "worker_exit_unconfirmed"),
    }[kind]

    def refuse(*_):
        raise error

    if stage == "model":
        monkeypatch.setattr(service, "_call_model", refuse)
    else:
        from dataclasses import replace
        service.runtime = replace(service.runtime, snapshot_provider=refuse)
    before = path.read_bytes()
    with pytest.raises(type(error)) as raised:
        service.run_pending()
    assert raised.value is error and path.read_bytes() == before
    assert not context.store.ledger_path.exists()
    assert list_learning_proposals(context.store) == []
    assert not context.store.skill_path(NAME).exists()


@pytest.mark.parametrize("fault", ["cancel", "generation", "none"])
def test_actual_c_learning_wait_uses_final_original_write_gate(tmp_path, monkeypatch, fault):
    with curator_case(tmp_path, monkeypatch, source_kind="learning") as (agent, service, backend, gate, claim, close):
        source = Path(agent.home_paths.owner_home_dir / read_json_file(claim.path)["metadata"]["background_source"]["source_ref"])
        p3 = reservation_file(gate.paths, claim.request_id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            try:
                assert backend.started.wait(5), "original learning model must really enter wait"
                before = source.read_bytes()
                registry = service.store.registry_path.read_bytes()
                with gateway_turn_transition(gate.paths, claim.request_id):
                    assert read_json_file(claim.path)["status"] == "processing"
                    if fault == "cancel":
                        update_json_file_atomic(claim.path, lambda row: {**row, "cancel_requested": True, "turn_phase": "closing"})
                if fault == "generation":
                    close()
            finally:
                backend.release.set()
            if fault == "none":
                response = future.result(5)
                assert response["ok"] and response["background_result"]["status"] == "proposed"
                assert len(list_learning_proposals(service.store)) == 1
            else:
                with pytest.raises(RequestExecutionOutcomeUncertainError) as raised:
                    future.result(5)
                assert raised.value.request_id == claim.request_id
                assert source.read_bytes() == before and service.store.registry_path.read_bytes() == registry
                assert list_learning_proposals(service.store) == []
                assert not service.store.ledger_path.exists()
                assert p3.exists() and not (gate.paths.terminal / claim.path.name).exists()
        assert backend.calls == 1 and not service.store.skill_path(NAME).exists()


@pytest.mark.parametrize("stage", ["model", "snapshot"])
def test_ordinary_same_code_keeps_original_learning_policy(tmp_path, monkeypatch, stage):
    context, service = learning_case(tmp_path)
    error = RuntimeError("ordinary model failure")
    error.error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"

    def refuse(*_):
        raise error

    if stage == "model":
        monkeypatch.setattr(service, "_call_model", refuse)
    else:
        from dataclasses import replace
        service.runtime = replace(service.runtime, snapshot_provider=refuse)
    result = service.run_pending()
    assert result.status == ("retry" if stage == "model" else "proposed")


@pytest.mark.parametrize("damage", ["none", "schema", "key"])
def test_source_validation_uses_main_request_schema(tmp_path, damage):
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
        _validate_learning_payload,
    )
    context, _ = learning_case(tmp_path)
    [path] = context.store.pending_requests()
    value = context.store.read_request(path)
    assert value is not None and "status" not in value
    ref = {"source_id": value["request_key"]}
    if damage == "schema":
        value["schema_version"] = "other.v1"
    elif damage == "key":
        value["request_key"] = "another-request"
    if damage == "none":
        _validate_learning_payload(ref, value)
    else:
        with pytest.raises(ValueError):
            _validate_learning_payload(ref, value)
