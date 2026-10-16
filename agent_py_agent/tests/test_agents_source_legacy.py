"""显式来源不能经旧Persona默认路径绕过来源授权或修改无关原件。"""
import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
    PersonaRepositoryError,
)
from agent_py_agent.tests.test_agents_source_repository import choose
from agent_py_agent.tests.test_agents_source_repository import source as source


@pytest.fixture
def personas(source):
    home = source.owner_home
    repository = PersonaRepository(owner_home=home, soul_path=home / "SOUL.md",
                                   user_path=home / "USER.md", agents_path=source.default_path)
    repository.agents_source = source
    path = home / "explicit-rules.data"
    path.write_text("# 本人指定来源\n- 保留原件。\n")
    choose(source, path)
    return repository, source, path


@pytest.mark.parametrize("operation", ["path_for", "list_entries", "current_sha256", "history", "mutate"])
def test_legacy_default_path_cannot_read_or_write_when_source_is_explicit(personas, operation):
    repository, source, path = personas
    before = source.default_path.read_bytes(), path.read_bytes(), source.config_path.read_bytes()
    with pytest.raises(PersonaRepositoryError, match="AGENTS_SOURCE_NATIVE_API_REQUIRED"):
        if operation == "mutate":
            repository.mutate(PersonaMutationRequest("agents", "add", content="绝不写错默认文件"))
        else:
            getattr(repository, operation)("agents")
    assert (source.default_path.read_bytes(), path.read_bytes(), source.config_path.read_bytes()) == before
    assert repository.load("agents").content.startswith("# 本人指定来源")
