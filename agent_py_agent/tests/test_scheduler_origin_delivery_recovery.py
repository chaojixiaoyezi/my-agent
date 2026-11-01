"""本地 origin 交付失败仍保留原 wake；恢复不得重新执行业务脚本。"""
import errno
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_delivery import (
    BackgroundDeliveryDependencies,
    cached_owner_delivery,
    record_background_response,
)
from agent_py_agent.agent.conversation.channels import DeliveryContext
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry
from agent_py_agent.tests.test_background_owner_delivery_commit import _agent


def test_local_origin_freezes_before_canonical_eio_and_preserves_exact_envelope(tmp_path, monkeypatch):
    agent, backend = _agent(tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id': 'alice', 'channel': 'cli',
        'channel_conversation_id': 'console', 'channel_user_id': 'alice'})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task-local', 'goal': '本地交付', 'status': 'active'})
    signal = store.wakes.raise_signal({'thread_id': thread.thread_id, 'root_task_id': 'task-local',
        'reason': 'scheduled_progress_report'})
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id='task-local', reason=signal.reason,
        conversation_turn_id='original-turn', wake_signal=signal.to_dict())
    channels = DeliveryService(build_default_channel_registry(agent.config))
    real = store.messages.append_once

    def fail(request, **options):
        if request.get('metadata', {}).get('assistant_part_id') == 'final':
            raise OSError(errno.EIO, 'single canonical EIO')
        return real(request, **options)

    monkeypatch.setattr(store.messages, 'append_once', fail)
    with pytest.raises(OSError):
        record_background_response(BackgroundDeliveryDependencies(agent, store, channels, lambda request: 'active'),
            request, DeliveryContext(channel='cli', target='console', thread_id=thread.thread_id, task_id='task-local'),
            '准确正文', assistant_commentaries=('准确过程',), deliver=True, delivery_reason='tool_rule_result')
    pending = store.wakes.pending_one(signal.wake_signal_id)
    frozen = cached_owner_delivery(pending)
    assert frozen is not None and frozen['canonical_pending'] is True
    assert frozen['external_sent'] is False and frozen['content'] == '准确正文'
    assert frozen['assistant_commentaries'] == ['准确过程']
    assert frozen['message_metadata']['conversation_request_id'] == 'original-turn'
    assert backend.prompts == []


