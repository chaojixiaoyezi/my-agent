#!/usr/bin/env python3
"""Verify the review bundle or an explicitly selected repository checkout.

This script checks the plan's file ownership and runs existing behavior tests.
It does not implement the plan, create missing test files, install dependencies,
change the caller's HOME, or treat an unexecuted gate as a pass.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path, PurePosixPath

BASELINE_TESTS = (
    "agent_py_agent/tests/test_skill_learning.py",
    "agent_py_agent/tests/test_skill_learning_integration.py",
    "agent_py_agent/tests/test_gateway_learning_discovery.py",
)
IMPORT_GATE = "scripts/check_import_boundaries.py"
PRIVATE_TMP = Path("/private/tmp")
PHASE_CLAIMS = {
    "bundle": "Plan metadata and file ownership only; no production behavior has been executed or implemented.",
    "baseline": "Three fixed pre-change regression files plus import boundaries only; passing does not verify the target architecture.",
    "post": "Selected implementation behavior files, import boundaries and lint only; a subset pass is not an integrated release pass.",
}


class ReviewError(Exception):
    """A requested verification prerequisite is absent or inconsistent."""


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReviewError(f"JSON metadata contains a duplicate key: {key}")
        value[key] = item
    return value


def _read_object(path: Path) -> dict:
    if not path.is_file():
        raise ReviewError(f"Required metadata file is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        raise ReviewError(f"Cannot read JSON metadata {path}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewError(f"Metadata must be a JSON object: {path}")
    return value


def _path_list(row: dict, key: str, *, required: bool = False) -> list[str]:
    if required and key not in row:
        raise ReviewError(f"{row.get('id', '?')} is missing {key}")
    values = row.get(key, [])
    if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
        raise ReviewError(f"{row.get('id', '?')}.{key} must be an array of nonempty paths")
    if len(set(values)) != len(values):
        raise ReviewError(f"{row.get('id', '?')}.{key} contains duplicate paths")
    return values


def _repo_path(repo: Path, value: str) -> Path:
    path = PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute() or ".." in path.parts
            or value != path.as_posix() or not path.parts or ":" in path.parts[0]):
        raise ReviewError(f"Repository path must be canonical and relative: {value!r}")
    resolved = (repo / value).resolve()
    if not resolved.is_relative_to(repo):
        raise ReviewError(f"Repository path escapes the selected checkout: {value}")
    return resolved


def load_workstreams(bundle: Path) -> tuple[dict, list[dict]]:
    metadata = _read_object(bundle / "workstreams.json")
    _read_object(bundle / "interface_contracts.json")
    rows = metadata.get("workstreams")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ReviewError("workstreams.json must contain a nonempty workstreams array")
    ids = [row.get("id") for row in rows]
    if any(not isinstance(item, str) or not re.fullmatch(r"W(?:0|[1-9][0-9]*)", item) for item in ids):
        raise ReviewError("Each workstream must have an id of the form W0, W1, ...")
    if len(ids) != len(set(ids)):
        raise ReviewError("Duplicate workstream ids")
    expected = {f"W{index}" for index in range(len(rows))}
    if set(ids) != expected:
        raise ReviewError("Workstream ids must form a contiguous W0..WK set")
    return metadata, sorted(rows, key=lambda row: int(row["id"][1:]))


def _acceptance_groups(metadata: dict, known: set[str]) -> list[dict]:
    groups = metadata.get("acceptance_groups", [])
    if not isinstance(groups, list):
        raise ReviewError("acceptance_groups must be an array")
    ids: set[str] = set()
    grouped: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise ReviewError("Each acceptance group must be an object")
        gid = group.get("id")
        members = group.get("workstreams")
        if not isinstance(gid, str) or not re.fullmatch(r"G(?:0|[1-9][0-9]*)", gid) or gid in ids:
            raise ReviewError("Acceptance group IDs must be unique G0, G1, ... identifiers")
        if (not isinstance(members, list) or len(members) < 2 or
                any(not isinstance(member, str) for member in members)):
            raise ReviewError(f"Acceptance group {gid} must name at least two workstreams")
        if len(set(members)) != len(members) or set(members) - known:
            raise ReviewError(f"Acceptance group {gid} contains duplicate or unknown workstreams")
        if grouped & set(members):
            raise ReviewError(f"Acceptance groups must be disjoint: {gid}")
        if group.get("requires_combined_commit") is not True:
            raise ReviewError(f"Acceptance group {gid} must require a combined commit")
        if not isinstance(group.get("reason"), str) or not group["reason"].strip():
            raise ReviewError(f"Acceptance group {gid} needs a concrete reason")
        ids.add(gid)
        grouped.update(members)
    return groups


def _selection(metadata: dict, rows: list[dict], requested: list[str], *, post: bool) -> dict:
    groups = _acceptance_groups(metadata, {row["id"] for row in rows})
    requested_ids = [row["id"] for row in _select_rows(rows, requested)]
    effective = set(requested_ids)
    if post:
        for group in groups:
            if effective & set(group["workstreams"]):
                effective.update(group["workstreams"])
    return {"acceptance_groups": groups, "requested_workstreams": requested_ids,
            "effective_workstreams": [row["id"] for row in rows if row["id"] in effective]}


def validate_bundle(repo: Path, bundle: Path, *, post: bool = False,
                    selected_workstreams: list[str] | None = None) -> tuple[list[dict], dict]:
    """Validate exact write ownership; running a test never grants write ownership.

    Only files + new_files grant ownership. Post selection expands indivisible
    acceptance groups before checking files; bundle checks need no new files.
    """
    metadata, rows = load_workstreams(bundle)
    owners: dict[str, list[str]] = defaultdict(list)
    errors: list[str] = []
    known = {row["id"] for row in rows}
    selection = _selection(metadata, rows, selected_workstreams or [], post=post)
    required_new_ids = set(selection["effective_workstreams"])
    dependencies: dict[str, set[str]] = {}
    for row in rows:
        wid = row["id"]
        existing = _path_list(row, "files", required=True)
        additions = _path_list(row, "new_files", required=True)
        tests = _path_list(row, "existing_tests", required=True)
        new_tests = _path_list(row, "new_tests")
        cross_tests = _path_list(row, "read_only_cross_slice_tests")
        forbidden = _path_list(row, "shared_forbidden", required=True)
        owned = existing + additions
        for value in owned:
            owners[value].append(wid)
        if set(owned) & set(forbidden):
            errors.append(f"{wid} owns forbidden paths: {sorted(set(owned) & set(forbidden))}")
        for value in existing + tests + cross_tests + forbidden + new_tests + additions:
            _repo_path(repo, value)
        for value in existing + tests + cross_tests:
            if not _repo_path(repo, value).is_file():
                errors.append(f"{wid}: required existing file is missing: {value}")
        for value in additions:
            present = _repo_path(repo, value).exists()
            if post and wid in required_new_ids and not _repo_path(repo, value).is_file():
                errors.append(f"{wid}: required new file is missing after implementation: {value}")
            elif not post and present:
                errors.append(f"{wid}: new_files path already exists in the supplied baseline: {value}")
        for value in new_tests:
            if value not in additions:
                errors.append(f"{wid}: new_tests path is not owned in new_files: {value}")
        expected_plan = f"plan-{wid.lower()}.md"
        if row.get("plan_file", expected_plan) != expected_plan:
            errors.append(f"{wid}: plan_file must be {expected_plan}")
        plan = bundle / expected_plan
        if not plan.is_file() or not plan.read_text(encoding="utf-8").strip():
            errors.append(f"{wid}: dispatch plan is missing or empty: {plan.name}")
        elif plan.read_text(encoding="utf-8").splitlines()[0] != f"=== FILE: {expected_plan} ===":
            errors.append(f"{wid}: dispatch plan is missing its exact first-line FILE marker")
        deps = row.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
            errors.append(f"{wid}: depends_on must be an array of workstream ids")
            deps = []
        if set(deps) - known or wid in deps:
            errors.append(f"{wid}: invalid dependencies: {deps}")
        if len(set(deps)) != len(deps):
            errors.append(f"{wid}: duplicate dependencies: {deps}")
        dependencies[wid] = set(deps)
    overlaps = {name: ids for name, ids in owners.items() if len(ids) > 1}
    overlap_count = sum(len(ids) - 1 for ids in overlaps.values())
    if type(metadata.get("overlap_count")) is not int or metadata["overlap_count"] != 0:
        errors.append("Declared overlap_count must be the integer 0")
    if overlap_count:
        errors.append(f"Computed write ownership overlap_count={overlap_count}: {overlaps}")
    if "file_owners" in metadata:
        expected_owners = {path: ids[0] for path, ids in owners.items()}
        if metadata["file_owners"] != expected_owners:
            errors.append("file_owners does not exactly match files + new_files ownership")
    resolved: set[str] = set()
    while len(resolved) < len(rows):
        ready = {wid for wid, deps in dependencies.items() if wid not in resolved and deps <= resolved}
        if not ready:
            errors.append("Workstream dependency graph has a cycle or an unknown dependency")
            break
        resolved.update(ready)
    if errors:
        raise ReviewError("\n".join(errors))
    return rows, {"workstream_count": len(rows), "owned_paths": len(owners),
                  "overlap_count": overlap_count, "dependency_graph": "acyclic",
                  "path_validation": "post" if post else "baseline",
                  **selection}


def _interpreter(value: str) -> str:
    candidate = shutil.which(value) if not Path(value).is_absolute() else value
    if not candidate or not Path(candidate).is_file():
        raise ReviewError(f"Requested Python interpreter was not found: {value}")
    # Resolving a virtualenv's executable symlink discards its pyvenv.cfg and
    # silently selects the base interpreter instead of the requested one.
    return str(Path(candidate).absolute())


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")[:150] or "gate"


def _terminate_group(process: subprocess.Popen) -> None:
    """Signal only the child group created by this runner, never other sessions."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # A descendant may ignore TERM after its immediate parent exits. The group
    # belongs to the new session we created; terminate remaining members too.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=5)


