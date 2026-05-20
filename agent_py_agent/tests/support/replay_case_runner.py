from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .trace_replay import TraceReplayResult, replay_contract_trace


@dataclass(frozen=True)
class ReplayCaseResult:
    name: str
    ok: bool
    trace: str
    errors: tuple[str, ...]
    replay_result: TraceReplayResult


def run_replay_case(spec_path: Path, run_dir: Path) -> ReplayCaseResult:
    spec = _spec(spec_path)
    _prepare_run_dir(run_dir, spec.get("setup_files"))
    trace = (spec_path.parent.parent / str(spec.get("trace") or "")).resolve()
    result = replay_contract_trace(trace, run_dir)
    errors = _validate_expected(spec.get("expected"), result)
    return ReplayCaseResult(
        name=str(spec.get("name") or spec_path.stem),
        ok=not errors,
        trace=str(trace),
        errors=errors,
        replay_result=result,
    )


def _spec(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _prepare_run_dir(run_dir: Path, setup_files: object) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    if not isinstance(setup_files, list):
        return
    for item in setup_files:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip()
        if not rel:
            continue
        target = run_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(item.get("content") or ""), encoding="utf-8")


def _validate_expected(expected: object, result: TraceReplayResult) -> tuple[str, ...]:
    if not isinstance(expected, dict):
        return ("expected_block_missing",)
    errors: list[str] = []
    _expect_equal(errors, "contract_ok", expected.get("contract_ok"), result.contract_result.ok)
    _expect_equal(errors, "blocked", expected.get("blocked"), result.blocked)
    _expect_equal(errors, "block_reason", expected.get("block_reason"), result.block_reason)
    _expect_list(errors, "replay_error_codes", expected.get("replay_error_codes"), result.replay_error_codes)
    _expect_list(errors, "contract_error_codes", expected.get("contract_error_codes"), result.contract_result.error_codes)
    return tuple(errors)


def _expect_equal(errors: list[str], name: str, expected: object, actual: object) -> None:
    if expected != actual:
        errors.append(f"{name}: expected={expected!r} actual={actual!r}")


def _expect_list(errors: list[str], name: str, expected: object, actual: object) -> None:
    expected_values = tuple(str(item) for item in expected) if isinstance(expected, list) else ()
    actual_values = tuple(str(item) for item in actual)
    if expected_values != actual_values:
        errors.append(f"{name}: expected={expected_values!r} actual={actual_values!r}")


__all__ = ["ReplayCaseResult", "run_replay_case"]
