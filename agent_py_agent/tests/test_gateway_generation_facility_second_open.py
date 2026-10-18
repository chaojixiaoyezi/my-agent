# LLM: 复用241原真实atomic writer反例，原assert/函数AST保持；仅临时目录/受控线程，不创建服务或native/模型权限。
# 模块用途: 验证精确暂存设施第二次打开前自然消失不误判替换，责任原字节和writer结果仍逐项核对。
import threading

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.gateway_parts import multiprocess_drain_sources as sources
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


def test_original_atomic_cleanup_before_second_facility_open_is_not_source_change(tmp_path, monkeypatch):
    paths=gateway_paths_from_root(tmp_path/'gateway')
    paths.processing.mkdir(parents=True)
    canonical=paths.processing/'legacy.json'
    old=b'{"id":"legacy","route":{"c_run_id":"new-c"}}'
    canonical.write_bytes(old)
    entered,resume,finished=threading.Event(),threading.Event(),threading.Event()
    writer_error=OSError('controlled serialization failure before publication')
    errors=[]
    def serialize(*args,**kwargs):
        entered.set()
        assert resume.wait(5)
        raise writer_error
    monkeypatch.setattr(json_io,'serialize_private_json_no_newline',serialize)
    def writer():
        try:
            json_io.write_private_json_file_atomic_no_newline_unlocked(canonical,{'new':True})
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()
    thread=threading.Thread(target=writer)
    thread.start()
    try:
        assert entered.wait(5)
        stages=list(paths.processing.glob('.*.tmp'))
        assert len(stages)==1
        stage=stages[0]
        original=sources._generation_facility_identity
        calls=0
        def observe(parent,name,kind):
            nonlocal calls
            if name==stage.name:
                calls+=1
                if calls==2:
                    resume.set()
                    assert finished.wait(5)
                    assert not stage.exists()
            return original(parent,name,kind)
        monkeypatch.setattr(sources,'_generation_facility_identity',observe)
        observed=history.generation_source_files(paths)
        assert observed=={'requests/processing/legacy.json':old}
        assert calls==2
    finally:
        resume.set()
        thread.join(5)
        assert not thread.is_alive()
        assert errors==[writer_error]
        assert canonical.read_bytes()==old
