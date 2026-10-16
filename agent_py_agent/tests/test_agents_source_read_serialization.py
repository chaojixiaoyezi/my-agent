"""Real source and Persona entry readers must not observe cooperative partial writes."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Event

import pytest

from agent_py_agent.agent.capability import agents_source_files as files
from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    persona_entry_id,
)
from agent_py_agent.tests.test_agents_source_repository import grant
from agent_py_agent.tests.test_agents_source_repository import source as source
from agent_py_agent.tests.test_persona_read_boundaries import _repo, _try_read_during_write


# LLM: 同一来源被两个独立仓库消费；写者分别走原生授权入口与默认Persona事务。
# 函数用途: 不用假锁断言，实际暂停在短写之后、截断之前检查所有公开读取入口。
@pytest.mark.parametrize('writer', ['source', 'persona'])
@pytest.mark.parametrize('reader', ['source', 'persona', 'list', 'sha'])
def test_source_readers_wait_for_complete_write(source, monkeypatch, writer, reader):
    old, new = '- ' + 'X' * 5000 + '\n', '- SHORT\n'
    source.default_path.write_text(old)
    repo = _repo(source.owner_home)
    repo.agents_source = source
    other = _repo(source.owner_home)
    other.agents_source = source
    entered, release, reading = Event(), Event(), Event()
    write_all = files._write_all
    auth, context = grant(source, 'write', new)

    def pause(fd, body):
        write_all(fd, body)
        if body == new.encode():
            entered.set()
            assert release.wait(5)

    def write():
        if writer == 'source':
            return source.write_text(new, authorization=auth, context=context)
        return repo.mutate(PersonaMutationRequest('agents', 'replace', content='SHORT', entry_id=persona_entry_id('agents', 'X' * 5000)))

    def read():
        reading.set()
        if reader == 'source':
            return source.load().snapshot.content_sha256
        if reader == 'persona':
            return other.load('agents').diagnostic.sha256
        if reader == 'list':
            return other.list_entries('agents')['sha256']
        return other.current_sha256('agents')

    monkeypatch.setattr(files, '_write_all', pause)
    with ThreadPoolExecutor(2) as pool:
        writing = pool.submit(write)
        try:
            assert entered.wait(5)
            future = pool.submit(read)
            assert reading.wait(5)
            observed = _try_read_during_write(future)
        finally:
            release.set()
        writing.result(timeout=5)
        observed = future.result(timeout=5) if observed is None else observed
    assert observed in {sha256(old.encode()).hexdigest(), sha256(new.encode()).hexdigest()}


# LLM: 默认只读入口不应为了同步首次创建控制目录或锁文件。
# 函数用途: 核对读取存在/缺失默认正文均不落盘。
@pytest.mark.parametrize('missing', [False, True])
def test_source_read_creates_no_state(source, missing):
    if missing:
        source.default_path.unlink()
    before = set(source.owner_home.rglob('*'))
    loaded = source.load()
    assert loaded.content == ('' if missing else source.default_path.read_bytes().decode())
    assert set(source.owner_home.rglob('*')) == before
