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


def test_f06_self_map_bootstrap_wires_scripts_tool_workspace_provider(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.self_map_bootstrap import register_self_map
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore

    home, project = tmp_path / 'owner', tmp_path / 'project'
    home.mkdir()
    project.mkdir()
    ScriptStore(home, workspace_root=project).save_script(
        AssetDescriptor(id='proj_only', kind='script', name='项目专属脚本'),
        'print("{\\"from_workspace\\": true}")',
        target_scope='workspace',
    )
    registered = {}
    agent = SimpleNamespace(
        tools=SimpleNamespace(owner_type='main_agent', register=lambda t: registered.setdefault(type(t).__name__, t)),
        home_paths=SimpleNamespace(owner_home_dir=home, is_admin=False),
        config=SimpleNamespace(enable_self_map=False),
        effective_workspace_root=project,
        current_skill_snapshot=lambda: None,
    )
    register_self_map(agent)
    scripts_tool = registered['ScriptsTool']
    got = json.loads(scripts_tool.execute({'action': 'get', 'script_id': 'proj_only'}).output)
    assert got['scope_layer'] == 'workspace'


def test_e1_end_to_end_autonomous_learning_and_router_closed_loop(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import asset_promotion
    from agent_py_agent.agent.capability.router import CapabilityRouter
    from agent_py_agent.agent.capability.skill_learning_maintenance import (
        LearningObservationSource,
        observe_learning,
    )
    from agent_py_agent.agent.capability.skill_learning_publish import PublishRequest
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotEntry
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.tests.test_wave7_autonomous_learning_closure import (
        decision,
        snapshot,
    )

    home = ensure_my_agent_home(tmp_path / 'home')
    store = SkillLearningStore.for_home(home)
    dec = decision('e1-csv-deduper')
    event = asset_promotion.promote_verified_skill(store, PublishRequest(dec), None)
    assert event is not None and event.event == 'published'
    reg_entry = store.load_registry().skills['e1-csv-deduper']
    entries = [
        SkillSnapshotEntry(
            'owner:e1-csv-deduper',
            'e1-csv-deduper',
            dec.description,
            str(store.skill_path('e1-csv-deduper')),
            'owner',
            'learned',
            reg_entry.sha256,
        )
    ]
    router = CapabilityRouter(skill_snapshot=snapshot(entries))
    assert 'e1-csv-deduper' in router.render_skill_metadata_index(context_window_tokens=128000)
    search_out = json.loads(
        SkillSearchTool(SimpleNamespace(capability_router=router)).execute({'query': 'e1-csv-deduper'}).output
    )
    assert any('e1-csv-deduper' in str(m.get('skill_id') or m.get('id') or '') for m in search_out['matches'])
    observe_learning(
        store,
        'e1-csv-deduper',
        sha256=reg_entry.sha256,
        task_id='e1-task-1',
        outcome='adopted',
    )
    observe_learning(
        store,
        'e1-csv-deduper',
        sha256=reg_entry.sha256,
        task_id='e1-task-1',
        outcome='effective',
        source=LearningObservationSource(authority='host', receipt_id='e1-task-1'),
    )
    updated_entry = store.load_registry().skills['e1-csv-deduper']
    assert updated_entry.maintenance.get('helped_count') == 1 and updated_entry.maintenance.get('last_used_at', 0) > 0
