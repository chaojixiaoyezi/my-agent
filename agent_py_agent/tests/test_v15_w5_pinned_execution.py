"""W1真实历史存储到引擎/W6 seam的冻结引用合同。"""

import inspect

import pytest

from agent_py_agent.agent.assets.revision_history import record_version
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.workflows.engine import WorkflowEngine, _script_pin
from agent_py_agent.agent.workflows.models import ScriptExecutionResult
from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow


# LLM: 版本由W1原接口发表，不手写历史记录。
# 函数用途: 冻结脚本真实正文和执行身份。
def publish_script(home):
    row = ScriptStore(home).get_script('collect')
    pin = _script_pin('collect', row)
    version = record_version(home, 'script', 'collect', payload=row, code=row['code'].encode(),
                             execution_definition_ref=pin['execution_definition_ref'], storage_revision=row.get('revision', 0))
    return pin, version['record']


# LLM: 只替代尚未集成的W6历史代码执行；严格核真实引擎传入pin。
# 函数用途: 观察运行seam实际收到的冻结代码摘要。
def observe_seam(monkeypatch):
    seen = []

    # LLM: 结果是组件替身，不声称执行了历史脚本字节。
    # 函数用途: 回传本次seam收到的代码身份。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        seen.append(expected_script_pin)
        return ScriptExecutionResult(script_id, True, 0, 1, 1, 2,
                                     {'value': expected_script_pin['code_sha256']}, '', '')

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources', execute)
    return seen


# LLM: 必须在创建运行之前拒绝未知版本，不能留下幽灵run。
# 函数用途: 验证公开keyword和结构化版本缺失回执。
def test_unknown_workflow_version_has_no_run(tmp_path):
    parameter = inspect.signature(WorkflowEngine.run_workflow).parameters.get('definition_version')
    assert parameter is not None and parameter.kind == parameter.KEYWORD_ONLY and parameter.default is None
    WorkflowStore(tmp_path).save_workflow(workflow(step('judge', mode='llm_skill_analysis')))
    result = WorkflowEngine(tmp_path).run_workflow('daily', definition_version=99)
    assert result['error_code'] == 'ASSET_VERSION_NOT_FOUND' and result['effect_outcome'] == 'not_started'
    assert not list((tmp_path / 'data/workflow_runs').glob('*/run.json'))


# LLM: 当前脚本及工作流均改变后，版本运行仍从历史定义、Markdown与脚本pins恢复。
# 函数用途: 验证workflow@v1连锁固定script@v1及持久化字段。
@pytest.mark.parametrize('flat', [False, True])
def test_workflow_version_uses_history_definition_and_chained_script_pin(tmp_path, monkeypatch, flat):
    assert 'definition_version' in inspect.signature(WorkflowEngine.run_workflow).parameters
    script(tmp_path)
    old_pin, old = publish_script(tmp_path)
    store = WorkflowStore(tmp_path)
    saved = store.save_workflow(workflow(step(on_error='fail')))
    payload = saved.to_dict()
    if flat:
        payload = {**saved.descriptor.to_dict(), 'steps': [item.to_dict() for item in saved.steps], 'body_markdown': ''}
    record_version(tmp_path, 'workflow', 'daily', payload=payload, side_files={'WORKFLOW.md': 'historical body'},
                   pins={'scripts': {'collect': {'version': old['version'], 'code_sha256': old_pin['code_sha256'],
                                                'execution_definition_ref': old_pin['execution_definition_ref']}}})
    script(tmp_path, 'print(\'{"value":2}\')')
    publish_script(tmp_path)
    store.save_workflow(workflow(step('current', mode='llm_skill_analysis')))
    seen = observe_seam(monkeypatch)
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: pytest.fail('current definition executed'))
    run = engine.run_workflow('daily', definition_version=1)
    assert run.status == 'succeeded' and run.definition_version == 1
    assert len(seen) == 1 and seen[0]['code_sha256'] == old_pin['code_sha256']
    assert run.final_output == {'value': old_pin['code_sha256']}
    snapshot = engine.store.load_workflow_snapshot(run.workflow_run_id)
    assert snapshot['body_markdown'] == 'historical body'
    assert snapshot['_frozen_scripts']['collect']['code_sha256'] == old_pin['code_sha256']
    assert engine.store.load_run(run.workflow_run_id).definition_version == 1


# LLM: 在途运行冻结旧sha，显式allow才可改用当前；缺blob时保留原漂移门。
# 函数用途: 验证EC39恢复、缺失历史及显式重新钉住。
@pytest.mark.parametrize('remove_blob,allow,expected_status', [
    (False, False, 'succeeded'), (True, False, 'paused_for_resume'), (True, True, 'succeeded')])
def test_resume_preserves_historical_pin_unless_explicitly_allowed(tmp_path, monkeypatch, remove_blob, allow, expected_status):
    script(tmp_path)
    old_pin, _ = publish_script(tmp_path)
    seen = observe_seam(monkeypatch)
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: {'_pause_for_resume': True})
    run = engine.run_workflow(workflow(step('pause', mode='llm_skill_analysis'), step(on_error='fail')))
    assert run.status == 'paused_for_resume' and not seen
    script(tmp_path, 'print(\'{"value":2}\')')
    current_pin, _ = publish_script(tmp_path)
    if remove_blob:
        sha = old_pin['code_sha256']
        (tmp_path / 'data/asset_evolution/blobs' / sha[:2] / sha).unlink()
    resumed = engine.resume_workflow_run(run.workflow_run_id, patched_step_output={'ready': True},
                                          allow_script_drift=allow)
    assert resumed.status == expected_status
    if expected_status == 'succeeded':
        expected = current_pin if allow else old_pin
        assert seen[-1]['code_sha256'] == expected['code_sha256']
        assert resumed.final_output['value'] == expected['code_sha256']
    else:
        assert not seen and resumed.steps[1].error_code == 'SCRIPT_REVISION_DRIFT'
