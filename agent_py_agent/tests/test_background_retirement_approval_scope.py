"""退役检查仅沿原active TaskLink读准确审批根，共用时间/条预算。"""
from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
from agent_py_agent.tests.test_background_tool_approval import _pending_request
from agent_py_agent.tests.test_background_tool_approval import background as background


def test_checkpoint_uses_original_root_and_persisted_permission_identity(background):
    ctx = background
    _pending_request(ctx)
    refs, _ = identity_outstanding(ctx.agent, ctx.thread)
    assert f'approval:{ctx.request.permission_id}' in refs
    assert 'approval:None' not in refs


def test_all_active_roots_share_deadline_and_scan_budget(background, monkeypatch):
    from agent_py_agent.agent.conversation import agent_tool_approval
    ctx = background
    ctx.store.tasks.bind({'thread_id': ctx.thread.thread_id, 'task_id': 'second-root',
                          'goal': '另一准确根', 'status': 'active'})
    calls = []

    def read(agent, *, root_task_id, deadline, scan_limit, scan_progress):
        calls.append((root_task_id, deadline, scan_limit))
        scan_progress['entries'] = 100 if len(calls) == 1 else 28
        return ([{'request': {'permission_id': 'persisted-permission'}}] if len(calls) == 1 else [], len(calls) == 1)

    monkeypatch.setattr(agent_tool_approval, 'list_pending_agent_tool_approvals_report', read)
    refs, _ = identity_outstanding(ctx.agent, ctx.thread)
    assert [row[0] for row in calls] == ['root-work', 'second-root']
    assert calls[0][1] == calls[1][1]
    assert [row[2] for row in calls] == [128, 28]
    assert 'approval:persisted-permission' in refs and 'approvals:incomplete' in refs


def test_corrupt_pending_identity_stays_unconfirmed(background, monkeypatch):
    from agent_py_agent.agent.conversation import agent_tool_approval
    ctx = background
    monkeypatch.setattr(agent_tool_approval, 'list_pending_agent_tool_approvals_report',
                        lambda *args, **kwargs: ([{'request': {}}], True))
    refs, _ = identity_outstanding(ctx.agent, ctx.thread)
    assert 'approvals:unreadable' in refs
