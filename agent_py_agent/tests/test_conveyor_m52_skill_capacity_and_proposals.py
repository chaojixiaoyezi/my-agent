import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_learning_proposals as proposals
from agent_py_agent.agent.capability.skill_learning_protection import learning_protection_for_agent
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision


def _host(tmp_path, jobs):
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=tmp_path, owner_skill_learning_dir=tmp_path / "learning"),
        conversation_store=SimpleNamespace(tasks=SimpleNamespace(unsettled_report=lambda: ([], []))),
        scheduler_repository=SimpleNamespace(list_jobs=lambda **_: (jobs, [])),
        subagents=SimpleNamespace(list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])),
    )


def test_protection_reaches_1500_sources(tmp_path):
    jobs = [{"allowed_skills": ["owner:last"]} if number == 1499 else {} for number in range(1500)]
    first = _host(tmp_path, jobs)
    assert not learning_protection_for_agent(first)["protection_complete"]
    store = SkillLearningStore.for_home(first.home_paths)
    with store.state_lock():
        registry = store.load_registry()
        saved = registry.protection_scan
        assert saved["generation"] and saved["after"]["scheduler"] > 0
        store.save_registry(replace(registry, daily_calls=1))
    for _ in range(20):
        result = learning_protection_for_agent(_host(tmp_path, jobs))
        if result["protection_complete"]:
            break
    assert result["protection_complete"]
    assert result["protected_ids"] == ("owner:last",)
    assert store.load_registry().daily_calls == 1


@pytest.fixture
def proposal_store(tmp_path):
    store = SkillLearningStore(tmp_path / "learning", tmp_path / "skills")
    store.directory.mkdir()
    row = {
        "schema_version": "skill-learning-proposal.v1", "status": "pending", "revision": 1,
        "request": {"request_key": "sample"}, "decision": {"name": "sample"},
        "expected_revision": 0, "expected_sha256": "",
    }
    proposal_id = row["proposal_id"] = proposals._identity_id(row)
    proposals.write_text_atomic_beneath(store.directory, ("proposals", proposal_id + ".json"), json.dumps(row))
    bad_name = "learning-proposal-" + "a" * 32 + ".json"
    proposals.write_text_atomic_beneath(
        store.directory, ("proposals_overflow", bad_name), "{",
    )
    return store, row, bad_name


def test_exact_read_does_not_enumerate_proposal_directories(proposal_store, monkeypatch):
    store, row, _ = proposal_store
    proposal_id = row["proposal_id"]
    reads = []
    original = proposals._read_proposal

    def read(store, folder, requested_id):
        reads.append((folder, requested_id))
        return original(store, folder, requested_id)

    monkeypatch.setattr(proposals, "_read_proposal", read)
    assert proposals.learning_proposal(store, proposal_id) == row
    assert sorted(reads) == [("proposals", proposal_id), ("proposals_overflow", proposal_id)]


def test_proposal_pages_report_the_unreadable_row_and_keep_exact_resolution(proposal_store):
    store, row, bad_name = proposal_store
    found, errors, after = [], [], {}
    while after is not None:
        page, diagnostics, after = proposals.learning_proposal_page(store, after=after, limit=1)
        found.extend(page)
        errors.extend(diagnostics)
    assert found == [row]
    assert [(error.folder, error.file_name, error.error) for error in errors] == [
        ("proposals_overflow", bad_name, "JSONDecodeError"),
    ]
    resolution = proposals.resolve_learning_proposal(store, row["proposal_id"], 1, decision="reject")
    assert (resolution["status"], resolution["revision"]) == ("rejected", 2)
    assert (store.directory / "proposals_overflow" / bad_name).read_text() == "{"


def test_displacement_rechecks_reference_generation(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_publish

    jobs = []
    agent = _host(tmp_path, jobs)
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store, PublishRequest(decision("existing-method"), max_skills=1))
    previous = store.skill_path("existing-method").read_bytes()
    original = skill_learning_publish._registered_publication

    def introduce_reference(*args):
        registration = original(*args)
        jobs.append({"allowed_skills": ["owner:existing-method"]})
        return registration

    monkeypatch.setattr(skill_learning_publish, "_registered_publication", introduce_reference)
    with pytest.raises(SkillLearningGateError) as error:
        publish_learned_skill(store, PublishRequest(
            decision("new-method"), max_skills=1,
            protection_provider=lambda: learning_protection_for_agent(agent),
        ))
    assert error.value.detail["reason"] == "protection_generation_changed"
    assert store.skill_path("existing-method").read_bytes() == previous
    assert set(store.load_registry().skills) == {"existing-method"}
    assert not store.skill_path("new-method").exists()
