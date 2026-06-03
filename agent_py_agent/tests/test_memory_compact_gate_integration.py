
from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.tests.memory_compact_support import write_compact_fixture


def test_apply_memory_compact_records_compaction_gate(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)

    result = _apply_compact(root)

    gate = result["compaction_gate"]
    assert gate["pre"]["gate"] == "compaction_gate"
    assert gate["pre"]["allowed"] is True
    assert gate["state_snapshot"]["recovery_packet"]
    assert gate["state_snapshot"]["artifact_refs"]


def test_memory_compact_resume_checks_compaction_gate_state_loss(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)

    result = _apply_compact(root)
    work_state_path = Path(result["refs"]["work_state_snapshot"])
    work_state = json.loads(work_state_path.read_text(encoding="utf-8"))
    work_state.pop("restore_refs")
    work_state_path.write_text(json.dumps(work_state, ensure_ascii=False), encoding="utf-8")

    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=result["apply_id"]))
    checks = {item["name"]: item for item in resume["consistency_report"]["checks"]}
    assert checks["compaction_gate_ok"]["ok"] is False
    assert resume["ok"] is False


def _apply_compact(root: Path) -> dict:
    return apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
