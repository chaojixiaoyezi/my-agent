from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .contract_fixture_runner import ContractFixtureResult, verify_contract_fixture
from .fake_tools import FakeToolRunner


@dataclass(frozen=True)
class FakeLLMRunResult:
    final_status: str
    tool_trace: tuple[dict[str, object], ...]
    contract_result: ContractFixtureResult
    blocked: bool = False
    block_reason: str = ""


class FakeLLMRunner:
    def __init__(self, fixture: dict[str, object], fixture_root: Path):
        self.fixture = fixture
        self.fixture_root = fixture_root

    @classmethod
    def from_fixture(cls, fixture: dict[str, object], *, fixture_root: Path | None = None) -> FakeLLMRunner:
        root = fixture_root or Path(__file__).resolve().parents[1] / "fake_llm"
        return cls(fixture, root)

    def run(self, run_dir: Path) -> FakeLLMRunResult:
        tools = FakeToolRunner(run_dir)
        final_status = "UNKNOWN"
        failure_counts: dict[str, int] = {}
        block_reason = ""
        for step in self._steps():
            step_type = str(step.get("type") or "")
            if step_type == "tool_call":
                result = tools.execute(str(step.get("tool") or ""), _dict(step.get("params")))
                if not bool(result.get("ok")):
                    key = _failure_key(tools.trace[-1])
                    failure_counts[key] = failure_counts.get(key, 0) + 1
                    if failure_counts[key] >= 3 and not block_reason:
                        block_reason = "TOOL_REPEATED_EXACT_FAILURE"
            elif step_type == "final":
                final_status = str(step.get("status") or "UNKNOWN")
        contract = self._contract()
        result = verify_contract_fixture(run_dir, contract, tool_trace=tools.trace, final_status=final_status)
        return FakeLLMRunResult(
            final_status=final_status,
            tool_trace=tuple(tools.trace),
            contract_result=result,
            blocked=bool(block_reason),
            block_reason=block_reason,
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
