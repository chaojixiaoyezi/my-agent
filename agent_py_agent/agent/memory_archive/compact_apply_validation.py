# LLM: Compact apply validation keeps self-check and ledger writes out of the apply orchestrator.
# 模块用途: 在 compact apply 产物全部写好后，补齐 compaction gate、自检、metadata 和 ledger。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact_apply_io import append_jsonl as _append_jsonl
from .compact_apply_io import write_json as _write_json
from .compact_apply_payloads import compact_apply_refs as _refs
from .compact_apply_payloads import ledger_record as _ledger_record
from .compact_apply_self_check import (
    build_self_check_failure_payload as _self_check_failure_payload,
)
from .compact_apply_self_check import (
    build_self_check_payload as _self_check_payload,
)
from .compact_gate_bridge import evaluate_pre_compaction_state


@dataclass(frozen=True)
class CompactApplyFinalizeRequest:
    """Bundle final compact apply validation and ledger-write inputs."""

    payload: dict[str, Any]
    plan: dict[str, Any]
    paths: dict[str, Path]
    now: str
    restore_refs: dict[str, Any]
    work_state: dict[str, Any]
    apply_bundle: dict[str, Any]


def finalize_apply_payload(request: CompactApplyFinalizeRequest) -> None:
    """Attach validation outputs and write metadata plus ledgers."""

    _attach_compaction_gate(request.payload, request.restore_refs, request.work_state)
    self_check = _attach_self_check(request)
    request.payload["post_compact_self_check"] = self_check
    request.payload["restore_refs"] = request.restore_refs
    request.payload["work_state_snapshot"] = request.work_state
    request.payload["apply_bundle"] = request.apply_bundle
    _write_json(request.paths["self_check_json"], self_check)
    _write_json(request.paths["metadata_json"], request.payload)
    _append_jsonl(request.paths["ledger_jsonl"], _ledger_record(request.payload))
    if request.paths["global_ledger_jsonl"] != request.paths["ledger_jsonl"]:
        _append_jsonl(request.paths["global_ledger_jsonl"], _ledger_record(request.payload))


# LLM: _attach_compaction_gate stores the shared compaction gate decision on apply metadata.
# 函数用途: 把 compact 前状态快照写成机器字段，供 memory-resume 做 post_compact 对比。
def _attach_compaction_gate(
    payload: dict[str, Any], restore_refs: dict[str, Any], work_state: dict[str, Any]
) -> None:
    compaction_gate = evaluate_pre_compaction_state(payload, restore_refs, work_state)
    payload["compaction_gate"] = compaction_gate
    if not compaction_gate["pre"]["allowed"]:
        payload["ok"] = False
        payload["compact_status"] = "blocked_compaction_gate_failed"


# LLM: _attach_self_check finalizes compact apply validation without expanding the entrypoint.
# 函数用途: 执行原有 self-check，失败时写结构化 failure payload 并更新 compact_status。
def _attach_self_check(request: CompactApplyFinalizeRequest) -> dict[str, Any]:
    self_check = _self_check_payload(request.plan, request.paths, request.now, request.work_state)
    if not self_check["ok"]:
        request.payload["ok"] = False
        request.payload["compact_status"] = "blocked_self_check_failed"
        request.payload["self_check_failure"] = _self_check_failure_payload(
            request.payload, self_check, _refs(request.paths), request.now
        )
        _write_json(request.paths["failed_self_check_json"], request.payload["self_check_failure"])
    return self_check
