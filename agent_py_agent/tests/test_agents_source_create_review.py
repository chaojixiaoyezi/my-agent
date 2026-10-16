"""首次创建第二轮复审：冻结状态、既有FD与活权限。"""
import os
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.capability import agents_source_create as creation
from agent_py_agent.agent.capability import agents_source_rollback as rollback
from agent_py_agent.agent.capability import persona_repository as persona
from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.tests.test_agents_source_create import legacy
from agent_py_agent.tests.test_agents_source_repository import source as source


# LLM: 在调用方记录不存在后、writer选择前创建空外部文件，再于版本快照阶段写外部正文。
# 函数用途: 冻结状态不一致时也不能进入裸unlink删除外部文件。
def test_creation_race_between_state_and_writer_preserves_external_file(source, monkeypatch):
    repo = legacy(source)
    factory = persona._persona_mutation_writer
    version = repo._write_version_snapshot

    # LLM: 精确遵循实际writer的四参数协议，文件竞争点仍在writer选择前。
    # 函数用途: 模拟外部空文件抢先到达。
    @contextmanager
    def external_arrives(repository, path, previous, document_existed):
        path.write_text("")
        with factory(repository, path, previous, document_existed) as writer:
            yield writer

    def external_fills(*args, **kwargs):
        result = version(*args, **kwargs)
        source.default_path.write_text("EXTERNAL_BODY\n")
        return result

    monkeypatch.setattr(persona, "_persona_mutation_writer", external_arrives)
    monkeypatch.setattr(repo, "_write_version_snapshot", external_fills)
    with pytest.raises(AgentsContractError):
        repo.mutate(persona.PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert source.default_path.exists(), "拒绝旧写入后不能删除外部创建的文件"
    assert source.default_path.read_text() == "EXTERNAL_BODY\n"
    assert not repo.versions_path.exists()


# LLM: 旧写FD在改名前取得，核验返回后再写并关闭；不可依赖私有路径阻止已打开的FD。
# 函数用途: 回滚不销毁包含外部迟到编辑的最后一个可恢复目录项。
def test_open_external_fd_after_quarantine_validation_remains_recoverable(source, monkeypatch):
    repo = legacy(source)
    verify = rollback._verify_captured_source
    descriptors = []

    def fail_ledger(*args, **kwargs):
        descriptors.append(os.open(source.default_path, os.O_WRONLY))
        raise OSError("ledger failure")

    def external_writes_after_verification(*args):
        verify(*args)
        descriptor = descriptors.pop()
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, b"LATE_EXTERNAL_BODY\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    monkeypatch.setattr(persona, "append_private_jsonl_records", fail_ledger)
    monkeypatch.setattr(rollback, "_verify_captured_source", external_writes_after_verification)
    try:
        with pytest.raises(AgentsContractError):
            repo.mutate(persona.PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    captures = list((source.owner_home / "persona").glob("agents-rollback-*/captured"))
    assert any(path.read_bytes() == b"LATE_EXTERNAL_BODY\n" for path in captures)
    assert not source.default_path.exists()
    assert not repo.versions_path.exists()


# LLM: 使用repository真实活权限提供器，扫描或读前像之后变成fail-closed；不替换权限判定方法。
# 函数用途: 验证耗时步骤后、正文副作用前重新检查当前权限。
@pytest.mark.parametrize("point", ["scan", "read"])
def test_first_create_rechecks_live_permission_after_io(source, monkeypatch, point):
    repo = legacy(source)
    revoked = []
    policy = source.path_policy

    def live_policy():
        if revoked:
            raise AgentsContractError("AGENTS_SOURCE_PERMISSION_REVOKED", "locator")
        return policy

    monkeypatch.setattr(source, "_path_policy_provider", live_policy)
    original = source._scan if point == "scan" else creation.read_source_bytes

    def revoke_after(*args, **kwargs):
        result = original(*args, **kwargs)
        revoked.append(True)
        return result

    monkeypatch.setattr(source if point == "scan" else creation,
        "_scan" if point == "scan" else "read_source_bytes", revoke_after)
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_PERMISSION_REVOKED"):
        repo.mutate(persona.PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert not source.default_path.exists()
    assert not repo.versions_path.exists()
