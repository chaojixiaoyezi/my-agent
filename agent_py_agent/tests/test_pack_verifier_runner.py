"""能力包 v2 块 2：宿主在唯一沙箱入口里跑钉住的原版检查程序（断网、整根只读、只写临时目录）。

真实沙箱用例按平台运行（macOS Seatbelt / Linux bwrap），本机沙箱不可用时 skip；不可用的结构化分支另用替身验证。
"""

from __future__ import annotations

import json
import socket
import sys
import threading

import pytest

from agent_py_agent.agent.attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    SandboxUnavailableError,
)
from agent_py_agent.agent.capability.pack_verifier_runner import (
    PackVerifierRequest,
    run_pack_verifier,
)
from agent_py_agent.agent.capability_verifier_consent import (
    verifier_confirmation_details,
    verifier_consent_sha256,
)
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_package import content_bundle

HEADER = "import json, sys\n"
PASS = HEADER + "print(json.dumps({'schema': 'pack_verifier_result.v1', 'valid': True, 'warnings': [{'code': 'w1'}]}))\n"
FAIL = HEADER + ("print(json.dumps({'schema': 'pack_verifier_result.v1', 'valid': False, "
                 "'errors': [{'code': 'placeholder_text'}, {'code': 'placeholder_text'}, {'code': 'quote'}]}))\n")
GARBAGE = "print('all good, 0 warnings')\n"
SLEEP = "import time\ntime.sleep(10)\n"
NETWORK = HEADER + (
    "import socket\nok = True\ntry:\n    socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=2).close()\n"
    "except OSError:\n    ok = False\n"
    "print(json.dumps({'schema': 'pack_verifier_result.v1', 'valid': not ok, "
    "'errors': [{'code': 'network_reachable'}] if ok else []}))\n")
WRITE = HEADER + (
    "import os\nok = True\ntry:\n    open(os.path.join(os.path.dirname(sys.argv[1]), 'pwn.txt'), 'w').write('x')\n"
    "except OSError:\n    ok = False\n"
    "print(json.dumps({'schema': 'pack_verifier_result.v1', 'valid': not ok, "
    "'errors': [{'code': 'target_dir_writable'}] if ok else []}))\n")
BASELINE = HEADER + (
    "ok = len(sys.argv) == 4 and sys.argv[2] == '--baseline-project' and open(sys.argv[3]).read() == 'old'\n"
    "print(json.dumps({'schema': 'pack_verifier_result.v1', 'valid': ok}))\n")


def _verification(args, runtime="python", baseline=False):
    verifier = {"id": "check", "member": "scripts/check.py", "runtime": runtime, "applies_to": "delivery",
                "args": args, "timeout_seconds": 2}
    if baseline:
        verifier["baseline"] = {"source": "input_same_deliverable", "flag": "--baseline-project"}
    return {"deliverables": [{"id": "delivery", "path_patterns": ["**/*.json"], "required": True}],
            "verifiers": [verifier]}


def _installed(tmp_path, script, *, args=("{target}",), runtime="python", baseline=False, consent=True):
    def change(manifest):
        manifest["capability"]["verification"] = _verification(list(args), runtime, baseline)
    bundle = content_bundle(files={"CAPABILITY.md": b"# c\n", "scripts/check.py": script.encode()}, change=change)
    owner = resolve_owner_home(tmp_path / "home")
    store = PluginInstallStore(owner)
    entry = store.install(PluginInstallRequest(inspect_plugin_package(bundle), "install", 0)).installation
    details = verifier_confirmation_details(entry.manifest, entry.package_sha256)
    activation = PluginContentActivation("enable", entry.manifest.plugin_id, entry.package_sha256, entry.revision,
                                         entry.settings_revision,
                                         verifier_consent_sha256=verifier_consent_sha256(details) if consent else "")
    store.change_activation(PluginActivationRequest("enable", entry.revision, activation))
    return owner, store.snapshot()[0]


def _target(tmp_path, content='{"schema": "x"}'):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    target = workspace / "out" / "delivery.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(content)
    return workspace, target


def _run(tmp_path, script, **kwargs):
    baseline_text = kwargs.pop("baseline_text", None)
    owner, entry = _installed(tmp_path, script, **kwargs)
    workspace, target = _target(tmp_path)
    baseline = None
    if baseline_text is not None:
        baseline = workspace / "inputs.json"
        baseline.write_text(baseline_text)
    return run_pack_verifier(PackVerifierRequest(owner, entry, "check", target, workspace, baseline)), target


def _sandbox_ready(tmp_path) -> bool:
    spec = AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path, network_access=False)
    try:
        return AttemptExecutionSandbox(spec).require_ready().ready
    except SandboxUnavailableError:
        return False


@pytest.fixture
def real_sandbox(tmp_path):
    if not _sandbox_ready(tmp_path):
        pytest.skip("本机平台沙箱不可用")


def test_macos_profile_denies_network_only_when_requested(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_py_agent.agent.attempt.sandbox.platform.system", lambda: "Darwin")
    for network, expected in ((False, True), (True, False)):
        spec = AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path, network_access=network,
                                  macos_sandbox_exec="/usr/bin/sandbox-exec")
        sandbox = AttemptExecutionSandbox(spec)
        argv = sandbox._macos_argv(["/bin/true"])
        assert ("(deny network*)" in argv[2]) is expected


def test_passing_checker_records_pinned_identity(tmp_path, real_sandbox):
    result, target = _run(tmp_path, PASS)
    assert result.status == "passed" and result.valid is True and result.returncode == 0
    assert result.warning_counts == {"w1": 1} and result.error_counts == {}
    assert result.target == "out/delivery.json" and len(result.target_sha256) == 64
    assert len(result.member_sha256) == 64 and result.member == "scripts/check.py"
    assert result.summary()["warning_count"] == 1


