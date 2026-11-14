"""Tests for WF-09 (Formal Nested Subworkflows) and WF-11 (Bounded Workflow/Step Health View)."""
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.conversation.business_contracts import WorkflowScope
from agent_py_agent.agent.workflows.business_scope import with_state, workflow_state
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.pinned_definitions import (
    PinnedDefinitionError,
    freeze_execution_definition,
    resolve_pinned_definition,
)
from agent_py_agent.agent.workflows.query_health import (
    build_workflow_health_view,
    summarize_run_step_health,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore


def _descriptor(workflow_id: str, owner_id: str = 'local_owner', **kwargs) -> AssetDescriptor:
    return AssetDescriptor(
        id=workflow_id,
        kind='workflow',
        name=f'Name_{workflow_id}',
        owner_id=owner_id,
        **kwargs,
    )


def _setup_script(owner_home: Path, script_id: str, output_dict: dict | None = None, fail: bool = False) -> str:
    store = ScriptStore(owner_home)
    desc = AssetDescriptor(id=script_id, kind='script', name=f'Script_{script_id}', owner_id='local_owner')
    if fail:
        code = 'import sys\nsys.stderr.write("Simulated script failure\\n")\nsys.exit(1)\n'
    else:
        out = output_dict or {'result': f'output_from_{script_id}'}
        code = f'import json\nprint(json.dumps({json.dumps(out)}))\n'
    store.save_script(desc, code)
    return script_id


# =========================================================================
# WF-09: Nested Subworkflow Formal Execution Tests
# =========================================================================

def test_subworkflow_ref_resolution_and_validation(tmp_path):
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    # 1. Missing subworkflow_ref
    step_no_ref = WorkflowStepSpec('sw_missing', 'No Ref', 'subworkflow')
    wf_no_ref = WorkflowDefinition(_descriptor('wf_no_ref'), (step_no_ref,))
    store.save_workflow(wf_no_ref)
    run1 = engine.run_workflow(wf_no_ref)
    assert run1.status in ('failed', 'paused_for_resume')
    assert run1.steps[0].error_code == 'SUBWORKFLOW_REF_INVALID'
    assert run1.steps[0].needs_repair is True

    # 2. Invalid characters in config workflow_id
    step_bad_ref = WorkflowStepSpec('sw_bad', 'Bad Ref', 'subworkflow', config={'workflow_id': '../../evil'})
    wf_bad_ref = WorkflowDefinition(_descriptor('wf_bad_ref'), (step_bad_ref,))
    store.save_workflow(wf_bad_ref)
    run2 = engine.run_workflow(wf_bad_ref)
    assert run2.status in ('failed', 'paused_for_resume')
    assert run2.steps[0].error_code == 'SUBWORKFLOW_REF_INVALID'

    # 3. Child workflow not found in store
    step_not_found = WorkflowStepSpec('sw_nf', 'Not Found', 'subworkflow', subworkflow_ref='workflow:non_existent_child')
    wf_nf = WorkflowDefinition(_descriptor('wf_nf'), (step_not_found,))
    store.save_workflow(wf_nf)
    run3 = engine.run_workflow(wf_nf)
    assert run3.status in ('failed', 'paused_for_resume')
    assert run3.steps[0].error_code == 'SUBWORKFLOW_REF_INVALID'


def test_subworkflow_cycle_detection_direct_and_indirect(tmp_path):
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    # Direct cycle: A -> A
    step_self = WorkflowStepSpec('sw_self', 'Self', 'subworkflow', subworkflow_ref='workflow:wf_cycle_self')
    wf_self = WorkflowDefinition(_descriptor('wf_cycle_self'), (step_self,))
    store.save_workflow(wf_self)
    run_self = engine.run_workflow(wf_self)
    assert run_self.status in ('failed', 'paused_for_resume')
    assert run_self.steps[0].error_code == 'SUBWORKFLOW_CYCLE_DETECTED'
    assert run_self.steps[0].needs_repair is True

    # Indirect cycle: A -> B -> A
    step_a_to_b = WorkflowStepSpec('sw_a', 'Call B', 'subworkflow', subworkflow_ref='workflow:wf_indirect_b')
    wf_a = WorkflowDefinition(_descriptor('wf_indirect_a'), (step_a_to_b,))
    step_b_to_a = WorkflowStepSpec('sw_b', 'Call A', 'subworkflow', subworkflow_ref='workflow:wf_indirect_a')
    wf_b = WorkflowDefinition(_descriptor('wf_indirect_b'), (step_b_to_a,))
    store.save_workflow(wf_a)
    store.save_workflow(wf_b)

    run_indirect = engine.run_workflow(wf_a)
    assert run_indirect.status in ('failed', 'paused_for_resume')
    assert run_indirect.steps[0].status == 'failed'
    assert run_indirect.steps[0].error_code == 'SUBWORKFLOW_CYCLE_DETECTED'


def test_subworkflow_bounded_depth_enforcement(tmp_path):
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    _setup_script(owner_home, 'leaf_script', {'leaf': 'done'})
    step_leaf = WorkflowStepSpec('s_leaf', 'Leaf', 'deterministic_script', script_id='leaf_script')
    wf_leaf = WorkflowDefinition(_descriptor('wf_leaf'), (step_leaf,))
    store.save_workflow(wf_leaf)

    step_w1 = WorkflowStepSpec('sw_w1', 'Call leaf', 'subworkflow', subworkflow_ref='workflow:wf_leaf')
    wf_w1 = WorkflowDefinition(_descriptor('wf_w1'), (step_w1,))
    store.save_workflow(wf_w1)

    step_w0 = WorkflowStepSpec('sw_w0', 'Call W1', 'subworkflow', subworkflow_ref='workflow:wf_w1')
    wf_w0 = WorkflowDefinition(_descriptor('wf_w0'), (step_w0,))
    store.save_workflow(wf_w0)

    # 1. Configured max_depth = 1: Root calls W1 (depth 0 < 1: allowed); W1 calls leaf (depth 1 >= 1: rejected)
    engine.max_subworkflow_depth = 1
    run1 = engine.run_workflow(wf_w0)
    assert run1.status in ('failed', 'paused_for_resume')
    assert run1.steps[0].status == 'failed'
    assert run1.steps[0].error_code == 'SUBWORKFLOW_MAX_DEPTH_EXCEEDED'

    # 2. Default max_depth = 5: build chain of 6 subworkflows (W0 -> W1 -> W2 -> W3 -> W4 -> W5 -> leaf)
    engine_default = WorkflowEngine(owner_home)  # default max_subworkflow_depth = 5
    wf_prev = 'wf_leaf'
    for lvl in range(5, -1, -1):
        step_lvl = WorkflowStepSpec(f'sw_{lvl}', f'Call {wf_prev}', 'subworkflow', subworkflow_ref=f'workflow:{wf_prev}')
        wf_lvl = WorkflowDefinition(_descriptor(f'wf_lvl_{lvl}'), (step_lvl,))
        store.save_workflow(wf_lvl)
        wf_prev = f'wf_lvl_{lvl}'

    # At W0 (wf_lvl_0), calls W1..W5..leaf -> depth reaches 5 at W5, W5 calling leaf exceeds max_depth 5
    run_deep = engine_default.run_workflow('wf_lvl_0')
    assert run_deep.status in ('failed', 'paused_for_resume')
    assert run_deep.steps[0].status == 'failed'
    assert run_deep.steps[0].error_code == 'SUBWORKFLOW_MAX_DEPTH_EXCEEDED'



def test_subworkflow_disabled_rejection(tmp_path):
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    _setup_script(owner_home, 'dummy_script')
    child_step = WorkflowStepSpec('cs1', 'Child step', 'deterministic_script', script_id='dummy_script')
    child_wf = WorkflowDefinition(_descriptor('wf_disabled_child', enabled=False), (child_step,), enabled=False)
    store.save_workflow(child_wf)

    parent_step = WorkflowStepSpec('ps1', 'Call disabled child', 'subworkflow', subworkflow_ref='workflow:wf_disabled_child')
    parent_wf = WorkflowDefinition(_descriptor('wf_parent_of_disabled'), (parent_step,))
    store.save_workflow(parent_wf)

    run = engine.run_workflow(parent_wf)
    assert run.status in ('failed', 'paused_for_resume')
    assert run.steps[0].error_code == 'SUBWORKFLOW_DISABLED'
    assert run.steps[0].needs_repair is True


def test_subworkflow_scope_and_governance(tmp_path):
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    _setup_script(owner_home, 'scope_script')

    # 1. Cross owner rejection
    bob_child_step = WorkflowStepSpec('bs1', 'Bob step', 'deterministic_script', script_id='scope_script')
    bob_child_wf = WorkflowDefinition(_descriptor('wf_bob_child', owner_id='bob_owner'), (bob_child_step,))
    store.save_workflow(bob_child_wf)

    alice_parent_step = WorkflowStepSpec('as1', 'Call bob', 'subworkflow', subworkflow_ref='workflow:wf_bob_child')
    alice_parent_wf = WorkflowDefinition(_descriptor('wf_alice_parent', owner_id='alice_owner'), (alice_parent_step,))
    store.save_workflow(alice_parent_wf)

    run_cross = engine.run_workflow(alice_parent_wf, owner_id='alice_owner')
    assert run_cross.status in ('failed', 'paused_for_resume')
    assert run_cross.steps[0].error_code == 'SUBWORKFLOW_SCOPE_DENIED'

    # 2. Space governance: unassigned workflow invoked under space governance
    unassigned_wf = WorkflowDefinition(_descriptor('wf_unassigned'), (WorkflowStepSpec('us1', 'step', 'deterministic_script', script_id='scope_script'),))
    saved_un = store.save_workflow(unassigned_wf)
    store.apply_business_operation(
        'wf_unassigned',
        action='workflows.scope.set',
        intent={'scope': {'scope_mode': 'unassigned', 'space_ids': []}},
        idempotency_key='k_un',
        expected_revision=saved_un.descriptor.revision,
        revision_target='definition',
        transform=lambda prev: (with_state(prev, {**workflow_state(prev), 'scope': WorkflowScope('unassigned', (), 1).to_dict()}), {'status': 'ok'}),
    )

    space_parent_step = WorkflowStepSpec('sps1', 'Call unassigned', 'subworkflow', subworkflow_ref='workflow:wf_unassigned')
    space_parent_wf = WorkflowDefinition(_descriptor('wf_space_parent'), (space_parent_step,))
    store.save_workflow(space_parent_wf)

    step_rec = StepExecutionRecord('sps1', 'Step', 'subworkflow', 'running', 0.0, 0.0, 0.0, False)
    from agent_py_agent.agent.workflows.subworkflow_steps import execute_subworkflow_step
    parent_record = WorkflowRunRecord(
        'run_sp_parent',
        'wf_space_parent',
        'local_owner',
        'manual',
        'running',
        0.0,
        0.0,
        0.0,
        business_context={'space_id': 'sp_finance', 'strict_space_governance': True},
    )
    execute_subworkflow_step(
        engine=engine,
        run=parent_record,
        workflow=space_parent_wf,
        step=step_rec,
        spec=space_parent_step.to_dict(),
    )
    assert step_rec.status == 'failed'
    assert step_rec.error_code == 'SUBWORKFLOW_SCOPE_DENIED'


def test_subworkflow_space_mismatch_denied(tmp_path):
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    _setup_script(owner_home, 'space_script_mismatch')
    child_wf = WorkflowDefinition(_descriptor('wf_space2_child'), (WorkflowStepSpec('s2', 'step', 'deterministic_script', script_id='space_script_mismatch'),))
    saved_child = store.save_workflow(child_wf)
    store.apply_business_operation(
        'wf_space2_child',
        action='workflows.scope.set',
        intent={'scope': {'scope_mode': 'spaces', 'space_ids': ['space_marketing']}},
        idempotency_key='k_sp2',
        expected_revision=saved_child.descriptor.revision,
        revision_target='definition',
        transform=lambda prev: (with_state(prev, {**workflow_state(prev), 'scope': WorkflowScope('spaces', ('space_marketing',), 1).to_dict()}), {'status': 'ok'}),
    )

    parent_step = WorkflowStepSpec('p_step', 'Call space2 child', 'subworkflow', subworkflow_ref='workflow:wf_space2_child')
    parent_wf = WorkflowDefinition(_descriptor('wf_space1_parent'), (parent_step,))
    store.save_workflow(parent_wf)

    step_rec = StepExecutionRecord('p_step', 'Step', 'subworkflow', 'running', 0.0, 0.0, 0.0, False)
    from agent_py_agent.agent.workflows.subworkflow_steps import execute_subworkflow_step
    parent_record = WorkflowRunRecord(
        'run_sp1_parent',
        'wf_space1_parent',
        'local_owner',
        'manual',
        'running',
        0.0,
        0.0,
        0.0,
        business_context={'space_id': 'space_finance', 'strict_space_governance': True},
    )
    execute_subworkflow_step(
        engine=engine,
        run=parent_record,
        workflow=parent_wf,
        step=step_rec,
        spec=parent_step.to_dict(),
    )
    assert step_rec.status == 'failed'
    assert step_rec.error_code == 'SUBWORKFLOW_SCOPE_DENIED'



def test_subworkflow_pinned_definition_matching_and_mismatch(tmp_path):
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    _setup_script(owner_home, 'pin_script', {'pin': 'value'})
    child_step = WorkflowStepSpec('cs', 'Child', 'deterministic_script', script_id='pin_script')
    child_wf = WorkflowDefinition(_descriptor('wf_pin_child'), (child_step,))
    store.save_workflow(child_wf)

    frozen = freeze_execution_definition(owner_home, child_wf)
    correct_ref = frozen['execution_definition_ref']

    # 1. Matching pinned_definition_ref -> succeeds
    match_step = WorkflowStepSpec('ms', 'Match', 'subworkflow', subworkflow_ref='workflow:wf_pin_child',
                                  config={'pinned_definition_ref': correct_ref})
    parent_wf_match = WorkflowDefinition(_descriptor('wf_parent_match'), (match_step,))
    store.save_workflow(parent_wf_match)

    run_match = engine.run_workflow(parent_wf_match)
    assert run_match.status == 'succeeded'
    assert run_match.steps[0].status == 'succeeded'
    assert run_match.steps[0].escalation_result['subworkflow_receipt']['pinned_definition_ref'] == correct_ref

    # 2. Mismatched pinned_definition_ref -> fails with SUBWORKFLOW_PIN_MISMATCH
    mismatch_step = WorkflowStepSpec('mms', 'Mismatch', 'subworkflow', subworkflow_ref='workflow:wf_pin_child',
                                     config={'pinned_definition_ref': 'sha256:0000000000000000000000000000000000000000000000000000000000000000'})
    parent_wf_mismatch = WorkflowDefinition(_descriptor('wf_parent_mismatch'), (mismatch_step,))
    store.save_workflow(parent_wf_mismatch)

    run_mismatch = engine.run_workflow(parent_wf_mismatch)
    assert run_mismatch.status in ('failed', 'paused_for_resume')
    assert run_mismatch.steps[0].error_code == 'SUBWORKFLOW_PIN_MISMATCH'


def test_nested_subworkflows_parent_child_linkage_and_output_mapping(tmp_path):
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    # Grandchild: executes script
    _setup_script(owner_home, 'gc_script', {'grandchild_key': 'grandchild_val', 'shared_key': 'from_gc'})
    step_gc = WorkflowStepSpec('step_gc', 'GC Step', 'deterministic_script', script_id='gc_script')
    wf_grandchild = WorkflowDefinition(_descriptor('wf_grandchild'), (step_gc,))
    store.save_workflow(wf_grandchild)

    # Child: calls grandchild and maps output
    step_call_gc = WorkflowStepSpec(
        'step_call_gc',
        'Call Grandchild',
        'subworkflow',
        subworkflow_ref='workflow:wf_grandchild',
        config={'output_mapping': {'child_mapped': 'grandchild_key'}},
    )
    wf_child = WorkflowDefinition(_descriptor('wf_child'), (step_call_gc,))
    store.save_workflow(wf_child)

    # Root: calls child with input mapping
    step_call_child = WorkflowStepSpec(
        'step_call_child',
        'Call Child',
        'subworkflow',
        subworkflow_ref='workflow:wf_child',
        config={'inputs': {'init_flag': True}},
    )
    wf_root = WorkflowDefinition(_descriptor('wf_root'), (step_call_child,))
    store.save_workflow(wf_root)

    # Execute root workflow
    root_run = engine.run_workflow(wf_root, inputs={'root_input': 'hello'})
    assert root_run.status == 'succeeded'

    # Check root step
    root_step = root_run.steps[0]
    assert root_step.status == 'succeeded'
    child_run_id = root_step.subworkflow_run_id
    assert child_run_id != ''
    assert root_step.effect_id == child_run_id
    receipt = root_step.escalation_result['subworkflow_receipt']
    assert receipt['child_workflow_id'] == 'wf_child'
    assert receipt['child_run_id'] == child_run_id
    assert receipt['status'] == 'succeeded'
    assert receipt['depth'] == 1

    # Load child run from store and verify linkage
    child_run = engine.store.load_run(child_run_id)
    assert child_run is not None
    assert child_run.parent_workflow_run_id == root_run.workflow_run_id
    assert child_run.subworkflow_depth == 1
    assert child_run.status == 'succeeded'

    # Check grandchild linkage
    child_step = child_run.steps[0]
    assert child_step.status == 'succeeded'
    gc_run_id = child_step.subworkflow_run_id
    assert gc_run_id != ''
    gc_receipt = child_step.escalation_result['subworkflow_receipt']
    assert gc_receipt['child_workflow_id'] == 'wf_grandchild'
    assert gc_receipt['depth'] == 2

    gc_run = engine.store.load_run(gc_run_id)
    assert gc_run is not None
    assert gc_run.parent_workflow_run_id == child_run.workflow_run_id
    assert gc_run.subworkflow_depth == 2
    assert gc_run.status == 'succeeded'

    # Verify output mapping
    assert child_step.output_snapshot.get('child_mapped') == 'grandchild_val'


def test_subworkflow_child_failure_propagation(tmp_path):
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    _setup_script(owner_home, 'failing_script', fail=True)
    child_step = WorkflowStepSpec('fail_step', 'Fail', 'deterministic_script', script_id='failing_script')
    child_wf = WorkflowDefinition(_descriptor('wf_child_fail'), (child_step,))
    store.save_workflow(child_wf)

    parent_step = WorkflowStepSpec('parent_sw', 'Call Failing Child', 'subworkflow', subworkflow_ref='workflow:wf_child_fail')
    parent_wf = WorkflowDefinition(_descriptor('wf_parent_of_fail'), (parent_step,))
    store.save_workflow(parent_wf)

    run = engine.run_workflow(parent_wf)
    assert run.status in ('failed', 'paused_for_resume')
    assert run.needs_repair is True
    p_step = run.steps[0]
    assert p_step.status == 'failed'
    assert p_step.needs_repair is True
    assert p_step.subworkflow_run_id != ''
    assert p_step.error_code in ('SCRIPT_EXIT_NONZERO', 'STEP_EXECUTION_ERROR')


# =========================================================================
# WF-11: Bounded Health View & Step Summaries Tests
# =========================================================================

def test_pinned_definitions_freeze_and_resolve(tmp_path):
    owner_home = tmp_path
    store = WorkflowStore(owner_home)

    _setup_script(owner_home, 'freeze_script')
    wf = WorkflowDefinition(_descriptor('wf_to_freeze'), (
        WorkflowStepSpec('s1', 'Step 1', 'deterministic_script', script_id='freeze_script'),
    ))
    store.save_workflow(wf)

    frozen = freeze_execution_definition(owner_home, wf)
    assert frozen['workflow_id'] == 'wf_to_freeze'
    assert frozen['execution_definition_ref'] != ''

    # Resolve with matching ref
    resolved_def, pins = resolve_pinned_definition(owner_home, 'wf_to_freeze', frozen['execution_definition_ref'])
    assert resolved_def['descriptor']['id'] == 'wf_to_freeze'

    # Resolve with mismatching ref raises PinnedDefinitionError
    with pytest.raises(PinnedDefinitionError):
        resolve_pinned_definition(owner_home, 'wf_to_freeze', 'sha256:deadbeefdeadbeef')


def test_summarize_run_step_health_canonical_refs_and_redaction():
    step1 = StepExecutionRecord(
        step_id='step_tool',
        name='Tool step',
        mode='tool_call',
        status='succeeded',
        started_at=10.0,
        ended_at=12.5,
        duration_ms=2500.0,
        llm_involved=False,
        attempt_count=2,
        max_attempts=3,
        effect_id='eff_tool_01',
        escalation_result={'tool_receipt': {'operation_id': 'op_tool_99'}},
    )
    step2 = StepExecutionRecord(
        step_id='step_subwf',
        name='Subwf step',
        mode='subworkflow',
        status='failed',
        started_at=12.5,
        ended_at=13.0,
        duration_ms=500.0,
        llm_involved=False,
        attempt_count=1,
        max_attempts=1,
        subworkflow_run_id='child_run_888',
        error_code='SUBWORKFLOW_CYCLE_DETECTED',
        error_message='Authorization: Bearer sensitive_api_key_12345 leaked in internal path',
        needs_repair=True,
    )
    run = WorkflowRunRecord(
        workflow_run_id='run_demo_01',
        workflow_id='wf_demo',
        owner_id='user_alice',
        trigger_kind='manual',
        status='failed',
        started_at=10.0,
        ended_at=13.0,
        duration_ms=3000.0,
        steps=(step1, step2),
    )

    summary = summarize_run_step_health(run, redact=True)
    assert summary['workflow_run_id'] == 'run_demo_01'
    assert summary['status'] == 'failed'
    assert len(summary['steps']) == 2

    s1 = summary['steps'][0]
    assert s1['step_id'] == 'step_tool'
    assert s1['retried'] is True
    assert s1['attempt_count'] == 2
    assert s1['effect_id'] == 'eff_tool_01'
    assert s1['operation_id'] == 'op_tool_99'

    s2 = summary['steps'][1]
    assert s2['step_id'] == 'step_subwf'
    assert s2['subworkflow_run_id'] == 'child_run_888'
    assert s2['error_code'] == 'SUBWORKFLOW_CYCLE_DETECTED'
    # Redaction verified: sensitive token is masked
    assert 'sensitive_api_key_12345' not in s2['error_message']


def test_build_workflow_health_view_bounded_and_coverage(tmp_path):
    owner_home = tmp_path
    run_store = WorkflowRunStore(owner_home)

    # Create 3 synthetic run records
    for i in range(3):
        step_ok = StepExecutionRecord(
            step_id='step_calc',
            name='Calc',
            mode='deterministic_script',
            status='succeeded',
            started_at=10.0,
            ended_at=11.0 + i,
            duration_ms=100.0 * (i + 1),
            llm_involved=False,
            attempt_count=1 + (1 if i == 2 else 0),
            effect_id=f'eff_{i}',
        )
        step_status = 'failed' if i == 1 else 'succeeded'
        step_err = 'STEP_ERROR' if i == 1 else ''
        step_post = StepExecutionRecord(
            step_id='step_post',
            name='Post',
            mode='tool_call',
            status=step_status,
            started_at=12.0,
            ended_at=13.0,
            duration_ms=200.0,
            llm_involved=False,
            error_code=step_err,
            error_message='Authorization: Bearer secret_token_xyz' if i == 1 else '',
        )
        run = WorkflowRunRecord(
            workflow_run_id=f'run_00{i}',
            workflow_id='wf_analytics',
            owner_id='local_owner',
            trigger_kind='manual',
            status='failed' if i == 1 else 'succeeded',
            started_at=10.0 + i,
            ended_at=13.0 + i,
            duration_ms=300.0,
            steps=(step_ok, step_post),
        )
        run_store.create_run(run)

    # 1. Query with limit=2 (fewer than 3 runs) -> partial coverage & truncated
    view_partial = build_workflow_health_view(run_store, workflow_id='wf_analytics', limit=2)
    assert view_partial['schema_version'] == 'workflow_health.v1'
    assert view_partial['bounded_limit'] == 2
    assert view_partial['sampled_runs'] == 2
    assert view_partial['coverage'] == 'partial'
    assert view_partial['truncated'] is True

    # 2. Query with limit=50 (more than 3 runs) -> complete coverage
    view_complete = build_workflow_health_view(run_store, workflow_id='wf_analytics', limit=50)
    assert view_complete['coverage'] == 'complete'
    assert view_complete['truncated'] is False
    assert view_complete['sampled_runs'] == 3

    # Status counts
    counts = view_complete['run_counts']
    assert counts['succeeded'] == 2
    assert counts['failed'] == 1
    assert counts['running'] == 0

    # Step duration summaries
    step_calc = view_complete['step_health']['step_calc']
    dur = step_calc['duration_ms']
    assert dur['count'] == 3
    assert dur['min_ms'] == 100.0
    assert dur['max_ms'] == 300.0
    assert dur['avg_ms'] == 200.0
    assert dur['p95_ms'] >= 200.0

    # Failure counts and reason classes
    step_post = view_complete['step_health']['step_post']
    assert step_post['failure_count'] == 1
    assert 'STEP_ERROR' in step_post['reason_classes']
    assert view_complete['reason_classes'].get('STEP_ERROR') == 1

    # Retries
    calc_retries = step_calc['retry_stats']
    assert calc_retries['retried_steps'] == 1
    assert calc_retries['total_attempts'] == 4

    # Canonical references
    assert len(step_calc['canonical_references']['effect_ids']) == 3


def test_subworkflow_parallel_dag_siblings_no_deadlock_and_depth(tmp_path):
    # SA-R2-F3 & SA-R2-F4: Parallel sibling subworkflow steps in DAG wave:
    # wf_root runs: sub_a -> wf_mid_dag (which in turn has depends_on -> wf_leaf)
    #               sub_b -> wf_leaf
    # Verify:
    # (a) zero deadlock (lock-free ancestry lookup in _collect_ancestry);
    # (b) accurate parent_workflow_run_id and subworkflow_depth (1 and 2) on all child/grandchild runs;
    # (c) subsequent top-level engine.run_workflow('wf_leaf') on the same engine instance has
    #     parent_workflow_run_id == '' and subworkflow_depth == 0.
    owner_home = tmp_path
    engine = WorkflowEngine(owner_home)
    store = WorkflowStore(owner_home)

    _setup_script(owner_home, 'leaf_worker_script', {'leaf_out': 'ok'})
    _setup_script(owner_home, 'mid_prep_script', {'mid_prep': 'ready'})

    # 1. wf_leaf: single step deterministic script
    leaf_step = WorkflowStepSpec('s_leaf', 'Leaf step', 'deterministic_script', script_id='leaf_worker_script')
    wf_leaf = WorkflowDefinition(_descriptor('wf_leaf'), (leaf_step,))
    store.save_workflow(wf_leaf)

    # 2. wf_mid_dag: multi-step DAG with depends_on -> calls wf_leaf as subworkflow
    mid_prep = WorkflowStepSpec('m_prep', 'Mid Prep', 'deterministic_script', script_id='mid_prep_script')
    mid_sub = WorkflowStepSpec(
        'm_sub',
        'Mid Call Leaf',
        'subworkflow',
        subworkflow_ref='workflow:wf_leaf',
        depends_on=['m_prep'],
    )
    wf_mid_dag = WorkflowDefinition(_descriptor('wf_mid_dag'), (mid_prep, mid_sub))
    store.save_workflow(wf_mid_dag)

    # 3. wf_root: DAG with two parallel sibling subworkflows sub_a and sub_b in the same wave
    root_prep = WorkflowStepSpec('r_prep', 'Root Prep', 'deterministic_script', script_id='mid_prep_script')
    sub_a = WorkflowStepSpec(
        'sub_a',
        'Call Mid DAG',
        'subworkflow',
        subworkflow_ref='workflow:wf_mid_dag',
        depends_on=['r_prep'],
    )
    sub_b = WorkflowStepSpec(
        'sub_b',
        'Call Leaf Directly',
        'subworkflow',
        subworkflow_ref='workflow:wf_leaf',
        depends_on=['r_prep'],
    )
    root_desc = _descriptor('wf_root', metadata={'execution_mode': 'dag', 'max_parallel_steps': 4})
    wf_root = WorkflowDefinition(root_desc, (root_prep, sub_a, sub_b))
    store.save_workflow(wf_root)

    t0 = time.monotonic()
    root_run = engine.run_workflow(wf_root)
    elapsed = time.monotonic() - t0

    # (a) Zero deadlock - completes within < 10s
    assert elapsed < 10.0, f"Parallel subworkflows deadlocked or took too long: {elapsed:.2f}s"
    assert root_run.status == 'succeeded'

    step_map = {s.step_id: s for s in root_run.steps}
    assert step_map['sub_a'].status == 'succeeded'
    assert step_map['sub_b'].status == 'succeeded'

    # (b) Accurate parent_workflow_run_id and subworkflow_depth on all child/grandchild runs
    child_a_id = step_map['sub_a'].subworkflow_run_id
    child_b_id = step_map['sub_b'].subworkflow_run_id
    assert child_a_id and child_b_id

    child_a_run = engine.store.load_run(child_a_id)
    child_b_run = engine.store.load_run(child_b_id)

    # Direct children of root: depth=1, parent=root_run.workflow_run_id
    assert child_a_run.parent_workflow_run_id == root_run.workflow_run_id
    assert child_a_run.subworkflow_depth == 1
    assert child_b_run.parent_workflow_run_id == root_run.workflow_run_id
    assert child_b_run.subworkflow_depth == 1

    # Grandchild: child of mid_dag
    mid_step_map = {s.step_id: s for s in child_a_run.steps}
    grandchild_id = mid_step_map['m_sub'].subworkflow_run_id
    assert grandchild_id

    grandchild_run = engine.store.load_run(grandchild_id)
    assert grandchild_run.parent_workflow_run_id == child_a_id
    assert grandchild_run.subworkflow_depth == 2

    # (c) Subsequent top-level engine.run_workflow('wf_leaf') on SAME engine instance
    top_leaf_run = engine.run_workflow('wf_leaf')
    assert top_leaf_run.status == 'succeeded'
    assert top_leaf_run.parent_workflow_run_id == ''
    assert top_leaf_run.subworkflow_depth == 0

    persisted_top = engine.store.load_run(top_leaf_run.workflow_run_id)
    assert persisted_top.parent_workflow_run_id == ''
    assert persisted_top.subworkflow_depth == 0


@pytest.mark.parametrize('current_scope,allowed', [('space_a', True), ('space_b', False), ('unassigned', False)])
def test_pinned_child_uses_current_scope_and_preserves_old_execution(tmp_path, current_scope, allowed):
    """旧版本创建时未分配不否定后来授权；旧授权也不能对抗当前移出范围。"""
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext

    _setup_script(tmp_path, 'pinned_scope_script', {'value': 7})
    store = WorkflowStore(tmp_path)
    child = WorkflowDefinition(_descriptor('pinned_scope_child'),
        (WorkflowStepSpec('calc', '原版本计算', 'deterministic_script', script_id='pinned_scope_script'),))
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
    actor = SimpleNamespace(owner_id='local_owner')
    service = WorkflowScopeService(tmp_path, owner_id='local_owner', authorize=lambda *args: True)
    service.execute('workflows.create', {'definition': {'descriptor': {
        'id': child.descriptor.id, 'kind': 'workflow', 'name': '固定原版本',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'steps': [{'step_id': 'calc', 'name': '原版本计算', 'mode': 'deterministic_script',
                   'script_id': 'pinned_scope_script'}]},
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, actor=actor,
        idempotency_key='pinned-scope-create')
    scope = (WorkflowScope('unassigned', (), 2) if current_scope == 'unassigned'
             else WorkflowScope('spaces', (current_scope,), 2))
    service.execute('workflows.scope.set', {'workflow_ref': 'pinned_scope_child',
        'scope': {'scope_mode': scope.scope_mode, 'space_ids': list(scope.space_ids)}},
        actor=actor, idempotency_key='pinned-scope-set', expected_revision=1)
    live = store.get_workflow('pinned_scope_child')
    store.save_workflow(replace(live, descriptor=replace(live.descriptor, summary='当前正文已编辑')))
    parent = WorkflowDefinition(_descriptor('pinned_scope_parent'),
        (WorkflowStepSpec('child', '固定旧版本', 'subworkflow',
            subworkflow_ref='workflow:pinned_scope_child', config={'definition_version': 1}),))
    context = BusinessExecutionContext('local_owner', 'space_a', None, 'parent-task', 1,
        None, (), (), 'business-context.v1')
    authorizations = []
    engine = WorkflowEngine(tmp_path, owner_id='local_owner',
        run_authorizer=lambda record, phase: authorizations.append((record.workflow_id, str(phase))))
    run = engine.run_workflow(parent, business_context=context,
        scope_snapshot=WorkflowScope('spaces', ('space_a',), 1).to_dict())
    children = engine.store.list_runs(workflow_id='pinned_scope_child', limit=10)
    if allowed:
        assert run.status == 'succeeded', run.to_dict()
        assert len(children) == 1 and children[0].scope_snapshot == scope.to_dict()
        assert children[0].final_output == {'value': 7}
        snapshot = engine.store.load_workflow_snapshot(children[0].workflow_run_id)
        assert snapshot['descriptor']['summary'] != '当前正文已编辑'
        assert ('pinned_scope_child', 'preflight') in authorizations
        assert ('pinned_scope_child', 'calc') in authorizations
    else:
        assert run.steps[0].error_code == 'SUBWORKFLOW_SCOPE_DENIED'
        assert children == []


def test_pinned_child_still_requires_original_child_authorizer(tmp_path):
    from agent_py_agent.agent.workflows.business_scope import WorkflowPolicyRefusal

    _setup_script(tmp_path, 'review_child_script', {'value': 7})
    store = WorkflowStore(tmp_path)
    child = WorkflowDefinition(_descriptor('review_child'),
        (WorkflowStepSpec('calc', '需原宿主审核', 'deterministic_script', script_id='review_child_script'),))
    store.save_workflow(child)
    parent = WorkflowDefinition(_descriptor('review_parent'),
        (WorkflowStepSpec('child', '固定子流', 'subworkflow', subworkflow_ref='workflow:review_child',
            config={'definition_version': 1}),))
    checked = []
    def authorize(record, phase):
        checked.append((record.workflow_id, str(phase)))
        if record.workflow_id == 'review_child':
            raise WorkflowPolicyRefusal('WORKFLOW_REVIEW_REQUIRED')
    engine = WorkflowEngine(tmp_path, run_authorizer=authorize)
    run = engine.run_workflow(parent)
    assert ('review_child', 'preflight') in checked
    assert run.steps[0].error_code == 'WORKFLOW_REVIEW_REQUIRED'
    assert engine.store.list_runs(workflow_id='review_child', limit=10) == []
