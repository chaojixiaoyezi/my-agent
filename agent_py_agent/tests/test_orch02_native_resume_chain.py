"""真实原生工具与子 runner 串起能力申请、父裁决及获权后执行。"""
import json

from agent_py_agent.agent.agent_core.runner.worker import (
    RunSubagentWorkerParams,
    _run_subagent_worker,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_native_request_parent_decision_and_child_resumed_execution(tmp_path, record_testsuite_property):
    config = AgentConfig(model_backend='echo', enable_subagents=True, my_agent_home=str(tmp_path / 'home'))
    agent = SimpleAgent(config, tmp_path)
    store = agent.conversation_store
    thread = store.threads.create(owner_id=agent.home_paths.owner_id, owner_home=str(agent.home_paths.owner_home_dir))
    parent_id = 'native-parent'
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': parent_id, 'status': 'active'})
    agent.subagents.runtime_db.record_run_creation(owner_id=agent.home_paths.owner_id, goal='parent',
        run_id=parent_id, role='main', conversation_task_id=parent_id, thread_id=thread.thread_id)
    target = agent.home_paths.owner_home_dir / 'authorized-input.txt'
    target.write_text('NATIVE_RESUME_READ_PROOF')
    class CreateBackend(EchoBackend):
        calls = 0
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'create-child', 'name': 'create_subagents', 'input': {'items': [{
                        'goal': '读取获授权输入', 'role': 'worker', 'allowed_tools': ['capability_request']}]}}])
            return ModelResponse(text='子任务已创建。', backend=self.name)

    agent.backend = CreateBackend()
    created = agent.run('创建隔离子任务', params=RunParams(run_id=parent_id, task_id=parent_id,
        request_id='parent-create', save=False, allowed_tools=['create_subagents', 'read_file', 'capability_request'],
        task_attributes={'conversation_thread_id': thread.thread_id, 'conversation_task_id': parent_id}))
    assert all(r['ok'] for r in created.archive_tool_calls), created.archive_tool_calls
    child = agent.subagents.list_runs_for_root_report(parent_id).runs[0]

    class ChildBackend(EchoBackend):
        calls = 0
        seen = []
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            self.seen.append(kwargs.get('messages', []))
            if self.calls == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{'id': 'request-read',
                    'name': 'capability_request', 'input': {'problem': '缺少此路径读取范围',
                        'needed_capability': 'read_file', 'capability_type': 'tool',
                        'requested_tools': ['read_file'], 'path_scope': [str(target)], 'risk_level': 'low'}}])
            if self.calls == 2:
                return ModelResponse(text='等待父级处理精确读取请求。', backend=self.name)
            if self.calls == 3:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'after-grant-read', 'name': 'read_file', 'input': {'path': str(target)}}])
            return ModelResponse(text='已读取授权输入。', backend=self.name)

    child_backend = ChildBackend()
    params = RunSubagentWorkerParams(config=config, root=tmp_path, run_id=child.id, instruction='',
        dry_run=False, max_cards=0, probe=False, retry_reason='', backend_override=child_backend)
    first = _run_subagent_worker(params)
    loaded = agent.subagents.load(child.id)
    requests = [r for r in loaded.capability_requests if r.status == 'OPEN']
    assert len(requests) == 1, [b.get('content', '')[:1200] for m in child_backend.seen[-1] for b in m.get('content', []) if isinstance(b, dict) and b.get('type') == 'tool_result']
    assert first.status == "BLOCKED"
    request_id = requests[0].id
    assert any(w.reason == 'subagent_capability_request_open' for w in store.wakes.pending())

    class ParentBackend(EchoBackend):
        calls = 0
        seen = []
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            self.seen.append(kwargs.get('messages', []))
            if self.calls == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'pending-inbox', 'name': 'list_pending_requests', 'input': {}}])
            if self.calls == 2:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'parent-grant', 'name': 'resolve_capability_requests',
                    'input': {'run_id': child.id, 'request_id': request_id, 'decision': 'grant',
                              'reason': '本次隔离测试已授权精确读取路径'}}])
            return ModelResponse(text='读取权限已裁决。', backend=self.name)

    parent_backend = ParentBackend()
    agent.backend = parent_backend
    parent = agent.run('检查并裁决本任务的能力请求', params=RunParams(run_id=parent_id, task_id=parent_id,
        request_id='parent-decision', save=False, allowed_tools=['list_pending_requests', 'resolve_capability_requests', 'read_file'],
        task_attributes={'conversation_thread_id': thread.thread_id, 'conversation_task_id': parent_id}))
    assert all(row['ok'] for row in parent.archive_tool_calls), [(r['error_code'], r['output_preview']) for r in parent.archive_tool_calls]
    assert request_id in json.dumps(parent_backend.seen[0])
    loaded = agent.subagents.load(child.id)
    assert loaded.capability_requests[0].status == 'GRANTED'
    second = _run_subagent_worker(params)
    loaded = agent.subagents.load(child.id)
    continuation = loaded.attributes['capability_resolution_continuations_by_request'][request_id]
    assert continuation['status'] == 'resumed', (second, continuation)
    assert second.status == 'DONE'
    for key, value in {'child_run_id': child.id, 'request_id': request_id,
                       'first_status': first.status, 'second_status': second.status,
                       'continuation': continuation}.items():
        record_testsuite_property(key, json.dumps(value, ensure_ascii=False))
    attempt_id = continuation['resumed_attempt_id']
    events = agent.subagents.runtime_db.events_for_attempt(attempt_id)
    assert any(e['event_type'] == 'capability_continuation.resumed' for e in events)
    assert any(e['event_type'] == 'tool_completed' and e.get('payload', {}).get('tool') == 'read_file' and e['payload'].get('ok') is True for e in events), [(e['event_type'], list(e.get('payload', {}))) for e in events]

    assert 'NATIVE_RESUME_READ_PROOF' in json.dumps(child_backend.seen[-1])
