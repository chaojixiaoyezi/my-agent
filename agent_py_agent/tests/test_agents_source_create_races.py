"""首次创建及回滚的确定性调度窗口，全部使用临时owner。"""
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.capability import agents_source_create as module
from agent_py_agent.agent.capability import persona_repository as persona
from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.tests.test_agents_source_create import legacy
from agent_py_agent.tests.test_agents_source_repository import source as source


# LLM: 正文实际写出后注入账本错误，只改变可复现时序，不替换被测写入/恢复逻辑。
# 函数用途: 在真实回滚读取校验结束后抢先替换公开文件。
def test_rollback_cannot_unlink_replacement_after_validation(source, monkeypatch):
    repo = legacy(source)
    reading = module.read_source_bytes
    restoring = []

    def fail_ledger(*args, **kwargs):
        restoring.append(True)
        raise OSError("ledger failure")

    def replace_after_read(path, **kwargs):
        result = reading(path, **kwargs)
        if restoring:
            restoring.clear()
            source.default_path.rename(source.owner_home / "original-first-write")
            source.default_path.write_text("EXTERNAL_BODY\n")
        return result

    monkeypatch.setattr(persona, "append_private_jsonl_records", fail_ledger)
    monkeypatch.setattr(module, "read_source_bytes", replace_after_read)
    with pytest.raises(AgentsContractError):
        repo.mutate(persona.PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert source.default_path.read_text() == "EXTERNAL_BODY\n"


# LLM: 在公开文件首次以FD打开后发生同inode外部写；准备文件私有化后不再假定公开O_CREAT入口。
# 函数用途: 保留原冲突正文/异常断言，检验公开之后的同inode编辑不能被覆盖或误报提交。
def test_first_create_preserves_external_nonempty_preimage(source, monkeypatch):
    repo = legacy(source)
    original = module.source_descriptor
    edited = []

    @contextmanager
    def write_before_yield(path, **kwargs):
        with original(path, **kwargs) as opened:
            if path == source.default_path and not edited:
                path.write_text("EXTERNAL_BODY\n")
                edited.append(True)
            yield opened

    monkeypatch.setattr(module, "source_descriptor", write_before_yield)
    with pytest.raises(AgentsContractError):
        repo.mutate(persona.PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert edited
    assert source.default_path.read_text() == "EXTERNAL_BODY\n"


# LLM: 真实来源扫描返回前替换owner目录；创建不得重新选择新目录，也不能误报成功。
# 函数用途: 验证首次创建绑定版本快照之前固定的父目录。
def test_first_create_rejects_parent_replacement_after_check(source, monkeypatch):
    repo = legacy(source)
    scan = source._scan

    def replace_parent(text):
        scan(text)
        source.owner_home.rename(source.owner_home.with_name("old-owner"))
        source.owner_home.mkdir()

    monkeypatch.setattr(source, "_scan", replace_parent)
    with pytest.raises(AgentsContractError):
        repo.mutate(persona.PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert not source.default_path.exists()


# LLM: 在隔离认领完成后让外部进程重建公开名；恢复只许排他发布，不能吞掉另一份用户数据。
# 函数用途: 验证自有捕获物及外部冲突时公开文件和恢复材料都不丢失；未知外部FD存在时禁止销毁恢复件。
@pytest.mark.parametrize("capture_external", [False, True])
def test_quarantine_does_not_overwrite_recreated_public_file(source, monkeypatch, capture_external):
    from agent_py_agent.agent.capability import agents_source_rollback as rollback
    repo = legacy(source)
    original = rollback._verify_captured_source

    def fail_ledger(*args, **kwargs):
        raise OSError("ledger failure")

    # LLM: 在隔离后的首个核验点重建公开名，原核验和恢复继续真实执行。
    # 函数用途: 固定公开名被外部抢占的时序。
    def recreate_public(quarantine, owned, expected):
        import os
        if capture_external:
            descriptor = os.open("captured", os.O_WRONLY | os.O_TRUNC, dir_fd=quarantine)
            try:
                os.write(descriptor, b"CAPTURED_EXTERNAL\n")
            finally:
                os.close(descriptor)
        source.default_path.write_text("NEW_PUBLIC_BODY\n")
        return original(quarantine, owned, expected)

    monkeypatch.setattr(persona, "append_private_jsonl_records", fail_ledger)
    monkeypatch.setattr(rollback, "_verify_captured_source", recreate_public)
    with pytest.raises(AgentsContractError):
        repo.mutate(persona.PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert source.default_path.read_text() == "NEW_PUBLIC_BODY\n"
    captures = list((source.owner_home / "persona").glob("agents-rollback-*/captured"))
    if capture_external:
        assert len(captures) == 1
        assert captures[0].read_text() == "CAPTURED_EXTERNAL\n"
    else:
        assert len(captures) == 1
        assert "FIRST_BODY" in captures[0].read_text()
