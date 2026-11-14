"""工作流引用只读真实结构字段；旧索引升级必须先恢复覆盖证明。"""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTask
from agent_py_agent.agent.scheduler.execution_policy import normalize_execution_target
from agent_py_agent.agent.workflows import reference_index as index
from agent_py_agent.agent.workflows.reference_sources import WorkflowReferenceReader


def test_default_business_host_reads_references_and_refuses_unknown(tmp_path):
    from agent_py_agent.agent.gateway_parts.business_api import BusinessServices
    from agent_py_agent.agent.workflows.orphan_maintenance import cleanup_status

    host = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=tmp_path, owner_id='local/main'),
        config=SimpleNamespace(business_spaces_enabled=True),
    )
    service = BusinessServices(host).workflows
    definition = WorkflowDefinition(
        descriptor=AssetDescriptor(id='target', kind='workflow', name='target'), steps=(),
    )
    service.store.save_workflow(definition)
    assert cleanup_status(service, service._get('target'))['blockers'] == []
    # 后装配的真实任务端口必须即时可见，不能缓存启动时的空引用。
    host.conversation_store = SimpleNamespace(session_tasks=SimpleNamespace(
        list_page=lambda **kwargs: {'items': [_task({'workflow_ref': 'target'})],
                                   'coverage': 'complete', 'next_cursor': None},
    ))
    assert cleanup_status(service, service._get('target'))['blockers'][0]['reason_code'] == 'OPEN_SESSION_TASK'
    host.conversation_store.session_tasks.list_page = lambda **kwargs: {'items': [], 'coverage': 'unknown'}
    codes = {row['reason_code'] for row in cleanup_status(service, service._get('target'))['blockers']}
    assert codes == {'REFERENCE_COVERAGE_UNKNOWN', 'SESSION_TASK_REFERENCE_SOURCE_INCOMPLETE'}


def test_scheduler_created_after_reference_reader_is_not_assumed_empty(tmp_path):
    reader = WorkflowReferenceReader(tmp_path)
    assert reader('target')['coverage'] == 'complete'
    scheduler = tmp_path / 'data' / 'scheduler'
    scheduler.mkdir(parents=True)
    (scheduler / 'store.json').write_text('{broken', encoding='utf-8')
    result = reader('target')
    assert result['coverage'] == 'unknown'
    assert any(row['reason_code'] == 'SCHEDULER_REFERENCE_SOURCE_INCOMPLETE'
               for row in result['blockers'])


def _task(metadata, *, status="queued", dedupe=""):
    return SessionTask(
        "task",
        "sender",
        "recipient",
        "target is just prose",
        status=status,
        dedupe_key=dedupe,
        metadata=metadata,
    )


def _tasks_reader(task):
    return WorkflowReferenceReader(
        session_task_store=SimpleNamespace(
            list_page=lambda **kwargs: {
                "items": [task],
                "coverage": "complete",
                "next_cursor": None,
            }
        )
    )


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"note": "workflow:target", "details": {"text": "target"}}, False),
        ({"workflow_id": "target-other"}, False),
        ({"workflow_ref": "workflow:target"}, True),
        ({"execution_target": {"kind": "workflow", "asset_id": "target"}}, True),
        ({"execution_target": {"kind": "script", "asset_id": "target"}}, False),
        ({"business_context": {"selected_method_refs": ["workflow:target"]}}, True),
        (
            {
                "business_context": {
                    "selected_method_refs": ["package:target", "workflow:target-other"]
                }
            },
            False,
        ),
    ],
)
def test_task_reference_uses_only_exact_structured_fields(metadata, expected):
    reader = _tasks_reader(_task(metadata, dedupe="arbitrary-target-in-dedupe"))
    blockers = []
    assert reader._check_session_tasks("target", blockers)
    assert bool(blockers) is expected


def test_terminal_cancelled_task_keeps_reference_until_original_cleanup_settles():
    task = _task(
        {
            "workflow_ref": "workflow:target",
            "cancellation_control": {"outstanding_refs": ["original-attempt"]},
        },
        status="cancelled",
    )
    blockers = []
    assert _tasks_reader(task)._check_session_tasks("target", blockers)
    assert blockers[0]["ref"] == task.task_id
    task = replace(
        task,
        metadata={
            "workflow_ref": "workflow:target",
            "cancellation_control": {"outstanding_refs": []},
        },
    )
    blockers = []
    assert _tasks_reader(task)._check_session_tasks("target", blockers)
    assert blockers == []


