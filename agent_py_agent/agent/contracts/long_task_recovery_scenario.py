# LLM: Long-task recovery scenario materializes checkpoint, compact, and resume refs.
# 模块用途: 提供单主代理长任务恢复的小型可验收场景，避免直接靠真实长任务调试。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .long_task_recovery_contract import validate_long_task_recovery


# LLM: LongTaskRecoveryScenarioReport records the deterministic recovery scenario.
# 类用途: 保存恢复场景报告、recovery ref、验证错误码和事实快照。
@dataclass(frozen=True)
class LongTaskRecoveryScenarioReport:
    ok: bool
    report_ref: str
    recovery_ref: str
    validation_error_codes: tuple[str, ...]
    facts: dict[str, object]

    # LLM: to_dict serializes the recovery scenario report.
    # 函数用途: 输出长任务恢复场景结果，保留 refs 和结构化校验结果。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "report_ref": self.report_ref,
            "recovery_ref": self.recovery_ref,
            "validation_error_codes": list(self.validation_error_codes),
            "facts": dict(self.facts),
        }


# LLM: run_long_task_recovery_scenario writes every ref before running the recovery contract.
# 函数用途: 生成 checkpoint、state、artifact、compact/apply/resume 和 idempotency 文件。
def run_long_task_recovery_scenario(workspace: Path) -> LongTaskRecoveryScenarioReport:
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    refs = _materialize_refs(root)
    facts = _facts(refs)
    recovery_ref = _write_json(root / "long_task_recovery_scenario" / "recovery.json", facts, root)
    validation = validate_long_task_recovery(facts)
    report = LongTaskRecoveryScenarioReport(
        ok=validation.ok,
        report_ref="long_task_recovery_scenario/report.json",
        recovery_ref=recovery_ref,
        validation_error_codes=validation.error_codes,
        facts=facts,
    )
    _write_json(root / report.report_ref, report.to_dict(), root)
    return report


# LLM: _materialize_refs writes every recovery input file before validation.
# 函数用途: 生成 RunScope、checkpoint、state、artifact、compact/resume 和幂等账本 refs。
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
        key: _write_json(base / filename, payload, root)
        for key, (filename, payload) in payloads.items()
    }


# LLM: _facts builds the long-task recovery contract payload.
# 函数用途: 从已写入的 refs 构造 validate_long_task_recovery 所需机器事实。
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


# LLM: _write_json persists one structured recovery artifact.
# 函数用途: 写入 JSON 并返回 workspace-relative ref。
def _write_json(path: Path, payload: object, root: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return str(path.resolve().relative_to(root.resolve()))


__all__ = ["LongTaskRecoveryScenarioReport", "run_long_task_recovery_scenario"]
