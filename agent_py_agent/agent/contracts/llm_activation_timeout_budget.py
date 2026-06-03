
from __future__ import annotations

from dataclasses import dataclass
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
