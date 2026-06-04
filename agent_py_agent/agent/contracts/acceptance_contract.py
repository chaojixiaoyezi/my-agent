
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .artifact_acceptance import ArtifactAcceptanceReport
from .contract_validation_recovery import recovery_for_findings
from .state_machine import RunStateFacts, can_closeout


@dataclass(frozen=True)
class AcceptanceContract:
    items: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    latest_tests: list[str] = field(default_factory=list)
    required_artifact_kinds: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AcceptanceInput:
    contract: AcceptanceContract
    artifact_reports: list[ArtifactAcceptanceReport] = field(default_factory=list)
    test_records: list[Any] = field(default_factory=list)
    run_state: RunStateFacts = field(default_factory=lambda: RunStateFacts(status="PLANNING"))


@dataclass(frozen=True)
class AcceptanceResult:
    ok: bool
    status: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    recovery: dict[str, object] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"ok": self.ok, "status": self.status, "findings": list(self.findings)}
        recovery = self.recovery or recovery_for_findings("acceptance_contract", _failed_findings(self.findings))
        if recovery is not None:
            payload["recovery"] = recovery
        return payload


def evaluate_acceptance_contract(request: AcceptanceInput) -> AcceptanceResult:
    findings = [
        _state_finding(request.run_state),
        _artifact_finding(request.contract, request.artifact_reports),
        _test_finding(request.test_records),
        _criteria_finding(request.contract),
    ]
    ok = all(item["ok"] for item in findings if item["severity"] == "hard")
    return AcceptanceResult(
        ok=ok,
        status="accepted" if ok else "rejected",
        findings=findings,
        recovery=recovery_for_findings("acceptance_contract", _failed_findings(findings)),
    )


def _state_finding(run_state: RunStateFacts) -> dict[str, Any]:
    ok = can_closeout(run_state)
    return {
        "code": "ACCEPTANCE_STATE_OK" if ok else "ACCEPTANCE_STATE_INCOMPLETE",
        "ok": ok,
        "severity": "hard",
        "message": "run state is DONE/VERIFIED" if ok else "run state is not DONE/VERIFIED",
    }


def _artifact_finding(contract: AcceptanceContract, reports: list[ArtifactAcceptanceReport]) -> dict[str, Any]:
    passed_kinds = {item.artifact_kind for item in reports if item.ok}
    required = {str(item) for item in contract.required_artifact_kinds if str(item).strip()}
    missing = sorted(required - passed_kinds)
    failed = [item.artifact_ref for item in reports if not item.ok]
    ok = not missing and not failed
    return {
        "code": "ACCEPTANCE_ARTIFACTS_OK" if ok else "ACCEPTANCE_ARTIFACTS_FAILED",
        "ok": ok,
        "severity": "hard",
        "message": "required artifacts passed" if ok else "required artifacts missing or failed",
        "missing_artifact_kinds": missing,
        "failed_artifact_refs": failed,
    }


def _test_finding(records: list[Any]) -> dict[str, Any]:
    failed = [getattr(item, "test_name", "") for item in records if not bool(getattr(item, "passed", False))]
    ok = not failed
    return {
        "code": "ACCEPTANCE_TESTS_OK" if ok else "ACCEPTANCE_TESTS_FAILED",
        "ok": ok,
        "severity": "hard" if failed else "soft",
        "message": "tests passed" if ok else "some tests failed",
        "failed_tests": failed,
    }


def _criteria_finding(contract: AcceptanceContract) -> dict[str, Any]:
    criteria = [str(item) for item in contract.items if str(item).strip()]
    constraints = [str(item) for item in contract.constraints if str(item).strip()]
    latest_tests = [str(item) for item in contract.latest_tests if str(item).strip()]
    required_artifact_kinds = [str(item) for item in contract.required_artifact_kinds if str(item).strip()]
    ok = bool(criteria or constraints or required_artifact_kinds or latest_tests)
    return {
        "code": "ACCEPTANCE_CRITERIA_RECORDED" if ok else "ACCEPTANCE_CRITERIA_MISSING",
        "ok": ok,
        "severity": "soft",
        "message": "acceptance criteria recorded" if ok else "acceptance criteria not recorded",
        "items": criteria,
        "constraints": constraints,
        "latest_tests": latest_tests,
        "required_artifact_kinds": required_artifact_kinds,
    }


def _failed_findings(findings: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(item for item in findings if item.get("ok") is not True and item.get("severity") == "hard")


__all__ = [
    "AcceptanceContract",
    "AcceptanceInput",
    "AcceptanceResult",
    "evaluate_acceptance_contract",
]
