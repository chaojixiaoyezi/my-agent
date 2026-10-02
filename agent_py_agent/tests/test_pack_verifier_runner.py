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
# 每个关联输入文件的内容写成它的参数名；检查程序核对内容，并把看到的参数按顺序报成警告码，供断言传参顺序。
INPUTS = HEADER + (
    "args = sys.argv[2:]\npairs = list(zip(args[::2], args[1::2]))\n"
    "ok = len(args) % 2 == 0 and all(open(p).read() == f for f, p in pairs)\n"
    "print(json.dumps({'schema': 'pack_verifier_result.v1', 'valid': ok, "
    "'errors': [] if ok else [{'code': 'bad_input'}], "
    "'warnings': [{'code': 'flag' + str(i) + f} for i, (f, p) in enumerate(pairs)]}))\n")
DECLARED_INPUTS = [
    {"flag": "--source", "source": "task_input", "path_patterns": ["**/*.json"], "required": True,
     "field_match": {"format": "json", "field": "schema", "equals": ["source.v1"]}},
    {"flag": "--handoff", "source": "turn_output", "path_patterns": ["**/*.json"], "required": False},
]


def _verification(args, runtime="python", inputs=False):
    verifier = {"id": "check", "member": "scripts/check.py", "runtime": runtime, "applies_to": "delivery",
                "args": args, "timeout_seconds": 2}
    if inputs:
        verifier["inputs"] = DECLARED_INPUTS
    return {"deliverables": [{"id": "delivery", "path_patterns": ["**/*.json"], "required": True}],
            "verifiers": [verifier]}


def _installed(tmp_path, script, *, args=("{target}",), runtime="python", inputs=False, consent=True):
    def change(manifest):
        manifest["capability"]["verification"] = _verification(list(args), runtime, inputs)
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
    provided = kwargs.pop("provided", ())
    owner, entry = _installed(tmp_path, script, **kwargs)
    workspace, target = _target(tmp_path)
    inputs = []
    for flag in provided:
        path = workspace / f"{flag.strip('-')}.json"
        path.write_text(flag)
        inputs.append((flag, path))
    return run_pack_verifier(PackVerifierRequest(owner, entry, "check", target, workspace, tuple(inputs))), target


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


@pytest.mark.parametrize("provided,seen", [
    (("--evil", "--handoff", "--source"), {"flag0--source": 1, "flag1--handoff": 1}),
    (("--source",), {"flag0--source": 1}),
])
def test_declared_inputs_are_passed_in_declaration_order_and_recorded(tmp_path, real_sandbox, provided, seen):
    result, _ = _run(tmp_path, INPUTS, inputs=True, provided=provided)
    assert result.status == "passed", result
    assert result.warning_counts == seen
    facts = result.to_fact()["inputs"]
    assert [item["flag"] for item in facts] == ["--source", "--handoff"]
    assert facts[0]["path"] == "source.json" and len(facts[0]["sha256"]) == 64 and facts[0]["required"]
    assert facts[1]["path"] == ("handoff.json" if "--handoff" in provided else "")


def test_missing_required_input_is_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(AttemptExecutionSandbox, "run", lambda *a, **k: pytest.fail("must not run"))
    result, _ = _run(tmp_path, INPUTS, inputs=True, provided=("--handoff",))
    assert result.status == "not_run" and result.reason_code == "verifier_input_unresolved"
    assert [item["path"] for item in result.to_fact()["inputs"]] == ["", "handoff.json"]


def test_required_input_that_is_not_a_regular_file_counts_as_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr(AttemptExecutionSandbox, "run", lambda *a, **k: pytest.fail("must not run"))
    owner, entry = _installed(tmp_path, INPUTS, inputs=True)
    workspace, target = _target(tmp_path)
    (workspace / "folder.json").mkdir()
    for path in (workspace / "folder.json", workspace / "gone.json"):
        result = run_pack_verifier(PackVerifierRequest(owner, entry, "check", target, workspace, (("--source", path),)))
        assert result.status == "not_run" and result.reason_code == "verifier_input_unresolved", path


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
    for valid, errors in ((True, [{"code": "e"}]), (False, [])):
        inconsistent = _parsed_result(json.dumps({"schema": "pack_verifier_result.v1", "valid": valid,
                                                  "errors": errors}), dict(base))
        assert inconsistent.reason_code == "verifier_output_invalid", (valid, errors)
    many = [{"code": f"e{index}"} for index in range(7)]
    result = _parsed_result(json.dumps({"schema": "pack_verifier_result.v1", "valid": False, "errors": many}), dict(base))
    assert result.status == "failed" and len(result.summary()["error_codes"]) == 5
    assert result.summary()["error_count"] == 7
