
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...contracts.error_taxonomy import error_contract
from .artifact_repair import (
    append_artifact_finding_repair_actions,
    failed_findings,
)
from .builder_repair import (
    BuilderRepairRequest,
    append_failed_builder_output_actions,
)
from .collection_repair import append_collection_value_repair_actions
from .recovery_codes import recovery_error_code
from .recovery_models import RecoveryActionLedger
from .staging_recovery import (
    append_contract_staging_recovery_actions,
    staging_action_context,
)


def _recovery_actions(
    report: dict[str, Any],
    *,
    contract: dict[str, Any],
    workspace_root: Path,
) -> list[dict[str, object]]:
    ledger = RecoveryActionLedger(actions=[], seen=set())
    ledger.actions.extend(_contract_recovery_actions(contract, workspace_root=workspace_root, seen=ledger.seen))
    _append_failure_recovery_actions(report, ledger, contract=contract, workspace_root=workspace_root)
    if ledger.actions:
        return ledger.actions
    return [_generic_recovery_action("ACCEPTANCE_FAILED")]


def _append_failure_recovery_actions(
    report: dict[str, Any],
    ledger: RecoveryActionLedger,
    *,
    contract: dict[str, Any],
    workspace_root: Path,
) -> None:
    append_collection_value_repair_actions(report, contract, ledger)
    append_artifact_finding_repair_actions(report, ledger)
    append_failed_builder_output_actions(
        BuilderRepairRequest(
            report=report,
            contract=contract,
            workspace_root=workspace_root,
            ledger=ledger,
            staging_context=staging_action_context,
        )
    )
    for finding in failed_findings(report):
        recovery_contract = error_contract(recovery_error_code(str(finding.get("code") or "")))
        if recovery_contract.code in ledger.seen:
            continue
        ledger.seen.add(recovery_contract.code)
        ledger.actions.append(_recovery_contract_action(recovery_contract))


def _generic_recovery_action(code: str) -> dict[str, object]:
    return _recovery_contract_action(error_contract(code))


def _recovery_contract_action(contract) -> dict[str, object]:
    return {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
    }


def _contract_recovery_actions(
    contract: dict[str, Any],
    *,
    workspace_root: Path,
    seen: set[str],
) -> list[dict[str, object]]:
    return append_contract_staging_recovery_actions(contract, workspace_root=workspace_root, seen=seen)
