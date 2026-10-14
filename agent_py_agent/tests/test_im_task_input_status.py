import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationTaskStatus,
    render_conversation_task_status,
)
from agent_py_agent.agent.conversation.execution_threads import IM_TASK_EXECUTION_KEY
from agent_py_agent.agent.conversation.im_task_status import im_task_status_facts
from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    create_input_dependency,
    input_dependency_path,
    resolve_input_dependency,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.tests.test_background_tool_approval import background

_FIXTURES = (background,)


def _im_context(ctx):
    source = ctx.store.threads.get_or_create({'channel':'feishu','channel_user_id':'ou_synthetic',
        'channel_conversation_id':'oc_synthetic','canonical_user_id':'ou_synthetic'})
    task = ctx.store.session_tasks.create(SessionTaskDraft(source.thread_id,ctx.thread.thread_id,'合成后台任务'))
    ctx.store.session_tasks.advance(task.task_id,SessionTaskUpdate('accepted',conversation_request_id=task.task_id))
    ctx.store.threads.update_atomic(ctx.thread.thread_id, lambda current: replace(current,metadata={
        **current.metadata,IM_TASK_EXECUTION_KEY:{'root_task_id':ctx.task_id,'session_task_id':task.task_id}}))
    scope = SimpleNamespace(channel='feishu',user_id='ou_synthetic')
    return source, task, scope


def _input(ctx, input_id='status-input', run_id=None):
    row = create_input_dependency(ctx.agent,tool_approval_scope(ctx.agent,run_id or ctx.task_id),
        input_id=input_id,provider='synthetic',expires_at=time.time()+60)
    arm_input_dependency(ctx.agent,input_id)
    return row


def test_waiting_input_is_separate_from_accepted_running_and_renders_no_worker(background):
    source, _task, scope = _im_context(background)
    before = im_task_status_facts(background.agent,source,scope)
    assert before['queued']==0 and before['running']==1 and before['waiting_input']==0
    row = _input(background)
    facts = im_task_status_facts(background.agent,source,scope)
    assert facts['running']==0 and facts['waiting_input']==1
    text = render_conversation_task_status(ConversationTaskStatus(im_task_status=facts))
    assert '待输入 1（等待不占执行资源）' in text and '运行/汇总 0' in text
    resolve_input_dependency(background.agent,row['input_id'],status='ready',result_refs=('opaque-ref',))
    after = im_task_status_facts(background.agent,source,scope)
    assert after['running']==1 and after['waiting_input']==0


def test_blocked_child_input_is_waiting_and_does_not_count_running(background):
    source, _task, scope = _im_context(background)
    child = background.agent.subagents.create_run(goal='合成孩子',root_id=background.task_id,parent_id=background.task_id,
        attributes={'conversation_thread_id':background.thread.thread_id})
    child = background.agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    _input(background,run_id=child.id)
    background.agent.subagents.lifecycle.set_status(child.id,'BLOCKED')
    facts = im_task_status_facts(background.agent,source,scope)
    assert facts['running']==0 and facts['waiting_input']==1
    assert facts['subagent_running']==0 and facts['subagent_waiting']==1 and facts['subagent_waiting_input']==1
    text = render_conversation_task_status(ConversationTaskStatus(im_task_status=facts))
    assert '等待/受阻 1（待输入 1）' in text


def test_status_reads_only_exact_refs_and_keeps_other_chat_private(background):
    source, _task, scope = _im_context(background)
    _input(background)
    other = background.store.threads.get_or_create({'channel':'feishu','channel_user_id':'ou_other',
        'channel_conversation_id':'oc_other','canonical_user_id':'ou_other'})
    # 未登记、损坏的历史输入不应被本会话扫描。
    path = input_dependency_path(background.agent,'unreferenced-history')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('not-json')
    assert im_task_status_facts(background.agent,source,scope)['waiting_input']==1
    assert im_task_status_facts(background.agent,other,SimpleNamespace(channel='feishu',user_id='ou_other'))['waiting_input']==0


@pytest.mark.parametrize('damage',['input','root','target'])
def test_bad_referenced_state_is_unavailable_instead_of_zero(background,damage):
    source, _task, scope = _im_context(background)
    row = _input(background)
    if damage=='input':
        path = input_dependency_path(background.agent,row['input_id'])
    elif damage=='root':
        path = background.store.tasks.storage.task_path(background.task_id)
    else:
        path = background.store.threads.storage.thread_path(background.thread.thread_id)
    path.write_text('not-json')
    facts = im_task_status_facts(background.agent,source,scope)
    assert facts == {'available':False}
    text = render_conversation_task_status(ConversationTaskStatus(im_task_status=facts))
    assert '状态读取不可用' in text and '待输入 0' not in text


def test_bad_exact_child_record_is_unavailable(background):
    source, _task, scope = _im_context(background)
    child = background.agent.subagents.create_run(goal='合成孩子',root_id=background.task_id,parent_id=background.task_id)
    child = background.agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    _input(background,run_id=child.id)
    (background.agent.subagents.workspace/child.id/'task.json').write_text('not-json')
    assert im_task_status_facts(background.agent,source,scope) == {'available':False}
