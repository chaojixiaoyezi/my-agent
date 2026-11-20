from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.feishu_agent_panel import _check_session_lock
from agent_py_agent.agent.workflows import WorkflowEngine, conditions
from agent_py_agent.agent.workflows.notification_steps import (
    execute_notification_step,
    notify_unattended_run_failure,
)
from agent_py_agent.tests.test_wf_05_06_notifications_and_locks import _make_run, _make_step
from agent_py_agent.tests.test_workflow_engine import save_node

_SUBJECT = "a" * 4096 + "!"
_CONDITION_CODE = "WORKFLOW_CONDITION_SUBJECT_TRUNCATED"
_NOTIFICATION_CODE = "WORKFLOW_NOTIFICATION_RESULT_UNRECOGNIZED"


def _regex(value):
    return {"op": "regex", "ref": {"step_id": "$inputs", "key": "text"}, "value": value}


def test_truncated_end_anchor_is_indeterminate():
    with pytest.raises(Exception, match=_CONDITION_CODE) as raised:
        conditions.evaluate_condition(_regex("^a+$"), {}, {"text": _SUBJECT})
    assert isinstance(raised.value, conditions.ConditionIndeterminate)
    assert raised.value.code == _CONDITION_CODE
    assert conditions.evaluate_condition(_regex("a+"), {}, {"text": _SUBJECT}) is True


@pytest.mark.parametrize("dag", [False, True], ids=["sequential", "dag"])
def test_engine_does_not_dispatch_indeterminate_condition_or_dependents(tmp_path, monkeypatch, dag):
    dispatched = []
    engine = WorkflowEngine(tmp_path)
    original = engine._execute_step_with_retry_and_watermarks

    def observe(step, spec, outputs, definition, record):
        dispatched.append(step.step_id)
        return original(step, spec, outputs, definition, record)

    monkeypatch.setattr(engine, "_execute_step_with_retry_and_watermarks", observe)
    flow = {
        "descriptor": {"id": "flow", "kind": "workflow", "name": "flow", "owner_id": "alice"},
        "steps": [
            {"step_id": "check", "name": "check", "mode": "script_transform", "config": {"mapping": {}},
             "condition": _regex("^a+$")},
            {"step_id": "dependent", "name": "dependent", "mode": "script_transform", "config": {"mapping": {}},
             **({"depends_on": ["check"]} if dag else {})},
        ],
    }
    result = engine.run_workflow(flow, inputs={"text": _SUBJECT})
    assert dispatched == []
    assert result.status == "failed"
    saved = engine.store.load_run(result.workflow_run_id)
    records = {step.step_id: step for step in saved.steps}
    step = records["check"]
    assert step.status == "failed" and step.error_code == _CONDITION_CODE
    assert step.escalation_result["effect_state"] == "not_started"
    assert step.escalation_result["condition_outcome"] == "unknown"
    assert records["dependent"].status == "pending"

    skipped = engine.run_workflow(flow, inputs={"text": "a!"})
    assert skipped.steps[0].status == "skipped"
    assert skipped.steps[0].error_code == ""
    assert skipped.steps[0].escalation_result["skip_reason"] == "condition_false"


def test_debug_reports_indeterminate_condition(tmp_path):
    save_node(tmp_path, "script_transform", config={"mapping": {}}, condition=_regex("^a+$"))
    result = WorkflowEngine(tmp_path).debug_step("debug-flow", "target", mock_inputs={"text": _SUBJECT})
    assert result["status"] == "failed"
    assert result["condition_branch"] is None
    assert result["error_detail"]["code"] == _CONDITION_CODE
    assert result["error_detail"]["effect_state"] == "not_started"
    assert result["error_detail"]["condition_outcome"] == "unknown"


@pytest.mark.parametrize("result", [None, {"status": "custom"}, {"status": []}, True])
@pytest.mark.parametrize("surface", ["step", "failure_notice"])
def test_unrecognized_sender_receipts_remain_unknown(tmp_path, surface, result):
    calls = []

    def sender(**kwargs):
        calls.append(kwargs)
        return result

    engine = SimpleNamespace(owner_home=tmp_path, notification_sender=sender)
    run, step = _make_run(tmp_path), _make_step()
    if surface == "step":
        def attempt():
            execute_notification_step(
                engine=engine, run=run, workflow=None, step=step,
                spec={"notification_spec": {"channel": "im", "template": "notice"}}, context=None, outputs={},
            )
            return step.output_snapshot
    else:
        run.status = "failed"

        def attempt():
            return notify_unattended_run_failure(engine=engine, run=run)

    receipt = attempt()
    assert receipt["status"] == receipt["effect_outcome"] == "unknown"
    assert receipt["error_code"] == _NOTIFICATION_CODE
    assert receipt["retry_same_request"] is False
    assert "delivered_at" not in receipt and "notified_at" not in receipt
    assert attempt() == receipt and len(calls) == 1
    if surface == "step":
        assert step.status == "failed" and step.error_code == _NOTIFICATION_CODE


