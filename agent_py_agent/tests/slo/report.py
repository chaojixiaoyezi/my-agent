"""C-S1W6-SLO-REPORT: retain offered samples and correlated durable receipts."""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path


class SLOTargetFailure(AssertionError):
    """A measured target breach, distinct from fixture or protocol errors."""


# LLM: Only completed measurements may become baseline expected failures.
# 函数用途: Raise a dedicated target breach without masking fixture assertions.
def require_target(condition, report):
    if not condition:
        if not report.get("report_path"):
            raise RuntimeError("a measured target failure requires a persisted report")
        raise SLOTargetFailure(report)


# LLM: A report-only baseline retains failed checks; the stage gate enforces the identical frozen targets.
# 函数用途: Validate the report's target verdict and optionally fail the integration gate.
def apply_report_gate(report, *, enforce):
    checks = report.get("target_checks")
    if not isinstance(checks, dict) or not checks or any(type(value) is not bool for value in checks.values()):
        raise ValueError("target checks must be a nonempty mapping of observed boolean verdicts")
    if report.get("targets_met") is not all(checks.values()):
        raise ValueError("target verdict does not match its individual checks")
    if enforce:
        require_target(report["targets_met"], report)


# LLM: Nearest-rank percentiles include every offered sample, including failures at timeout.
# 函数用途: Calculate a percentile without discarding unsuccessful observations.
def percentile(values, fraction):
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]) if ordered else 0.0


# LLM: Every planned token retains all physical emissions; duplicate calls cannot replace an earlier source timestamp.
# 函数用途: Join provider emissions with exact owner/call/offset client receipts and retain missing or duplicate failures.
def correlate_forwarding(tokens, receipts, owners, token_count, timeout_ms):
    sources = defaultdict(list)
    for token in tokens:
        if token["owner"] in owners:
            if not 0 <= token["offset"] < token_count:
                raise ValueError("unexpected provider token offset")
            sources[token["owner"], token["offset"]].append(token)
    result = []
    for owner in sorted(owners):
        for offset in range(token_count):
            emissions = [{"turn": token["turn"], "emitted_at": token["at"],
                "received_at": receipts.get((owner, token["turn"], offset))} for token in sources[owner, offset]]
            failure = "NO_PROVIDER_EMISSION" if not emissions else "DUPLICATE_PROVIDER_EMISSION" if len(emissions) > 1 else ""
            source = emissions[0]["emitted_at"] if emissions else None
            received = emissions[0]["received_at"] if emissions else None
            if not failure and received is None:
                failure = "NO_PUBLIC_FRAME_RECEIPT"
            latency = (received - source) * 1000 if not failure else timeout_ms
            result.append({"owner": owner, "offset": offset, "emissions": emissions, "emitted_at": source,
                "received_at": received, "latency_ms": latency, "failure": failure})
    return result


# LLM: The report schema is consumed by later stage integrators; extra measurements are additive.
# 函数用途: Build a per-scenario report from denominator-preserving samples.
def build_report(scenario, profile, samples, **measurements):
    failures = Counter(sample.code or "RECEIPT_UNCONFIRMED" for sample in samples if sample.accepted_at is None)
    values = [sample.latency_ms() for sample in samples]
    phases = {}
    for phase in ("cold", "warm"):
        subset = [sample.latency_ms() for sample in samples if sample.phase == phase]
        phases[phase] = {"samples": len(subset), "p99_ms": percentile(subset, 0.99)}
    return {"scenario": scenario, "load_profile": profile, "samples": len(samples), "denominator": len(samples),
            "failures": {"count": sum(failures.values()), "by_reason": dict(sorted(failures.items()))},
            "p50_ms": percentile(values, 0.50), "p95_ms": percentile(values, 0.95),
            "p99_ms": percentile(values, 0.99),
            "receipt_correlation": sum(bool(sample.receipt) for sample in samples) / len(samples) if samples else 0.0,
            "cold_warm": phases, "observations": [sample.as_dict() for sample in samples],
            "stop_acknowledged": None, "exit_proof": None, **measurements}


# LLM: Reports cannot overwrite repository files; the caller supplies an isolated report directory.
# 函数用途: Atomically publish one report and return its actual path.
def write_report(directory, report):
    directory = Path(directory).resolve()
    repo = Path(__file__).resolve().parents[3]
    if directory == repo or directory.is_relative_to(repo):
        raise ValueError("SLO reports must be outside the repository")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report['scenario']}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return path
