"""审核材料来自原运行与内容寻址脚本；模型正文不是材料或授权。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.native_governance import NativeWorkflowGovernance
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_leaf_provenance_review import invoke, leaf  # noqa: F401


def pending(case, *, business=None):
    home = case.agent.home_paths.owner_home_dir
    scripts = ScriptStore(home)
    scripts.save_script(AssetDescriptor(id='release-review', kind='script', name='待审脚本'),
                        'import json\nprint(json.dumps({"value": 7}))')
    store = WorkflowRunStore(home)
    gov = NativeWorkflowGovernance(None, store, owner_home=home, owner_id=case.owner, author_provider=lambda: 'parent-run')
    engine = WorkflowEngine(home, workspace_root=case.agent.root, run_authorizer=gov.require_effect,
                            analysis_handler=lambda *_: {'value': 7, 'api_key': 'do-not-disclose'})
    flow = {'descriptor': {'id': 'review-material', 'kind': 'workflow', 'name': '审核材料',
            'metadata': {'workflow_review_policy': {'steps': ['release']}}}, 'steps': [
        {'step_id': 'prefix', 'name': 'prefix', 'mode': 'llm_skill_analysis'},
        {'step_id': 'release', 'name': 'release', 'mode': 'deterministic_script', 'script_id': 'release-review',
         'inputs_map': {'value': {'step_id': 'prefix', 'key': 'value'}}}]}
    run = engine.run_workflow(flow, owner_id=case.owner, business_context=business or case.business)
    assert run.steps[-1].error_code == 'WORKFLOW_REVIEW_REQUIRED', run.to_dict()
    return store, run, run.steps[-1].escalation_result['pending_review']


def query(case, binding):
    return invoke(case, {'action': 'query_provenance', 'requirement_id': binding['requirement_id']})


def test_real_leaf_query_reads_frozen_inputs_upstream_and_hash_checked_script(leaf):  # noqa: F811
    store, run, binding = pending(leaf)
    # 当前可编辑脚本变化不能污染原待审候选材料。
    ScriptStore(leaf.agent.home_paths.owner_home_dir).save_script(
        AssetDescriptor(id='release-review', kind='script', name='新版'), 'print("different-current")')
    result = query(leaf, binding)
    assert result.ok, result.output
    material = json.loads(result.output)['workflow_review_material']
    assert material['binding'] == binding
    assert material['step']['input_snapshot']['value'] == 7
    assert material['upstream_outputs'][0]['output_snapshot']['value'] == 7
    assert 'do-not-disclose' not in result.output
    assert 'different-current' not in material['script']['code']
    assert 'json.dumps' in material['script']['code'] and material['script']['coverage'] == 'complete'
    assert store.load_run(run.workflow_run_id).revision == run.revision
    assert leaf.repo.list_independent_reviews(leaf.owner, requirement_id=binding['requirement_id']) == []


@pytest.mark.parametrize('change', ['attempt', 'candidate', 'version', 'effect_unknown'])
def test_stale_or_unproven_pending_binding_returns_no_material(leaf, change):  # noqa: F811
    store, run, binding = pending(leaf)
    step = run.steps[-1]
    if change == 'effect_unknown':
        step.escalation_result.pop('effect_started')
    else:
        field = {'attempt': 'attempt_id', 'candidate': 'candidate_hash', 'version': 'input_version'}[change]
        step.escalation_result['pending_review'][field] = 'wrong'
    store.save_run(run)
    result = query(leaf, binding)
    assert not result.ok and result.effect_outcome == 'not_started'
    assert 'workflow_review_material' not in result.output


def test_missing_code_blob_is_explicit_partial_never_current_fallback(leaf):  # noqa: F811
    store, run, binding = pending(leaf)
    digest = store.load_workflow_snapshot(run.workflow_run_id)['_frozen_scripts']['release']['code_sha256']
    home = leaf.agent.home_paths.owner_home_dir
    (home / 'data' / 'asset_evolution' / 'blobs' / digest[:2] / digest).unlink()
    result = query(leaf, binding)
    assert result.ok, result.output
    material = json.loads(result.output)['workflow_review_material']
    assert material['coverage'] == 'partial' and material['script']['coverage'] == 'unavailable'
    assert material['script']['reason_code'] == 'FROZEN_SCRIPT_BLOB_MISSING'
    assert 'code' not in material['script']


def test_ordinary_provenance_query_keeps_existing_payload(leaf):  # noqa: F811
    result = invoke(leaf, {'action': 'query_provenance', 'requirement_id': 'R'})
    assert result.ok and 'workflow_review_material' not in json.loads(result.output)


def test_material_projection_is_bounded_and_does_not_leak_secret_prefixes(leaf):  # noqa: F811
    store, run, binding = pending(leaf)
    step = run.steps[-1]
    step.input_snapshot = {'api_key': 'private-secret' * 10000, 'large': 'x' * 100000,
                           'nested': [{'value': 'a' * 5000} for _ in range(200)]}
    store.save_run(run)
    result = query(leaf, binding)
    assert result.ok, result.output
    material = json.loads(result.output)['workflow_review_material']
    assert material['coverage'] == 'partial' and material['truncated'] is True
    assert 'private-secret' not in result.output
    assert len(json.dumps(material)) < 65000


def test_other_business_task_cannot_read_frozen_material(leaf):  # noqa: F811
    _, _, binding = pending(leaf, business=replace(leaf.business, task_id='other-task'))
    result = query(leaf, binding)
    assert not result.ok and 'workflow_review_material' not in result.output
    assert 'REVIEW_TASK_SCOPE_MISMATCH' in result.output
