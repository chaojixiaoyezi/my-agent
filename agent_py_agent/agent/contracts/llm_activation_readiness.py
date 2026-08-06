"""LLM 激活就绪度合同。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..agent_core.model.call_monitor import (
    FirstTokenTimeoutContext,
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
)
from ..common.json_io import write_json_file
from .model_call_ledger import (
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallStartedParams,
)
from .offline_model_adapter_contract import validate_model_adapter_facts
from .offline_prompt_context_contract import validate_prompt_context
from .offline_side_effect_contract import validate_side_effects
from .pre_real_task_validation import PreRealTaskValidationReport
from .run_trace_contract import validate_run_trace_events
from .small_real_acceptance_gate import validate_small_real_acceptance_gate


# ---- 数据模型（原 llm_activation_readiness_models.py 并入）----
@dataclass(frozen=True)
class LLMActivationReadinessRequest:
    workspace: Path
    pre_real_report: PreRealTaskValidationReport | None = None


@dataclass(frozen=True)
class LLMActivationReadinessPhase:
    phase_id: str
    status: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "phase_id": self.phase_id,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class LLMActivationReadinessReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    phases: tuple[LLMActivationReadinessPhase, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "phases": [phase.to_dict() for phase in self.phases],
        }


def readiness_phase(
    phase_id: str,
    ok: bool,
    evidence_refs: list[str],
    issues: list[str],
) -> LLMActivationReadinessPhase:
    return LLMActivationReadinessPhase(
        phase_id=phase_id,
        status="PASSED" if ok else "FAILED",
        evidence_refs=evidence_refs,
        issues=issues,
    )


def readiness_summary(phases: tuple[LLMActivationReadinessPhase, ...]) -> dict[str, int]:
    return {
        "failed": sum(phase.status == "FAILED" for phase in phases),
        "passed": sum(phase.status == "PASSED" for phase in phases),
        "total": len(phases),
    }


__all__ = [
    "LLMActivationReadinessPhase",
    "LLMActivationReadinessReport",
    "LLMActivationReadinessRequest",
    "readiness_phase",
    "readiness_summary",
]


# ---- 超时预算（原 llm_activation_timeout_budget.py 并入）----
@dataclass(frozen=True)
class ProbeSample:
    call_id: str
    input_tokens: int
    first_token_latency_seconds: float


@dataclass
class _FakeClock:
    now_seconds: float = 0.0

    def now(self) -> float:
        return self.now_seconds

    def advance(self, seconds: float) -> None:
        self.now_seconds += float(seconds)


def build_model_timeout_budget(workspace: Path) -> dict[str, object]:
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    clock = _FakeClock()
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=clock.now))
    _record_probe(ledger, clock, ProbeSample("probe-5k", 5000, 13.0))
    _record_probe(ledger, clock, ProbeSample("probe-10k", 10000, 23.0))
    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=15000,
            ledger=ledger,
            options=FirstTokenTimeoutOptions(
                safety_margin=1.5,
                min_timeout_seconds=1.0,
                max_timeout_seconds=180.0,
            ),
            context=FirstTokenTimeoutContext(required_probe_tokens=(5000, 10000)),
        )
    )
    ledger_ref = "model_call_ledger.json"
    payload = {
        "ledger_ref": ledger_ref,
        "estimate": estimate.to_dict(),
        "probe_tokens": [5000, 10000],
        "target_input_tokens": 15000,
    }
    write_json_file(root / ledger_ref, [record.to_dict() for record in ledger.records()])
    write_json_file(root / "timeout_budget.json", payload)
    return payload


def timeout_budget_issues(budget: dict[str, object]) -> list[str]:
    estimate = budget.get("estimate") if isinstance(budget.get("estimate"), dict) else {}
    issues: list[str] = []
    if estimate.get("source") != "probe_5k_10k":
        issues.append("MODEL_TIMEOUT_PROBE_SOURCE_MISSING")
    if not isinstance(estimate.get("timeout_seconds"), (int, float)) or float(estimate["timeout_seconds"]) <= 0:
        issues.append("MODEL_TIMEOUT_SECONDS_INVALID")
    if estimate.get("cache_suspected") is not False:
        issues.append("MODEL_TIMEOUT_CACHE_SAMPLE_USED")
    if not str(budget.get("ledger_ref") or ""):
        issues.append("MODEL_TIMEOUT_LEDGER_REF_MISSING")
    return issues


def _record_probe(
    ledger: ModelCallLedger,
    clock: _FakeClock,
    sample: ProbeSample,
) -> None:
    ledger.started(
        ModelCallStartedParams(
            call_id=sample.call_id,
            backend="test-backend",
            model="test-model",
            input_tokens=sample.input_tokens,
            output_tokens_estimate=1,
            request_id=sample.call_id,
            run_id="activation-probe",
            is_probe=True,
        )
    )
    clock.advance(sample.first_token_latency_seconds)
    ledger.first_token(ModelCallFirstTokenParams(call_id=sample.call_id))
    ledger.finished(ModelCallFinishParams(call_id=sample.call_id, output_tokens=1))


__all__ = ["ProbeSample", "build_model_timeout_budget", "timeout_budget_issues"]


# ---- 校验夹具/门（原 llm_activation_readiness_fixtures.py 并入）----
def build_small_llm_canary_gate(workspace: Path) -> dict[str, object]:
    root = Path(workspace).expanduser().resolve()
    return {
        "cases": [
            _canary_case(root, "llm_canary_file_artifact", ("read_only", "dry_run"), ("output.md",)),
            _canary_case(root, "llm_canary_tool_failure_repair", ("read_only",), ("repair_report.md",)),
            _canary_case(root, "llm_canary_compact_resume", ("read_only", "dry_run"), ("resume_state.json",)),
            _canary_case(root, "llm_canary_static_web_artifact", ("read_only", "dry_run"), ("index.html",)),
        ],
    }


def model_adapter_facts() -> dict[str, object]:
    return {
        "tool_calls": [
            {"tool_name": "read_file", "call_id": "provider-call-1"},
            {"tool_name": "write_file", "call_id": "generated-call-2"},
        ],
        "stream": {"complete": True, "partial_json": False},
        "retry_limit": 2,
        "model_errors": [{"code": "rate_limit", "retryable": True}],
        "model_switch": {
            "from_schema": "canonical_tool_runtime_v1",
            "to_schema": "canonical_tool_runtime_v1",
        },
    }


def prompt_context_facts() -> dict[str, object]:
    required = (
        "contract_hash",
        "allowed_tools",
        "required_artifacts",
        "acceptance_contract_ref",
        "run_scope_ref",
        "tool_manifest_ref",
    )
    return {
        "required_contract_fields": required,
        "assembled_context": {
            "contract_hash": "sha256:activation-contract",
            "allowed_tools": ("read_file", "write_file"),
            "required_artifacts": ({"path": "artifacts/output.md"},),
            "acceptance_contract_ref": "acceptance://activation",
            "run_scope_ref": "runscope://activation",
            "tool_manifest_ref": "toolmanifest://activation",
        },
        "truncated_fields": (),
        "allowed_tools": ("read_file", "write_file"),
        "tool_schemas": {
            "read_file": {"input": {"path": "string"}},
            "write_file": {"input": {"path": "string", "content_ref": "string"}},
        },
        "untrusted_inputs": [{"source": "user", "applied_to_contract": False}],
        "memory_entries": [{"entry_ref": "memory://preference", "attempted_contract_override": False}],
    }


def side_effect_events() -> tuple[dict[str, object], ...]:
    return (
        {"type": "tool_registration", "tool": "read_file", "effect": "read_only"},
        {"type": "tool_registration", "tool": "write_file", "effect": "mutating"},
        {"type": "tool_call", "tool": "read_file", "effect": "read_only", "wrote_paths": []},
        {
            "type": "tool_call",
            "tool": "write_file",
            "effect": "mutating",
            "idempotency_key": "idem-write-activation",
            "replay_mode": False,
            "executed": True,
        },
        {"type": "tool_result", "tool": "write_file", "mode": "dry_run", "claimed_real_execution": False},
    )


def trace_events() -> tuple[dict[str, object], ...]:
    return (
        {"type": "state_transition", "run_id": "run-activation", "from": "PLANNING", "event": "PLAN_DONE", "to": "RUNNING"},
        {
            "type": "state_transition",
            "run_id": "run-activation",
            "from": "RUNNING",
            "event": "NEED_TOOL",
            "to": "WAITING_FOR_TOOL",
        },
        {
            "type": "tool_result",
            "run_id": "run-activation",
            "tool": "read_file",
            "operation_id": "op-read-1",
            "ok": True,
            "duration_ms": 7,
        },
        {
            "type": "state_transition",
            "run_id": "run-activation",
            "from": "WAITING_FOR_TOOL",
            "event": "TOOL_OK",
            "to": "RUNNING",
        },
        {"type": "state_transition", "run_id": "run-activation", "from": "RUNNING", "event": "TASK_DONE", "to": "VERIFYING"},
        {"type": "state_transition", "run_id": "run-activation", "from": "VERIFYING", "event": "VERIFY_PASS", "to": "DONE"},
    )


def trace_capture_manifest() -> dict[str, object]:
    return {
        "refs_only": True,
        "redaction_ok": True,
        "required_refs": (
            "llm_input_ref",
            "llm_output_ref",
            "tool_trace_ref",
            "state_events_ref",
            "artifact_refs",
            "acceptance_report_ref",
            "replay_spec_ref",
            "failure_sample_ref",
        ),
        "capture_refs": {
            "llm_input_ref": "llm-input://run-activation/turn-1",
            "llm_output_ref": "llm-output://run-activation/turn-1",
            "tool_trace_ref": "tooltrace://run-activation",
            "state_events_ref": "runlog://run-activation",
            "artifact_refs": ("artifact://run-activation/output.md",),
            "acceptance_report_ref": "acceptance://run-activation/report",
            "replay_spec_ref": "replay://run-activation/spec",
            "failure_sample_ref": "failure-sample://run-activation/if-failed",
        },
    }


def trace_manifest_issues(manifest: dict[str, object]) -> list[str]:
    issues: list[str] = []
    refs = manifest.get("capture_refs") if isinstance(manifest.get("capture_refs"), dict) else {}
    if manifest.get("refs_only") is not True:
        issues.append("TRACE_CAPTURE_NOT_REFS_ONLY")
    if manifest.get("redaction_ok") is not True:
        issues.append("TRACE_CAPTURE_REDACTION_MISSING")
    for key in _string_tuple(manifest.get("required_refs")):
        if key not in refs:
            issues.append("TRACE_CAPTURE_REF_MISSING")
            break
    return issues


def _canary_case(
    root: Path,
    case_id: str,
    tool_modes: tuple[str, ...],
    artifact_paths: tuple[str, ...],
) -> dict[str, object]:
    expected_artifacts = [{"path": path, "min_size": 1} for path in artifact_paths]
    return {
        "case_id": case_id,
        "complexity": "small",
        "workspace_ref": f"workspace://{root.name}/{case_id}",
        "isolation_ok": True,
        "real_execution_allowed": False,
        "allowed_effects": tuple("dry_run" if mode == "dry_run" else "read_only" for mode in tool_modes),
        "tool_modes": tool_modes,
        "prompt_ref": f"prompt://llm-canary/{case_id}",
        "expected_artifacts": expected_artifacts,
        "verification_refs": (f"acceptance://llm-canary/{case_id}",),
        "replay_capture_enabled": True,
        "max_runtime_seconds": 600,
    }


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(text for item in value for text in (str(item or "").strip(),) if text)


__all__ = [
    "build_small_llm_canary_gate",
    "model_adapter_facts",
    "prompt_context_facts",
    "side_effect_events",
    "trace_capture_manifest",
    "trace_events",
    "trace_manifest_issues",
]


# ---- 就绪度主流程 ----
def run_llm_activation_readiness(
    request: LLMActivationReadinessRequest,
) -> LLMActivationReadinessReport:
    workspace = Path(request.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    phases = (
        _validate_model_adapter_phase(workspace),
        _validate_prompt_context_phase(workspace),
        _validate_side_effect_phase(workspace),
        _validate_canary_phase(workspace),
        _validate_trace_phase(workspace),
        _validate_timeout_phase(workspace),
        _validate_total_gate_phase(request.pre_real_report),
    )
    report = LLMActivationReadinessReport(
        ok=not any(phase.status == "FAILED" for phase in phases),
        summary=readiness_summary(phases),
        report_ref="llm_activation_readiness/report.json",
        phases=phases,
    )
    write_json_file(workspace / report.report_ref, report.to_dict())
    return report


def _validate_model_adapter_phase(workspace: Path) -> LLMActivationReadinessPhase:
    facts = model_adapter_facts()
    validation = validate_model_adapter_facts(facts)
    ref = "llm_activation_readiness/model_adapter_facts.json"
    write_json_file(workspace / ref, facts)
    return readiness_phase("model_adapter_entry_contract", validation.ok, [ref], list(validation.error_codes))


def _validate_prompt_context_phase(workspace: Path) -> LLMActivationReadinessPhase:
    facts = prompt_context_facts()
    validation = validate_prompt_context(facts)
    ref = "llm_activation_readiness/prompt_context_facts.json"
    write_json_file(workspace / ref, facts)
    return readiness_phase("prompt_context_assembly_contract", validation.ok, [ref], list(validation.error_codes))


def _validate_side_effect_phase(workspace: Path) -> LLMActivationReadinessPhase:
    events = side_effect_events()
    validation = validate_side_effects(events)
    ref = "llm_activation_readiness/side_effect_events.json"
    write_json_file(workspace / ref, list(events))
    return readiness_phase("tool_side_effect_gate", validation.ok, [ref], list(validation.error_codes))


def _validate_canary_phase(workspace: Path) -> LLMActivationReadinessPhase:
    gate = build_small_llm_canary_gate(workspace / "llm-canary")
    validation = validate_small_real_acceptance_gate(gate)
    ref = "llm_activation_readiness/small_llm_canary_gate.json"
    write_json_file(workspace / ref, gate)
    return readiness_phase("small_llm_canary_design", validation.ok, [ref], list(validation.error_codes))


def _validate_trace_phase(workspace: Path) -> LLMActivationReadinessPhase:
    events = trace_events()
    validation = validate_run_trace_events(events)
    manifest = trace_capture_manifest()
    events_ref = "llm_activation_readiness/trace_events.json"
    manifest_ref = "llm_activation_readiness/trace_capture_manifest.json"
    write_json_file(workspace / events_ref, list(events))
    write_json_file(workspace / manifest_ref, manifest)
    issues = list(validation.error_codes) + trace_manifest_issues(manifest)
    return readiness_phase("llm_trace_replay_capture", not issues, [events_ref, manifest_ref], issues)


def _validate_timeout_phase(workspace: Path) -> LLMActivationReadinessPhase:
    budget = build_model_timeout_budget(workspace / "llm-timeout")
    ref = "llm-timeout/timeout_budget.json"
    issues = timeout_budget_issues(budget)
    return readiness_phase("model_timeout_input_speed_budget", not issues, [ref, str(budget["ledger_ref"])], issues)


def _validate_total_gate_phase(
    pre_real_report: PreRealTaskValidationReport | None,
) -> LLMActivationReadinessPhase:
    if pre_real_report is None:
        return readiness_phase("pre_llm_total_gate", False, [], ["PRE_REAL_VALIDATION_MISSING"])
    if pre_real_report.ok is not True:
        return readiness_phase("pre_llm_total_gate", False, [pre_real_report.report_ref], ["PRE_REAL_VALIDATION_NOT_PASSED"])
    return readiness_phase("pre_llm_total_gate", True, [pre_real_report.report_ref], [])


__all__ = [
    "LLMActivationReadinessPhase",
    "LLMActivationReadinessReport",
    "LLMActivationReadinessRequest",
    "build_model_timeout_budget",
    "build_small_llm_canary_gate",
    "run_llm_activation_readiness",
]
