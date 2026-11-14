"""GUI/IM无业务DTO的正规SessionTask子登记，仍核原来源/父链/输出合同与版本。"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.conversation.business_access import BusinessActor
from agent_py_agent.agent.conversation.execution_threads import (
    IM_TASK_EXECUTION_KEY,
    bind_delegated_task,
    ensure_execution_thread,
)
from agent_py_agent.agent.conversation.registered_artifact_observation import registered_artifact
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space.home_layout import task_workspace_path
from agent_py_agent.tests.test_im_execution_threads import fixture_agent


def prepared(tmp_path):
    from dataclasses import replace
    agent, sender = fixture_agent(tmp_path / 'app')
    store = agent.conversation_store
    receiver = ensure_execution_thread(agent, sender.thread_id, 'artifact-order', kind='delegated')
    store.threads.update_atomic(receiver.thread_id, lambda value: replace(value,
        metadata={**value.metadata, IM_TASK_EXECUTION_KEY: {'mode': 'delegated'}}))
    task = store.session_tasks.create(SessionTaskDraft(sender.thread_id, receiver.thread_id, 'CSV交付', dedupe_key='fixture-order'))
    bind_delegated_task(store, receiver.thread_id, task.task_id)
    store.session_tasks.bind_turn(task.task_id, turn_id=task.task_id)
    root = agent.home_paths.owner_home_dir / 'runs/fixture-root'
    root.mkdir(parents=True)
    store.tasks.bind({'task_id': task.task_id, 'thread_id': receiver.thread_id, 'task_path': str(root)},
        host_channel_actor=agent._current_run_params.host_channel_actor)
    repo = RuntimeRepository(agent.home_paths.owner_home_dir / 'runtime.db')
    parent = repo.record_run_creation(owner_id=agent.home_paths.owner_id, run_id=task.task_id,
        thread_id=receiver.thread_id, conversation_task_id=task.task_id, role='main')
    child_id = 'child-registered'
    child = repo.record_run_creation(owner_id=agent.home_paths.owner_id, run_id=child_id,
        thread_id=receiver.thread_id, host_parent_execution=repo.parent_execution_ref(owner_id=agent.home_paths.owner_id, run_id=task.task_id))
    child_root = root / 'work/agents' / child_id
    child_root.mkdir(parents=True)
    store.tasks.bind({'task_id': child_id, 'thread_id': receiver.thread_id, 'task_path': str(child_root)})
    template = 'tasks/{date}/{task_slug}'
    day = datetime.fromtimestamp(task.created_at, timezone.utc).date().isoformat()
    output = task_workspace_path(agent.home_paths.owner_home_dir, template, date=day, task_name=task.task_id) / 'output'
    output.mkdir(parents=True)
    path = output / 'data.csv'
    path.write_bytes(b'label,value\nalpha,7\nbeta,9\n')
    (root/'work/run_workspace.json').write_text(json.dumps({'schema_version': 'run_workspace.v1',
        'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir),
        'task_root': str(root), 'run_id': task.task_id, 'task_id': task.task_id,
        'delivery_contract': {'task_workspace': {'output_dir': str(output)}}}))
    record = register_artifact(ArtifactRegistration(child_root, path, run_id=child_id, task_id=task.task_id,
        agent_id=agent.home_paths.owner_id, status='ready', source='tool_result', created_by_tool='write_file'))
    source = SimpleNamespace(home=agent.home_paths.owner_home_dir, owner=agent.home_paths.owner_id,
        repo=repo, store=store, workspace_task_path_template=template)
    service = SimpleNamespace(owner_id=source.owner)
    actor = BusinessActor(source.owner, 'qa', service, lambda *_: True)
    return SimpleNamespace(agent=agent, store=store, source=source, task=task, parent=parent,
        child=child, root=root, child_root=child_root, path=path, record=record, authority=(service, actor, 'artifacts.get'))


def test_original_unassigned_im_child_registry_reads_exact_bytes_and_sha(tmp_path):
    w = prepared(tmp_path)
    facts, record, content = registered_artifact(w.source, w.record.artifact_id, w.record.sha256, authority=w.authority)
    assert content == w.path.read_bytes() and len(content) == 27
    assert hashlib.sha256(content).hexdigest() == record.sha256
    assert facts.task_id == w.task.task_id and facts.owner_id == w.source.owner and facts.space_id is None
    assert w.source.repo.get_task_run(w.parent['task_run_id'])['metadata_json'] == '{}'


@pytest.mark.parametrize('change', ['wrong_version', 'foreign_owner', 'parent', 'actor', 'contract', 'bytes', 'missing_child_link'])
def test_paths_or_manifest_never_replace_original_authority(tmp_path, change):
    from agent_py_agent.agent.conversation.business_observability import ObservationSourceError
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
    w = prepared(tmp_path)
    version = w.record.sha256
    authority = w.authority
    if change == 'wrong_version':
        version = '0' * 64
    elif change == 'foreign_owner':
        authority = (authority[0], BusinessActor('local/other', 'qa', authority[0], lambda *_: True), 'artifacts.get')
    elif change == 'parent':
        with w.source.repo._runtime_connection() as db:
            db.execute("UPDATE agent_runs SET parent_agent_run_id='' WHERE run_id=?", ('child-registered',))
            db.commit()
    elif change == 'actor':
        p = w.store.storage.task_path(w.task.task_id)
        update_json_file_atomic(p, lambda row: {**row, 'host_channel_actor': {**row['host_channel_actor'], 'actor_id': 'ou_foreign'}})
    elif change == 'contract':
        p = w.root/'work/run_workspace.json'
        value = json.loads(p.read_text())
        value['delivery_contract']['task_workspace']['output_dir'] = str(w.source.home/'config')
        p.write_text(json.dumps(value))
    elif change == 'bytes':
        w.path.write_bytes(b'other version')
    else:
        w.store.storage.task_path('child-registered').unlink()
    with pytest.raises(ObservationSourceError):
        registered_artifact(w.source, w.record.artifact_id, version, authority=authority)
