"""结果consumer退出与worker发表精确交错，不靠GC丢原异常或假签物理退出。"""
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation


@pytest.mark.parametrize('publication', ['after_exit', 'before_exit'])
def test_abandoned_result_releases_exception_request_without_gc(tmp_path, publication):
    code = r'''
import gc,threading,weakref,sys
from types import SimpleNamespace
from agent_py_agent.agent.agent_core import tool_model_generation as generation
class Payload:
    def __init__(self):self.body=bytearray(512*1024)
class Token:
    def release(self):released.set()
class Ledger:
    def retain_call(self,*args):return Token()
release=threading.Event();published=threading.Event();released=threading.Event();workers=[];queues=[]
publication=sys.argv[1]
def provider(request,*args):
    assert release.wait(3)
    raise RuntimeError('synthetic late failure')
def wait(request,state,results,worker,*args,**kwargs):
    workers.append(worker);queues.append(results)
    assert not released.is_set(), 'caller不能代worker释放原保留权'
    if publication=='before_exit':
        release.set()
        assert published.wait(3)
    return None
# queue.put返回后才证明发表完毕；不靠sleep或轮询队列等待。
class ObservedQueue(generation._GenerationResultQueue):
    def put(self,*args,**kwargs):
        super().put(*args,**kwargs)
        published.set()
generation._GenerationResultQueue=ObservedQueue
generation._effective_model_request_timeout_seconds=lambda *args,**kwargs:1
generation._generate_backend_response=provider
generation._wait_for_generation_result=wait
payload=Payload();reference=weakref.ref(payload)
request=SimpleNamespace(agent=SimpleNamespace(),prompt='',payload=payload)
state=SimpleNamespace(ledger=Ledger(),call_id='synthetic')
gc.disable()
generation._generate_through_wall_guard(request,state)
del payload,request,state
release.set();workers[0].join(3)
assert not workers[0].is_alive() and released.is_set()
assert queues[0].empty(), 'consumer退出前已入队或之后迟到的异常均不可滞留'
workers.clear();queues.clear()
assert reference() is None, 'worker退出后仍由无人接收的异常环保留请求'
'''
    home = tmp_path / 'home'
    home.mkdir()
    completed = subprocess.run([sys.executable, '-c', code, publication], capture_output=True, text=True,
        timeout=15, env={**os.environ, 'HOME': str(home), 'MY_AGENT_HOME': str(home)})
    assert completed.returncode == 0, completed.stderr


def test_consumed_provider_failure_preserves_original_exception_and_traceback(monkeypatch):
    released = threading.Event()
    workers = []
    error = ValueError('synthetic provider error')
    class Token:
        def release(self):
            released.set()
    class Ledger:
        def retain_call(self, call_id):
            return Token()
    def provider(*args):
        workers.append(threading.current_thread())
        raise error
    monkeypatch.setattr(generation, '_effective_model_request_timeout_seconds', lambda *args, **kwargs: 2)
    monkeypatch.setattr(generation, '_generate_backend_response', provider)
    request = SimpleNamespace(agent=SimpleNamespace(backend=None), prompt='')
    state = SimpleNamespace(ledger=Ledger(), call_id='synthetic')
    with pytest.raises(ValueError) as caught:
        generation._generate_through_wall_guard(request, state)
    assert caught.value is error
    frames, trace = [], caught.value.__traceback__
    while trace is not None:
        frames.append(trace.tb_frame.f_code.co_name)
        trace = trace.tb_next
    assert 'provider' in frames and '_target' in frames
    workers[0].join(3)
    assert not workers[0].is_alive() and released.is_set()
