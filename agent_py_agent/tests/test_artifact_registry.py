from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_artifact_registry_updates_same_artifact_id_path(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records,
        register_artifact,
    )

    first = tmp_path / "outputs" / "weekly.xlsx"
    first.parent.mkdir()
    first.write_bytes(b"first")
    second = tmp_path / "weekly.xlsx"
    second.write_bytes(b"second")
    created = register_artifact(
        ArtifactRegistration(
            workspace_root=tmp_path,
            path=first,
            artifact_id="github_weekly",
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="xlsx",
            source="test",
        )
    )
    updated = register_artifact(
        ArtifactRegistration(
            workspace_root=tmp_path,
            path=second,
            artifact_id=created.artifact_id,
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="xlsx",
            source="test",
        )
    )

    assert updated.artifact_id == "github_weekly"
    assert updated.path == str(second.resolve())
    assert updated.sha256 != created.sha256
    assert latest_artifact_records(tmp_path)["github_weekly"].path == str(second.resolve())


def test_artifact_registry_reports_bad_rows_without_losing_good_records(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records_report,
        register_artifact,
        registry_path,
    )

    artifact = tmp_path / "outputs" / "report.md"
    artifact.parent.mkdir()
    artifact.write_text("ok", encoding="utf-8")
    register_artifact(ArtifactRegistration(workspace_root=tmp_path, path=artifact, artifact_id="report", run_id="run-1"))
    with registry_path(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write("{bad-json\n")

    report = latest_artifact_records_report(tmp_path)

    assert report.records["report"].path == str(artifact.resolve())
    assert report.errors[0]["context"] == "artifact_registry.read_line"
    assert report.errors[0]["category"] == "data_parse"


def test_artifact_registry_rejects_replaced_workspace_root_symlink(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records_report,
        register_observed_artifact,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = tmp_path / "workspace-original"
    workspace.rename(original)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    try:
        workspace.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    report = latest_artifact_records_report(workspace)

    assert report.records == {}
    assert report.errors
    with pytest.raises(OSError):
        register_observed_artifact(
            ArtifactRegistration(
                workspace_root=workspace,
                path=workspace / "result.txt",
                artifact_id="result",
                status="ready",
            ),
            observed_sha256="digest",
            observed_size_bytes=1,
        )
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (outside / "data" / "artifacts" / "registry.jsonl").exists()


def test_artifact_registry_rejects_hardlinked_ledger(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
        registry_path,
    )

    workspace = tmp_path / "workspace"
    ledger = registry_path(workspace)
    ledger.parent.mkdir(parents=True)
    outside = tmp_path / "outside-ledger.jsonl"
    outside.write_text("sentinel\n", encoding="utf-8")
    try:
        ledger.hardlink_to(outside)
    except OSError:
        pytest.skip("hardlink creation is unavailable on this platform")

    with pytest.raises(OSError):
        register_observed_artifact(
            ArtifactRegistration(
                workspace_root=workspace,
                path=workspace / "result.txt",
                artifact_id="result",
                status="ready",
            ),
            observed_sha256="digest",
            observed_size_bytes=1,
        )

    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_artifact_registry_records_logical_file_group(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactGroupRegistration,
        latest_artifact_records,
        register_artifact_group,
    )

    site = tmp_path / "outputs" / "site"
    site.mkdir(parents=True)
    html = site / "index.html"
    css = site / "styles.css"
    html.write_text("<!doctype html><html><body>ok</body></html>", encoding="utf-8")
    css.write_text("body { color: black; }", encoding="utf-8")
    registered = register_artifact_group(
        ArtifactGroupRegistration(
            workspace_root=tmp_path,
            paths=[html, css],
            artifact_id="site_bundle",
            run_id="run-1",
            source="test",
        )
    )

    record = latest_artifact_records(tmp_path)["site_bundle"]
    assert registered.status == "ready"
    assert record.path == str(site.resolve())
    assert record.metadata["artifact_type"] == "file_group"
    assert [row["relative_path"] for row in record.metadata["members"]] == [
        "outputs/site/index.html",
        "outputs/site/styles.css",
    ]


def test_subagent_file_path_field_is_not_recovered_as_artifact_item():
    from agent_py_agent.agent.subagents.parsing.artifacts import artifact_items_from_payload

    assert artifact_items_from_payload({"summary": "表格已生成", "file_path": "/tmp/run/weekly.xlsx"}) == []


def test_subagent_structured_artifact_registers_registry_ref(mock_task, tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records
    from agent_py_agent.agent.subagents.models import SubAgentParsedOutput
    from agent_py_agent.agent.subagents.result_processors import _process_structured_output

    task_dir = tmp_path / "data" / "subagents" / "run-1"
    task_dir.mkdir(parents=True)
    artifact = task_dir / "weekly.xlsx"
    artifact.write_bytes(b"workbook")
    mock_task.id = "run-1"
    mock_task.root_id = "root-1"
    mock_task.task_dir = str(task_dir)
    mock_task.task_workspace_dir = str(tmp_path)
    mock_task.output_dir = str(task_dir)
    mock_task.reports_dir = str(task_dir / "reports")
    mock_task.attributes = {"workspace_root": str(tmp_path)}

    result = _process_structured_output(
        mock_task,
        SubAgentParsedOutput(
            found=True,
            ok=True,
            parse_error="",
            status="DONE",
            artifacts=[{"path": "weekly.xlsx", "kind": "xlsx", "summary": "周报"}],
        ),
        123456.0,
        None,
    )

    assert result["artifacts"][0]["registry_ref"]["path"] == str(artifact)
    assert mock_task.attributes["artifact_registry_refs"][0]["path"] == str(artifact)
    assert latest_artifact_records(tmp_path)[result["artifacts"][0]["artifact_id"]].path == str(artifact)


def test_write_file_result_exposes_machine_path_for_registry(tmp_path: Path):
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool

    result = WriteFileTool(tmp_path).execute({"path": "outputs/report.md", "content": "hello"})

    assert result.ok is True
    assert result.result_envelope["path"] == str((tmp_path / "outputs" / "report.md").resolve())
    assert result.result_envelope["artifact_ref"] == result.result_envelope["path"]


def test_tool_archive_registers_write_file_artifact(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )

    outcome = WriteFileTool(tmp_path).execute(
        {"path": "outputs/report.md", "content": "hello"}
    )
    call = canonical_history_call(
        "write_file",
        {"path": "outputs/report.md"},
        call_id="write-report",
        run_id="run-1",
    )
    result = canonical_history_result(
        call,
        outcome.output,
        handler_details=outcome.result_envelope,
    )
    params = _empty_tool_loop_params()
    archive_tool_call_record(
        SimpleNamespace(root=tmp_path, config=SimpleNamespace()),
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            call=call,
            result=result,
        ),
    )

    records = latest_artifact_records(tmp_path)
    assert len(records) == 1
    assert next(iter(records.values())).path == str((tmp_path / "outputs" / "report.md").resolve())


def test_tool_output_archive_registers_shell_preimage_exclusion(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
    from agent_py_agent.agent.artifacts.registry import (
        ARTIFACT_ROLE_METADATA_KEY,
        ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE,
        SHELL_PREIMAGE_POLICY_EXCLUDE,
        SHELL_PREIMAGE_POLICY_METADATA_KEY,
        latest_artifact_records,
    )
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ToolResult,
        ToolResultRef,
        ToolSuccessFacts,
    )
    from agent_py_agent.tests._tool_runtime_harness import canonical_history_call

    archived = tmp_path / "work" / "blobs" / "tool_outputs" / "read-file.json"
    archived.parent.mkdir(parents=True)
    archived.write_text('{"output":"complete"}\n', encoding="utf-8")
    call = canonical_history_call(
        "read_file",
        {"path": "report.md"},
        call_id="read-report",
        run_id="run-output",
    )
    result = ToolResult.succeeded(
        call,
        "preview",
        facts=ToolSuccessFacts(
            refs=(
                ToolResultRef(
                    kind="tool_output",
                    ref=str(archived),
                    sha256="abc",
                    size_bytes=archived.stat().st_size,
                    summary="complete raw tool output",
                ),
            )
        ),
    )
    output_record = archive_tool_call_record(
        SimpleNamespace(root=tmp_path, config=SimpleNamespace()),
        ToolCallRecordParams(
            params=_empty_tool_loop_params(),
            tool_rounds=1,
            idx=1,
            call=call,
            result=result,
        ),
    )

    registry_ref = output_record["artifact_registry_refs"][0]
    record = latest_artifact_records(tmp_path)[registry_ref["artifact_id"]]
    assert record.kind == "tool_output"
    assert record.metadata[ARTIFACT_ROLE_METADATA_KEY] == ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE
    assert record.metadata[SHELL_PREIMAGE_POLICY_METADATA_KEY] == SHELL_PREIMAGE_POLICY_EXCLUDE


def _empty_tool_loop_params():
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )


# LLM: 实际补丁执行和统一归档一起验证，避免直接伪造 registry 绕开漏交接入口。
# 函数用途: 把真实补丁结果按生产工具回执形式归档到测试 run。
def _archive_patch_outcome(root, outcome):
    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )

    call = canonical_history_call('apply_patch', {}, call_id='patch-result', run_id='run-1')
    result = canonical_history_result(call, outcome.output, ok=outcome.ok, handler_details=outcome.result_envelope)
    return archive_tool_call_record(
        SimpleNamespace(root=root, config=SimpleNamespace()),
        ToolCallRecordParams(params=_empty_tool_loop_params(), tool_rounds=1, idx=1, call=call, result=result),
    )