def _junit_counts(path: Path) -> dict:
    if not path.is_file():
        raise ReviewError(f"Pytest did not produce its JUnit report: {path}")
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ReviewError(f"Unreadable JUnit report: {path}: {exc}") from exc
    suites = [suite for suite in root.iter("testsuite") if not list(suite.iter("testsuite"))[1:]]
    if not suites:
        raise ReviewError(f"JUnit report contains no test suite: {path}")
    try:
        values = [{key: int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")}
                  for suite in suites]
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewError(f"JUnit report has missing or invalid test counts: {path}") from exc
    if any(any(count < 0 for count in value.values()) or
           value["tests"] < sum(value[key] for key in ("failures", "errors", "skipped")) for value in values):
        raise ReviewError(f"JUnit report has inconsistent test counts: {path}")
    counts = {key: sum(value[key] for value in values) for key in ("tests", "failures", "errors", "skipped")}
    counts["passed"] = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    return counts


def run_gate(command: list[str], *, label: str, repo: Path, output: Path,
             timeout: float, junit: Path | None = None) -> dict:
    logfile = output / (_safe_label(label) + ".log")
    record = {"label": label, "command": command, "cwd": str(repo), "log": str(logfile),
              "started_at": _utc_now(), "status": "failed", "returncode": None}
    started = time.monotonic()
    process = None
    print(f"[RUN] {label}", flush=True)
    try:
        PRIVATE_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="my-agent-review-", dir=PRIVATE_TMP) as directory:
            env = os.environ.copy()
            env.pop("PYTEST_ADDOPTS", None)
            env.pop("PYTEST_PLUGINS", None)
            env.update(HOME=directory, MY_AGENT_HOME=directory, PYTHONDONTWRITEBYTECODE="1",
                       PYTHONNOUSERSITE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
            record["isolated_home"] = directory
            with logfile.open("wb") as log:
                process = subprocess.Popen(command, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    record["returncode"] = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    record["status"] = "timed_out"
                    record["error"] = f"Exceeded {timeout:g} seconds; terminated this gate's child process group"
                    _terminate_group(process)
                    record["returncode"] = process.returncode
                else:
                    record["status"] = "passed" if record["returncode"] == 0 else "failed"
    except KeyboardInterrupt:
        if process is not None:
            _terminate_group(process)
        record.update(status="interrupted", error="User interrupted this runner")
    except (OSError, subprocess.SubprocessError) as exc:
        if process is not None:
            _terminate_group(process)
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    record["duration_seconds"] = round(time.monotonic() - started, 3)
    if junit is not None:
        record["junit"] = str(junit)
        try:
            record["counts"] = _junit_counts(junit)
            if record["counts"]["tests"] == 0:
                record.update(status="failed", error="The test target collected no tests")
            elif record["counts"]["skipped"] == record["counts"]["tests"]:
                record.update(status="failed", error="Every collected test was skipped; no behavior was verified")
            elif (record["counts"]["errors"] or record["counts"]["failures"]) and record["status"] == "passed":
                record.update(status="failed", error="JUnit contains failures despite a zero exit code")
        except ReviewError as exc:
            if record["status"] == "passed":
                record["status"] = "failed"
            record["junit_error"] = str(exc)
    print(f"[{record['status'].upper()}] {label}; log={logfile}", flush=True)
    return record


def _test_paths(rows: list[dict]) -> list[str]:
    selected: set[str] = set()
    for row in rows:
        selected.update(_path_list(row, "existing_tests"))
        selected.update(_path_list(row, "new_tests"))
        selected.update(_path_list(row, "read_only_cross_slice_tests"))
        selected.update(value for value in _path_list(row, "files") + _path_list(row, "new_files")
                        if value.startswith("agent_py_agent/tests/") and Path(value).name.startswith("test_")
                        and value.endswith(".py"))
    if not selected:
        raise ReviewError("Selected workstream(s) contain no behavior test targets")
    return sorted(selected)


def _select_rows(rows: list[dict], requested: list[str]) -> list[dict]:
    if not requested:
        return rows
    parts = [part.strip().upper() for item in requested for part in item.split(",")]
    wanted = set(parts)
    known = {row["id"] for row in rows}
    if not wanted or "" in wanted or wanted - known:
        raise ReviewError(f"Unknown workstream(s): {sorted(wanted - known)}; valid: {sorted(known)}")
    return [row for row in rows if row["id"] in wanted]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Source checkout to inspect or test")
    parser.add_argument("--bundle-dir", type=Path, default=Path(__file__).resolve().parent,
                        help="Directory containing workstreams, contracts and plan-wN files")
    parser.add_argument("--phase", choices=("bundle", "baseline", "post"), default="bundle")
    parser.add_argument("--workstream", action="append", default=[], help="Post/bundle subset, e.g. W2 or W2,W3; repeatable")
    parser.add_argument("--python", default=sys.executable, help="Explicit Python executable (default: this interpreter)")
    parser.add_argument("--timeout", type=float, default=300.0, help="Maximum seconds per subprocess gate")
    parser.add_argument("--output-dir", type=Path, help="New result directory; by default bundle-dir/verification/<run>")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or not args.timeout < float("inf"):
        parser.error("--timeout must be positive and finite")
    if args.phase == "baseline" and args.workstream:
        parser.error("--workstream applies only to bundle/post phases; baseline always uses the three fixed targets")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo, bundle = args.repo.resolve(), args.bundle_dir.resolve()
    run_name = f"{args.phase}-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    output = args.output_dir.resolve() if args.output_dir else bundle / "verification" / run_name
    report = {"schema_version": "skill-memory-review-results.v1", "phase": args.phase,
              "started_at": _utc_now(), "repo": str(repo), "bundle_dir": str(bundle),
              "selected_workstreams": args.workstream, "checks": [], "overall": "failed",
              "requested_workstreams": args.workstream, "effective_workstreams": [],
              "verification_scope": PHASE_CLAIMS[args.phase], "target_post_gates_passed": False,
              "integrated_post_gates_passed": False}
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print(f"[FAILED] Cannot create a fresh results directory {output}: {exc}", file=sys.stderr)
        return 2
    try:
        if not repo.is_dir():
            raise ReviewError(f"Repository directory does not exist: {repo}")
        python = _interpreter(args.python)
        report["python"] = python
        rows = []
        if args.phase in {"bundle", "post"}:
            # Record the acceptance unit even when subsequent file checks fail.
            manifest, declared_rows = load_workstreams(bundle)
            report.update(_selection(manifest, declared_rows, args.workstream, post=args.phase == "post"))
            rows, metadata = validate_bundle(repo, bundle, post=args.phase == "post",
                                             selected_workstreams=args.workstream)
            rows = _select_rows(rows, metadata["effective_workstreams"])
            report["bundle_validation"] = metadata
            report["selected_workstreams"] = [row["id"] for row in rows]
            report["requested_workstreams"] = metadata["requested_workstreams"]
            report["effective_workstreams"] = metadata["effective_workstreams"]
        if args.phase == "bundle":
            report["overall"] = "passed"
        else:
            targets = list(BASELINE_TESTS) if args.phase == "baseline" else _test_paths(rows)
            report["requested_test_targets"] = targets
            for path in [*targets, IMPORT_GATE]:
                if not _repo_path(repo, path).is_file():
                    raise ReviewError(f"Required verification target is missing: {path}")
            modules = ["pytest"] + (["ruff"] if args.phase == "post" else [])
            dependencies = {}
            for module in modules:
                dependency_code = (
                    "import importlib.util, json, sys; "
                    f"name={module!r}; found=importlib.util.find_spec(name) is not None; "
                    "print(json.dumps({'required': name, 'available': found})); "
                    "sys.exit(0 if found else 2)"
                )
                dependency = run_gate([python, "-c", dependency_code], label=f"dependency-{module}", repo=repo,
                                      output=output, timeout=args.timeout)
                dependencies[module] = dependency
                report["checks"].append(dependency)
                if dependency["status"] == "interrupted":
                    raise ReviewError("Verification interrupted; remaining gates were not executed")
            boundary = run_gate([python, IMPORT_GATE, "--json"], label="import-boundaries", repo=repo,
                                output=output, timeout=args.timeout)
            report["checks"].append(boundary)
            if boundary["status"] == "interrupted":
                raise ReviewError("Verification interrupted; remaining gates were not executed")
            if dependencies["pytest"]["status"] == "passed":
                for index, target in enumerate(targets):
                    xml = output / f"pytest-{index:03d}-{Path(target).stem}.xml"
                    command = [python, "-m", "pytest", target, "-o", "addopts=", "-q", "--tb=short",
                               "-p", "no:cacheprovider", f"--junitxml={xml}"]
                    result = run_gate(command, label=f"pytest-{index:03d}-{Path(target).stem}",
                                      repo=repo, output=output, timeout=args.timeout, junit=xml)
                    result["target"] = target
                    report["checks"].append(result)
                    if result["status"] == "interrupted":
                        raise ReviewError("Verification interrupted; remaining gates were not executed")
            else:
                report["checks"].append({"label": "pytest-targets", "status": "blocked", "targets": targets,
                                          "reason": "The requested interpreter is missing required dependencies; nothing was silently skipped"})
            if args.phase == "post":
                if args.workstream:
                    lint_paths = sorted({path for row in rows for path in row["files"] + row["new_files"]
                                         if path.endswith(".py")})
                else:
                    lint_paths = ["agent_py_agent", "scripts"]
                if not lint_paths:
                    raise ReviewError("Selected workstream(s) have no Python lint targets")
                if dependencies["ruff"]["status"] == "passed":
                    report["checks"].append(run_gate([python, "-m", "ruff", "check", "--no-cache", *lint_paths],
                        label="ruff", repo=repo, output=output, timeout=args.timeout))
                else:
                    report["checks"].append({"label": "ruff", "status": "blocked", "targets": lint_paths,
                                              "reason": "The requested interpreter does not provide ruff"})
            report["overall"] = "passed" if report["checks"] and all(
                check["status"] == "passed" for check in report["checks"]) else "failed"
    except (ReviewError, OSError, ValueError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["overall"] = "failed"
    finally:
        report["finished_at"] = _utc_now()
        counted = [check["counts"] for check in report["checks"] if "counts" in check]
        report["test_counts"] = {key: sum(row[key] for row in counted)
                                 for key in ("tests", "passed", "failures", "errors", "skipped")}
        report["tests_executed"] = report["test_counts"]["passed"] + report["test_counts"]["failures"] > 0
        report["target_post_gates_passed"] = args.phase == "post" and report["overall"] == "passed"
        report["integrated_post_gates_passed"] = report["target_post_gates_passed"] and not args.workstream
        result_path = output / "results.json"
        result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"overall": report["overall"], "phase": args.phase, "results": str(result_path),
                      "test_counts": report["test_counts"], "tests_executed": report["tests_executed"],
                      **({"error": report["error"]} if "error" in report else {})}, ensure_ascii=False))
    return 0 if report["overall"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
