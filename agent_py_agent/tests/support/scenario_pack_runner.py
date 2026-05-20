from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .contract_fixture_runner import FixtureRunFacts, verify_contract_fixture
from .fake_llm_runner import FakeLLMRunner
from .trace_replay import replay_contract_trace


@dataclass(frozen=True)
class ScenarioPackResult:
    ok: bool
    summary: dict[str, int]
    cases: tuple[dict[str, object], ...]


def run_scenario_pack(manifest_path: Path, run_root: Path) -> ScenarioPackResult:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = manifest.get("cases") if isinstance(manifest, dict) else None
    results = [
        _run_case(dict(case), manifest_path.parent, run_root / str(case.get("case_id") or index))
        for index, case in enumerate(cases if isinstance(cases, list) else [])
        if isinstance(case, dict)
    ]
    passed = sum(bool(item.get("ok")) for item in results)
    return ScenarioPackResult(
        ok=passed == len(results),
        summary={"failed": len(results) - passed, "passed": passed, "total": len(results)},
        cases=tuple(results),
    )


def _run_case(case: dict[str, object], manifest_root: Path, run_dir: Path) -> dict[str, object]:
    kind = str(case.get("kind") or "")
    if kind == "contract":
        return _run_contract_case(case, manifest_root, run_dir)
    if kind == "fake_llm":
        return _run_fake_llm_case(case, manifest_root, run_dir)
    if kind == "replay":
        return _run_replay_case(case, manifest_root, run_dir)
    return {"case_id": str(case.get("case_id") or ""), "kind": kind, "ok": False, "reason": "unknown_case_kind"}


def _run_contract_case(case: dict[str, object], manifest_root: Path, run_dir: Path) -> dict[str, object]:
    contract = json.loads((manifest_root / str(case.get("contract_ref") or "")).resolve().read_text(encoding="utf-8"))
    result = verify_contract_fixture(
        run_dir,
        contract,
        FixtureRunFacts(
            tool_trace=tuple(_list_of_dicts(case.get("tool_trace"))),
            final_status=str(case.get("final_status") or "UNKNOWN"),
        ),
    )
    return _case_result(case, result.error_codes)


def _run_fake_llm_case(case: dict[str, object], manifest_root: Path, run_dir: Path) -> dict[str, object]:
    fixture_path = (manifest_root / str(case.get("fixture_ref") or "")).resolve()
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    result = FakeLLMRunner.from_fixture(fixture, fixture_root=fixture_path.parent).run(run_dir)
    expected_runtime_issue_codes = set(_string_list(case.get("expect_runtime_issue_codes")))
    actual_runtime_issue_codes = _issue_codes(result.runtime_issues)
    payload = _case_result(case, result.contract_result.error_codes)
    payload["runtime_issue_codes"] = sorted(actual_runtime_issue_codes)
    payload["ok"] = bool(payload["ok"]) and expected_runtime_issue_codes.issubset(actual_runtime_issue_codes)
    return payload


def _run_replay_case(case: dict[str, object], manifest_root: Path, run_dir: Path) -> dict[str, object]:
    result = replay_contract_trace((manifest_root / str(case.get("trace_ref") or "")).resolve(), run_dir)
    expected = str(case.get("expect_block_reason") or "")
    expected_error_codes = set(_string_list(case.get("expect_error_codes")))
    expected_runtime_issue_codes = set(_string_list(case.get("expect_runtime_issue_codes")))
    expected_replay_error_codes = set(_string_list(case.get("expect_replay_error_codes")))
    actual_runtime_issue_codes = _issue_codes(result.runtime_issues)
    return {
        "case_id": str(case.get("case_id") or ""),
        "kind": "replay",
        "ok": (
            (not expected or result.block_reason == expected)
            and expected_error_codes.issubset(set(result.contract_result.error_codes))
            and expected_runtime_issue_codes.issubset(actual_runtime_issue_codes)
            and expected_replay_error_codes.issubset(set(result.replay_error_codes))
        ),
        "block_reason": result.block_reason,
        "error_codes": sorted(set(result.contract_result.error_codes)),
        "runtime_issue_codes": sorted(actual_runtime_issue_codes),
        "replay_error_codes": sorted(set(result.replay_error_codes)),
    }


def _case_result(case: dict[str, object], error_codes: tuple[str, ...]) -> dict[str, object]:
    expected = set(_string_list(case.get("expect_error_codes")))
    actual = set(error_codes)
    return {
        "case_id": str(case.get("case_id") or ""),
        "kind": str(case.get("kind") or ""),
        "ok": expected.issubset(actual),
        "error_codes": sorted(actual),
    }


def _list_of_dicts(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value] if isinstance(value, list) else []


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for raw in value if (item := str(raw).strip())]


def _issue_codes(items: tuple[dict[str, object], ...] | list[dict[str, object]]) -> set[str]:
    return {
        code
        for item in items
        if isinstance(item, dict) and (code := str(item.get("code") or "").strip())
    }
