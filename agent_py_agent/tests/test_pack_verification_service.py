"""能力包 v2 块 3：宿主在写工具之后和收尾时用钉住的原版检查程序核验交付物（第 11 条）。

流程用例用替身检查程序（不起进程、确定性），最后一条用真实沙箱和真实运行器跑通；不起 Gateway、不碰真实 owner home。
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    SandboxUnavailableError,
)
from agent_py_agent.agent.capability import pack_verification_scope, pack_verification_service
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.pack_verification_hooks import (
    attach_post_write_verification,
    capture_baseline_before_tool,
    closeout_rework_block,
    written_paths,
)
from agent_py_agent.agent.capability.pack_verification_ledger import PackVerificationLedger
from agent_py_agent.agent.capability.pack_verification_report import (
    pack_verification_notice_text,
    run_pack_verification_facts,
)
from agent_py_agent.agent.capability.pack_verifier_runner import PackVerificationResult
from agent_py_agent.agent.capability_verifier_consent import (
    verifier_confirmation_details,
    verifier_consent_sha256,
)
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_package import content_bundle

VERIFICATION = {
    "deliverables": [{"id": "delivery", "path_patterns": ["out/**"], "required": True,
                      "field_match": {"format": "json", "field": "schema", "equals": ["delivery.v1"]}}],
    "verifiers": [{"id": "check", "member": "scripts/check.py", "runtime": "python", "applies_to": "delivery",
                   "args": ["{target}"], "timeout_seconds": 5,
                   "inputs": [{"flag": "--source", "source": "task_input", "path_patterns": ["**/*.json"], "required": False,
                               "field_match": {"format": "json", "field": "schema", "equals": ["source.v1"]}},
                              {"flag": "--handoff", "source": "turn_output", "path_patterns": ["**/*.json"],
                               "required": False,
                               "field_match": {"format": "json", "field": "schema", "equals": ["handoff.v1"]}},
                              {"flag": "--peer", "source": "turn_output", "path_patterns": ["out/**"], "required": False,
                               "field_match": {"format": "json", "field": "schema", "equals": ["delivery.v1"]}}]}],
    "input_policy": "preserve_originals"}
# 真实沙箱用例里的检查程序：交付物里 bad=true 就报一条带位置的错误。
REAL_CHECKER = (
    "import json, sys\n"
    "bad = json.load(open(sys.argv[1])).get('bad') is True\n"
    "print(json.dumps({'schema': 'pack_verifier_result.v1', 'valid': not bad,\n"
    "                  'errors': [{'code': 'placeholder_text', 'location': 'SH01.start_state'}] if bad else [],\n"
    "                  'warnings': [{'code': 'creative_quality_and_media_not_checked'}]}))\n")


class _Repo:
    def __init__(self):
        self.events = []

    def agent_run_for_run_id(self, run_id):
        return {"agent_run_id": "ar-1", "task_run_id": "tr-1"} if run_id == "run-1" else None

    def append_event(self, **kwargs):
        self.events.append(kwargs)


def _install(tmp_path, checker=REAL_CHECKER):
    def change(manifest):
        manifest["capability"]["verification"] = VERIFICATION
    bundle = content_bundle(files={"CAPABILITY.md": b"# c\n", "scripts/check.py": checker.encode()}, change=change)
    owner = resolve_owner_home(tmp_path / "home")
    store = PluginInstallStore(owner)
    entry = store.install(PluginInstallRequest(inspect_plugin_package(bundle), "install", 0)).installation
    details = verifier_confirmation_details(entry.manifest, entry.package_sha256)
    store.change_activation(PluginActivationRequest("enable", entry.revision, PluginContentActivation(
        "enable", entry.manifest.plugin_id, entry.package_sha256, entry.revision, entry.settings_revision,
        verifier_consent_sha256=verifier_consent_sha256(details))))
    entry = store.snapshot()[0]
    ref = {"kind": "capability_package", "stable_id": f"capability:{entry.manifest.plugin_id}",
           "package_id": entry.manifest.plugin_id, "activation_id": entry.activation_id,
           "content_sha256": entry.package_sha256}
    return owner, ref


# LLM: 块 3 起的流程用例共用的环境：真实安装并同意的包、任务根、工作区、钉住引用、开关打开的替身 Agent；installed 可以换成
#   用别的检查程序安装的 (owner, ref)。只改 monkeypatch 能撤销的模块属性，不碰真实 owner home。
# 函数用途: 搭一套宿主核验的测试环境（普通函数，供各测试文件的夹具调用）。
def build_env(tmp_path, monkeypatch, installed=None):
    owner, ref = installed or _install(tmp_path)
    workspace, task_root = tmp_path / "ws", tmp_path / "task"
    workspace.mkdir()
    pins = [ref]
    monkeypatch.setattr(pack_verification_service, "verification_owner", lambda agent: owner)
    monkeypatch.setattr(pack_verification_scope, "pinned_package_references", lambda agent, attrs: list(pins))
    monkeypatch.setattr(pack_verification_service, "_workspace_root", lambda agent, params: workspace)
    monkeypatch.setattr("agent_py_agent.agent.agent_core.run_task_workspace_writer.current_run_task_workspace_root",
                        lambda agent, params=None: task_root)
    repo = _Repo()
    agent = types.SimpleNamespace(
        _capability_config_runtime_snapshot=types.SimpleNamespace(
            config=CapabilityConfig(capability_pack_host_verification_enabled=True)),
        subagents=types.SimpleNamespace(runtime_db=repo))
    params = types.SimpleNamespace(run_id="run-1", attempt_id="att-1", task_attributes={}, tool_runtime_snapshot=None,
                                   tool_context=[], executed_tools=[], context_scope="default", live_archive_state={})
    return types.SimpleNamespace(agent=agent, params=params, workspace=workspace, task_root=task_root, repo=repo,
                                 pins=pins, ledger=PackVerificationLedger(task_root / "data/pack_verification/run-1.jsonl"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    return build_env(tmp_path, monkeypatch)


# LLM: 交付物里 bad=true 就报一条带位置的 placeholder_text；返回记录下来的请求列表，供断言调用次数和输入。
# 函数用途: 把运行器换成不起进程的确定性替身（普通函数，供各测试文件的夹具调用）。
def install_fake_runner(monkeypatch):
    calls = []

    def run(request):
        calls.append(request)
        bad = json.loads(request.target.read_text()).get("bad") is True
        target = request.target.relative_to(request.workspace_root).as_posix()
        return PackVerificationResult(
            request.verifier_id, request.installation.manifest.plugin_id, "failed" if bad else "passed",
            package_version=request.installation.manifest.version, member="scripts/check.py", target=target,
            valid=not bad, error_counts={"placeholder_text": 1} if bad else {},
            warning_counts={"creative_quality_and_media_not_checked": 1},
            error_samples=({"code": "placeholder_text", "location": "SH01.start_state"},) if bad else ())
    monkeypatch.setattr(pack_verification_service, "run_pack_verifier", run)
    return calls


@pytest.fixture
def fake_runner(monkeypatch):
    return install_fake_runner(monkeypatch)


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def _tool_result(path: Path) -> ToolResult:
    return ToolResult("call-1", "write_file", "succeeded",
                      metadata={"handler_details": {"path": str(path), "target_path": str(path)}})


def test_switch_off_is_inert(env, fake_runner):
    env.agent._capability_config_runtime_snapshot.config = CapabilityConfig()
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    target = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    result = _tool_result(target)
    assert attach_post_write_verification(env.agent, env.params, result) is result
    assert closeout_rework_block(env.agent, env.params) == ""
    assert not env.task_root.exists() and fake_runner == []


def test_baseline_is_captured_once_and_only_before_mutating_tools(env):
    capture_baseline_before_tool(env.agent, env.params, "read_file")
    assert env.ledger.baseline() is None
    _write(env.workspace / "in/source.json", {"schema": "source.v1"})
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    _write(env.workspace / "in/late.json", {"schema": "source.v1"})
    capture_baseline_before_tool(env.agent, env.params, "apply_patch")
    baselines = [row for row in env.ledger.records() if row["kind"] == "baseline"]
    assert len(baselines) == 1 and list(baselines[0]["scan"]["files"]) == ["in/source.json"]
    assert (env.ledger.path.stat().st_mode & 0o777) == 0o600


def test_post_write_checks_matching_deliverable_and_attaches_bounded_summary(env, fake_runner):
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    target = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    attached = attach_post_write_verification(env.agent, env.params, _tool_result(target))
    [summary] = attached.metadata["handler_details"]["pack_verification"]
    assert summary["status"] == "failed" and summary["error_codes"] == ["placeholder_text"]
    assert summary["warning_codes"] == ["creative_quality_and_media_not_checked"]
    assert summary["error_samples"] == [{"code": "placeholder_text", "location": "SH01.start_state"}]
    assert [event["event_type"] for event in env.repo.events] == ["pack_verification_completed"]
    other = _write(env.workspace / "out/notes.json", {"schema": "other"})
    untouched = _tool_result(other)
    assert attach_post_write_verification(env.agent, env.params, untouched) is untouched
    outside = _write(env.workspace.parent / "elsewhere.json", {"schema": "delivery.v1"})
    assert attach_post_write_verification(env.agent, env.params, _tool_result(outside)).metadata == \
        _tool_result(outside).metadata
    failed_write = ToolResult("call-2", "write_file", "failed", error_code="X",
                              metadata={"handler_details": {"path": str(target)}})
    assert attach_post_write_verification(env.agent, env.params, failed_write) is failed_write
    assert len(fake_runner) == 1


def test_inputs_are_resolved_by_source_and_must_be_unique(env, fake_runner):
    source = _write(env.workspace / "in/source.json", {"schema": "source.v1"})
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    handoff = _write(env.workspace / "out/handoff.json", {"schema": "handoff.v1"})
    target = _write(env.workspace / "out/d.json", {"schema": "delivery.v1"})
    attach_post_write_verification(env.agent, env.params, _tool_result(target))
    assert dict(fake_runner[-1].inputs) == {"--source": source, "--handoff": handoff}, "目标本身不算 --peer"
    peer = _write(env.workspace / "out/peer.json", {"schema": "delivery.v1"})
    _write(target, {"schema": "delivery.v1", "v": 1})
    attach_post_write_verification(env.agent, env.params, _tool_result(target))
    assert dict(fake_runner[-1].inputs) == {"--source": source, "--handoff": handoff, "--peer": peer}
    peer.unlink()
    source.write_text(json.dumps({"schema": "source.v1", "edited": True}))
    _write(target, {"schema": "delivery.v1", "v": 2})
    attach_post_write_verification(env.agent, env.params, _tool_result(target))
    assert dict(fake_runner[-1].inputs) == {"--handoff": handoff}, "被就地改过的输入不算任务开始时的原件"
    _write(env.workspace / "out/handoff2.json", {"schema": "handoff.v1"})
    _write(target, {"schema": "delivery.v1", "v": 3})
    attach_post_write_verification(env.agent, env.params, _tool_result(target))
    assert dict(fake_runner[-1].inputs) == {}, "匹配到两个就不交"
    last = [row for row in env.ledger.records() if row["kind"] == "result"][-1]
    assert last["input_matches"] == {"--source": 0, "--handoff": 2, "--peer": 0}


def test_closeout_checks_shell_written_files_reworks_once_and_reuses_results(env, fake_runner):
    capture_baseline_before_tool(env.agent, env.params, "run_command")
    assert env.ledger.baseline() is None, "没有运行策略声明 mutates_workspace 的工具不记基线"
    shell = types.SimpleNamespace(runtime_policy=types.SimpleNamespace(mutates_workspace=True))
    env.params.tool_runtime_snapshot = types.SimpleNamespace(runtime=lambda name: shell if name == "run_command" else None)
    _write(env.workspace / "out/old.json", {"schema": "delivery.v1", "bad": True})
    capture_baseline_before_tool(env.agent, env.params, "run_command")
    target = _write(env.workspace / "out/nested/d.json", {"schema": "delivery.v1", "bad": True})
    text = closeout_rework_block(env.agent, env.params)
    assert "out/nested/d.json" in text and "placeholder_text @ SH01.start_state" in text and len(fake_runner) == 1
    assert "out/old.json" not in text, "基线里原有、本回合没动的交付物不查"
    assert closeout_rework_block(env.agent, env.params) == "" and len(fake_runner) == 1, "内容没变就复用，返工只有一次"
    _write(target, {"schema": "delivery.v1"})
    assert closeout_rework_block(env.agent, env.params) == "" and len(fake_runner) == 2
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["closeout_checked"] and facts["rework_count"] == 1
    assert [item["status"] for item in facts["results"]] == ["passed"], "只报最后一次收尾时的内容状态"


def test_closeout_without_baseline_or_pins_checks_nothing(env, fake_runner):
    _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    assert closeout_rework_block(env.agent, env.params) == ""
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    stale = {**env.pins[0], "content_sha256": "0" * 64}
    env.pins[:] = [stale]
    _write(env.workspace / "out/e.json", {"schema": "delivery.v1", "bad": True})
    assert closeout_rework_block(env.agent, env.params) == "", "pin 的整包摘要和安装项对不上就不核验"
    env.pins.clear()
    assert closeout_rework_block(env.agent, env.params) == "" and fake_runner == []


def test_no_rework_when_the_rework_cannot_be_recorded(env, fake_runner, monkeypatch):
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    original = PackVerificationLedger.append
    monkeypatch.setattr(PackVerificationLedger, "append",
                        lambda self, record: False if record.get("kind") == "rework" else original(self, record))
    assert closeout_rework_block(env.agent, env.params) == ""


def test_facts_and_notice_come_only_from_the_ledger(env, fake_runner):
    assert run_pack_verification_facts(env.agent, env.params) is None
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    target = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    attach_post_write_verification(env.agent, env.params, _tool_result(target))
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["schema"] == "pack_verifications.v1" and not facts["closeout_checked"]
    text = pack_verification_notice_text(facts)
    assert "out/d.json 无效，错误 1 条（placeholder_text）" in text and "没有正常收尾" in text
    closeout_rework_block(env.agent, env.params)
    closed = pack_verification_notice_text(run_pack_verification_facts(env.agent, env.params))
    assert "没有正常收尾" not in closed and len(closed) <= 480


def test_final_facts_follow_the_last_closeout_not_stale_writes(env, fake_runner):
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    target = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    attach_post_write_verification(env.agent, env.params, _tool_result(target))
    target.unlink()
    assert closeout_rework_block(env.agent, env.params) == ""
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["closeout_checked"] and facts["results"] == [], "被删掉的文件不再报写入时的结论"


def test_no_tool_calls_decision_uses_the_pack_closeout(env, fake_runner):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        _no_tool_calls_decision,
        _NoToolCallsRequest,
    )

    capture_baseline_before_tool(env.agent, env.params, "write_file")
    _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    response = types.SimpleNamespace(text="完成了，检查都通过。", truncated=False, runtime_status="ok",
                                     runtime_reason="", runtime_source="")

    def decide():
        return _no_tool_calls_decision(_NoToolCallsRequest(env.agent, env.params, response, ToolLoopRepairCounters(),
                                                           False))
    assert decide().action == "continue" and "placeholder_text" in env.params.tool_context[-1]
    assert decide().action == "break" and len(env.params.tool_context) == 1


def test_receipt_rendering_archive_and_written_paths():
    from agent_py_agent.agent.agent_core.tool_call_archive_record import _compact_result_envelope
    from agent_py_agent.agent.tooling.runtime_facts import render_tool_runtime_facts

    summary = [{"verifier_id": "check", "status": "failed", "error_codes": ["placeholder_text"]}]
    assert "[pack-verification]" in render_tool_runtime_facts({"pack_verification": summary})
    assert render_tool_runtime_facts({"pack_verification": []}) == ""
    result = ToolResult("call-3", "write_file", "succeeded",
                        metadata={"handler_details": {"path": "/x", "pack_verification": summary}})
    assert _compact_result_envelope(result)["pack_verification"] == summary
    details = {"path": "/w/a.json", "artifact_ref": "/w/a.json", "artifact_refs": [
        {"path": "/w/b.json", "status": "ready"}, {"path": "/w/old.json", "status": "deleted"}, {"path": "rel.json"}]}
    assert written_paths(details) == [Path("/w/a.json"), Path("/w/b.json")]


def test_gateway_notice_is_built_from_facts(monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_pack_verification_notice as notice_module

    queued = []
    monkeypatch.setattr(notice_module, "queue_host_notice",
                        lambda store, thread_id, notice, replace_same_code: queued.append((thread_id, notice)) or True)
    facts = {"results": [{"package_id": "story-content", "package_version": "1.0", "member": "scripts/check.py",
                          "target": "out/d.json", "status": "passed", "warning_counts": {"w": 2}}],
             "closeout_checked": True, "rework_count": 0}
    context = types.SimpleNamespace(agent=types.SimpleNamespace(conversation_store=object()))
    conversation = types.SimpleNamespace(thread_id="thread-1")
    [notice] = notice_module.queue_pack_verification_notice(context, conversation, types.SimpleNamespace(pack_verifications=facts))
    assert notice.source == "pack_verification" and "out/d.json 有效，警告 2 条" in notice.text
    assert queued[0][0] == "thread-1"
    assert notice_module.queue_pack_verification_notice(context, conversation, types.SimpleNamespace()) == ()


def test_switch_defaults_off_and_is_an_admin_only_boundary():
    from agent_py_agent.agent.settings.user_config_capability import (
        BOUNDARY_KEYS,
        USER_SETTINGS_BOUNDARY_KEYS,
    )

    assert CapabilityConfig().capability_pack_host_verification_enabled is False
    assert "capability_pack_host_verification_enabled" in BOUNDARY_KEYS
    assert "capability_pack_host_verification_enabled" in USER_SETTINGS_BOUNDARY_KEYS


def test_pinned_package_references_reads_main_task_and_child_pins(monkeypatch):
    from agent_py_agent.agent.capability import task_references

    ref = {"kind": "capability_package", "stable_id": "capability:p", "package_id": "p",
           "activation_id": "a" * 64, "content_sha256": "b" * 64}
    link = types.SimpleNamespace(thread_id="t1", skill_snapshot_refs=[ref, {"stable_id": "s", "content_sha256": "c" * 64}])
    store = types.SimpleNamespace(tasks=types.SimpleNamespace(load_report=lambda task_id: (link, "")))
    agent = types.SimpleNamespace(conversation_store=store)
    monkeypatch.setattr(task_references, "current_subagent_run_id", lambda agent: "")
    attrs = {"conversation_task_id": "task-1", "conversation_thread_id": "t1"}
    assert [row["package_id"] for row in task_references.pinned_package_references(agent, attrs)] == ["p"]
    assert task_references.pinned_package_references(agent, {**attrs, "conversation_thread_id": "other"}) == []
    assert task_references.pinned_package_references(agent, {}) == []
    child = types.SimpleNamespace(attributes={"skill_snapshot_refs": [ref]}, capability_grants=())
    monkeypatch.setattr(task_references, "current_subagent_run_id", lambda agent: "run-c")
    agent.subagents = types.SimpleNamespace(load=lambda run_id: child)
    assert [row["package_id"] for row in task_references.pinned_package_references(agent, {})] == ["p"]


def test_real_sandbox_post_write_and_closeout(env):
    try:
        AttemptExecutionSandbox(AttemptSandboxSpec(env.workspace, env.workspace, env.workspace, env.workspace,
                                                   network_access=False)).require_ready()
    except SandboxUnavailableError:
        pytest.skip("本机平台沙箱不可用")
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    target = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    attached = attach_post_write_verification(env.agent, env.params, _tool_result(target))
    [summary] = attached.metadata["handler_details"]["pack_verification"]
    assert summary["status"] == "failed", summary
    assert summary["error_samples"] == [{"code": "placeholder_text", "location": "SH01.start_state"}]
    assert "placeholder_text @ SH01.start_state" in closeout_rework_block(env.agent, env.params)
    _write(target, {"schema": "delivery.v1"})
    assert closeout_rework_block(env.agent, env.params) == ""
    facts = run_pack_verification_facts(env.agent, env.params)
    assert [item["status"] for item in facts["results"]] == ["passed"] and len(facts["results"][0]["member_sha256"]) == 64


# LLM: 光有钩子正确还不够——要证明工具执行唯一缝隙真的调用了它们：基线钩子在 handler 之前（pre_handler_gate 放行时），
#   写后钩子作用在最终结果上。守卫、审计、追踪等与本块无关的重件用替身挡掉，执行器是只调 pre_handler_gate 的假执行器。
# 函数用途: 验证 execute_traced_tool_call 把两个宿主核验钩子接在正确位置。
def test_tool_execution_seam_calls_both_hooks(monkeypatch, tmp_path):
    from dataclasses import dataclass

    from agent_py_agent.agent.agent_core import tool_call_runtime
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall

    order = []
    hooks = types.SimpleNamespace(
        capture_baseline_before_tool=lambda agent, params, tool: order.append(("baseline", tool)),
        attach_post_write_verification=lambda agent, params, result: order.append(("post", result)) or "attached")
    monkeypatch.setattr(tool_call_runtime, "_pack_verification_hooks", lambda: hooks)
    monkeypatch.setattr(tool_call_runtime, "guarded_tool_call_result", lambda request: None)
    monkeypatch.setattr(tool_call_runtime, "_record_passive_verification", lambda agent, call, result: result)
    monkeypatch.setattr(tool_call_runtime, "audit_privileged_tool_call", lambda *args: None)
    monkeypatch.setattr(tool_call_runtime, "trace_runner_tool_call_finished", lambda *args: None)
    monkeypatch.setattr(tool_call_runtime, "write_boundary_with_runtime_ledger", lambda agent, params: {})
    monkeypatch.setattr("agent_py_agent.agent.conversation.process_events.process_completion_target", lambda *a: None)
    monkeypatch.setattr("agent_py_agent.agent.agent_core.tool_loop.recovery.runtime_run_scope",
                        lambda *a: types.SimpleNamespace(to_dict=dict))

    @dataclass(frozen=True)
    class Execution:
        call: object
        result: object

    def execute_tool(call, **kwargs):
        assert kwargs["pre_handler_gate"](call) is None
        order.append(("handler", call.tool_name))
        return Execution(call, "raw")

    call = ToolCall("c1", "write_file", {"path": str(tmp_path / "a.json")}, "native", "sha256:" + "0" * 64,
                    "run-1", "turn-1", "att-1")
    params = types.SimpleNamespace(tool_runtime_snapshot=None, task_attributes={}, cancellation_token=None,
                                   one_shot_tool_calls=set())
    agent = types.SimpleNamespace(tools=types.SimpleNamespace(execute_tool=execute_tool))
    request = types.SimpleNamespace(params=params, tool_rounds=0, idx=0, model_call=None)
    execution = tool_call_runtime.execute_traced_tool_call(
        tool_call_runtime.ToolCallRuntimeRequest(agent=agent, request=request, call=call))
    assert order == [("baseline", "write_file"), ("handler", "write_file"), ("post", "raw")]
    assert execution.result == "attached"


# 9b 复审 F4 的探针改成的用例：检查程序把宿主路径写进 location，宿主转给模型前必须脱敏。
LEAKY_CHECKER = (
    "import json, os, sys\n"
    "home = os.path.expanduser('~')\n"
    "print(json.dumps({'schema': 'pack_verifier_result.v1', 'valid': False, 'warnings': [],\n"
    "                  'errors': [{'code': 'argv_target', 'location': sys.argv[1]},\n"
    "                             {'code': 'own_file', 'location': __file__},\n"
    "                             {'code': 'interpreter', 'location': sys.executable},\n"
    "                             {'code': 'home', 'location': 'see ' + home + '/notes'},\n"
    "                             {'code': 'pointer', 'location': '/shots/0/start_state'}]}))\n")


def test_location_host_paths_are_redacted_before_reaching_the_model(tmp_path, monkeypatch):
    try:
        AttemptExecutionSandbox(AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path, network_access=False)).require_ready()
    except SandboxUnavailableError:
        pytest.skip("本机平台沙箱不可用")
    owner, ref = _install(tmp_path, checker=LEAKY_CHECKER)
    env = build_env(tmp_path, monkeypatch, installed=(owner, ref))
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    target = _write(env.workspace / "out/d.json", {"schema": "delivery.v1"})
    attached = attach_post_write_verification(env.agent, env.params, _tool_result(target))
    [summary] = attached.metadata["handler_details"]["pack_verification"]
    rework = closeout_rework_block(env.agent, env.params)
    ledger_text = env.ledger.path.read_text()
    last_result = [row for row in map(json.loads, ledger_text.splitlines()) if row["kind"] == "result"][-1]
    samples = {item["code"]: item["location"] for item in last_result["fact"]["error_samples"]}
    assert samples == {"argv_target": "out/d.json", "own_file": "<verifier>/verifier.py", "interpreter": "<python>",
                       "home": "<redacted>", "pointer": "/shots/0/start_state"}, samples
    for text in (json.dumps(summary, ensure_ascii=False), rework, ledger_text):
        assert str(env.workspace) not in text and str(Path.home()) not in text and "pack-verifier-" not in text


def test_redact_location_keeps_json_pointers_and_redacts_host_paths():
    from agent_py_agent.agent.capability.pack_verifier_redaction import redact_location

    replacements = [("/w/ws/out/d.json", "out/d.json"), ("/w/ws/", "")]
    assert redact_location("/w/ws/out/d.json:3", replacements) == "out/d.json:3"
    assert redact_location("/references/0/id", replacements) == "/references/0/id"
    assert redact_location("SH01.start_state", replacements) == "SH01.start_state"
    assert redact_location("~/x", replacements) == "<redacted>"
    assert redact_location("C:\\Users\\x", replacements) == "<redacted>"
    assert redact_location(f"in {Path.home()}/a", replacements) == "<redacted>"


def test_truncated_baseline_only_reworks_files_proven_written_this_run(env, fake_runner, monkeypatch):
    from agent_py_agent.agent.capability import pack_verification_matching as matching

    for index in range(3):
        _write(env.workspace / f"in/{index}.json", {"schema": "other"})
    old = _write(env.workspace / "out/old.json", {"schema": "delivery.v1", "bad": True})
    monkeypatch.setattr(matching, "MAX_SCAN_MATCHED_FILES_COUNT", 2)
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    monkeypatch.setattr(matching, "MAX_SCAN_MATCHED_FILES_COUNT", 512)
    assert "out/old.json" not in env.ledger.baseline()["scan"]["files"] and env.ledger.baseline()["scan"]["truncated"]
    assert closeout_rework_block(env.agent, env.params) == "", "漏扫的老文件只记事实，不返工"
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["uncertain_targets"] == ["out/old.json"] and [item["target"] for item in facts["results"]] == ["out/old.json"]
    new = _write(env.workspace / "out/new.json", {"schema": "delivery.v1", "bad": True})
    attach_post_write_verification(env.agent, env.params, _tool_result(new))
    assert "out/new.json" in closeout_rework_block(env.agent, env.params), "有写工具回执证明的新文件照常返工"
    assert old.exists()


def test_current_scan_truncation_is_recorded(env, fake_runner, monkeypatch):
    from agent_py_agent.agent.capability import pack_verification_matching as matching

    capture_baseline_before_tool(env.agent, env.params, "write_file")
    for index in range(3):
        _write(env.workspace / f"out/{index}.json", {"schema": "delivery.v1"})
    monkeypatch.setattr(matching, "MAX_SCAN_MATCHED_FILES_COUNT", 2)
    closeout_rework_block(env.agent, env.params)
    assert run_pack_verification_facts(env.agent, env.params)["current_truncated"] is True


def test_final_facts_skip_the_ledger_when_switch_is_off(env, fake_runner, monkeypatch):
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    attach_post_write_verification(env.agent, env.params, _tool_result(_write(env.workspace / "out/d.json", {"schema": "delivery.v1"})))
    assert run_pack_verification_facts(env.agent, env.params) is not None
    env.agent._capability_config_runtime_snapshot.config = CapabilityConfig()
    monkeypatch.setattr(PackVerificationLedger, "records", lambda self: pytest.fail("开关关着不该读账本"))
    assert run_pack_verification_facts(env.agent, env.params) is None
