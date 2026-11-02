"""R9-R2 P1-R9-08 插件：准入后的活记录改写不能替换本次被调用的脚本正文。

真实 Host API 组合、授权、ExecutionDomain、原 ScriptRunner 与 WorkflowEngine；只在原 runner
加载记录前改写活记录（模拟准入后的定义漂移），并保留原逐步 live fence 的 ASSET_CHANGED 判定。
"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.attempt import sandbox as attempt
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.workflows import engine as workflow_engine
from agent_py_agent.agent.workflows import script_runner
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_rework_plugin_host_source_scope import (
    _EXPECTED,
    _call,
    _configure,
    _flow,
)
from agent_py_agent.tests.test_rework_plugin_host_source_scope import (
    asset_case as asset_case,
)
from agent_py_agent.tests.test_rework_plugin_host_source_scope import (
    asset_root as asset_root,
)

_EDITED = '''import json, sys
from pathlib import Path
inputs = json.load(sys.stdin)
Path(inputs["report"]).write_text(json.dumps({"body": "edited"}))
print(json.dumps({"body": "edited"}))
'''


def _payload(case, action):
    payload = {'thread_id': case.thread, 'expected_revision': 1}
    if action == 'execute.script':
        return {**payload, 'script_id': 'source-probe', 'inputs': case.inputs}
    return {**payload, 'workflow_id': 'source-flow'}


def _edit_live_script_before_runner_load(case, monkeypatch):
    real = script_runner.load_script
    edits = []

    def edit_then_load(owner_home, script_id, **kwargs):
        if not edits:
            # 模拟准入后的定义漂移：准入与宿主核对已完成，原 runner 读取记录前活记录被改写。
            ScriptStore(case.home, extra_roots=()).save_script(
                AssetDescriptor('source-probe', 'script', 'source-probe', metadata={'revision': 1}), _EDITED)
            edits.append(script_id)
        return real(owner_home, script_id, **kwargs)

    monkeypatch.setattr(script_runner, 'load_script', edit_then_load)
    return edits


def _terminal_rows(case):
    resources = case.host[0].plugin_host.composition.ports.observations.services.resources
    paths = resources.paths_for(SimpleNamespace(owner_home=resources.users.owner_home('alice')))
    return [json.loads(path.read_text()) for path in sorted(paths.terminal.glob('plugin-exec-*.json'))]


@pytest.mark.parametrize('action', ['execute.script', 'execute.workflow'])
def test_live_script_edit_after_admission_cannot_replace_invoked_body(asset_case, monkeypatch, action):
    case = asset_case
    _configure(case, 'agent')
    monkeypatch.setattr(script_runner, '_outer_seatbelt_active', lambda: True)
    edits = _edit_live_script_before_runner_load(case, monkeypatch)

    with pytest.raises(HostAPIError) as changed:
        _call(case, action, _payload(case, action), status=202)

    # 被调用的是已准入正文；活记录确已改写，原 live fence 仍在执行后拒绝漂移。
    assert edits == ['source-probe']
    assert json.loads(case.report.read_text()) == _EXPECTED
    assert ScriptStore(case.home, extra_roots=()).get_script('source-probe')['code'] == _EDITED
    assert (changed.value.code, changed.value.status) == ('ASSET_CHANGED', 409)
    (terminal,) = _terminal_rows(case)
    assert terminal['status'] == 'failed' and terminal['terminal_response']['error_code'] == 'ASSET_CHANGED'


def test_engine_reload_cannot_replace_admitted_workflow_steps(asset_case, monkeypatch):
    case = asset_case
    _configure(case, 'agent')
    monkeypatch.setattr(script_runner, '_outer_seatbelt_active', lambda: True)
    edited_report = case.workspace / 'edited-report.json'
    edited = _flow('source-flow', {**case.inputs, 'report': str(edited_report)})

    class DriftedStore(workflow_engine.WorkflowStore):
        # 模拟引擎自身按 ID 重读时观察到另一份可变版本（准入与 live fence 读取的仍是原定义）。
        def get_workflow(self, workflow_id):
            return edited if workflow_id == 'source-flow' else super().get_workflow(workflow_id)

    monkeypatch.setattr(workflow_engine, 'WorkflowStore', DriftedStore)

    accepted = _call(case, 'execute.workflow', _payload(case, 'execute.workflow'), status=202)

    assert accepted['accepted']
    assert json.loads(case.report.read_text()) == _EXPECTED and not edited_report.exists()
    (record,) = WorkflowRunStore(case.home).list_runs(workflow_id='source-flow')
    assert record.status == 'succeeded' and record.steps[0].output_snapshot == _EXPECTED
    (terminal,) = _terminal_rows(case)
    assert terminal['status'] == 'done' and terminal['terminal_response']['ok'] is True


# 不替换原生沙箱、不伪造就绪；平台/就绪缺失明确 skip。
@pytest.mark.parametrize('action', ['execute.script', 'execute.workflow'])
def test_native_execution_runs_only_the_admitted_body(asset_case, monkeypatch, action):
    if script_runner._outer_seatbelt_active():
        pytest.skip('外层 Seatbelt 禁止嵌套 sandbox_apply；未验证内层原生执行')
    case = asset_case
    _configure(case, 'agent')
    sandbox = attempt.AttemptExecutionSandbox(attempt.AttemptSandboxSpec(
        attempt_view=case.workspace, staging_root=case.workspace, shared_workspace=case.workspace,
        owner_home=case.home, network_access=False))
    try:
        sandbox.require_ready()
    except attempt.SandboxUnavailableError as exc:
        pytest.skip(f'原生沙箱未就绪：{exc}')
    edits = _edit_live_script_before_runner_load(case, monkeypatch)

    with pytest.raises(HostAPIError) as changed:
        _call(case, action, _payload(case, action), status=202)

    assert edits == ['source-probe'] and changed.value.code == 'ASSET_CHANGED'
    assert json.loads(case.report.read_text()) == _EXPECTED
