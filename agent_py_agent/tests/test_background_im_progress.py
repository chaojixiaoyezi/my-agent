"""真实任务/claim/原 DeliveryService；供应商运输替身，周期加速但不替模型编造进展。"""
from dataclasses import replace

from agent_py_agent.agent.conversation.background_im_progress import BackgroundIMProgress
from agent_py_agent.agent.conversation.execution_threads import (
    IM_TASK_EXECUTION_KEY,
    ensure_execution_thread,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.settings import AgentConfig


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