def test_patch_add_update_move_delete_reach_natural_child_handoff(tmp_path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records,
        register_artifact,
    )
    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.result_registered_artifacts import (
        collect_registered_artifacts,
    )
    from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool

    for name in ('a.txt', 'b.txt', 'delete.txt'):
        path = tmp_path / name
        path.write_text('old\n')
        register_artifact(ArtifactRegistration(workspace_root=tmp_path, path=path, run_id='run-1'))
    task = SubAgentTask(id='run-1', goal='修改资料', thought='', plan=[], agent_run_workspace_dir=str(tmp_path))
    collect_registered_artifacts(task)
    outcome = ApplyPatchTool(tmp_path).execute({'patch': (
        '*** Begin Patch\n*** Update File: a.txt\n-old\n+new\n'
        '*** Update File: b.txt\n*** Move to: 中文.txt\n-old\n+moved\n'
        '*** Add File: empty.txt\n+\n*** Delete File: delete.txt\n*** End Patch'
    )})
    assert outcome.ok, outcome.output
    _archive_patch_outcome(tmp_path, outcome)
    items = collect_registered_artifacts(task)
    assert {item['path'] for item in items} == {str(tmp_path / name) for name in ('a.txt', '中文.txt', 'empty.txt')}
    assert set(task.artifact_refs) == {item['path'] for item in items}
    records = {Path(row.path).name: row for row in latest_artifact_records(tmp_path).values()}
    assert records['b.txt'].status == records['delete.txt'].status == 'deleted'
    assert records['a.txt'].created_by_tool == 'apply_patch'