def test_real_script_once_then_new_process_only_repairs_origin_delivery(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import tool_only_execution
    from agent_py_agent.agent.scheduler.repository import (
        SchedulerJobCreateRequest,
        SchedulerRunFinish,
    )
    from agent_py_agent.agent.scheduler.tool_work import execute_scheduled_tool
    from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
    from agent_py_agent.agent.workflows import ScriptRunner
    from agent_py_agent.tests.test_scheduler_execution import execution_case
    from agent_py_agent.tests.test_workflow_script_engine import script

    case = execution_case(tmp_path)
    repo = case.agent.scheduler_repository
    old = repo.claim_run(case.signal.root_task_id, lease_seconds=60, now=1001)
    repo.finish_run(old['run_id'], old['claim_id'], SchedulerRunFinish(status='cancelled', now=1002))
    case.store.wakes.mark_handled(case.signal.wake_signal_id, now=1002)
    repo.pause_job(case.job['job_id'], expected_version=repo.get_job(case.job['job_id'])['version'], now=1002)
    current = int(time.time())
    origin = case.store.threads.get_or_create({'canonical_user_id': 'user-1', 'channel': 'cli',
        'channel_conversation_id': 'console', 'channel_user_id': 'user-1', 'cwd': str(tmp_path)})
    job = repo.create_job(SchedulerJobCreateRequest(name='脚本交付', prompt='', thread_id=origin.thread_id,
        schedule={}, rules=[{'id': 'once', 'name': '真实脚本', 'use_llm': False,
            'tool': {'name': 'scripts', 'arguments': {'action': 'run', 'script_id': 'collect'}},
            'schedule': {'kind': 'every', 'every_seconds': 3600, 'anchor_at': current, 'timezone': 'UTC'}}],
        delivery={'mode': 'origin'}, now=current-10))[0]
    case.agent.scheduler_service.enqueue_ready_runs(now=current)
    signal = next(row for row in case.store.wakes.pending() if row.root_task_id != case.signal.root_task_id)
    claim = case.agent.scheduler_service.claim_wake(signal, now=current).claim
    repo.mark_run_running(claim.run_id, claim.claim_id, now=current)
    owner = case.agent.home_paths.owner_home_dir
    counter = tmp_path / 'script-counter'
    script(owner, f'from pathlib import Path\np=Path({str(counter)!r})\np.write_text(str(int(p.read_text())+1) if p.exists() else "1")\nprint(\'{{"value": 17}}\')')

    def execute(agent, params, *, tool_name, arguments):
        result = ScriptRunner(owner).run_script('collect')
        assert result.ok, result.stderr_preview
        output = json.dumps(result.output_data)
        tool = ToolResult(call_id='real-script-call', tool_name='scripts', status='succeeded', handler_executed=True)
        return SimpleNamespace(result=tool, call=SimpleNamespace(call_id='real-script-call')), output

    monkeypatch.setattr(tool_only_execution, 'execute_tool_work', execute)
    channels = DeliveryService(build_default_channel_registry(case.agent.config))
    case.agent.delivery_service = channels
    real = case.store.messages.append_once

    def fail(request, **options):
        if request.get('thread_id') == origin.thread_id and request.get('metadata', {}).get('assistant_part_id') == 'final':
            raise OSError(errno.EIO, 'single canonical EIO')
        return real(request, **options)

    monkeypatch.setattr(case.store.messages, 'append_once', fail)
    execute_scheduled_tool(case.agent, case.agent.scheduler_service, claim, signal,
        {'thread_id': claim.run['thread_id'], 'task_id': claim.run_id, 'reason': signal.reason,
         'wake_signal': signal.to_dict(), 'now': current})
    assert counter.read_text() == '1'
    pending = case.store.wakes.pending_one(signal.wake_signal_id)
    assert cached_owner_delivery(pending)['canonical_pending'] is True
    assert repo.get_active_run(claim.run_id)['status'] == 'waiting'
    # 新解释器不继承注入的执行器或 EIO；从原持久 wake/claim/asset 恢复。
    program = '''import json, os, sys, time
sys.path.insert(0, os.getcwd())
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.conversation import BackgroundMainAgentRuntime, BackgroundMainAgentScheduler
from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry
from agent_py_agent.agent.scheduler.tool_work import execute_scheduled_tool
agent=SimpleAgent(AgentConfig(my_agent_home=sys.argv[1],enable_tools=False,memory_path='memory.jsonl',orphan_supervision_interval_seconds=0),sys.argv[2])
assert str(agent.conversation_store.storage.root) == sys.argv[5], (str(agent.conversation_store.storage.root), sys.argv[5])
channels=DeliveryService(build_default_channel_registry(agent.config))
runtime=BackgroundMainAgentRuntime(agent=agent,store=agent.conversation_store,channels=channels)
agent.delivery_service=channels
for delta in (31,62,93):
    signal=agent.conversation_store.wakes.pending_one(sys.argv[4])
    if signal is None: break
    decision=agent.scheduler_service.claim_wake(signal,now=time.time()+delta)
    assert decision.claim is not None, decision.status
    claim=decision.claim
    execute_scheduled_tool(agent,agent.scheduler_service,claim,signal,{'thread_id':signal.thread_id,'task_id':signal.root_task_id,'reason':signal.reason,'wake_signal':signal.to_dict(),'now':time.time()+delta})
rows=agent.conversation_store.messages.recent(sys.argv[3],limit=0)
finals=[r.content for r in rows if r.metadata.get('assistant_part_id')=='final']
print(json.dumps({'finals':finals,'pending':agent.conversation_store.wakes.pending_one(sys.argv[4]) is not None}))
'''
    done = subprocess.run([sys.executable, '-c', program, str(case.agent.home_paths.root), str(tmp_path),
        origin.thread_id, signal.wake_signal_id, str(case.store.storage.root)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    observed = json.loads(done.stdout.strip().splitlines()[-1])
    assert observed == {'finals': ['{"value": 17}'], 'pending': False}, done.stderr
    assert counter.read_text() == '1' and case.backend.prompts == []
    assert job['delivery']['mode'] == 'origin'
