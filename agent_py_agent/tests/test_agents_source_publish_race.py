"""默认来源公开以后、末次活权限核验期间的真实文件竞争。"""
import pytest

from agent_py_agent.agent.capability import agents_source_create as creation
from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.agent.capability.persona_repository import PersonaMutationRequest
from agent_py_agent.tests.test_agents_source_create import legacy
from agent_py_agent.tests.test_agents_source_repository import source as source


# LLM: 注入点是读取之后的活权限调用，读/写/回滚全部是真实实现；不修改断言迁就内部重排。
# 函数用途: 外部在校验后编辑或换身，既不能丢正文，也不能伪造一次版本提交。
@pytest.mark.parametrize("external", ["edit", "replace"])
def test_permission_callback_cannot_overwrite_external_source(source, monkeypatch, external):
    repo = legacy(source)
    reading = creation.read_source_bytes
    permission = source.require_permission
    ready, edited = [], []

    def after_read(path, **kwargs):
        result = reading(path, **kwargs)
        if path == source.default_path and not edited:
            ready.append(True)
        return result

    def edit_at_permission(*args, **kwargs):
        permission(*args, **kwargs)
        if ready and not edited:
            if external == "replace":
                source.default_path.rename(source.owner_home / "displaced-created-source")
            source.default_path.write_text("EXTERNAL_AFTER_READ\n")
            edited.append(True)

    monkeypatch.setattr(creation, "read_source_bytes", after_read)
    monkeypatch.setattr(source, "require_permission", edit_at_permission)
    with pytest.raises(AgentsContractError):
        repo.mutate(PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert edited
    assert source.default_path.read_text() == "EXTERNAL_AFTER_READ\n"
    assert not repo.versions_path.exists()


# LLM: 真实私有正文写入之前观察公开名，发布后读取完整字节，禁止空文件暴露与部分正文提交。
# 函数用途: 验证准备阶段不创建公开AGENTS，公开时已有完整正文且只剩一个链接。
def test_first_source_is_complete_at_publication(source, monkeypatch):
    import os

    from agent_py_agent.agent.capability import agents_source_publish as publishing
    repo = legacy(source)
    write_all, publish = publishing._write_all, publishing.PreparedDefaultSource.publish
    observations = []

    def write_private(fd, body):
        assert not source.default_path.exists()
        observations.append("private")
        write_all(fd, body)

    def after_publish(prepared, name):
        publish(prepared, name)
        assert "FIRST_BODY" in source.default_path.read_text()
        assert os.stat(source.default_path).st_nlink == 1
        observations.append("public")

    monkeypatch.setattr(publishing, "_write_all", write_private)
    monkeypatch.setattr(publishing.PreparedDefaultSource, "publish", after_publish)
    repo.mutate(PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert observations == ["private", "public"]


# LLM: 发布系统调用前插入外部创建，不能因为准备件已有完整正文就覆盖用户抢占的名字。
# 函数用途: 测试最后一个公开发布窗口的无覆盖保障。
def test_external_creation_at_publication_wins(source, monkeypatch):
    from agent_py_agent.agent.capability import agents_source_publish as publishing
    repo = legacy(source)
    publish = publishing.PreparedDefaultSource.publish

    def before_publish(prepared, name):
        source.default_path.write_text("EXTERNAL_AT_PUBLISH\n")
        publish(prepared, name)

    monkeypatch.setattr(publishing.PreparedDefaultSource, "publish", before_publish)
    with pytest.raises(AgentsContractError):
        repo.mutate(PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert source.default_path.read_text() == "EXTERNAL_AT_PUBLISH\n"
    assert not repo.versions_path.exists()


# LLM: 发布成功后尚未首次读取即报错；事务必须根据发布责任清退，而非依赖后续opened字段。
# 函数用途: 验证发布后异常不遗留错误的公开正文或版本账本。
def test_error_after_publication_keeps_rollback_responsibility(source, monkeypatch):
    from agent_py_agent.agent.capability import agents_source_publish as publishing
    repo = legacy(source)
    publish = publishing.PreparedDefaultSource.publish

    def after_publish(prepared, name):
        publish(prepared, name)
        raise OSError("failure before public validation")

    monkeypatch.setattr(publishing.PreparedDefaultSource, "publish", after_publish)
    with pytest.raises(AgentsContractError):
        repo.mutate(PersonaMutationRequest("agents", "add", content="FIRST_BODY"))
    assert not source.default_path.exists()
    assert not repo.versions_path.exists()
    captures = list((source.owner_home / "persona").glob("agents-rollback-*/captured"))
    assert len(captures) == 1 and "FIRST_BODY" in captures[0].read_text()


# LLM: 故障发生在link成功后的首次私有unlink，真实回滚必须处理双链接中间态，不能放宽外部硬链接校验。
# 函数用途: 模拟首次发布中途清理失败，验证不遗留无账本的公开正文。
def test_first_private_unlink_failure_withdraws_published_body(source, monkeypatch):
    from agent_py_agent.agent.capability import agents_source_publish as publishing

    repo = legacy(source)
    unlink = publishing.os.unlink
    failed = []

    def fail_once(path, *args, **kwargs):
        if str(path).startswith('.agents-create-') and not failed:
            failed.append(True)
            assert source.default_path.exists()
            raise OSError('private unlink interrupted')
        return unlink(path, *args, **kwargs)

    from types import SimpleNamespace
    monkeypatch.setattr(publishing, 'os', SimpleNamespace(**dict(vars(publishing.os), unlink=fail_once)))
    with pytest.raises(AgentsContractError):
        repo.mutate(PersonaMutationRequest('agents', 'add', content='FIRST_BODY'))
    assert failed
    assert not source.default_path.exists()
    assert not repo.versions_path.exists()
    assert not list((source.owner_home / 'persona').glob('.agents-create-*'))
