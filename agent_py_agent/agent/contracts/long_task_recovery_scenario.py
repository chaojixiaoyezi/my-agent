
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import write_json_file
from .long_task_recovery_contract import validate_long_task_recovery


@dataclass(frozen=True)
class LongTaskRecoveryScenarioReport:
    ok: bool
    report_ref: str
    recovery_ref: str
    validation_error_codes: tuple[str, ...]
    facts: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "report_ref": self.report_ref,
            "recovery_ref": self.recovery_ref,
            "validation_error_codes": list(self.validation_error_codes),
            "facts": dict(self.facts),
        }


def run_long_task_recovery_scenario(workspace: Path) -> LongTaskRecoveryScenarioReport:
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    refs = _materialize_refs(root)
    facts = _facts(refs)
    recovery_ref = _write_artifact_json_ref(root / "long_task_recovery_scenario" / "recovery.json", facts, root)
    validation = validate_long_task_recovery(facts)
    report = LongTaskRecoveryScenarioReport(
        ok=validation.ok,
        report_ref="long_task_recovery_scenario/report.json",
        recovery_ref=recovery_ref,
        validation_error_codes=validation.error_codes,
        facts=facts,
    )
    _write_artifact_json_ref(root / report.report_ref, report.to_dict(), root)
    return report


def _materialize_refs(root: Path) -> dict[str, str]:
    base = root / "long_task_recovery_scenario" / "run-long-1"
    payloads = {
        "run_scope_ref": ("scope.json", {"allowed_write_roots": ["workspace://long-task"]}),
        "checkpoint_ref": ("checkpoint-001.json", {"sequence": 1}),
        "state_ref": ("state-001.json", {"status": "RUNNING"}),
        "artifact_ref": ("output.part.json", {"rows_written": 10}),
        "bundle_ref": ("context-bundle.json", {"schema": "main_context_bundle.v1"}),
        "apply_ref": ("compact-apply.json", {"applied": True}),
        "resume_ref": ("resume-packet.json", {"next": "continue"}),
        "idempotency_state_ref": ("idempotency.json", {"executed_action_ids": []}),
    }
    return {
        key: _write_artifact_json_ref(base / filename, payload, root)
        for key, (filename, payload) in payloads.items()
    }


def _facts(refs: dict[str, str]) -> dict[str, object]:
    return {
        "task_id": "task-long-1",
        "run_id": "run-long-1",
        "run_scope_ref": f"artifact://{refs['run_scope_ref']}",
        "checkpoints": [
            {
                "checkpoint_ref": f"artifact://{refs['checkpoint_ref']}",
                "sequence": 1,
                "state_ref": f"artifact://{refs['state_ref']}",
                "artifact_refs": [f"artifact://{refs['artifact_ref']}"],
                "status": "RUNNING",
            }
        ],
        "compact_cycles": [
            {
                "cycle_id": "compact-1",
                "bundle_ref": f"artifact://{refs['bundle_ref']}",
                "apply_ref": f"artifact://{refs['apply_ref']}",
                "resume_ref": f"artifact://{refs['resume_ref']}",
            }
        ],
        "latest_resume_packet": {
            "packet_ref": f"artifact://{refs['resume_ref']}",
            "restored_state_refs": [f"artifact://{refs['state_ref']}"],
            "next_action_refs": ["action://continue-writing"],
        },
        "side_effect_ledger": {
            "executed_action_ids": [],
            "replayed_action_ids": [],
            "idempotency_state_ref": f"artifact://{refs['idempotency_state_ref']}",
        },
    }


def _write_artifact_json_ref(path: Path, payload: object, root: Path) -> str:
    write_json_file(path, payload)
    return str(path.resolve().relative_to(root.resolve()))


__all__ = ["LongTaskRecoveryScenarioReport", "run_long_task_recovery_scenario"]
