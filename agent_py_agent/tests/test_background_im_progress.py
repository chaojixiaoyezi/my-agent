"""真实任务/claim/原 DeliveryService；供应商运输替身，周期加速但不替模型编造进展。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.background_im_progress import (
    BackgroundIMProgress,
    make_background_progress,
)
from agent_py_agent.agent.conversation.execution_threads import (
    IM_TASK_EXECUTION_KEY,
    ensure_execution_thread,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_scheduler_repository import _every


# LLM: 使用正式独立线程、任务和 claim；发送替身仍由原 DeliveryService 保存真实返回的消息句柄。
# 函数用途: 创建没有 TUI 消费者的 IM 后台工作以及可记录编辑的通道。
def _work(tmp_path, *, interval=120):
    from types import SimpleNamespace
    agent=SimpleAgent(AgentConfig(model_backend='echo',my_agent_home=str(tmp_path/'home'),
        enable_plugins=False,im_stream_heartbeat_seconds=interval),tmp_path)
    store=agent.conversation_store
    source=store.threads.get_or_create({'channel':'feishu','channel_user_id':'ou_test','channel_conversation_id':'oc_private',
        'canonical_user_id':'ou_test','owner_id':agent.home_paths.owner_id,'owner_home':str(agent.home_paths.owner_home_dir)})
    work=ensure_execution_thread(agent,source.thread_id,'synthetic-progress',kind='delegated')
    store.threads.update_atomic(work.thread_id,lambda t:replace(t,metadata={**t.metadata,IM_TASK_EXECUTION_KEY:{'mode':'delegated'}}))
    store.tasks.bind({'thread_id':work.thread_id,'task_id':'synthetic-root','goal':'合成后台工作','status':'active'})
    claim=store.claims.acquire({'thread_id':work.thread_id,'task_id':'synthetic-root','reason':'thread_goal_continue'})
    sent,edits=[],[]
    adapter=SimpleNamespace(start_progress_message=lambda target,text,**kwargs: sent.append((target,text,kwargs)) or 'om_progress',
        edit_progress_message=lambda message_id,text: edits.append((message_id,text)) or True)
    agent.channel_registry.register_adapter('feishu',adapter,capabilities=ChannelCapabilities(proactive=True,edit_progress=True))
    return agent,work,claim,sent,edits


def test_background_reuses_same_window_limit_and_stops_before_final(tmp_path):
    agent,work,claim,sent,edits=_work(tmp_path)
    observer=BackgroundIMProgress(agent,'synthetic-root')
    try:
        assert observer.thread is not None and observer.interval==120
        observer.update('执行命令 '+('合成内容'*80))
        observer._deliver()
        observer._deliver()
        observer.close()
        assert len(sent)==1 and len(edits)==2
        assert all(len(text)<=100 and '更新于 ' in text for _,text in edits)
        assert all(message_id=='om_progress' for message_id,_ in edits)
        assert '本轮已结束' in edits[-1][1]
        assert not observer.thread.is_alive()
    finally:
        observer.close()


def test_cancelled_claim_does_not_emit_new_heartbeat_or_overwrite_next_attempt(tmp_path):
    agent,work,claim,sent,edits=_work(tmp_path)
    observer=BackgroundIMProgress(agent,'synthetic-root')
    observer._deliver()
    agent.conversation_store.claims.finish({'thread_id':work.thread_id,'claim_id':claim['claim_id']})
    observer.close()
    assert len(sent)==1 and not edits


def test_final_close_waits_for_inflight_provider_edit_even_past_short_join_timeout(tmp_path, monkeypatch):
    import threading

    agent,work,claim,sent,edits=_work(tmp_path,interval=.01)
    sending, release=threading.Event(),threading.Event()
    adapter=agent.channel_registry.adapter_for('feishu')
    original=adapter.start_progress_message
    def slow_send(target,text,**kwargs):
        sending.set()
        assert release.wait(2)
        return original(target,text,**kwargs)
    adapter.start_progress_message=slow_send
    observer=BackgroundIMProgress(agent,'synthetic-root')
    assert sending.wait(2)
    join=observer.thread.join
    # 保持真实线程与网络在途顺序；只缩短旧 15 秒超时，避免回归要等整整 15 秒。
    monkeypatch.setattr(observer.thread,'join',lambda timeout=None:join(.015 if timeout is not None else None))
    timer=threading.Timer(.08,release.set)
    timer.start()
    try:
        observer.close()
        assert release.is_set() and not observer.thread.is_alive()
        assert len(sent)==1 and len(edits)==1 and '本轮已结束' in edits[0][1]
    finally:
        release.set()
        join(2)
        timer.join(2)


def test_disabled_or_non_delegated_work_does_not_create_im_window(tmp_path):
    agent,work,claim,sent,edits=_work(tmp_path,interval=0)
    observer=BackgroundIMProgress(agent,'synthetic-root')
    assert observer.thread is None
    observer.close()
    agent.config.im_stream_heartbeat_seconds=120
    agent.conversation_store.threads.update_atomic(work.thread_id,lambda t:replace(t,
        metadata={k:v for k,v in t.metadata.items() if k!=IM_TASK_EXECUTION_KEY}))
    plain=BackgroundIMProgress(agent,'synthetic-root')
    assert plain.thread is None
    plain.close()
    assert not sent and not edits


# LLM: 只用正式repository冻结通知策略，再绑定原根任务/claim；运输仍是原DeliveryService的合成适配器。
# 函数用途: 复现定时任务followup沿已有委派线程执行的合法状态，不手写scheduler运行快照。
def _scheduled_followup(tmp_path, delivery):
    import time

    agent,work,old_claim,sent,edits=_work(tmp_path)
    store=agent.conversation_store
    store.claims.finish({'thread_id':work.thread_id,'claim_id':old_claim['claim_id']})
    current=time.time()
    job,_=agent.scheduler_repository.create_job(SchedulerJobCreateRequest(name='合成跟随监控',prompt='检查合成事实',
        thread_id=work.thread_id,schedule=_every(),delivery=delivery,now=current))
    run=agent.scheduler_repository.reserve_manual_run(job['job_id'],now=current)
    scheduler_claim=agent.scheduler_repository.claim_run(run['run_id'],lease_seconds=30,now=current)
    agent.scheduler_repository.mark_run_running(run['run_id'],scheduler_claim['claim_id'],now=current)
    store.tasks.bind({'thread_id':work.thread_id,'task_id':run['run_id'],'goal':'合成跟随监控','status':'active'})
    store.claims.acquire({'thread_id':work.thread_id,'task_id':run['run_id'],'reason':'scheduled_job_due'})
    return agent,work,job,run,sent,edits


@pytest.mark.parametrize('delivery', [{'mode':'origin','reply_policy':'explicit'}, {'mode':'local'}, {'mode':'none'}])
def test_conditional_or_silent_scheduler_never_opens_delegated_progress_window(tmp_path, delivery):
    agent,work,job,run,sent,edits=_scheduled_followup(tmp_path,delivery)
    observer=BackgroundIMProgress(agent,run['run_id'])
    observer.update('合成长工具等待')
    observer.close()
    assert observer.thread is None and not sent and not edits


@pytest.mark.parametrize('delivery', [None, {'mode':'origin'}, {'mode':'origin','reply_policy':'always'}])
def test_legacy_origin_always_keeps_existing_progress_window(tmp_path, delivery):
    agent,work,job,run,sent,edits=_scheduled_followup(tmp_path,delivery)
    observer=BackgroundIMProgress(agent,run['run_id'])
    try:
        assert observer.thread is not None and observer.interval==120
        observer._deliver()
    finally:
        observer.close()
    assert len(sent)==1 and len(edits)==1


def test_progress_reads_current_run_frozen_policy_instead_of_updated_job(tmp_path):
    agent,work,job,run,sent,edits=_scheduled_followup(tmp_path,{'mode':'origin','reply_policy':'explicit'})
    agent.scheduler_repository.update_job(job['job_id'],patch={'delivery':{'mode':'origin','reply_policy':'always'}},
        expected_version=job['version'])
    observer=BackgroundIMProgress(agent,run['run_id'])
    observer.close()
    assert observer.thread is None and not sent and not edits
    assert agent.scheduler_repository.get_active_run(run['run_id'])['delivery']=={'mode':'origin','reply_policy':'explicit'}


@pytest.mark.parametrize('field', ['run_id','thread_id'])
def test_wrong_scope_record_cannot_impersonate_current_scheduler_progress(tmp_path, monkeypatch, field):
    agent,work,job,run,sent,edits=_scheduled_followup(tmp_path,{'mode':'origin','reply_policy':'always'})
    mismatched={**agent.scheduler_repository.get_active_run(run['run_id']),field:'other-scope'}
    monkeypatch.setattr(agent.scheduler_repository,'get_active_run',lambda _task_id:mismatched)
    with pytest.raises(ValueError,match='原根任务或执行线程'):
        BackgroundIMProgress(agent,run['run_id'])
    assert make_background_progress(agent,run['run_id']) is None
    assert not sent and not edits
