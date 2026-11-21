"""工作区快照、文件回执与暂存交付的运行回归。"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from agent_py_agent.agent.tooling.registry_workspace import freeze_workspace_ref, read_workspace_ref
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


# ---- B4-M28-P1-01..05: staging identity, landing replay, rejected adoption, digest authority, legacy cwd ----

def _assets(agent, contents):
    from io import BytesIO

    from agent_py_agent.agent.gateway_parts.business_artifacts_http import upload_artifact

    agent.config.business_host_ports_enabled = True
    context, services = local_business_context(agent, model=True)
    actor = services.actor(context)
    sid = services.spaces.create({"name": "暂存采用"}, actor=actor, idempotency_key="m28-staged-space-key")["space_id"]
    rows = []
    for index, content in enumerate(contents):
        value = upload_artifact(BytesIO(content), {"space_id": sid, "idempotency_key": f"m28-staged-upload-{index}"},
                                f"material-{index}.txt", context, services)
        ref = {key: value[key] for key in ("artifact_id", "version_ref", "content_sha256")}
        asset = services.spaces.call("assets.register", {"space_id": sid, "name": f"资料{index}", "kind": "document",
                                                         "artifact_ref": ref}, actor=actor,
                                     idempotency_key=f"m28-staged-register-{index}")
        rows.append({"action": "assets.versions.accept", "request_id": f"m28-request-{index}",
                     "idempotency_key": f"m28-staged-adopt-{index}", "expected_revision": 0,
                     "params": {"space_id": sid, "asset_id": asset["asset_id"], "version_ref": ref["version_ref"]}})
    register_business_tools(agent)
    return services, actor, rows, agent.tools.tools["business_assets_versions_accept"]


def _adopt(agent, tool, args, approval_dir, operation):
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    def run(approved):
        return execute_canonical_test_call(
            agent.root, tools={tool.model_spec.name: tool}, tool_name=tool.model_spec.name, arguments=args,
            write_boundary={"approved_actions": list(approved)}, operation_id=operation,
            idempotency_key=f"{operation}-key", operation_owner_id=agent.home_paths.owner_id)

    pending = run(())
    assert pending.decision.status == "ask", pending
    approval_dir.mkdir(parents=True, exist_ok=True)
    return run([approve_through_original_input(approval_dir, pending.call, agent.home_paths.owner_id)]).result


def _parent_turn(agent, tmp_path, parent_id, workspace):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.processing.mkdir(parents=True, exist_ok=True)
    claim = claim_request(paths, write_gateway_request(paths, {"id": parent_id, "prompt": "land approved files"}))
    assert claim is not None
    request = read_json_file_report(claim.path, context="test.load_request").payload
    writer = GatewayTaskBindingWriter(claim.path, parent_id, request, request["execution_attempt_id"],
                                      workspace_ref=workspace)
    assert writer.bind_runtime_authority({
        "invocation_run_id": parent_id, "task_id": "parent-task", "run_id": "parent-run",
        "agent_run_id": parent_id, "attempt_id": "parent-attempt",
    })
    agent._current_run_params = replace(agent._current_run_params, request_id=parent_id,
                                        conversation_task_binding_callback=writer, workspace_ref=workspace)
    return writer, claim.path, request


def _staged_child(agent, destinations, contents):
    child = agent.subagents.create_run(goal="prepare files", thought="prepare", plan=["write"], parent_id="parent",
                                      attributes={"required_file_refs": [str(path) for path in destinations]})
    refs = {row["target_path"]: row["staging_ref"] for row in anchored_output_refs(child).warnings
            if "staging_ref" in row}
    for destination, content in zip(destinations, contents):
        if content is not None:
            staged = Path(refs[str(destination)])
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
    return child, refs


def _closeout_params(boundary, attributes=None):
    return SimpleNamespace(context_scope="conversation", run_id="parent", live_archive_state={},
                           write_boundary=boundary, task_attributes=attributes)


def _receipts(params):
    return {row["destination"]: row for row in deliverable_report(params)["apply_receipts"]}


def test_same_named_and_extensionless_declarations_keep_distinct_staging(tmp_path):
    from agent_py_agent.agent.subagents.services.output_alignment import staged_output_manifest

    project, child_dir = tmp_path / "project", tmp_path / "child"
    project.mkdir()
    child_dir.mkdir()
    west, east, license_file = project / "west" / "report.txt", project / "east" / "report.txt", project / "LICENSE"
    task = SimpleNamespace(
        id="child-1", task_dir=str(child_dir), output_dir=str(child_dir / "output"),
        allowed_write_roots=[str(child_dir)],
        attributes={"required_file_refs": [str(west), str(east), "LICENSE", str(project / "west" / ".." / "west"
                                                                                 / "report.txt")]},
        workspace_ref_record=_workspace_record(project),
    )
    staging = {row["target_path"]: row["staging_ref"] for row in anchored_output_refs(task).warnings
               if "staging_ref" in row}
    output = child_dir / "output"
    assert staging == {str(west): str(output / "west" / "report.txt"), str(east): str(output / "east" / "report.txt"),
                       str(license_file.resolve()): str(output / "LICENSE")}
    Path(staging[str(west)]).parent.mkdir(parents=True)
    Path(staging[str(west)]).write_bytes(b"west bytes")
    rows = {row["destination"]: row for row in staged_output_manifest(task)}
    assert len(rows) == 3, "a repeated target reuses its manifest row"
    assert rows[str(west)]["files_written"] and rows[str(west)]["sha256"] == sha256(b"west bytes").hexdigest()
    assert not rows[str(east)]["files_written"] and not rows[str(license_file.resolve())]["files_written"]


def test_staging_locations_keep_unique_names_and_honour_unambiguous_hints():
    from agent_py_agent.agent.path_access_policy import staging_relative_locations

    assert staging_relative_locations(["/p/report.txt"]) == {"/p/report.txt": "report.txt"}
    assert staging_relative_locations(["/a/out", "/b/out"], {"/a/out": "out", "/b/out": "out"}) == {
        "/a/out": "a/out", "/b/out": "b/out"}
    assert staging_relative_locations(["/a/x.txt", "/a/y.txt"], {"/a/x.txt": "keep/x.txt"}) == {
        "/a/x.txt": "keep/x.txt", "/a/y.txt": "y.txt"}


@pytest.mark.parametrize("reverse", [False, True])
def test_staging_files_coexist_and_land_in_both_write_orders(agent, tmp_path, reverse):
    first = agent.root / "package" / "app" / "LICENSE"
    second = agent.root / "package" / "lib" / "LICENSE"
    third = agent.root / "bin" / "app"
    entries = [(first, b"application license"), (second, b"library license"), (third, b"binary")]
    if reverse:
        entries.reverse()
    destinations, contents = zip(*entries, strict=True)
    for destination in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
    _, _, rows, tool = _assets(agent, contents)
    child, refs = _staged_child(agent, destinations, contents)
    assert child.staged_workspace_outputs() == agent.subagents.load(child.id).staged_workspace_outputs()
    manifest = {row["destination"]: row for row in child.staged_workspace_outputs()}
    assert manifest[str(second)]["expected_relative_location"] == "lib/LICENSE"
    for destination, content in entries:
        row = manifest[str(destination)]
        assert row["files_written"] and row["sha256"] == sha256(content).hexdigest()
        assert row["staging_ref"] == refs[str(destination)]
    workspace = freeze_workspace_ref(agent.root)
    _parent_turn(agent, tmp_path, child.parent_id, workspace)
    for index, destination in enumerate(destinations):
        args = {**rows[index], "staged_file": {"child_run_id": child.id, "destination": str(destination)}}
        assert _adopt(agent, tool, args, tmp_path / f"approval-{index}", f"p6-op-{index}").ok
    params = _closeout_params({"allowed_write_roots": [str(agent.root)], "workspace_ref": workspace.to_record()})
    assert deliverable_closeout_block(params, agent=agent) is None, deliverable_report(params)
    assert [destination.read_bytes() for destination in destinations] == list(contents)


def test_staging_hints_and_generated_locations_are_jointly_prefix_free():
    from agent_py_agent.agent.path_access_policy import staging_relative_locations

    targets = ["/pkg/app/LICENSE", "/pkg/lib/LICENSE", "/bin/app"]
    hints = {targets[0]: "app/LICENSE", targets[1]: "persisted/LICENSE", targets[2]: "app"}
    locations = staging_relative_locations(targets, hints)
    assert locations == staging_relative_locations(list(reversed(targets)) + targets[:1], hints)
    assert locations[targets[1]] == hints[targets[1]]
    paths = [Path(path) for path in locations.values()]
    assert all(not left.is_relative_to(right) for left in paths for right in paths if left is not right)


def test_legacy_staging_keeps_a_verified_file_when_its_old_path_blocks_a_child(agent):
    directories = [agent.root / "package" / name / "LICENSE" for name in ("app", "lib")]
    executable = agent.root / "bin" / "app"
    child, _ = _staged_child(agent, directories + [executable], [None, None, None])
    legacy = Path(child.output_dir) / "app"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"existing executable")
    reloaded = agent.subagents.load(child.id)
    rows = {row["destination"]: row for row in reloaded.staged_workspace_outputs()}
    assert rows[str(executable)]["staging_ref"] == str(legacy)
    assert rows[str(executable)]["sha256"] == sha256(b"existing executable").hexdigest()
    for destination in directories:
        path = Path(rows[str(destination)]["staging_ref"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"license")
    settled = {row["destination"]: row for row in agent.subagents.load(child.id).staged_workspace_outputs()}
    assert all(row["files_written"] and row["staging_ref"] == rows[key]["staging_ref"]
               for key, row in settled.items())
    assert legacy.read_bytes() == b"existing executable"


def test_staging_compares_equivalent_macos_path_components(monkeypatch):
    from agent_py_agent.agent import path_access_policy

    monkeypatch.setattr(path_access_policy, "sys", SimpleNamespace(platform="darwin"))
    targets = ["/package/café/LICENSE", "/other/lib/LICENSE", "/bin/CAFE\u0301"]
    locations = path_access_policy.staging_relative_locations(targets)
    assert locations[targets[0]] == "café/LICENSE"
    assert locations[targets[1]] == "lib/LICENSE"
    assert locations[targets[2]] == "bin/CAFE\u0301"


def test_same_named_children_land_their_own_exact_versions(agent, tmp_path):
    west, east = agent.root / "west" / "report.txt", agent.root / "east" / "report.txt"
    west.parent.mkdir()
    east.parent.mkdir()
    contents = [b"west material", b"east material"]
    _, _, rows, tool = _assets(agent, contents)
    child, refs = _staged_child(agent, [west, east], [contents[0], None])
    assert refs[str(west)] != refs[str(east)]
    manifest = {row["destination"]: row for row in child.staged_workspace_outputs()}
    assert manifest[str(west)]["files_written"] and not manifest[str(east)]["files_written"]
    Path(refs[str(east)]).parent.mkdir(parents=True, exist_ok=True)
    Path(refs[str(east)]).write_bytes(contents[1])
    workspace = freeze_workspace_ref(agent.root)
    _parent_turn(agent, tmp_path, child.parent_id, workspace)
    for index, destination in enumerate((west, east)):
        args = {**rows[index], "staged_file": {"child_run_id": child.id, "destination": str(destination)}}
        assert _adopt(agent, tool, args, tmp_path / f"approval-{index}", f"m28-op-{index}").ok
    params = _closeout_params({"allowed_write_roots": [str(agent.root)], "workspace_ref": workspace.to_record()})
    assert deliverable_closeout_block(params, agent=agent) is None, deliverable_report(params)
    assert west.read_bytes() == contents[0] and east.read_bytes() == contents[1]
    assert deliverable_report(params)["target_delivered"]


def test_completed_landing_replays_read_only_after_staging_is_removed(agent, tmp_path):
    destination = agent.root / "report.txt"
    _, _, rows, tool = _assets(agent, [b"original material"])
    child, refs = _staged_child(agent, [destination], [b"original material"])
    workspace = freeze_workspace_ref(agent.root)
    _parent_turn(agent, tmp_path, child.parent_id, workspace)
    args = {**rows[0], "staged_file": {"child_run_id": child.id, "destination": str(destination)}}
    assert _adopt(agent, tool, args, tmp_path / "approval", "m28-replay").ok
    boundary = {"allowed_write_roots": [str(agent.root)], "workspace_ref": workspace.to_record()}
    params = _closeout_params(boundary)
    assert deliverable_closeout_block(params, agent=agent) is None
    assert _receipts(params)[str(destination)]["state"] == "landed"
    accepted = sha256(b"original material").hexdigest()
    Path(refs[str(destination)]).unlink()
    before = destination.stat()

    params = _closeout_params(boundary)
    assert deliverable_closeout_block(params, agent=agent) is None, deliverable_report(params)
    receipt = _receipts(params)[str(destination)]
    assert (receipt["state"], receipt["landed_digest"], receipt["source_digest"]) == ("already_landed", accepted, "")
    after = destination.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns), "replay must not write"
    assert deliverable_report(params)["target_delivered"]

    destination.write_bytes(b"edited by someone")
    params = _closeout_params(boundary)
    assert deliverable_closeout_block(params, agent=agent)
    assert _receipts(params)[str(destination)]["state"] == "conflict"
    assert destination.read_bytes() == b"edited by someone"

    destination.unlink()
    params = _closeout_params(boundary)
    assert deliverable_closeout_block(params, agent=agent)
    assert _receipts(params)[str(destination)]["state"] == "source_missing"
    assert deliverable_report(params)["file_outcomes"][str(destination)] == "source_missing"
    assert not destination.exists()

    destination.write_bytes(b"original material")
    # 同一路径的根目录已被替换：记录本身合法，但登记的身份与当前目录不再一致。
    replaced = (0, 0)
    changed = {**workspace.to_record(), "root_identity": list(replaced), "root_identities": [list(replaced)],
               "workspace_ref": sha256(f"{workspace.root}\0{replaced[0]}\0{replaced[1]}".encode()).hexdigest()}
    assert read_workspace_ref(changed) is not None and not read_workspace_ref(changed).is_current()
    params = _closeout_params({"allowed_write_roots": [str(agent.root)], "workspace_ref": changed})
    assert deliverable_closeout_block(params, agent=agent), deliverable_report(params)
    assert _receipts(params)[str(destination)]["state"] == "scope_blocked"
    assert not deliverable_report(params)["target_delivered"]


def test_rejected_adoption_releases_the_destination_for_a_corrected_version(agent, tmp_path):
    from agent_py_agent.agent.gateway_parts.request_errors import ConversationPersistenceError

    destination = agent.root / "report.txt"
    services, actor, rows, tool = _assets(agent, [b"original material", b"other asset"])
    child, _ = _staged_child(agent, [destination], [b"original material"])
    workspace = freeze_workspace_ref(agent.root)
    writer, request_path, _ = _parent_turn(agent, tmp_path, child.parent_id, workspace)
    staged = {"child_run_id": child.id, "destination": str(destination)}
    wrong = {**rows[0], "params": {**rows[0]["params"], "version_ref": rows[1]["params"]["version_ref"]},
             "staged_file": staged}

    rejected = _adopt(agent, tool, wrong, tmp_path / "approval-wrong", "m28-wrong")
    assert not rejected.ok and rejected.effect_outcome == "not_started", rejected.output
    assert writer.staged_delivery_contract() is None, "a confirmed pre-effect rejection releases its reservation"

    corrected = {**rows[0], "staged_file": staged}
    adopted = _adopt(agent, tool, corrected, tmp_path / "approval-right", "m28-right")
    assert adopted.ok, adopted.output
    bound = writer.staged_delivery_contract()["staged_deliverables"]
    assert [row["version_ref"] for row in bound] == [rows[0]["params"]["version_ref"]]

    # Lost acknowledgement: the same accepted intent replays idempotently on the same binding.
    assert _adopt(agent, tool, corrected, tmp_path / "approval-replay", "m28-right").ok
    assert writer.staged_delivery_contract()["staged_deliverables"] == bound
    # A contending intent cannot displace the canonically accepted version.
    contender = _adopt(agent, tool, wrong, tmp_path / "approval-contender", "m28-contender")
    assert not contender.ok and contender.error_code == "CAPABILITY_DENIED", contender.output
    assert writer.staged_delivery_contract()["staged_deliverables"] == bound

    reloaded = GatewayTaskBindingWriter(request_path, child.parent_id,
                                        read_json_file_report(request_path, context="test.reload").payload,
                                        writer.execution_attempt_id)
    pending = {"destination": "/elsewhere/a.txt", "version_ref": "v-a"}
    reloaded.remember_staged_delivery(pending)
    replacement = {"destination": "/elsewhere/a.txt", "version_ref": "v-b"}
    reloaded.remember_staged_delivery(replacement, is_accepted=lambda row: False)
    rows_now = reloaded.staged_delivery_contract()["staged_deliverables"]
    assert pending not in rows_now and replacement in rows_now

    def unreadable(_row):
        raise PermissionError("BUSINESS_UNAVAILABLE")

    for check in (lambda row: True, unreadable):
        try:
            reloaded.remember_staged_delivery(pending, is_accepted=check)
        except ConversationPersistenceError:
            pass
        else:
            raise AssertionError("an accepted or unverifiable incumbent must not be replaced")
    assert replacement in reloaded.staged_delivery_contract()["staged_deliverables"]

    params = _closeout_params({"allowed_write_roots": [str(agent.root)], "workspace_ref": workspace.to_record()})
    deliverable_closeout_block(params, agent=agent)
    assert destination.read_bytes() == b"original material"
    assert _receipts(params)[str(destination)]["state"] == "landed"


def test_parent_required_digest_is_not_overridden_by_staged_digest(agent, tmp_path):
    destination = agent.root / "report.txt"
    staged_bytes = b"original material"
    required = sha256(b"what the parent asked for").hexdigest()
    accepted = sha256(staged_bytes).hexdigest()
    _, _, rows, tool = _assets(agent, [staged_bytes])
    child, _ = _staged_child(agent, [destination], [staged_bytes])
    workspace = freeze_workspace_ref(agent.root)
    writer, request_path, request = _parent_turn(agent, tmp_path, child.parent_id, workspace)
    args = {**rows[0], "staged_file": {"child_run_id": child.id, "destination": str(destination)}}
    assert _adopt(agent, tool, args, tmp_path / "approval", "m28-digest").ok
    boundary = {"allowed_write_roots": [str(agent.root)], "workspace_ref": workspace.to_record()}
    declared = {"required_file_refs": [{"path": str(destination), "sha256": required}]}

    for _turn in ("original", "resumed"):
        resumed = GatewayTaskBindingWriter(request_path, child.parent_id,
                                           read_json_file_report(request_path, context="test.resume").payload,
                                           request["execution_attempt_id"])
        agent._current_run_params = replace(agent._current_run_params, conversation_task_binding_callback=resumed)
        params = _closeout_params(boundary, declared)
        assert deliverable_closeout_block(params, agent=agent)
        receipt = _receipts(params)[str(destination)]
        assert (receipt["state"], receipt["declared_digest"], receipt["accepted_digest"]) == (
            "digest_mismatch", required, accepted)
        assert not deliverable_report(params)["target_delivered"] and not destination.exists()

    conflicting = SimpleNamespace(**{**vars(_closeout_params(boundary, declared)),
                                     "delivery_contract": {"required_file_refs": [
                                         {"path": str(destination), "sha256": accepted}]}})
    assert deliverable_closeout_block(conflicting, agent=agent)
    assert _receipts(conflicting)[str(destination)]["state"] == "digest_conflict" and not destination.exists()

    matching = _closeout_params(boundary, {"required_file_refs": [{"path": str(destination), "sha256": accepted}]})
    assert deliverable_closeout_block(matching, agent=agent) is None
    assert destination.read_bytes() == staged_bytes and deliverable_report(matching)["target_delivered"]


def test_pre_snapshot_task_keeps_relative_requirement_through_the_runner(agent, tmp_path):
    right = b"legacy deliverable"
    child = agent.subagents.create_run(goal="legacy", thought="legacy", plan=["write"], parent_id="parent",
                                      attributes={"required_file_refs": [
                                          {"path": "report.txt", "sha256": sha256(right).hexdigest()}]})
    persisted = agent.subagents.load(child.id)
    assert persisted.workspace_ref_record is None
    boundary = agent.subagents.runner_context._build_write_boundary(persisted)
    assert "workspace_ref" not in boundary and Path(boundary["execution_cwd"]).is_absolute()
    project = Path(boundary["execution_cwd"])
    project.mkdir(parents=True, exist_ok=True)
    target = str(project / "report.txt")

    def closeout(write_boundary):
        params = SimpleNamespace(context_scope="task_local", run_id=child.id, task_attributes=persisted.attributes,
                                 write_boundary=write_boundary, live_archive_state={})
        return deliverable_closeout_block(params), deliverable_report(params)

    block, report = closeout(boundary)
    assert block and target in block and report["file_outcomes"] == {target: "missing"}
    (project / "report.txt").write_bytes(b"something else")
    block, report = closeout(boundary)
    assert block and report["file_outcomes"] == {target: "mismatch"}
    (project / "report.txt").write_bytes(right)
    block, report = closeout(boundary)
    assert block is None and report["target_delivered"] and report["landed_files"] == [target]

    without_cwd = {key: value for key, value in boundary.items() if key != "execution_cwd"}
    block, report = closeout(without_cwd)
    assert block and "report.txt" in block
    assert report["file_outcomes"] == {"report.txt": "unresolved"} and not report["target_delivered"]
