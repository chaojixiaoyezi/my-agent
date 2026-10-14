import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    create_input_dependency,
    input_dependency_current,
    input_dependency_path,
    task_has_waiting_input,
)
from agent_py_agent.agent.conversation.runtime import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.subagents.cancellation import (
    CancelSubagentTaskRequest,
    cancel_subagent_tree,
)
from agent_py_agent.tests.test_background_tool_approval import background

_FIXTURES = (background,)


def _child_input(ctx, *, parent=None, input_id='input-child'):
    child = ctx.agent.subagents.create_run(goal='合成必要输入', root_id=ctx.task_id, parent_id=parent or ctx.task_id)
    child = ctx.agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    row = create_input_dependency(ctx.agent, tool_approval_scope(ctx.agent, child.id), input_id=input_id,
                                  provider='synthetic', expires_at=time.time()+180)
    arm_input_dependency(ctx.agent, input_id)
    return child, row


def test_real_child_subtree_cancel_clears_only_selected_inputs(background):
    child, row = _child_input(background)
    grandchild, grandrow = _child_input(background, parent=child.id, input_id='grandchild-input')
    sibling, siblingrow = _child_input(background, input_id='sibling-input')
    assert task_has_waiting_input(background.agent, background.task_id)
    result = cancel_subagent_tree(background.agent, CancelSubagentTaskRequest(child,'合成授权停止',source='test'))
    assert result['cancel_status'] == 'CANCELLED'
    for item in (row, grandrow):
        saved = read_json_object_report(input_dependency_path(background.agent,item['input_id']), context='test').payload
        assert saved['status'] == 'cancelled' and not saved['armed']
        assert not input_dependency_current(background.agent,saved)
    root = background.store.tasks.load(background.task_id)
    assert root.input_dependency_refs == (siblingrow['input_id'],)
    assert task_has_waiting_input(background.agent, background.task_id)
    cancel_subagent_tree(background.agent, CancelSubagentTaskRequest(sibling,'合成授权停止',source='test'))
    assert not task_has_waiting_input(background.agent, background.task_id)
    assert background.store.tasks.load(background.task_id).input_dependency_refs == ()
    assert grandchild.id in {row['run_id'] for row in result['descendants']['cancelled']}


def test_stale_cancelled_child_fact_cannot_hold_root_wait_guard(background):
    child, row = _child_input(background)
    background.agent.subagents.mutate(child.id, lambda task: setattr(task,'status','CANCELLED'))
    assert not input_dependency_current(background.agent,row)
    assert not task_has_waiting_input(background.agent,background.task_id)


def test_reconcile_does_not_replace_input_deadline_when_pending_page_omits_root(background, monkeypatch):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    repo = RuntimeRepository(background.agent.home_paths.owner_home_dir/'input-deadline-test.db')
    monkeypatch.setattr(background.agent.subagents,'runtime_db',repo)
    # 原队列存在超过一页的合法任务，根的输入字条真实存在但从查询投影被遗漏。
    for index in range(1001):
        repo.upsert_wake(root_task_id=f'other-root-{index}', next_due_at=time.time()+index+60, kind='sleep')
    root_scope = tool_approval_scope(background.agent, background.task_id)
    expiry = time.time()+9000
    create_input_dependency(background.agent,root_scope,input_id='root-input',provider='synthetic',expires_at=expiry)
    arm_input_dependency(background.agent,'root-input')
    assert background.task_id not in {row['root_task_id'] for row in repo.list_pending_wakes(limit=1000)}
    monkeypatch.setattr('agent_py_agent.agent.owner_wake_discovery.unfinished_task_ids',lambda *_args,**_kwargs:[background.task_id])
    runtime = BackgroundMainAgentRuntime(agent=background.agent,store=background.store)
    scheduler = BackgroundMainAgentScheduler({'runtime':runtime,'store':background.store})
    monkeypatch.setattr(scheduler,'_task_has_active_goal',lambda _task:True)
    monkeypatch.setattr(scheduler,'_task_work_kind',lambda _task:'')
    scheduler._reconcile_wake_queue(now=time.time())
    original = next(row for row in repo.list_pending_wakes(limit=2000) if row['root_task_id'] == background.task_id)
    assert original['kind'] == 'task_input_deadline' and original['next_due_at'] == expiry


def test_same_root_multiple_inputs_rearm_next_absolute_deadline(background, monkeypatch):
    from agent_py_agent.agent.conversation.input_dependencies import resolve_input_dependency
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    repo = RuntimeRepository(background.agent.home_paths.owner_home_dir/'input-multiple-deadline.db')
    monkeypatch.setattr(background.agent.subagents,'runtime_db',repo)
    scope = tool_approval_scope(background.agent,background.task_id)
    first, second = time.time()+60, time.time()+180
    for input_id, expiry in [('first',first),('second',second)]:
        create_input_dependency(background.agent,scope,input_id=input_id,provider='synthetic',expires_at=expiry)
        arm_input_dependency(background.agent,input_id)
    runtime = BackgroundMainAgentRuntime(agent=background.agent,store=background.store)
    scheduler = BackgroundMainAgentScheduler({'runtime':runtime,'store':background.store})
    scheduler._consume_due_wake_queue(now=first+1)
    deadline = next(row for row in repo.list_pending_wakes(limit=100) if row['root_task_id']==background.task_id)
    assert deadline['kind']=='task_input_deadline' and deadline['next_due_at']==second
    assert read_json_object_report(input_dependency_path(background.agent,'first'),context='test').payload['status']=='expired'
    assert task_has_waiting_input(background.agent,background.task_id)
    resolve_input_dependency(background.agent,'second',status='cancelled')
    assert not task_has_waiting_input(background.agent,background.task_id)


def test_real_gateway_stop_cancels_root_input_after_authorization(background):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    row = create_input_dependency(background.agent,tool_approval_scope(background.agent,background.task_id),
                                  input_id='root-stop-input',provider='synthetic',expires_at=time.time()+180)
    arm_input_dependency(background.agent,row['input_id'])
    result = execute_gateway_conversation_control(background.agent,gateway_paths(background.agent),
                                                   parse_conversation_control('/stop'),background.scope)
    assert result.ok
    assert background.store.tasks.load(background.task_id).input_dependency_refs == ()
    saved = read_json_object_report(input_dependency_path(background.agent,row['input_id']),context='test').payload
    assert saved['status']=='cancelled' and not saved['armed']
    assert not task_has_waiting_input(background.agent,background.task_id)
