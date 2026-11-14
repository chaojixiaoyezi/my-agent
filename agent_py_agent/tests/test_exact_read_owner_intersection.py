"""精确读根与owner墙求交；真实Seatbelt/bwrap验证跨owner、hint、默认cwd及进程启动前拒绝。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

SANDBOX_CAPABILITY = "nested_sandbox_exec" if sys.platform == "darwin" else "linux_bwrap"


# ------------------------------------------------------------------ boundary 1: exact read scope

def _two_owner_layout(tmp_path):
    root = (tmp_path / "my-agent-home").resolve()
    owner = root / "owners" / "local" / "main"
    other = root / "owners" / "providers" / "feishu" / "users" / "u-1"
    out = owner / "work" / "out"
    out.mkdir(parents=True)
    other.mkdir(parents=True)
    (owner / "allowed.txt").write_text("OWN_ALLOWED_MARKER", encoding="utf-8")
    (owner / "sibling.txt").write_text("OWN_SIBLING_MARKER", encoding="utf-8")
    (other / "secret.txt").write_text("OTHER_OWNER_SECRET_MARKER", encoding="utf-8")
    return root, owner, other, out


def _exact_boundary(owner, other, out):
    # 复现宿主错误根列表：显式输入旁混入其它owner路径，最终进程读权仍须与owner墙求交。
    return {"task_dir": str(out), "execution_cwd": str(out), "allowed_write_roots": [str(out)],
            "allowed_read_roots": [str(owner / "allowed.txt"), str(out), str(other / "secret.txt")],
            "owner_workspace_dir": str(owner), "read_scope_mode": "exact",
            "shell_access_mode": "workspace-write"}


@pytest.mark.sandbox_capability(SANDBOX_CAPABILITY)
@pytest.mark.parametrize("target,marker,expected_visible", [
    ("allowed", "OWN_ALLOWED_MARKER", True),
    ("sibling", "OWN_SIBLING_MARKER", False),
    ("other_owner", "OTHER_OWNER_SECRET_MARKER", False),
])
def test_exact_shell_scope_keeps_owner_wall(tmp_path, target, marker, expected_visible):
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    root, owner, other, out = _two_owner_layout(tmp_path)
    path = {"allowed": owner / "allowed.txt", "sibling": owner / "sibling.txt",
            "other_owner": other / "secret.txt"}[target]
    shell = ShellTool(owner, options=ShellToolOptions(owner_scope_root=str(owner), host_private_roots=(str(root),)))
    execution = execute_canonical_test_call(out, tools={"run_command": shell}, tool_name="run_command",
        arguments={"command": f"cat {path}", "working_dir": str(out), "timeout": 10},
        write_boundary=_exact_boundary(owner, other, out), owner_scope_root=str(owner),
        operation_owner_id="local/main")
    wire = repr(execution.result)
    assert (marker in wire) is expected_visible, wire[:1500]


def test_builtin_read_file_refuses_other_owner_root_under_same_boundary(tmp_path):
    from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions, ReadFileTool

    root, owner, other, out = _two_owner_layout(tmp_path)
    tool = ReadFileTool(owner, 100000, access_options=FileSystemAccessOptions(owner_scope_root=str(owner)))
    execution = execute_canonical_test_call(out, tools={"read_file": tool}, tool_name="read_file",
        arguments={"path": str(other / "secret.txt")}, write_boundary=_exact_boundary(owner, other, out),
        owner_scope_root=str(owner), operation_owner_id="local/main")
    wire = repr(execution.result)
    assert "OTHER_OWNER_SECRET_MARKER" not in wire


def _child_boundary(owner, other):
    from agent_py_agent.agent.agent_core.orchestration.create_context import create_context_manifest
    from agent_py_agent.agent.agent_core.orchestration.delegation_constraints import (
        StructuredDelegationConstraints,
    )
    from agent_py_agent.agent.agent_core.runner.ref_fields import params_explicit_input_refs
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    raw = {"goal": f"对照 {other / 'secret.txt'} 整理 {owner / 'allowed.txt'}",
           "input_files": [str(owner / "allowed.txt")]}
    manifest = create_context_manifest(raw)
    manager = SubAgentManager(owner)
    task = manager.create_run(goal=raw["goal"], thought="plan", plan=["p"])
    task.context_manifest.hint_read_paths = list(manifest.get("hint_read_paths") or [])
    task.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        actor_runtime="native", allowed_read_inputs=(str(owner / "allowed.txt"),)).to_dict()
    manager.save(task)
    boundary = manager.runner_context._build_write_boundary(task)
    return raw, manifest, params_explicit_input_refs(raw), boundary


def test_goal_text_path_becomes_unchecked_exact_read_root(tmp_path):
    root, owner, other, out = _two_owner_layout(tmp_path)
    raw, manifest, prechecked, boundary = _child_boundary(owner, other)
    secret = str(other / "secret.txt")
    assert boundary["read_scope_mode"] == "exact"
    assert secret not in prechecked
    # goal路径只作上下文，不得成为跨owner读权。
    assert secret not in boundary["allowed_read_roots"]


@pytest.mark.sandbox_capability(SANDBOX_CAPABILITY)
@pytest.mark.parametrize("working_dir", ["agent_run_workspace"])
def test_child_boundary_positive_and_cross_owner_shell(tmp_path, working_dir):
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    root, owner, other, out = _two_owner_layout(tmp_path)
    _, _, _, boundary = _child_boundary(owner, other)
    cwd = Path(boundary.get("execution_cwd") or owner)
    shell = ShellTool(cwd, options=ShellToolOptions(owner_scope_root=str(owner), host_private_roots=(str(root),)))
    results = {}
    for label, path in (("allowed", owner / "allowed.txt"), ("other_owner", other / "secret.txt")):
        arguments = {"command": f"cat {path}", "timeout": 10}
        if working_dir != "default":
            arguments["working_dir"] = str(boundary["allowed_write_roots"][1])
        execution = execute_canonical_test_call(cwd, tools={"run_command": shell}, tool_name="run_command",
            arguments=arguments, write_boundary=boundary, owner_scope_root=str(owner),
            operation_owner_id="local/main", call_id=f"call-{label}")
        wire = repr(execution.result)
        results[label] = (execution.result.status, execution.result.error_code,
                          "OWN_ALLOWED_MARKER" in wire, "OTHER_OWNER_SECRET_MARKER" in wire,
                          "RESTRICTED_CWD_NOT_ALLOWLISTED" in wire)
    assert results["allowed"][2], results
    assert not results["other_owner"][3], results


@pytest.mark.sandbox_capability(SANDBOX_CAPABILITY)
def test_child_default_cwd_positive_case(tmp_path):
    """Positive case through the real child boundary: parent project cwd, declared input inside it."""
    from agent_py_agent.agent.agent_core.orchestration.delegation_constraints import (
        StructuredDelegationConstraints,
    )
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    root, owner, other, out = _two_owner_layout(tmp_path)
    project = owner / "workspace" / "project"
    project.mkdir(parents=True)
    (project / "input.md").write_text("PROJECT_INPUT_MARKER", encoding="utf-8")
    manager = SubAgentManager(owner / "workspace")
    task = manager.create_run(goal="总结 input.md", thought="plan", plan=["p"])
    task.attributes["workspace_root"] = str(project)
    task.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        actor_runtime="native", allowed_read_inputs=(str(project / "input.md"),)).to_dict()
    manager.save(task)
    boundary = manager.runner_context._build_write_boundary(task)
    cwd = Path(boundary["execution_cwd"])
    shell = ShellTool(cwd, options=ShellToolOptions(owner_scope_root=str(owner), host_private_roots=(str(root),)))
    execution = execute_canonical_test_call(cwd, tools={"run_command": shell}, tool_name="run_command",
        arguments={"command": "cat input.md", "timeout": 10}, write_boundary=boundary,
        owner_scope_root=str(owner), operation_owner_id="local/main")
    text = "".join(block.text or "" for block in execution.result.content_blocks)
    assert "PROJECT_INPUT_MARKER" in text




@pytest.mark.sandbox_capability(SANDBOX_CAPABILITY)
@pytest.mark.parametrize("root_kind", ["data_ancestor", "owner_symlink", "file_symlink", "own_directory", "foreign_write_root"])
def test_exact_roots_intersect_owner_wall_for_broad_roots_and_symlinks(tmp_path, root_kind):
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    home, owner, other, out = _two_owner_layout(tmp_path)
    link = owner / "link"
    link.symlink_to(other if root_kind == "owner_symlink" else other / "secret.txt")
    declared = home if root_kind == "data_ancestor" else owner if root_kind in {"own_directory", "foreign_write_root"} else link
    boundary = _exact_boundary(owner, other, out)
    boundary['allowed_read_roots'] = [str(owner / 'allowed.txt'), str(out), str(declared)]
    if root_kind == 'foreign_write_root':
        boundary['allowed_write_roots'].append(str(other))
    shell = ShellTool(owner, options=ShellToolOptions(owner_scope_root=str(owner), host_private_roots=(str(home),)))
    result = execute_canonical_test_call(out, tools={'run_command': shell}, tool_name='run_command',
        arguments={'command': f'cat {other / "secret.txt"}; cat {link / "secret.txt"}; cat {link}; cat {owner / "allowed.txt"}', 'timeout': 10},
        write_boundary=boundary, owner_scope_root=str(owner), operation_owner_id='local/main').result
    assert 'OTHER_OWNER_SECRET_MARKER' not in repr(result)
    assert 'OWN_ALLOWED_MARKER' in repr(result)


def test_exact_controlled_exec_apply_refused_before_any_process(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.controlled_exec import ControlledExecTool
    from agent_py_agent.agent.tooling.registry_invoke import (
        RegistryToolInvokeRequest,
        invoke_registry_tool,
    )
    from agent_py_agent.tests._tool_runtime_harness import runtime_snapshot_for_tools

    def forbidden(*args, **kwargs):
        pytest.fail('exact apply must not reach subprocess.Popen')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    tool = ControlledExecTool()
    tools = {'controlled_exec': tool}
    snapshot = runtime_snapshot_for_tools(tools, run_id='exact-apply')
    result = invoke_registry_tool(RegistryToolInvokeRequest(tool_name='controlled_exec',
        arguments={'command': 'cat unapproved', 'apply': True}, tools=tools,
        workspace_root=tmp_path, workspace_roots=[tmp_path], allowed_tools=['controlled_exec'],
        write_boundary={'read_scope_mode': 'exact', 'allowed_read_roots': []},
        runtime_snapshot=snapshot, runtime=snapshot.runtime('controlled_exec')))
    assert result.handler_executed is False
    assert result.effect_outcome == 'not_started'
    assert 'EXACT_PROCESS_READ_SCOPE_UNSUPPORTED' in result.output


def test_exact_old_process_handle_refuses_output_but_preserves_owned_stop(tmp_path):
    import time

    from agent_py_agent.agent.tooling.background_process_launch import start_background_process
    from agent_py_agent.agent.tooling.process_registry import process_registry
    from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
    from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
    from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
    from agent_py_agent.tests._managed_process_harness import managed_request

    owner = tmp_path.resolve()
    secret = owner / 'parent-only.txt'
    secret.write_text('PARENT_ONLY_HANDLE_MARKER')
    access = ProcessAccessScope('owner-test', 'same-thread', str(owner))
    store = process_session_store_root(owner, owner)
    hosted = start_background_process(managed_request(owner,
        code=f'from pathlib import Path; import time; print(Path({str(secret)!r}).read_text(), flush=True); time.sleep(30)',
        access_scope=access, execution_scope=ProcessExecutionScope(str(owner), 'same-thread', 'parent', 'parent-run', 'parent-attempt'),
        store_root=store))
    record = process_registry.attach(hosted.record, hosted.process, hosted.store_root)
    tool = ProcessSessionTool(owner_scope_root=owner, workspace_root=owner)
    scope = {'owner_id': 'owner-test', 'owner_home': str(owner), 'session_id': 'same-thread',
             'root_task_id': 'parent', 'run_id': 'child-run'}

    def call(action, exact):
        return execute_canonical_test_call(owner, tools={'process_session': tool}, tool_name='process_session',
            arguments={'action': action, 'session_id': record.session_id}, run_id='child-run',
            owner_scope_root=str(owner), operation_owner_id='owner-test',
            write_boundary={'read_scope_mode': 'exact', 'allowed_read_roots': [], 'allowed_write_roots': []} if exact else None,
            trusted_run_context={'run_scope': scope}, call_id=f'{action}-{exact}').result

    try:
        deadline = time.monotonic() + 5
        while True:
            normal = call('status', False)
            if 'PARENT_ONLY_HANDLE_MARKER' in repr(normal):
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert normal.status == 'succeeded'
        for action in ('status', 'wait', 'list', 'network_status'):
            denied = call(action, True)
            assert denied.handler_executed is False
            assert 'EXACT_PROCESS_READ_SCOPE_UNSUPPORTED' in repr(denied)
            assert 'PARENT_ONLY_HANDLE_MARKER' not in repr(denied)
        assert hosted.process.poll() is None
        stopped = call('stop', True)
        assert stopped.status == 'succeeded'
        assert stopped.handler_executed is True
        assert 'PARENT_ONLY_HANDLE_MARKER' not in repr(stopped)
        assert 'EXACT_HANDLE_READ_SCOPE_UNVERIFIED' in repr(stopped)
    finally:
        if hosted.process.poll() is None:
            process_registry.kill(record.session_id, access, store)
        hosted.process.wait(timeout=5)


@pytest.mark.parametrize('action', ['read', 'list'])
def test_exact_old_terminal_handle_refused_before_handler(tmp_path, action):
    from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool
    from agent_py_agent.agent.tooling.registry_invoke import (
        RegistryToolInvokeRequest,
        invoke_registry_tool,
    )
    from agent_py_agent.agent.tooling.shell import ShellTool
    from agent_py_agent.tests._tool_runtime_harness import runtime_snapshot_for_tools

    tool = TerminalSessionTool(ShellTool(tmp_path))
    tool.execute = lambda params: pytest.fail('unverified PTY output must not be read')
    tools = {'terminal_session': tool}
    snapshot = runtime_snapshot_for_tools(tools, run_id='old-terminal')
    outcome = invoke_registry_tool(RegistryToolInvokeRequest(tool_name='terminal_session',
        arguments={'action': action, 'session_id': 'old-parent-handle'}, tools=tools,
        workspace_root=tmp_path, workspace_roots=[tmp_path], allowed_tools=['terminal_session'],
        write_boundary={'read_scope_mode': 'exact', 'allowed_read_roots': []},
        runtime_snapshot=snapshot, runtime=snapshot.runtime('terminal_session')))
    assert outcome.handler_executed is False
    assert outcome.effect_outcome == 'not_started'
    assert 'EXACT_PROCESS_READ_SCOPE_UNSUPPORTED' in outcome.output


@pytest.mark.parametrize('tool_name,action,ok', [('terminal_session', 'close', True),
    ('terminal_session', 'resize', True), ('process_session', 'stop', False)])
def test_exact_handle_control_receipts_withhold_content_and_preserve_outcome(tmp_path, tool_name, action, ok):
    import json

    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
    from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool
    from agent_py_agent.agent.tooling.registry_invoke import (
        RegistryToolInvokeRequest,
        invoke_registry_tool,
    )
    from agent_py_agent.agent.tooling.shell import ShellTool
    from agent_py_agent.tests._tool_runtime_harness import runtime_snapshot_for_tools

    tool = TerminalSessionTool(ShellTool(tmp_path)) if tool_name == 'terminal_session' else ProcessSessionTool()
    tool.execute = lambda params, **kwargs: ToolHandlerOutcome(tool_name, ok,
        json.dumps({'status': 'closed' if ok else 'unknown', 'session_id': 'old-handle',
            'output_tail': 'UNVERIFIED_HANDLE_CONTENT', 'command': 'UNVERIFIED_HANDLE_CONTENT'}),
        error_code='' if ok else 'TOOL_OPERATION_OUTCOME_UNKNOWN', effect_outcome='' if ok else 'unknown',
        result_envelope={'process': {'output_tail': 'UNVERIFIED_HANDLE_CONTENT'}})
    tools = {tool_name: tool}
    snapshot = runtime_snapshot_for_tools(tools, run_id='control')
    outcome = invoke_registry_tool(RegistryToolInvokeRequest(tool_name=tool_name,
        arguments={'action': action, 'session_id': 'old-handle'}, tools=tools,
        workspace_root=tmp_path, workspace_roots=[tmp_path], allowed_tools=[tool_name],
        write_boundary={'read_scope_mode': 'exact', 'allowed_read_roots': []},
        runtime_snapshot=snapshot, runtime=snapshot.runtime(tool_name)))
    assert outcome.handler_executed is True and outcome.ok is ok
    assert 'UNVERIFIED_HANDLE_CONTENT' not in repr(outcome)
    assert json.loads(outcome.output)['session_id'] == 'old-handle'
    assert outcome.effect_outcome == ('' if ok else 'unknown')
    assert outcome.error_code == ('' if ok else 'TOOL_OPERATION_OUTCOME_UNKNOWN')


def test_exact_self_process_state_is_content_free_and_attempt_bound(tmp_path, monkeypatch):
    """真实管理进程的原启动事实经canonical入口核验；禁止状态查询打开任何输出。"""
    import json
    from dataclasses import replace

    from agent_py_agent.agent.path_access_policy import PathAccessPolicy
    from agent_py_agent.agent.tooling.background_process_launch import start_background_process
    from agent_py_agent.agent.tooling.process_registry import ProcessRegistry, process_registry
    from agent_py_agent.agent.tooling.process_scope import (
        ProcessAccessScope,
        ProcessExecutionScope,
        exact_process_read_scope,
    )
    from agent_py_agent.agent.tooling.process_session_records import merge_process_record
    from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
    from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
    from agent_py_agent.agent.tooling.workspace_read_scope import build_workspace_read_context
    from agent_py_agent.tests._managed_process_harness import managed_request

    owner = tmp_path.resolve()
    boundary = {'read_scope_mode': 'exact', 'allowed_read_roots': [str(owner)], 'allowed_write_roots': [str(owner)]}
    context = build_workspace_read_context(cwd=owner, write_boundary=boundary,
        path_policy=PathAccessPolicy.from_values(owner_scope_root=str(owner)), granted_external_roots=())
    access = ProcessAccessScope('owner-test', 'thread-test', str(owner))
    execution = ProcessExecutionScope(str(owner), 'thread-test', 'task-test', 'self-run', 'self-attempt')
    store = process_session_store_root(owner, owner)
    hosted = start_background_process(managed_request(owner,
        code='import time; print("PRIVATE_PROCESS_OUTPUT", flush=True); time.sleep(0.6)',
        access_scope=access, execution_scope=execution, store_root=store,
        exact_read_scope=exact_process_read_scope(context, (owner,))))
    record = process_registry.attach(hosted.record, hosted.process, hosted.store_root)
    tool = ProcessSessionTool(owner_scope_root=owner, workspace_root=owner)
    scope = {'session_id': 'thread-test', 'root_task_id': 'task-test'}

    def call(action, *, run='self-run', attempt='self-attempt', owner_id='owner-test', extra=None):
        return execute_canonical_test_call(owner, tools={'process_session': tool}, tool_name='process_session',
            arguments={'action': action, 'session_id': record.session_id, **(extra or {})},
            run_id=run, attempt_id=attempt, call_id=f'{action}-{run}-{attempt}-{owner_id}',
            owner_scope_root=str(owner), operation_owner_id=owner_id, write_boundary=boundary,
            trusted_run_context={'run_scope': scope}).result

    def forbidden(*args, **kwargs):
        pytest.fail('state observation must never read output log')

    monkeypatch.setattr('agent_py_agent.agent.tooling.process_registry._read_log_tail', forbidden)
    try:
        for action in ('status', 'wait'):
            result = call(action)
            assert result.status == 'succeeded', repr(result)
            text = ''.join(block.text or '' for block in result.content_blocks)
            payload = json.JSONDecoder().raw_decode(text[text.index('{'):])[0]
            assert payload['content_withheld_reason'] == 'EXACT_PROCESS_STATE_ONLY'
            assert not ({'output', 'command', 'pid', 'output_file', 'cwd'} & payload.keys())
            assert 'PRIVATE_PROCESS_OUTPUT' not in repr(result)
        assert payload['status'] == 'exited' and payload['exit_code'] == 0
        # 冷恢复复用原私有Store，不要求保留旧Gateway内存；仍只有同attempt可读。
        cold = ProcessRegistry()
        assert cold.exact_state(record.session_id, access, execution, context, store)['status'] == 'exited'
        assert cold.exact_state(record.session_id, access, replace(execution, attempt_id='next'), context, store) is None
        for kwargs in ({'run': 'parent-run'}, {'attempt': 'next'}, {'owner_id': 'other-owner'}):
            denied = call('status', **kwargs)
            assert denied.status != 'succeeded'
            assert 'EXACT_PROCESS_READ_SCOPE_UNSUPPORTED' in repr(denied)
        for action in ('list', 'network_status'):
            assert call(action).status != 'succeeded'
        forged = call('status', run='parent-run', extra={'__run_scope': {'run_id': 'self-run'}})
        assert forged.status != 'succeeded'
        # 入门核验后权威被移除，不能把空结果涂为成功或吞掉精确原因。
        saved_exact = process_registry.exact_state
        with monkeypatch.context() as scoped:
            scoped.setattr(process_registry, 'exact_state',
                lambda *args, **kwargs: saved_exact(*args, **kwargs) if kwargs.get('refresh') is False else None)
            lost = call('status')
            assert lost.status != 'succeeded'
            assert 'EXACT_PROCESS_SCOPE_UNVERIFIED' in repr(lost)
            assert 'not_started' in repr(lost)
        original = dict(record.persisted_snapshot)
        for proof in (None, {**original['exact_read_scope'], 'sandbox_read_roots': []}):
            with pytest.raises(ValueError):
                merge_process_record(original, {**original, 'exact_read_scope': proof})
        legacy = {**original, 'exact_read_scope': None}
        with pytest.raises(ValueError):
            merge_process_record(legacy, original)
    finally:

        if hosted.process.poll() is None:
            process_registry.kill(record.session_id, access, store)
        hosted.process.wait(timeout=5)


@pytest.mark.sandbox_capability(SANDBOX_CAPABILITY)
def test_exact_shell_freezes_actual_launch_read_scope(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.process_registry import process_registry
    from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope
    from agent_py_agent.agent.tooling.process_session_store import (
        ProcessSessionStore,
        process_session_store_root,
    )
    from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    owner = tmp_path.resolve()
    boundary = {'read_scope_mode': 'exact', 'allowed_read_roots': [str(owner)], 'allowed_write_roots': [str(owner)]}
    scope = {'session_id': 'thread-test', 'root_task_id': 'task-test'}
    shell = ShellTool(owner, options=ShellToolOptions(owner_scope_root=str(owner)))
    def call(name, tool, params):
        result = execute_canonical_test_call(owner, tools={name: tool}, tool_name=name, arguments=params,
            run_id='self-run', attempt_id='self-attempt', call_id=name,
            owner_scope_root=str(owner), operation_owner_id='owner-test', write_boundary=boundary,
            trusted_run_context={'run_scope': scope}).result
        if result.status == 'approval_required':
            approved = {**result.metadata['action_decision']['approval_request'],
                        'approval_id': 'isolated-test-approval', 'status': 'APPROVED'}
            return execute_canonical_test_call(owner, tools={name: tool}, tool_name=name, arguments=params,
                run_id='self-run', attempt_id='self-attempt', call_id=name,
                owner_scope_root=str(owner), operation_owner_id='owner-test',
                write_boundary={**boundary, 'approved_actions': [approved]},
                trusted_run_context={'run_scope': scope}).result
        return result
    started = call('run_command', shell, {'command': 'printf SELF_STATE_TEST; sleep 0.2', 'run_in_background': True})
    assert started.status == 'succeeded', repr(started)
    # ID来自原启动回执，非扫描或从命令文本推测归属。
    import re
    session = re.search(r'bg-[0-9]+-[0-9a-f]+', repr(started)).group()
    store = process_session_store_root(owner, owner)
    raw = ProcessSessionStore(store).load(session).record
    assert raw['exact_read_scope']['read_context']['read_roots'] == [str(owner)]
    assert raw['execution_scope']['run_id'] == 'self-run'
    def forbidden(*args, **kwargs):
        pytest.fail('exact status/wait must not consume logs')
    monkeypatch.setattr('agent_py_agent.agent.tooling.process_registry._read_log_tail', forbidden)
    try:
        waited = call('process_session', ProcessSessionTool(owner_scope_root=owner, workspace_root=owner),
                      {'action': 'wait', 'session_id': session, 'timeout_seconds': 5})
        assert waited.status == 'succeeded', repr(waited)
        assert 'EXACT_PROCESS_STATE_ONLY' in repr(waited)
        assert 'SELF_STATE_TEST' not in repr(waited)
    finally:
        if ProcessSessionStore(store).load(session).record['status'] not in {'exited', 'killed', 'not_started'}:
            process_registry.kill(session, ProcessAccessScope('owner-test', 'thread-test', str(owner)), store)