def test_paused_schedule_uses_canonical_workflow_target_not_prompt_or_script():
    workflow = normalize_execution_target(
        {"kind": "workflow", "asset_id": "target", "pinned_version": 1}
    )
    script = normalize_execution_target(
        {"kind": "script", "asset_id": "target", "pinned_version": 1}
    )
    reader = WorkflowReferenceReader(
        scheduler_repo=SimpleNamespace(
            list_jobs=lambda **kwargs: (
                [
                    {"job_id": "paused", "status": "paused", "execution_target": workflow},
                    {
                        "job_id": "other",
                        "status": "enabled",
                        "execution_target": script,
                        "prompt": "workflow:target",
                    },
                ],
                [],
            )
        )
    )
    blockers = []
    assert reader._check_scheduler("target", blockers)
    assert [row["ref"] for row in blockers] == ["paused"]


def test_subscription_policy_is_exact_and_paused_reference_remains_live():
    catalog = {
        "spaces": {"space": {"state": "active", "governance": "a note mentioning target"}},
        "subscriptions": {
            "active": {"state": "active", "policy_ref": "workflow:target", "space_id": "space"},
            "paused": {"state": "paused", "policy_ref": "workflow:target", "space_id": "space"},
            "removed": {"state": "removed", "policy_ref": "workflow:target", "space_id": "space"},
            "text": {
                "state": "active",
                "source_ref": "workflow:target",
                "routing_instruction": "target",
                "policy_ref": "workflow:target-other",
                "space_id": "space",
            },
        },
    }
    blockers = []
    assert WorkflowReferenceReader(space_store=catalog)._check_spaces("target", blockers)
    assert {row["ref"] for row in blockers} == {"active", "paused"}


def _derived(name, source):
    return WorkflowDefinition(
        AssetDescriptor(name, "workflow", name, metadata={"derived_from": source}),
        (WorkflowStepSpec("step", "step", "condition"),),
    )


def test_explicit_derivation_blocks_but_package_source_does_not(tmp_path):
    store = WorkflowStore(tmp_path)
    store.save_workflow(_derived("dependent", "workflow:target"))
    store.save_workflow(_derived("package-copy", "capability:target/workflow/target.json"))
    assert index.read_parents(store, "target", limit=10) == (True, ["dependent"])
    from agent_py_agent.agent.workflows.business_scope import with_state, workflow_state

    current = store.get_workflow("dependent")
    with store._locked():
        store._save(with_state(current, {**workflow_state(current), "lifecycle": "recycled"}))
    assert index.read_parents(store, "target", limit=10) == (True, ["dependent"])


def test_old_complete_index_migration_cannot_hide_retained_derivation(tmp_path):
    store = WorkflowStore(tmp_path)
    store.save_workflow(_derived("dependent", "workflow:target"))
    path = tmp_path / "data" / index._FILENAME
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM refs")
        conn.execute("UPDATE state SET complete=1")
        conn.execute("PRAGMA user_version=1")
    diagnostics = {}
    complete, parents = index.read_parents(store, "target", limit=10, diagnostics=diagnostics)
    assert not complete and parents == []
    assert diagnostics["reason_code"] == "WORKFLOW_REFERENCE_DISCOVERY_INCOMPLETE"
    for _ in range(8):
        result = index.discover(store, limit=1)
        assert result["scanned"] <= 1
        if result["complete"]:
            break
    assert result["complete"]
    assert index.read_parents(store, "target", limit=10) == (True, ["dependent"])
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_truncated_catalog_and_corrupt_source_never_prove_no_reference(tmp_path):
    catalog = {"spaces": {f"space-{n}": {"state": "active"} for n in range(101)}}
    result = WorkflowReferenceReader(space_store=catalog).read_references("target")
    assert result["coverage"] == "unknown"
    assert any(
        row["reason_code"] == "BUSINESS_REFERENCE_SOURCE_INCOMPLETE" for row in result["blockers"]
    )
    (tmp_path / "data" / "business").mkdir(parents=True)
    (tmp_path / "data" / "business" / "catalog.json").write_text("{broken")
    result = WorkflowReferenceReader(tmp_path).read_references("target")
    assert result["coverage"] == "unknown"
