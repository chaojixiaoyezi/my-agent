"""Independent missing-default source/legacy commit race probes, synthetic homes only."""
import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
)
from agent_py_agent.tests.test_agents_source_repository import choose
from agent_py_agent.tests.test_agents_source_repository import source as source


def legacy(source):
    source.default_path.unlink()
    repo = PersonaRepository(owner_home=source.owner_home, agents_path=source.default_path,
                             user_path=source.owner_home/'USER.md', soul_path=source.owner_home/'SOUL.md')
    repo.agents_source = source
    return repo


def test_missing_default_normal_creation(source):
    repo = legacy(source)
    repo.mutate(PersonaMutationRequest('agents', 'add', content='NORMAL_FIRST_WRITE'))
    assert 'NORMAL_FIRST_WRITE' in source.default_path.read_text()
    assert source.configuration().kind == 'owner_default'


def test_missing_default_external_creation_not_overwritten(source, monkeypatch):
    repo = legacy(source)
    original = repo._write_version_snapshot
    def snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        source.default_path.write_text('USER_CREATED_FIRST\n')
        return result
    monkeypatch.setattr(repo, '_write_version_snapshot', snapshot)
    error = None
    try:
        repo.mutate(PersonaMutationRequest('agents', 'add', content='STALE_FIRST_WRITE'))
    except Exception as exc:
        error = str(exc)
    actual = source.default_path.read_text()
    assert actual == 'USER_CREATED_FIRST\n', {'body':actual, 'exception':error}


def test_missing_default_legacy_commit_cannot_follow_source_switch(source, monkeypatch):
    repo = legacy(source)
    target = source.owner_home/'new-source.txt'
    target.write_text('NEW_SELECTED_SOURCE\n')
    original = repo._write_version_snapshot
    switched = []
    def snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        if not switched:
            switched.append(choose(source, target))
        return result
    monkeypatch.setattr(repo, '_write_version_snapshot', snapshot)
    error = None
    try:
        repo.mutate(PersonaMutationRequest('agents', 'add', content='STALE_DEFAULT_BODY'))
    except Exception as exc:
        error = str(exc)
    assert source.configuration().kind == 'local_path'
    assert target.read_text() == 'NEW_SELECTED_SOURCE\n'
    assert not source.default_path.exists(), {'default_body':source.default_path.read_text(), 'exception':error}


# LLM: 在真实正文提交后使账本失败，原子写不能把失败误报成成功；保留外部编辑或身份替换。
# 函数用途: 验证首次写回滚仅清理本事务仍拥有的正文。
@pytest.mark.parametrize('external', ['none', 'edit', 'replace'])
def test_first_creation_ledger_failure_preserves_external_changes(source, monkeypatch, external):
    from agent_py_agent.agent.capability import persona_repository as module
    repo = legacy(source)

    def fail_ledger(*args, **kwargs):
        assert 'FIRST_BODY' in source.default_path.read_text()
        if external == 'replace':
            source.default_path.rename(source.owner_home / 'original-created.txt')
        if external != 'none':
            source.default_path.write_text('EXTERNAL_BODY\n')
        raise OSError('ledger failure')

    monkeypatch.setattr(module, 'append_private_jsonl_records', fail_ledger)
    from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
    expected_code = {'none': 'UNSAFE_PATH', 'edit': 'CONTENT_MISMATCH', 'replace': 'IDENTITY_CHANGED'}[external]
    # descriptor合同会把调用体的OSError映射为UNSAFE_PATH；外部编辑/换身则由恢复器精确拒绝。
    with pytest.raises(AgentsContractError, match='AGENTS_SOURCE_' + expected_code):
        repo.mutate(PersonaMutationRequest('agents', 'add', content='FIRST_BODY'))
    if external == 'none':
        assert not source.default_path.exists()
    else:
        assert source.default_path.read_text() == 'EXTERNAL_BODY\n'
    assert not repo.versions_path.exists()


# LLM: 配额/版本阶段之后再次核当前权限；拒绝时不能创建默认文件或残留已提交版本账本。
# 函数用途: 验证首次创建不沿用过时的权限结论。
def test_first_creation_permission_revocation(source, monkeypatch):
    from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
    repo = legacy(source)
    original = repo._write_version_snapshot

    def denied(*args):
        raise AgentsContractError('AGENTS_SOURCE_PERMISSION_REVOKED', 'locator')

    def snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(source, 'require_permission', denied)
        return result

    monkeypatch.setattr(repo, '_write_version_snapshot', snapshot)
    with pytest.raises(AgentsContractError, match='AGENTS_SOURCE_PERMISSION_REVOKED'):
        repo.mutate(PersonaMutationRequest('agents', 'add', content='FIRST_BODY'))
    assert not source.default_path.exists()
    assert not repo.versions_path.exists()
