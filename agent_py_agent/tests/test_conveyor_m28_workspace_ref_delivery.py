"""工作区快照、文件回执与暂存交付的运行回归。"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _record_foreground_wait,
    frozen_workspace_ref,
)
from agent_py_agent.agent.agent_core.tool_loop.deliverable_closeout import (
    deliverable_closeout_block,
    deliverable_report,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_context, request_execution
from agent_py_agent.agent.gateway_parts.business_api import (
    local_business_context,
    register_business_tools,
)
from agent_py_agent.agent.gateway_parts.io import read_json_file_report, write_gateway_request
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.gateway_parts.request_worker import claim_request
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.output_alignment import anchored_output_refs
from agent_py_agent.agent.tooling.registry_workspace import freeze_workspace_ref
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_business_model_tools import agent as agent
from agent_py_agent.tests.test_content_adoption_human_approval import (
    approve_through_original_input,
    execute,
    setup_adoption,
)
from agent_py_agent.tests.test_conveyor_m27_delegable_scope import _fixture_agent
from agent_py_agent.tests.test_gateway_conversation_compact import _agent
from agent_py_agent.tests.test_tools.backends import make_tool_registry


def _workspace_record(root):
    root = root.resolve()
    stat = root.stat()
    identity = (stat.st_dev, stat.st_ino)
    value = sha256(f"{root}\0{identity[0]}\0{identity[1]}".encode()).hexdigest()
    return {"workspace_ref": value, "root": str(root), "root_identity": list(identity), "roots": [str(root)]}


def test_registered_read_file_replays_the_issued_checkpoint(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_bytes("甲\r\n乙\r\n丙".encode())
    registry = make_tool_registry(tmp_path)
    first = execute_registry_test_call(
        registry, "read_file", {"path": "sample.txt", "end_line": 1}, call_id="first",
    )
    assert first.ok, first.output
    checkpoint = first.metadata["handler_details"]["decode_checkpoint"]
    second = execute_registry_test_call(
        registry, "read_file", {"path": "sample.txt", "checkpoint": checkpoint, "end_line": 2}, call_id="resume",
    )
    assert second.ok and second.output.splitlines()[0] == "2: 乙", second.output
    path.write_bytes(b"changed\n")
    stale = execute_registry_test_call(
        registry, "read_file", {"path": "sample.txt", "checkpoint": checkpoint}, call_id="stale",
    )
    assert stale.error_code == "CURSOR_SOURCE_CHANGED"
    extra = execute_registry_test_call(
        registry, "read_file", {"path": "sample.txt", "checkpoint": checkpoint, "unknown": True}, call_id="unknown",
    )
    assert extra.error_code == "TOOL_INVALID_ARGUMENTS"
    chars = execute_registry_test_call(
        registry, "read_file", {"path": "sample.txt", "offset": 1, "max_chars": 3}, call_id="chars",
    )
    assert chars.ok and chars.output.splitlines()[1] == "han", chars.output


def test_relative_registered_write_uses_admitted_workspace_and_receipt(tmp_path):
    frozen, current = tmp_path / "frozen", tmp_path / "current"
    frozen.mkdir()
    current.mkdir()
    registry = make_tool_registry(current)
    result = execute_registry_test_call(
        registry, "write_file", {"path": "report.txt", "content": "ready"},
        write_boundary={"workspace_ref": _workspace_record(frozen)},
    )
    assert (frozen / "report.txt").is_file(), result.output
    assert not (current / "report.txt").exists()
    details = result.metadata["handler_details"]
    assert details["requested_path"] == "report.txt"
    assert details["resolved_path"] == str(frozen / "report.txt")
    assert details["workspace_ref"] == _workspace_record(frozen)["workspace_ref"]


def test_main_required_file_is_not_delivered_while_missing(tmp_path):
    path = tmp_path / "report.txt"
    params = SimpleNamespace(
        context_scope="conversation", task_attributes={"required_file_refs": [str(path)]}, live_archive_state={},
    )
    block = deliverable_closeout_block(params)
    assert block and str(path) in block
    path.mkdir()
    assert deliverable_closeout_block(params)
    path.rmdir()
    path.write_bytes(b"wrong")
    params.task_attributes = {"required_file_refs": [{"path": str(path), "sha256": sha256(b"right").hexdigest()}]}
    assert deliverable_closeout_block(params) is None
    assert not deliverable_report(params)["target_delivered"]
    path.write_bytes(b"right")
    assert deliverable_closeout_block(params) is None
    assert deliverable_report(params)["target_delivered"]


def test_unwritable_output_advertises_child_staging_ref(tmp_path):
    project, child = tmp_path / "project", tmp_path / "child"
    project.mkdir()
    child.mkdir()
    target = project / "report.txt"
    task = SimpleNamespace(
        task_dir=str(child), output_dir=str(child / "output"), allowed_write_roots=[str(child)],
        attributes={"required_file_refs": [str(target)]},
    )
    warning = anchored_output_refs(task).warnings[0]
    assert warning["staging_ref"] == str(child / "output" / "report.txt")
    assert warning["expected_relative_location"] == "report.txt"


def test_accepted_version_lands_from_parent_closeout(agent, tmp_path):
    _, _, args, tool = setup_adoption(agent, tmp_path)
    destination = agent.root / "report.txt"
    child = agent.subagents.create_run(goal="prepare report", thought="prepare", plan=["write"], parent_id="parent",
                                      attributes={"required_file_refs": [str(destination)]})
    staged = Path(child.output_dir) / destination.name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"original material")
    workspace = freeze_workspace_ref(agent.root)
    boundary = {"allowed_write_roots": [str(agent.root)], "workspace_ref": workspace.to_record()}
    child_params = SimpleNamespace(context_scope="task_local", run_id=child.id,
                                   task_attributes=child.attributes, live_archive_state={})
    assert deliverable_closeout_block(child_params, agent=agent) is None
    child_report = deliverable_report(child_params)
    assert child_report["files_written"] and not child_report["target_delivered"]
    assert child_report["staged_files"][0]["sha256"] == sha256(staged.read_bytes()).hexdigest()

    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.processing.mkdir(parents=True)
    path = write_gateway_request(paths, {"id": child.parent_id, "prompt": "land approved version"})
    claim = claim_request(paths, path)
    assert claim is not None
    request_path = claim.path
    request = read_json_file_report(request_path, context="test.load_request").payload
    writer = GatewayTaskBindingWriter(request_path, child.parent_id, request, request["execution_attempt_id"],
                                      workspace_ref=workspace)
    assert writer.bind_runtime_authority({
        "invocation_run_id": child.parent_id, "task_id": "parent-task", "run_id": "parent-run",
        "agent_run_id": child.parent_id, "attempt_id": "parent-attempt",
    })
    agent._current_run_params = replace(agent._current_run_params, request_id=child.parent_id,
                                        conversation_task_binding_callback=writer, workspace_ref=workspace)
    args["staged_file"] = {"child_run_id": child.id, "destination": str(destination)}
    pending = execute(agent, tool, args)
    assert pending.decision.status == "ask"
    assert writer.staged_delivery_contract() is None
    binding = approve_through_original_input(tmp_path, pending.call, agent.home_paths.owner_id)
    adopted = execute(agent, tool, args, [binding]).result
    assert adopted.ok, adopted.output
    context, services = local_business_context(agent, model=True)
    actor = services.actor(context)
    view = services.spaces.call("assets.get", {k: args["params"][k] for k in ("space_id", "asset_id")}, actor=actor)
    assert view["accepted_version"] == args["params"]["version_ref"]
    assert not destination.exists()
    recovered = read_json_file_report(request_path, context="test.recover_staging").payload
    restarted = SimpleAgent(AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"),
        memory_path="memory.jsonl", execution_mode="local_unmanaged",
        business_spaces_enabled=True,
    ), agent.root)
    restarted.config.business_host_ports_enabled = True
    register_business_tools(restarted)
    assert restarted.home_paths.owner_id == child.owner
    assert restarted.subagents.load(child.id).output_dir == child.output_dir
    assert tool.model_spec.name in restarted.tools.tools
    recovered_writer = GatewayTaskBindingWriter(request_path, child.parent_id, recovered, request["execution_attempt_id"])
    restarted._current_run_params = replace(agent._current_run_params, conversation_task_binding_callback=recovered_writer)
    params = SimpleNamespace(
        context_scope="conversation", run_id=child.parent_id,
        live_archive_state={}, write_boundary=boundary,
    )
    block = deliverable_closeout_block(params, agent=restarted)
    outcomes = [(row["state"], row.get("error_code")) for row in deliverable_report(params)["apply_receipts"]]
    assert block is None, outcomes
    assert destination.read_bytes() == b"original material"
    assert deliverable_report(params)["apply_receipts"][0]["state"] == "landed"
    assert deliverable_closeout_block(params, agent=restarted) is None
    assert deliverable_report(params)["apply_receipts"][0]["state"] == "already_landed"
    destination.write_bytes(b"edited")
    assert deliverable_closeout_block(params, agent=restarted)
    report = deliverable_report(params)
    assert report["apply_receipts"][0]["state"] == "conflict"
    assert report["files_written"] and not report["target_delivered"]
    assert destination.read_bytes() == b"edited"
    destination.unlink()
    staged.write_bytes(b"edited source")
    assert deliverable_closeout_block(params, agent=restarted)
    assert deliverable_report(params)["apply_receipts"][0]["state"] == "digest_mismatch"
    assert not destination.exists()


def test_gateway_workspace_survives_compact_and_carries_two_roots(tmp_path, monkeypatch):
    host = _agent(tmp_path, context_tokens=30_000)
    project, other = tmp_path / "project", tmp_path / "second"
    project.mkdir()
    other.mkdir()
    request = {"id": "gw-workspace", "prompt": "整理材料"}
    conversation = request_context.GatewayConversationContext(
        thread_id="thread-workspace", cwd=str(project), runtime_workspace_roots=(str(project), str(other)),
    )
    context = request_context.GatewayAskRunContext(
        host, request, tmp_path / "request.json", tmp_path / "response.json", request["id"], None,
    )
    seen = []

    def run(_prompt, *, params):
        seen.append(params)
        return SimpleNamespace(runtime_status="context_overflow" if len(seen) == 1 else "done")

    def compact(_context, _prompt, current, _params, _rows, *, observer=None):
        return replace(current, cwd=str(other), compact_generation=current.compact_generation + 1)

    monkeypatch.setattr(host, "run", run)
    monkeypatch.setattr(request_execution, "_gateway_compact_overflowing_turn", compact)
    result, _ = request_execution._run_gateway_turn_with_conversation_compact(context, request["prompt"], conversation)
    assert result.runtime_status == "done"
    workspace = frozen_workspace_ref(seen[0])
    assert workspace.root == project and workspace.roots == (project, other)
    assert frozen_workspace_ref(seen[1]) is workspace
    assert frozen_workspace_ref(replace(seen[1], compact_auto_continue_depth=1)) is workspace
    registry = make_tool_registry(other)
    boundary = {"workspace_ref": workspace.to_record()}
    listed = execute_registry_test_call(registry, "list_files", {}, write_boundary=boundary)
    assert listed.ok and listed.metadata["handler_details"]["resolved_path"] == str(project)
    shell = execute_registry_test_call(registry, "run_command", {"command": "pwd"}, write_boundary=boundary)
    assert shell.ok and str(project) in shell.output, shell.output
    other.rename(tmp_path / "old-second")
    other.mkdir()
    denied = execute_registry_test_call(registry, "read_file", {"path": str(other / "report.txt")}, write_boundary=boundary)
    assert denied.error_code == "PATH_OUTSIDE_WORKSPACE"


def test_child_start_consumes_only_the_admitted_workspace(tmp_path):
    host, _, granted = _fixture_agent(tmp_path)
    host._current_run_params.workspace_ref = freeze_workspace_ref(granted)
    child = host.subagents.create_run(params=create_run_params(host, {}, "整理材料", ["write_file"]))
    persisted = host.subagents.load(child.id)
    boundary = host.subagents.runner_context._build_write_boundary(persisted)
    params = RunParams(context_scope="task_local", write_boundary=boundary)
    assert frozen_workspace_ref(params) == host._current_run_params.workspace_ref
    assert frozen_workspace_ref(replace(params, compact_auto_continue_depth=1)).root == granted


def test_waiting_parent_projects_foreground_and_wakeable_root_separately():
    params = SimpleNamespace(context_scope="task_local", run_id="parent-run", live_archive_state={})
    response = SimpleNamespace(runtime_status="unfinished", runtime_reason="SUBAGENTS_ACTIVE")
    _record_foreground_wait(None, params, response)
    assert params.live_archive_state["foreground_work"] == {
        "foreground_ended": True, "work_pending": True, "wakeable_root": "parent-run",
    }