@pytest.mark.parametrize("mode", ["missing", "service_error", "decision_error"])
def test_panel_lock_unavailable_denies_action(mode, monkeypatch):
    from agent_py_agent.agent.session_lock import admission

    def unavailable(*args, **kwargs):
        raise OSError("simulated I/O error")

    adapter = SimpleNamespace(_session_lock_enabled=True)
    if mode == "service_error":
        adapter._unlock = SimpleNamespace(status=unavailable)
    if mode == "decision_error":
        adapter._unlock = object()
        monkeypatch.setattr(admission, "check_session_admission", unavailable)
    denial = _check_session_lock(adapter, "actor")
    assert denial is not None
    assert denial["error_code"] == "FEISHU_PANEL_LOCK_UNAVAILABLE"
    assert denial["retryable"] is True
    assert _check_session_lock(SimpleNamespace(_session_lock_enabled=False), "actor") is None


@pytest.mark.parametrize("op,decisive", [("and", False), ("or", True)])
def test_condition_groups_apply_three_valued_logic_in_both_orders(op, decisive):
    known = {"op": "eq", "ref": {"step_id": "$inputs", "key": "flag"}, "value": True}
    for nodes in ([_regex("^a+$"), known], [known, _regex("^a+$")]):
        group = {"op": op, "conditions": nodes}
        assert conditions.evaluate_condition(group, {}, {"text": _SUBJECT, "flag": decisive}) is decisive
        with pytest.raises(conditions.ConditionIndeterminate) as raised:
            conditions.evaluate_condition(group, {}, {"text": _SUBJECT, "flag": not decisive})
        assert raised.value.code == _CONDITION_CODE


def test_condition_budget_is_registered_and_reaches_process_reader(tmp_path, monkeypatch):
    import json
    from dataclasses import fields
    from pathlib import Path

    from agent_py_agent.agent import owner_scoped_pool
    from agent_py_agent.agent.settings.config import AgentConfig, load_config
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry

    key = "workflow_condition_max_subject_chars"
    config_dir = Path(__file__).resolve().parents[1] / "config"
    field = next(field for field in fields(AgentConfig) if field.name == key)
    assert field.default == getattr(load_config(config_dir / "agent_config.yaml"), key) == 4096
    assert field.metadata["bounds"] == (1, None) and field.metadata["unit"] == "字符"
    assert "model_read_only" not in field.metadata
    with pytest.raises(ValueError, match=key):
        AgentConfig(workflow_condition_max_subject_chars=0)
    spec = parameter_registry()[key]
    assert spec.tier == "deployment" and not spec.user_editable
    catalog = json.loads((config_dir / "constants_catalog.json").read_text(encoding="utf-8"))
    entry = next(entry for entry in catalog["constants"] if entry["name"] == "DEFAULT_WORKFLOW_CONDITION_MAX_SUBJECT_CHARS")
    assert entry["value"] == 4096 and entry["unit"] == "字符"

    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(f"{key}: 8192\n", encoding="utf-8")
    config = load_config(config_path)
    assert getattr(config, key) == 8192
    monkeypatch.setattr(conditions, "_MAX_SUBJECT_CHARS", 4096)
    with pytest.raises(conditions.ConditionIndeterminate):
        conditions.evaluate_condition(_regex("^a+$"), {}, {"text": _SUBJECT})
    conditions.configure_from_config(config)
    assert conditions.evaluate_condition(_regex("^a+$"), {}, {"text": _SUBJECT}) is False
    with pytest.raises(conditions.ConditionIndeterminate):
        conditions.evaluate_condition(_regex("^a+$"), {}, {"text": "a" * 8192 + "!"})

    conditions.configure_from_config(AgentConfig())
    monkeypatch.setattr(owner_scoped_pool, "register_resident_owner_capacity", lambda _: None)
    monkeypatch.setattr(owner_scoped_pool.cache_capacity, "configure_from_config", lambda _: None)
    owner_scoped_pool.OwnerScopedAgentPool(config, tmp_path)
    assert conditions.evaluate_condition(_regex("^a+$"), {}, {"text": _SUBJECT}) is False


@pytest.mark.parametrize("mode", ["allowed", "unlock", "set"])
def test_panel_uses_the_existing_dm_decision(mode):
    cards = []
    adapter = SimpleNamespace(
        _unlock=SimpleNamespace(
            status=lambda actor, *, is_group: SimpleNamespace(
                locked=mode == "unlock", requires_password_setup=mode == "set",
            ),
        ),
        _send_password_card=lambda actor, action: cards.append((actor, action)),
    )
    decision = _check_session_lock(adapter, "actor")
    if mode == "allowed":
        assert decision is None and cards == []
    else:
        assert decision["toast"]["type"] == "warning" and cards == [("actor", mode)]


def test_new_error_codes_have_expected_retry_policy():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    for code, retryable in (
        (_CONDITION_CODE, False), (_NOTIFICATION_CODE, False), ("FEISHU_PANEL_LOCK_UNAVAILABLE", True),
    ):
        contract = error_contract(code)
        assert contract.code == code and contract.retryable is retryable


def test_sender_payload_edits_do_not_publish_delivery(tmp_path):
    def sender(**kwargs):
        kwargs["payload"].update(status="delivered", delivered_at="fixture")
        return {"status": "failed"}

    run, step = _make_run(tmp_path), _make_step()
    execute_notification_step(
        engine=SimpleNamespace(owner_home=tmp_path, notification_sender=sender),
        run=run, workflow=None, step=step,
        spec={"notification_spec": {"channel": "im", "template": "notice"}}, context=None, outputs={},
    )
    assert step.status == step.output_snapshot["status"] == "failed"
    assert "delivered_at" not in step.output_snapshot
