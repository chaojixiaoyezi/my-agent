from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .contract_fixture_runner import ContractFixtureResult, FixtureRunFacts, verify_contract_fixture
from .fake_tools import FakeToolRunner


@dataclass(frozen=True)
class FakeLLMRunResult:
    final_status: str
    tool_trace: tuple[dict[str, object], ...]
    contract_result: ContractFixtureResult
    blocked: bool = False
    block_reason: str = ""
    runtime_issues: tuple[dict[str, object], ...] = ()
    state_snapshots: tuple[dict[str, object], ...] = ()
    closeout_snapshots: tuple[dict[str, object], ...] = ()
    acceptance_reports: tuple[dict[str, object], ...] = ()


@dataclass
class _FakeLLMPlayback:
    final_status: str = "UNKNOWN"
    block_reason: str = ""
    failure_counts: dict[str, int] | None = None
    runtime_issues: list[dict[str, object]] | None = None
    state_snapshots: list[dict[str, object]] | None = None
    closeout_snapshots: list[dict[str, object]] | None = None
    acceptance_reports: list[dict[str, object]] | None = None


class FakeLLMRunner:
    def __init__(self, fixture: dict[str, object], fixture_root: Path):
        self.fixture = fixture
        self.fixture_root = fixture_root

    @classmethod
    def from_fixture(cls, fixture: dict[str, object], *, fixture_root: Path | None = None) -> FakeLLMRunner:
        root = fixture_root or Path(__file__).resolve().parents[1] / "fake_llm"
        return cls(fixture, root)

    def run(self, run_dir: Path) -> FakeLLMRunResult:
        tools = FakeToolRunner(run_dir, fixtures=_dict(self.fixture.get("tool_fixtures")))
        playback = _playback()
        for step in self._steps():
            _apply_step(step, tools, playback)
        contract = self._contract()
        result = verify_contract_fixture(
            run_dir,
            contract,
            FixtureRunFacts(
                tool_trace=tuple(tools.trace),
                final_status=playback.final_status,
                runtime_issues=tuple(playback.runtime_issues or []),
            ),
        )
        return FakeLLMRunResult(
            final_status=playback.final_status,
            tool_trace=tuple(tools.trace),
            contract_result=result,
            blocked=bool(playback.block_reason),
            block_reason=playback.block_reason,
            runtime_issues=tuple(playback.runtime_issues or []),
            state_snapshots=tuple(playback.state_snapshots or []),
            closeout_snapshots=tuple(playback.closeout_snapshots or []),
            acceptance_reports=tuple(playback.acceptance_reports or []),
        )

    def _steps(self) -> list[dict[str, object]]:
        steps = self.fixture.get("model_steps")
        return [dict(item) for item in steps] if isinstance(steps, list) else []

    def _contract(self) -> dict[str, object]:
        ref = str(self.fixture.get("contract_ref") or "").strip()
        path = (self.fixture_root / ref).resolve()
        return json.loads(path.read_text(encoding="utf-8"))


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _failure_key(trace_item: dict[str, object]) -> str:
    payload = {
        "params": trace_item.get("params") if isinstance(trace_item.get("params"), dict) else {},
        "result_error_code": _result_error_code(trace_item),
        "tool": str(trace_item.get("tool") or ""),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _result_error_code(trace_item: dict[str, object]) -> str:
    result = trace_item.get("result")
    if not isinstance(result, dict):
        return ""
    return str(result.get("error_code") or "")


def _playback() -> _FakeLLMPlayback:
    return _FakeLLMPlayback(
        failure_counts={},
        runtime_issues=[],
        state_snapshots=[],
        closeout_snapshots=[],
        acceptance_reports=[],
    )


def _apply_step(step: dict[str, object], tools: FakeToolRunner, playback: _FakeLLMPlayback) -> None:
    step_type = str(step.get("type") or "")
    if step_type == "tool_call":
        _apply_tool_call(step, tools, playback)
        return
    if step_type == "final":
        playback.final_status = str(step.get("status") or "UNKNOWN")
        return
    target = _step_bucket(playback, step_type)
    if target is not None:
        target.append(dict(step))


def _apply_tool_call(step: dict[str, object], tools: FakeToolRunner, playback: _FakeLLMPlayback) -> None:
    result = tools.execute(str(step.get("tool") or ""), _dict(step.get("params")))
    if bool(result.get("ok")):
        return
    key = _failure_key(tools.trace[-1])
    counts = playback.failure_counts
    if counts is None:
        counts = {}
        playback.failure_counts = counts
    counts[key] = counts.get(key, 0) + 1
    if counts[key] >= 3 and not playback.block_reason:
        playback.block_reason = "TOOL_REPEATED_EXACT_FAILURE"


def _step_bucket(playback: _FakeLLMPlayback, step_type: str) -> list[dict[str, object]] | None:
    buckets = {
        "acceptance_report": playback.acceptance_reports,
        "closeout_snapshot": playback.closeout_snapshots,
        "runtime_issue": playback.runtime_issues,
        "state_snapshot": playback.state_snapshots,
    }
    bucket = buckets.get(step_type)
    return bucket if isinstance(bucket, list) else None
