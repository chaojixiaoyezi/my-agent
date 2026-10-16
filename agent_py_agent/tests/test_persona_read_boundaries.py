"""Real Persona entry points: relative owners, concurrent readers, rollback quota."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaBatchMutationRequest,
    PersonaMutationRequest,
    PersonaRepository,
    persona_entry_id,
)
from agent_py_agent.agent.user_space.owner_quota import (
    OwnerQuotaEnforcer,
    OwnerQuotaExceeded,
    owner_logical_usage_bytes,
)


# LLM: 隔离owner显式路径，不改变生产人格；使用仓库真实mutate和batch入口。
# 函数用途: 构造不接来源服务的普通人格仓库。
def _repo(home):
    return PersonaRepository(owner_home=home, soul_path=home/'SOUL.md', user_path=home/'USER.md', agents_path=home/'AGENTS.md')


# LLM: 同一请求通过单条和批次两条公开入口提交，不能只验证底层writer。
# 函数用途: 共用测试操作，保留批次真实校验链。
def _mutate(repo, request, batch):
    return repo.mutate_batch(PersonaBatchMutationRequest(request.target, (request,))) if batch else repo.mutate(request)


# LLM: 相对路径在测试cwd解释，owner外不能出现正文；真实首次提交同时有快照和账本。
# 函数用途: 验证不会丢失相对路径第一个目录段。
@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('target', ['user', 'soul', 'agents'])
def test_relative_owner_stays_inside_boundary(tmp_path, monkeypatch, batch, target):
    monkeypatch.chdir(tmp_path)
    Path('home').mkdir()
    repo = _repo(Path('home'))
    result = _mutate(repo, PersonaMutationRequest(target, 'add', content='RELATIVE_RULE', confirmed=True), batch)
    assert result['ok'] and result['changed']
    expected = tmp_path/'home'/f'{target.upper()}.md'
    assert expected.is_file()
    assert 'RELATIVE_RULE' in expected.read_text()
    assert not (tmp_path/f'{target.upper()}.md').exists()
    assert repo.load(target).diagnostic.sha256 == result['sha256']
    assert repo.history(target)[-1]['sha256'] == result['sha256']


# LLM: 暂停在真实os.write之后、ftruncate之前；读者使用另一仓库实例，需返回完整正文而非混合字节。
# 函数用途: 单条/批次写入与load并发，必须共享整个写入和恢复阶段的锁。
@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('reader', ['load', 'list', 'sha'])
def test_inplace_write_not_visible_to_readers(tmp_path, monkeypatch, batch, reader):
    from hashlib import sha256

    from agent_py_agent.agent.capability import agents_source_files as files

    repo = _repo(tmp_path)
    other = _repo(tmp_path)
    path = repo.path_for('user')
    old = '- ' + 'X' * 5000 + '\n'
    path.write_text(old)
    entered, release, reading = Event(), Event(), Event()
    write_all = files._write_all

    def pause(descriptor, body):
        write_all(descriptor, body)
        if body == b'- SHORT\n':
            entered.set()
            assert release.wait(5)

    def read():
        reading.set()
        if reader == 'load':
            return other.load('user').diagnostic.sha256
        if reader == 'list':
            return other.list_entries('user')['sha256']
        return other.current_sha256('user')

    monkeypatch.setattr(files, '_write_all', pause)
    request = PersonaMutationRequest('user', 'replace', content='SHORT', entry_id=persona_entry_id('user', 'X' * 5000))
    with ThreadPoolExecutor(2) as pool:
        write = pool.submit(_mutate, repo, request, batch)
        try:
            assert entered.wait(5)
            future = pool.submit(read)
            assert reading.wait(5)
            # 旧实现立即返回混合正文；修复后读者在目标锁上等待。
            observed = _try_read_during_write(future)
        finally:
            release.set()
        result = write.result(timeout=5)
        observed = future.result(timeout=5) if observed is None else observed
    assert observed in {sha256(old.encode()).hexdigest(), result['sha256']}


# LLM: 大基线在回滚时保留到恢复目录，真实扫描最终用量；预算允许旧成功预测但不够失败终态。
# 函数用途: 删除缩短正文不能提前抵扣失败时会恢复的空间。
@pytest.mark.parametrize('batch', [False, True])
def test_failed_shrink_stays_within_quota(tmp_path, monkeypatch, batch):
    from agent_py_agent.agent.capability import persona_repository as module

    repo = _repo(tmp_path)
    content = 'X' * 30000
    original = '- ' + content + '\n'
    path = repo.path_for('user')
    path.write_text(original)
    limit = owner_logical_usage_bytes(tmp_path) + 10000
    repo.quota_enforcer = OwnerQuotaEnforcer(tmp_path, max_bytes=limit)

    def fail(*args):
        raise OSError('ledger staging unavailable')

    monkeypatch.setattr(module, 'append_private_jsonl_records', fail)
    request = PersonaMutationRequest('user', 'remove', entry_id=persona_entry_id('user', content))
    with pytest.raises((OSError, OwnerQuotaExceeded)):
        _mutate(repo, request, batch)
    assert path.read_text() == original
    assert not repo.versions_path.exists()
    assert owner_logical_usage_bytes(tmp_path) <= limit


# LLM: 有限等待区分旧版立即混合读取和新版等锁，不放宽最终完整摘要断言。
# 函数用途: 在线程屏障期间采样读者是否提前完成。
def _try_read_during_write(future):
    from concurrent.futures import TimeoutError
    try:
        return future.result(timeout=0.2)
    except TimeoutError:
        return None