def test_failing_checker_counts_codes_and_bounded_summary(tmp_path, real_sandbox):
    result, _ = _run(tmp_path, FAIL)
    assert result.status == "failed" and result.valid is False
    assert result.error_counts == {"placeholder_text": 2, "quote": 1}
    assert result.summary()["error_codes"] == ["placeholder_text", "quote"]
    assert result.summary()["error_count"] == 3


def test_non_contract_output_is_invalid(tmp_path, real_sandbox):
    result, _ = _run(tmp_path, GARBAGE)
    assert result.status == "error" and result.reason_code == "verifier_output_invalid"


def test_timeout_is_structured(tmp_path, real_sandbox):
    result, _ = _run(tmp_path, SLEEP)
    assert result.status == "error" and result.reason_code == "verifier_timeout"


def _accept_quietly(server):
    try:
        server.accept()
    except OSError:
        pass


def test_network_is_cut(tmp_path, real_sandbox):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    threading.Thread(target=_accept_quietly, args=(server,), daemon=True).start()
    try:
        result, _ = _run(tmp_path, NETWORK, args=("{target}", str(port)))
    finally:
        server.close()
    assert result.status == "passed", result


def test_target_directory_is_read_only(tmp_path, real_sandbox):
    result, target = _run(tmp_path, WRITE)
    assert result.status == "passed", result
    assert not (target.parent / "pwn.txt").exists()


def test_baseline_is_passed_with_declared_flag(tmp_path, real_sandbox):
    result, _ = _run(tmp_path, BASELINE, baseline=True, baseline_text="old")
    assert result.status == "passed", result
    assert result.baseline == "inputs.json" and len(result.baseline_sha256) == 64


def test_workspace_copy_is_never_used(tmp_path, real_sandbox):
    owner, entry = _installed(tmp_path, PASS)
    workspace, target = _target(tmp_path)
    forged = workspace / "scripts" / "check.py"
    forged.parent.mkdir()
    forged.write_text(FAIL)
    result = run_pack_verifier(PackVerifierRequest(owner, entry, "check", target, workspace))
    assert result.status == "passed"


@pytest.mark.parametrize("kwargs,reason", [
    ({"consent": False}, "verifier_consent_missing"),
    ({"runtime": "node"}, "unsupported_runtime"),
])
def test_refusals_never_start_a_process(tmp_path, monkeypatch, kwargs, reason):
    monkeypatch.setattr(AttemptExecutionSandbox, "run", lambda *a, **k: pytest.fail("must not run"))
    result, _ = _run(tmp_path, PASS, **kwargs)
    assert result.status == "not_run" and result.reason_code == reason


def test_sandbox_unavailable_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_py_agent.agent.attempt.sandbox.platform.system", lambda: "FreeBSD")
    AttemptExecutionSandbox._READINESS_CACHE.clear()
    try:
        result, _ = _run(tmp_path, PASS)
    finally:
        AttemptExecutionSandbox._READINESS_CACHE.clear()
    assert result.status == "not_run" and result.reason_code == "sandbox_unavailable"


def test_missing_target_and_unknown_verifier(tmp_path):
    owner, entry = _installed(tmp_path, PASS)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    missing = run_pack_verifier(PackVerifierRequest(owner, entry, "check", workspace / "nope.json", workspace))
    assert missing.status == "error" and missing.reason_code == "target_missing"
    unknown = run_pack_verifier(PackVerifierRequest(owner, entry, "other", workspace / "nope.json", workspace))
    assert unknown.status == "error" and unknown.reason_code == "verifier_not_declared"


def test_fact_is_json_serializable(tmp_path, real_sandbox):
    result, _ = _run(tmp_path, FAIL)
    json.dumps(result.to_fact())
    assert sys.executable


def test_network_isolation_unavailable_fails_closed_without_running(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.sandbox import SandboxReadiness

    monkeypatch.setattr(AttemptExecutionSandbox, "probe", lambda self, **kwargs: SandboxReadiness(True, "SANDBOX_READY", "ok"))
    monkeypatch.setattr(AttemptExecutionSandbox, "_probe_network_isolation",
                        lambda self: SandboxReadiness(False, "SANDBOX_NETWORK_ISOLATION_UNAVAILABLE", "no"))
    monkeypatch.setattr(AttemptExecutionSandbox, "run", lambda *a, **k: pytest.fail("must not run"))
    AttemptExecutionSandbox._READINESS_CACHE.clear()
    try:
        result, _ = _run(tmp_path, PASS)
        networked = AttemptExecutionSandbox(AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path))
        assert networked.require_ready().ready
    finally:
        AttemptExecutionSandbox._READINESS_CACHE.clear()
    assert result.status == "not_run" and result.reason_code == "sandbox_unavailable"



def test_output_contract_requires_schema_and_bounds_summary():
    from agent_py_agent.agent.capability.pack_verifier_runner import _parsed_result

    base = {"verifier_id": "check", "package_id": "p"}
    missing_schema = _parsed_result(json.dumps({"valid": True}), dict(base))
    assert missing_schema.status == "error" and missing_schema.reason_code == "verifier_output_invalid"
    bad_item = _parsed_result(json.dumps({"schema": "pack_verifier_result.v1", "valid": False,
                                          "errors": [{"message": "no code"}]}), dict(base))
    assert bad_item.reason_code == "verifier_output_invalid"
    many = [{"code": f"e{index}"} for index in range(7)]
    result = _parsed_result(json.dumps({"schema": "pack_verifier_result.v1", "valid": False, "errors": many}), dict(base))
    assert result.status == "failed" and len(result.summary()["error_codes"]) == 5
    assert result.summary()["error_count"] == 7
