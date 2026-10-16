"""终审反例：同inode外部编辑不被Persona提交或失败回滚覆盖。"""
import pytest

from agent_py_agent.agent.capability._agents_contract_fields import AgentsContractError
from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
)
from agent_py_agent.tests.test_agents_source_repository import grant
from agent_py_agent.tests.test_agents_source_repository import source as source


@pytest.fixture(params=[False, True])
def default_persona(source, request):
    if request.param:
        value = {"kind": "owner_default", "locator": str(source.default_path)}
        authorization, context = grant(source, "set_source", value)
        source.set_source(value["kind"], value["locator"], authorization=authorization, context=context)
    repository = PersonaRepository(owner_home=source.owner_home, agents_path=source.default_path,
                                   user_path=source.owner_home / "USER.md", soul_path=source.owner_home / "SOUL.md")
    repository.agents_source = source
    return source, repository


def test_final_persona_commit_rechecks_body_after_version_snapshot(default_persona, monkeypatch):
    source, repository = default_persona
    external = b"# external user edit\r\n- KEEP_USER_EDIT\r\n"
    before = source.default_path.read_bytes()
    inode = source.default_path.stat().st_ino
    original = repository._write_version_snapshot
    def snapshot_then_edit(*args, **kwargs):
        result = original(*args, **kwargs)
        source.default_path.write_bytes(external)
        return result
    monkeypatch.setattr(repository, "_write_version_snapshot", snapshot_then_edit)
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_CONTENT_MISMATCH"):
        repository.mutate(PersonaMutationRequest("agents", "add", content="MUST_NOT_COMMIT"))
    assert source.default_path.read_bytes() == external
    assert source.default_path.stat().st_ino == inode
    assert repository.history("agents") == []
    _assert_only_preimage(source, before)


def test_failed_ledger_rollback_preserves_later_external_edit(default_persona, monkeypatch):
    from agent_py_agent.agent.capability import persona_repository as module
    source, repository = default_persona
    external = b"# later user edit\r\n- DO_NOT_ROLL_BACK_USER\r\n"
    before = source.default_path.read_bytes()
    inode = source.default_path.stat().st_ino
    def edit_then_fail(*args, **kwargs):
        source.default_path.write_bytes(external)
        raise RuntimeError("injected ledger failure after user edit")
    monkeypatch.setattr(module, "append_private_jsonl_records", edit_then_fail)
    with pytest.raises((AgentsContractError, RuntimeError)):
        repository.mutate(PersonaMutationRequest("agents", "add", content="TRANSACTION_BODY"))
    assert source.default_path.read_bytes() == external, "rollback只能恢复仍由本事务写出的正文，不得覆盖用户修改"
    assert source.default_path.stat().st_ino == inode
    assert repository.history("agents") == []
    _assert_only_preimage(source, before)


# 函数用途: 持久前像合同——失败事务只留一份本次真实原文，不留任何版本快照或其它正文文件。
def _assert_only_preimage(source, before):
    retained = list((source.owner_home / "persona").rglob("*.md"))
    assert [path.parent.name for path in retained] == ["preimages"]
    assert retained[0].read_bytes() == before
