"""R-03/R-04: a refused scheduled public receipt is a contract error, and its run's TaskLink/TaskRun close instead of staying open."""
from __future__ import annotations

import pytest

from agent_py_agent.agent.contracts.background_public_receipt import BackgroundPublicReceiptError
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.runtime_errors import runtime_error_report
from agent_py_agent.tests.test_background_public_receipt_contract import _body
from agent_py_agent.tests.test_background_public_receipt_integration import _case


@pytest.mark.parametrize("code", ["BACKGROUND_RECEIPT_CONTRACT_INVALID", "BACKGROUND_RECEIPT_PUBLIC_OBLIGATION",
                                  "MODEL_RESPONSE_PROVENANCE_MISSING"])
def test_receipt_refusal_is_reported_as_a_contract_error_not_programmer_bug(code):
    report = runtime_error_report(BackgroundPublicReceiptError(code), context="gateway_background_main.iteration")
    assert report["category"] == "background_receipt_contract" and report["error_code"] == code
    assert report["recoverable"] is False and report["model_message"] == error_contract(code).recovery_hint


def test_refused_scheduled_run_closes_its_task_link_and_task_run(tmp_path):
    case, prompts, channels, scheduler = _case(tmp_path, ["说明\n" + _body("internal_only", "")])
    with pytest.raises(RuntimeError, match="BACKGROUND_RECEIPT_CONTRACT_INVALID"):
        scheduler.tick(now=case.job["next_run_at"])
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]["status"] == "failed" and len(prompts) == 1 and not channels.attempts
    run_id = history[0]["run_id"]
    assert case.store.tasks.load(run_id).status == "failed"
    runs = case.agent.subagents.runtime_db.task_runs_for_task(run_id)
    assert runs and all(row["closed_at"] > 0 and row["status"] == "failed" for row in runs)
    assert scheduler.tick(now=case.job["next_run_at"] + 31) == [] and len(prompts) == 1
