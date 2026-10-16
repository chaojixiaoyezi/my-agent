"""A路仓库硬帽反例：最终写入层必须先挡住，不靠工具展示层。"""
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaBatchMutationRequest,
    PersonaMutationRequest,
    PersonaRepository,
)


def make_repository(tmp_path: Path) -> PersonaRepository:
    home = tmp_path / "owner"
    home.mkdir()
    for name in ("AGENTS.md", "USER.md", "SOUL.md"):
        (home / name).write_text("# " + name + "\n", encoding="utf-8")
    return PersonaRepository(owner_home=home, agents_path=home / "AGENTS.md",
                             user_path=home / "USER.md", soul_path=home / "SOUL.md")


@pytest.mark.parametrize("batch", [False, True])
def test_agents_write_hard_cap_is_in_final_repository_layer(tmp_path, batch):
    repository = make_repository(tmp_path)
    original = repository.path_for("agents").read_bytes()
    request = PersonaMutationRequest(target="agents", action="add", content="常" * 20_000)
    with pytest.raises(ValueError, match="AGENTS_WRITE_LIMIT"):
        if batch:
            repository.mutate_batch(PersonaBatchMutationRequest(target="agents", operations=(request,)))
        else:
            repository.mutate(request)
    assert repository.path_for("agents").read_bytes() == original
    assert not repository.versions_path.exists()
    assert not repository.backups_dir.exists()


def test_agents_existing_over_limit_can_only_shrink(tmp_path):
    repository = make_repository(tmp_path)
    path = repository.path_for("agents")
    path.write_text("# AGENTS\n- " + "甲" * 15_000 + "\n- " + "乙" * 15_000 + "\n", encoding="utf-8")
    rows = repository.list_entries("agents")["entries"]
    result = repository.mutate(PersonaMutationRequest(target="agents", action="replace",
                                entry_id=rows[0]["entry_id"], content="甲" * 14_000))
    assert result["changed"] is True
    assert 20_000 < len(path.read_text()) < 30_018
    previous = path.read_bytes()
    with pytest.raises(ValueError, match="AGENTS_WRITE_LIMIT"):
        repository.mutate(PersonaMutationRequest(target="agents", action="add", content="不许长大"))
    assert path.read_bytes() == previous


def test_agents_rollback_may_not_restore_larger_over_limit_file(tmp_path):
    repository = make_repository(tmp_path)
    path = repository.path_for("agents")
    path.write_text("# AGENTS\n- " + "甲" * 22_000 + "\n", encoding="utf-8")
    row = repository.list_entries("agents")["entries"][0]
    repository.mutate(PersonaMutationRequest(target="agents", action="remove", entry_id=row["entry_id"]))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="AGENTS_WRITE_LIMIT"):
        repository.mutate(PersonaMutationRequest(target="agents", action="rollback", rollback_version=0))
    assert path.read_bytes() == before


def test_user_budget_is_not_mistaken_for_agents_write_cap(tmp_path):
    repository = make_repository(tmp_path)
    result = repository.mutate(PersonaMutationRequest(target="user", action="add", content="常" * 20_000))
    assert result["changed"] is True
