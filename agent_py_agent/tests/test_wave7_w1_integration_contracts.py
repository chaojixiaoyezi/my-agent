"""W1 待集成合同：从 W2 测试中原样分离，保留普通断言，不 skip/xfail。

这些测试仍被 pytest 全仓正常收集。W2 定向通过不代表 W1 CAS、恢复快照、最终输出合同已实现。
复现: python3 -m pytest agent_py_agent/tests/test_wave7_w1_integration_contracts.py -q --tb=short
"""
from __future__ import annotations

import json


def test_f06_script_tool_requires_and_honors_revision(tmp_path):
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool

    tool = ScriptsTool(tmp_path/'owner')
    descriptor = {'name':'CAS','access_targets':[],'data_sources':[],'credential_refs':[],'io_contract':{}}
    assert tool.execute({'action':'register','script_id':'cas','descriptor':descriptor,'code':'print("{}")'}).ok
    current = json.loads(tool.execute({'action':'get','script_id':'cas'}).output)
    revision = current['revision']
    assert tool.execute({'action':'update','script_id':'cas','expected_revision':revision,'code':'print("{}")\n# new revision'}).ok
    stale = tool.execute({'action':'update','script_id':'cas','expected_revision':revision,'code':'print("{}")'})
    assert not stale.ok


def test_f14_resumed_workflow_does_not_execute_changed_script(tmp_path):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStepSpec
    from agent_py_agent.agent.workflows import ScriptRunner, WorkflowEngine

    home = tmp_path/'owner'
    store = ScriptStore(home)
    for name,code in [('pause','print(\'{"_pause_for_resume":true}\')'),('tail','print(\'{"version":1}\')')]:
        store.save_script(AssetDescriptor(id=name,kind='script',name=name),code)
    engine = WorkflowEngine(home)
    engine.runner = ScriptRunner(home,use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor(id='flow',kind='workflow',name='flow'),tuple(
        WorkflowStepSpec(name,name,'deterministic_script',script_id=name) for name in ('pause','tail')))
    paused = engine.run_workflow(definition)
    assert paused.status == 'paused_for_resume'
    store.save_script(AssetDescriptor(id='tail',kind='script',name='tail'),'print(\'{"version":2}\')')
    resumed = engine.resume_workflow_run(paused.workflow_run_id,patched_step_output={'_pause_for_resume':False},resolution_origin='system')
    assert resumed.status != 'succeeded' or resumed.final_output.get('version') == 1


def test_f15_workflow_final_output_must_pass_contract(tmp_path):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor, IOContract
    from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStepSpec
    from agent_py_agent.agent.workflows import WorkflowEngine

    definition = WorkflowDefinition(AssetDescriptor(id='contract',kind='workflow',name='contract',
        io_contract=IOContract(outputs=('report_uri',),output_types={'report_uri':'string'})),
        (WorkflowStepSpec('analysis','analysis','llm_skill_analysis'),))
    result = WorkflowEngine(tmp_path/'owner',analysis_handler=lambda *args:{}).run_workflow(definition)
    assert result.status == 'failed'