def test_partial_patch_registers_committed_file_not_failed_target(tmp_path, monkeypatch):
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records
    from agent_py_agent.agent.tooling import _filesystem_patch as module

    for name in ('a', 'b'):
        (tmp_path / name).write_text('old\n')
    original = module._atomic_write_bytes

    def fail_second(path, data, **kwargs):
        if path.name == 'b':
            raise OSError('injected write failure')
        original(path, data, **kwargs)

    monkeypatch.setattr(module, '_atomic_write_bytes', fail_second)
    outcome = module.ApplyPatchTool(tmp_path).execute({'patch': (
        '*** Begin Patch\n*** Update File: a\n-old\n+new\n'
        '*** Update File: b\n-old\n+new\n*** End Patch'
    )})
    assert not outcome.ok
    assert outcome.result_envelope['partial_commit']
    _archive_patch_outcome(tmp_path, outcome)
    assert {row.path for row in latest_artifact_records(tmp_path).values()} == {str(tmp_path / 'a')}

def test_attachment_boundary_points_to_the_registry_that_received_the_write(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )

    task = tmp_path / "tasks" / "原任务"
    task.mkdir(parents=True)
    run = tmp_path / "runs" / "生成文档的运行"
    run.mkdir(parents=True)
    agent = SimpleNamespace(root=tmp_path, config=SimpleNamespace())
    params = replace(
        _empty_tool_loop_params(),
        task_attributes={"run_workspace": {"task_root": str(task)}},
        delivery_contract={"task_workspace": {"task_root": str(run)}},
        write_boundary={"artifact_registry_root": str(tmp_path / "外来输入"),
                        "artifact_gateway_root": str(tmp_path / "外来网关")},
    )
    name = "巡检流程图与实现说明.md"
    outcome = WriteFileTool(task).execute({"path": name, "content": "# 巡检\n"})
    assert outcome.ok
    call = canonical_history_call("write_file", {"path": name}, call_id="write-doc", run_id=params.run_id)
    result = canonical_history_result(call, outcome.output, handler_details=outcome.result_envelope)
    archive_tool_call_record(agent, ToolCallRecordParams(params=params, tool_rounds=1, idx=1, call=call, result=result))

    boundary = write_boundary_with_runtime_ledger(agent, params, for_attachment_lookup=True)
    assert boundary["task_root"] == str(task)
    assert boundary["artifact_registry_root"] == str(run)
    assert "artifact_gateway_root" not in boundary
    ordinary_boundary = write_boundary_with_runtime_ledger(agent, params)
    assert "artifact_registry_root" not in ordinary_boundary and "artifact_gateway_root" not in ordinary_boundary
    records = list(latest_artifact_records(boundary["artifact_registry_root"]).values())
    assert len(records) == 1 and records[0].path == str(task / name) and records[0].status == "ready"


