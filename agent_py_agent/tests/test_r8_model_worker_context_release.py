"""真实模型guard出生、结束与显式Context传播，不依赖强制GC释放。"""
import os
import subprocess
import sys


def test_completed_guard_does_not_retain_inherited_request_context(tmp_path):
    code = r'''
import contextvars,threading,weakref
from types import SimpleNamespace
from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.conversation.background_shutdown import BackgroundShutdown
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger,ModelCallStartedParams,ModelCallFinishParams
class Payload:
    def __init__(self):self.body=bytearray(512*1024)
current=contextvars.ContextVar('synthetic-request-history')
shutdown=BackgroundShutdown();ledger=ModelCallLedger();references=[];workers=[];seen=[]
def backend(*args):
    workers.append(threading.current_thread())
    seen.append(len(current.get().body))
    return 'done'
generation._effective_model_request_timeout_seconds=lambda *args,**kwargs:5.0
generation._generate_backend_response=backend
request=SimpleNamespace(agent=SimpleNamespace(backend=None),prompt='synthetic')
for i in range(3):
    payload=Payload();references.append(weakref.ref(payload));token=current.set(payload)
    ledger.started(ModelCallStartedParams(str(i),'fake','fake',0))
    state=SimpleNamespace(ledger=ledger,call_id=str(i),liveness=SimpleNamespace(abandon=lambda:None))
    try:
        with shutdown.observe(None):
            assert generation._generate_through_wall_guard(request,state)=='done'
            workers[-1].join(3)
            assert not workers[-1].is_alive()
            ledger.finished(ModelCallFinishParams(str(i)))
    finally:current.reset(token)
    del payload,token,state
assert seen==[512*1024]*3, '原显式Context仍必须传递业务身份和请求字段'
assert not shutdown.slices, '自然退出的原观察应可核收'
assert all(reference() is None for reference in references), '已退出Thread不能再钉住第二份隐式请求Context'
assert len(workers)==3 and all(not worker.is_alive() for worker in workers)
'''
    home = tmp_path / 'home'
    home.mkdir()
    args = [sys.executable]
    if hasattr(sys.flags, 'thread_inherit_context'):
        args += ['-X', 'thread_inherit_context=1']
    completed = subprocess.run([*args, '-c', code], capture_output=True, text=True, timeout=30,
        env={**os.environ, 'HOME': str(home), 'MY_AGENT_HOME': str(home)})
    assert completed.returncode == 0, completed.stderr
