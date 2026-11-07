"""Local SLO markers, explicit expected failures and non-dilutable stage gate."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from .fake_provider import FakeProvider
from .load import GatewayProcess
from .report import SLOTargetFailure, build_report, write_report

HERE = Path(__file__).parent


# LLM: Register the local marker without changing global pytest configuration.
# 函数用途: Initialize report and outcome accounting for this pytest invocation.
def pytest_configure(config):
    global _CONFIG
    _CONFIG = config
    config.addinivalue_line("markers", "slo: isolated Gateway latency or resource scenario")
    config._slo_reports = set()
    config._slo_bad_outcomes = []
    config._slo_stage = os.environ.get("MY_AGENT_SLO_STAGE_GATE") == "1"


# LLM: Exact node ids are strict xfails only in baseline mode; stale entries fail collection.
# 函数用途: Apply the local slow marker and validate the executable clearing ledger.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    ledger = json.loads(Path(os.environ.get("MY_AGENT_SLO_EXPECTED_FAILURES", HERE / "expected_failures.json")).read_text())
    assert ledger["schema"] == 1
    entries = ledger["entries"]
    nodeids = {item.nodeid for item in items}
    relevant = [item for item in items if Path(str(item.path)).is_relative_to(HERE)]
    for item in relevant:
        item.add_marker(pytest.mark.slow)
    # Targeted file runs still validate entries for that file; directory runs validate the full ledger.
    selected_files = {str(item.path.resolve()) for item in relevant}
    full = any(Path(str(arg).split("::")[0]).resolve() == HERE.resolve() for arg in config.args)
    for entry in entries:
        if not entry.get("reason") or not entry.get("added") or not entry.get("clears_when"):
            raise pytest.UsageError("incomplete SLO expected-failure entry")
        if entry["clears_when"] not in {"S1-W1", "S1-W2", "S1-W4", "S1-W5", "S1-INT"} and not entry["clears_when"].startswith("S2-"):
            raise pytest.UsageError("invalid clears_when workstream")
        rel = entry["nodeid"].split("::")[0]
        candidate = (Path(config.rootpath) / rel).resolve()
        if not candidate.is_file() or ((full or str(candidate) in selected_files) and entry["nodeid"] not in nodeids):
            raise pytest.UsageError(f"stale SLO expected failure: {entry['nodeid']}")
    lookup = {entry["nodeid"]: entry for entry in entries}
    for item in relevant:
        if item.nodeid in lookup and not config._slo_stage:
            entry = lookup[item.nodeid]
            item.add_marker(pytest.mark.xfail(strict=True, reason=f"{entry['clears_when']}: {entry['reason']}",
                                            raises=SLOTargetFailure))


# LLM: Capture setup, call and teardown outcomes so a capability skip cannot evade the stage gate.
# 函数用途: Record every skipped or expected-failure SLO outcome.
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if hasattr(report, "wasxfail") and report.when != "call":
        report.outcome = "failed"
        report.longrepr = "SLO expected failures are forbidden during fixture setup or teardown"
        del report.wasxfail
    if item.get_closest_marker("slo") and (report.skipped or hasattr(report, "wasxfail")):
        item.config._slo_bad_outcomes.append(item.nodeid)


# LLM: Module-level skips have no item report; stage mode must account for their collection reports.
# 函数用途: Reject a skipped SLO module even when every other scenario wrote a report.
def pytest_collectreport(report):
    if report.skipped and _CONFIG._slo_stage:
        source = Path(_CONFIG.rootpath) / report.nodeid.split("::")[0]
        if source.resolve().is_relative_to(HERE.resolve()):
            _CONFIG._slo_bad_outcomes.append(report.nodeid)


# LLM: Current-invocation reports, not old files, determine stage completeness.
# 函数用途: Fail stage mode for any skipped scenario or missing report.
def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if not config._slo_stage:
        return
    targets = json.loads(Path(os.environ.get("MY_AGENT_SLO_TARGETS", HERE / "slo_targets.json")).read_text())
    missing = {row["id"] for row in targets["scenarios"]} - config._slo_reports
    if missing or config._slo_bad_outcomes:
        config.get_terminal_writer().line(f"SLO stage gate: missing_reports={sorted(missing)} bad_outcomes={config._slo_bad_outcomes}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


# LLM: Scenario parameters live only in the versioned JSON data file.
# 函数用途: Load test-only profiles and fixture bounds.
@pytest.fixture
def slo_data():
    return json.loads((HERE / "slo_targets.json").read_text())


# LLM: The fake provider is owned by this test and stores TLS files only under tmp_path.
# 函数用途: Start a deterministic OpenAI provider for a SLO scenario.
@pytest.fixture
def fake_provider(tmp_path, request, slo_data):
    identifier = getattr(getattr(request.node, "callspec", None), "params", {}).get("isolated_gateway")
    options = next((row.get("provider", {}) for row in slo_data["scenarios"] if row["id"] == identifier), {})
    options = {"teardown_seconds": slo_data["fixture"]["teardown_seconds"], **options, **getattr(request, "param", {})}
    with FakeProvider(tmp_path / "provider", **options) as provider:
        yield provider


# LLM: Subprocess startup explicitly carries the isolated root despite the parent HOME fixture.
# 函数用途: Yield the base URL, temporary home and actual foreground Gateway subprocess.
@pytest.fixture
def isolated_gateway(tmp_path, request, fake_provider, slo_data):
    param = getattr(request, "param", {})
    if isinstance(param, str):
        param = next(row["config"] for row in slo_data["scenarios"] if row["id"] == param)
    gateway = GatewayProcess(tmp_path / "gateway", fake_provider, slo_data["fixture"], param)
    try:
        gateway.start()
        request.node._slo_gateway = gateway
        yield gateway.base_url, gateway.home, gateway.proc
    finally:
        gateway.close()


# LLM: A report is registered only after its atomic write succeeds outside the checkout.
# 函数用途: Publish one scenario report and track it for the stage gate.
@pytest.fixture
def slo_report(tmp_path_factory, request):
    directory = Path(os.environ.get("MY_AGENT_SLO_REPORT_DIR") or tmp_path_factory.mktemp("slo_reports"))
    # LLM: Tests supply actual measurements rather than precomputed success summaries.
    # 函数用途: Build, persist and return the report for an observed scenario.
    def publish(scenario, profile, samples, **measurements):
        report = build_report(scenario, profile, samples, **measurements)
        report["report_path"] = str(write_report(directory, report))
        request.config._slo_reports.add(scenario)
        return report
    return publish