def _business_registry_fixture(tmp_path):
    import json

    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_artifact,
        registry_path,
    )

    owner = tmp_path / 'owner'
    owner.mkdir(mode=0o700)
    root = owner
    for part in ('artifacts', 'business'):
        root = root / part
        root.mkdir(mode=0o700)
    target = root / ('artifact-' + '1' * 40 + '.blob')
    target.write_bytes(b'original')
    record = register_artifact(ArtifactRegistration(root, target, artifact_id='artifact-' + '1' * 40,
        source='business_upload', status='ready', metadata={'owner_id': 'qa', 'display_name': 'old'}))
    ledger = registry_path(root)
    return owner, ledger, record.to_dict(), json


def test_business_registry_large_history_and_page_proof_are_bounded(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_upload_registry as module

    owner, ledger, original, json = _business_registry_fixture(tmp_path)
    rows = [original]
    for index in range(2200):
        row = {**original, 'artifact_id': 'artifact-' + f'{index + 2:040x}',
               'metadata': {**original['metadata'], 'history': 'x' * 900}}
        rows.append(row)
    rows.append({**original, 'metadata': {**original['metadata'], 'display_name': 'new'}})
    ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    assert ledger.stat().st_size > 2 * 1024 * 1024
    found = module.lookup_business_upload(owner, original['artifact_id'])
    assert found.record.metadata['display_name'] == 'new'
    # 相同updated_at按最后物理行胜出；页证明只读原行，不重扫大簿。
    assert found.record.updated_at == original['updated_at']
    reads = []
    read = module._read
    def observe(fd, offset, count):
        reads.append(count)
        return read(fd, offset, count)
    monkeypatch.setattr(module, '_read', observe)
    assert module.verify_record_proof(owner, found.proof).metadata['display_name'] == 'new'
    assert reads == [found.proof.length] and sum(reads) < 2048


def test_business_registry_same_stamp_other_row_cannot_impersonate_cached_id(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_upload_registry as module

    owner, ledger, original, json = _business_registry_fixture(tmp_path)
    other = {**original, 'artifact_id': 'artifact-' + '2' * 40,
             'metadata': {**original['metadata'], 'display_name': 'new'}}
    before = json.dumps(original) + '\n' + json.dumps(other) + '\n'
    ledger.write_text(before)
    first = module.lookup_business_upload(owner, original['artifact_id'])
    assert first.record.metadata['display_name'] == 'old'
    stamp = first.proof.source_stamp
    # 固定全部可见元数据，另一行同长度改成目标ID，不能靠mtime/size或旧行抽样签latest。
    monkeypatch.setattr(module, '_stamp', lambda _fd: stamp)
    other['artifact_id'] = original['artifact_id']
    ledger.write_text(json.dumps(original) + '\n' + json.dumps(other) + '\n')
    assert ledger.stat().st_size == len(before.encode())
    second = module.lookup_business_upload(owner, original['artifact_id'])
    assert second.record.metadata['display_name'] == 'new'


def test_business_registry_progress_recovers_and_bad_rows_are_not_ignored(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_upload_registry as module
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    owner, ledger, original, json = _business_registry_fixture(tmp_path)
    rows = [original] * 20
    ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with pytest.raises(HostAPIError, match='本次推进'):
        module.lookup_business_upload(owner, original['artifact_id'], max_bytes=1024)
    found = None
    for _attempt in range(12):
        try:
            found = module.lookup_business_upload(owner, original['artifact_id'], max_bytes=1024)
            break
        except HostAPIError as error:
            assert error.code == 'HOST_NOT_READY' and '本次推进' in str(error)
    assert found is not None and found.record.artifact_id == original['artifact_id']
    monkeypatch.setattr(module, '_KEY', b'new-process-key-for-test-only')
    assert module.lookup_business_upload(owner, original['artifact_id']).record.artifact_id == original['artifact_id']
    with ledger.open('a') as file:
        file.write('{broken\n')
    with pytest.raises(HostAPIError, match='无效或未完成JSON行'):
        module.lookup_business_upload(owner, original['artifact_id'])


def test_business_registry_absent_owner_lookup_does_not_create_canonical_dirs(tmp_path):
    from agent_py_agent.agent.gateway_parts.business_upload_registry import lookup_business_upload

    owner = tmp_path / 'not-created'
    result = lookup_business_upload(owner, 'artifact-' + '0' * 40)
    assert result.record is None and result.registry_size == 0 and not owner.exists()
