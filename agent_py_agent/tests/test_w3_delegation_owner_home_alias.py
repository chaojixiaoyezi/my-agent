"""create_subagents write scopes accept the ~/.my-agent/owner display alias the model is shown.

Live clone (2026-11-17): the model reused '~/.my-agent/owner/tasks/<date>/<request>/output' from its
own context in create_subagents; the upper-bound check expanded '~' to the host HOME instead of the
owner home and refused it as DELEGATION_SCOPE_EXCEEDS_PARENT.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration import create_constraints, create_policy
from agent_py_agent.agent.agent_core.orchestration.delegation_constraints import (
    DelegationScopeExceedsParentError,
)
from agent_py_agent.agent.tooling.runtime_boundary import resolve_owner_home_alias


@pytest.fixture
def owner(tmp_path, monkeypatch):
    home = tmp_path / "home" / "owners" / "local" / "main"
    (home / "tasks").mkdir(parents=True)
    monkeypatch.setattr(create_constraints, "delegated_product_write_roots", lambda agent: (str(home),))
    return home


def _agent(owner_home):
    return SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home)),
                           _current_run_params=None, subagents=None, tools=None)


def _bind(agent, constraints):
    attrs: dict[str, object] = {}
    create_policy._bind_delegation_constraints(attrs, {"delegation_constraints": constraints}, agent)
    return attrs["delegation_constraints"]


def test_alias_write_paths_resolve_to_the_owner_home(owner):
    bound = _bind(_agent(owner), {"exact_write_set": ["~/.my-agent/owner/tasks/r1/output/report.md"],
                                  "allowed_output_dirs": ["~/.my-agent/owner/tasks/r1/output"]})
    assert bound["exact_write_set"] == [str((owner / "tasks/r1/output/report.md").resolve())]
    assert bound["allowed_output_dirs"] == [str((owner / "tasks/r1/output").resolve())]


@pytest.mark.parametrize("path", ["~/.my-agent/owner/../escape/out", "/definitely/outside/out"])
def test_paths_outside_the_parent_roots_are_still_refused(owner, path):
    with pytest.raises(DelegationScopeExceedsParentError):
        _bind(_agent(owner), {"exact_write_set": [path]})


def test_alias_helper_only_maps_the_alias_prefix(owner):
    assert resolve_owner_home_alias("~/.my-agent/owner", str(owner)) == str(owner.resolve())
    assert resolve_owner_home_alias("~/.my-agent/ownerx/a", str(owner)) == ""
    assert resolve_owner_home_alias("~/.my-agent/owner/a", "") == ""
    assert resolve_owner_home_alias("/abs/path", str(owner)) == ""
