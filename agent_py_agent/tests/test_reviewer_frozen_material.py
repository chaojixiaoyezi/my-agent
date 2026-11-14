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
    assert material['review_gate'] == {'scope': 'workflow_effect.v1', 'step_id': 'release',
                                       'minimum_independent_reviews': 1, 'effect_started': False}
    assert material['step']['input_snapshot']['value'] == 7
    assert material['upstream_outputs'][0]['output_snapshot']['value'] == 7
    assert 'do-not-disclose' not in result.output
    assert 'different-current' not in material['script']['code']
    assert 'json.dumps' in material['script']['code'] and material['script']['coverage'] == 'complete'
    assert store.load_run(run.workflow_run_id).revision == run.revision
    assert leaf.repo.list_independent_reviews(leaf.owner, requirement_id=binding['requirement_id']) == []


def test_bound_reviewer_can_use_returned_query_after_wrong_root(leaf):  # noqa: F811
    _, _, binding = pending(leaf)
    result = invoke(leaf, {'action': 'query_provenance', 'requirement_id': binding['requirement_id'],
                           'root': 'an-unrelated-workspace'})
    assert not result.ok and result.effect_outcome == 'not_started'
    error = json.loads(result.output)
    assert error['reason'] == 'REVIEW_ROOT_MISMATCH' and 'workflow_review_material' not in error
    example = error['details']['query_example']
    assert example == {'action': 'query_provenance', 'requirement_id': binding['requirement_id']}
    corrected = invoke(leaf, example)
    assert corrected.ok and json.loads(corrected.output)['workflow_review_material']['binding'] == binding
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


def test_leaf_material_pages_roundtrip_through_real_archive_and_model_reducer(leaf):  # noqa: F811
    import hashlib
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_call_archive_record import (
        archive_tool_output_projection,
    )
    from agent_py_agent.agent.agent_core.tool_context.reducer import (
        render_tool_result_for_live_prompt,
    )
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.tests._tool_runtime_harness import canonical_history_call
    from agent_py_agent.tests.test_tool_output_externalizer import _tool_loop_params

    _, _, binding = pending(leaf)
    first = query(leaf, binding)
    original = json.loads(first.output)['workflow_review_material']
    manifest = json.loads(first.metadata['handler_details']['tool_output_policy']['live_prompt_output'])
    assert first.metadata['handler_details']['tool_output_policy']['live_prompt_output'] in render_tool_result_for_live_prompt(
        first, {'output_externalized': True, 'output_preview': 'unreadable old head'})
    assert manifest['input_version'] == binding['input_version']
    assert manifest['gate']['effect_started'] is False
    assert manifest['read_example']['requirement_id'] == binding['requirement_id']
    cursor = manifest['read_example']['material_cursor']
    content, offsets = [], []
    count = 0
    while cursor is not None:
        result = invoke(leaf, {'action': 'query_provenance', 'requirement_id': binding['requirement_id'],
                               'material_cursor': cursor})
        assert result.ok, result.output
        view = result.metadata['handler_details']['tool_output_policy']['live_prompt_output']
        assert len(json.dumps(view, ensure_ascii=False).encode()) <= 4000
        page = json.loads(view)
        assert page['sha256'] == manifest['sha256']
        assert page['source_coverage'] == original['coverage']
        offsets.append(page['offset'])
        assert page['offset'] == len(''.join(content))
        content.append(page['content'])
        call = canonical_history_call('manage_requirement_provenance', {}, call_id='page-' + str(count), run_id='review-run')
        projection = archive_tool_output_projection(
            SimpleNamespace(root=leaf.agent.root, config=AgentConfig(tool_output_externalize_min_chars=1)),
            _tool_loop_params(request_id='review-request', run_id='review-run', task_id='review-task'), call, ToolHandlerOutcome('manage_requirement_provenance', True, result.output,
                result_envelope=result.metadata['handler_details']))
        archive = projection.metadata['archive_output_record']
        assert archive['output_externalized'] is True
        assert json.loads(result.output) == page
        assert 'workflow_review_material' not in json.loads(result.output)
        rendered = render_tool_result_for_live_prompt(
            result, archive)
        assert view in rendered  # 强制外置仍看到本页，不能再用1200日志预览盖掉。
        cursor = page['next_cursor']
        count += 1
        assert count < 100
    joined = ''.join(content)
    assert hashlib.sha256(joined.encode()).hexdigest() == manifest['sha256']
    assert json.loads(joined) == original
    assert 1 <= count <= 4 and len(offsets) == len(set(offsets))
    assert leaf.repo.list_independent_reviews(leaf.owner, requirement_id=binding['requirement_id']) == []


def test_material_cursor_rejects_changed_source_and_wrong_requirement_without_leak(leaf):  # noqa: F811
    store, run, binding = pending(leaf)
    first = json.loads(query(leaf, binding).metadata['handler_details']['tool_output_policy']['live_prompt_output'])
    args = first['read_example']
    denied = invoke(leaf, {**args, 'requirement_id': 'another-private-requirement'})
    assert not denied.ok and 'content' not in json.loads(denied.output)
    run.steps[0].output_snapshot = {'changed': 'new frozen fact'}
    store.save_run(run)
    changed = invoke(leaf, args)
    assert not changed.ok and 'REVIEW_MATERIAL_CHANGED' in changed.output
    assert 'new frozen fact' not in changed.output
    assert leaf.repo.list_independent_reviews(leaf.owner, requirement_id=binding['requirement_id']) == []


@pytest.mark.parametrize('cursor', ['not-a-cursor', '0' * 64 + ':99999999', '0' * 64 + ':-1'])
def test_material_cursor_is_structural_and_bounded(leaf, cursor):  # noqa: F811
    _, _, binding = pending(leaf)
    result = invoke(leaf, {'action': 'query_provenance', 'requirement_id': binding['requirement_id'],
                           'material_cursor': cursor})
    assert not result.ok and result.effect_outcome == 'not_started'
    assert 'material_page' not in json.loads(result.output)


def test_material_page_errors_give_distinct_safe_recovery(leaf, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.agent_core.orchestration.tools import (
        requirement_review_material as material,
    )

    _, _, binding = pending(leaf)
    expected = {
        'REVIEW_MATERIAL_CURSOR_INVALID': ('格式错误', 'read_example'),
        'REVIEW_MATERIAL_CHANGED': ('旧材料摘要', '丢弃全部旧页'),
        'REVIEW_MATERIAL_OFFSET_INVALID': ('偏移越界', 'next_cursor'),
        'REVIEW_MATERIAL_PAGE_BUDGET_TOO_SMALL': ('内部传输预算不足', '停止原样重试'),
    }
    for reason, words in expected.items():
        def reject(*args, _reason=reason, **kwargs):
            raise material.ReviewMaterialError(_reason)
        monkeypatch.setattr(material, 'review_material_page', reject)
        result = invoke(leaf, {'action': 'query_provenance', 'requirement_id': binding['requirement_id'],
                               'material_cursor': 'private-untrusted-cursor-body'})
        assert not result.ok and result.effect_outcome == 'not_started'
        payload = json.loads(result.output)
        assert payload['reason'] == reason
        assert all(word in payload['message'] for word in (*words, '未写入'))
        assert 'private-untrusted-cursor-body' not in result.output
        assert payload['details']['actual_type'] == 'str'
        assert payload['details']['actual_chars'] == 29
        if reason == 'REVIEW_MATERIAL_OFFSET_INVALID':
            assert payload['details']['total_chars'] > 0
    assert leaf.repo.list_independent_reviews(leaf.owner, requirement_id=binding['requirement_id']) == []
