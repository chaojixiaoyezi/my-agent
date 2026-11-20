"""Dream 迁移召回和会话回收的文件代次回归。"""
from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256

import pytest

from agent_py_agent.agent.conversation.store_messages import MessageStore
from agent_py_agent.agent.memory_store import recall, retention_apply
from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex
from agent_py_agent.agent.memory_store.recall import (
    runtime_long_term_scope,
    runtime_recall_predicate,
)
from agent_py_agent.agent.memory_store.retention_scan import read_live_extraction_switch
from agent_py_agent.agent.memory_store.user_profile_dream import run_user_profile_dream_if_due
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.user_space.home_doctor import build_home_doctor_report
from agent_py_agent.tests.test_mem_wave1_s4_bounded_reads_retention import (
    _NOW,
    _ROWS,
    _old_thread,
    _plan_all,
    _retention_owner,
    _write_curator_state,
)
from agent_py_agent.tests.test_nw3_w9_user_profile_dream_and_agents_receipt import (
    _make_owner_home,
    _make_repo,
    persona_entry_id,
)


def _recall(store, query, thread_id="thread-a"):
    scope = runtime_long_term_scope(store, task_attributes={"session_id": thread_id}, purpose="auto_inject")
    return store.search_scoped(query, 5, runtime_recall_predicate(scope, query))


def test_dream_relocated_fact_recalls_in_each_owner_thread(tmp_path):
    owner_home = _make_owner_home(tmp_path)
    repo = _make_repo(owner_home)
    store = JsonlMemory(owner_home / "memory" / "long_term" / "memory.jsonl")
    body = "[personal:personal] 2026-11-15 sprint log: Cedar Observatory calibration"
    (owner_home / "USER.md").write_text(f"# USER.md\n- {body}\n- 偏好：简洁中文总结\n", encoding="utf-8")

    report = run_user_profile_dream_if_due(
        owner_home=owner_home, persona_repository=repo, long_term_store=store,
        config=AgentConfig(user_profile_dream_entry_threshold=1), now=_NOW,
    )

    assert report["status"] == "consolidated", report
    assert "Cedar Observatory" not in repo.load("user").content
    for thread in ("thread-a", "thread-b"):
        [record] = _recall(store, "Cedar Observatory", thread)
        assert (record.attributes["scope_type"], record.attributes["scope_key"]) == ("personal", "personal")
        assert record.attributes["relocated_entry_id"] == persona_entry_id("user", body)
        assert record.attributes["relocated_body_sha256"] == sha256(body.encode()).hexdigest()
    other = JsonlMemory(tmp_path / "other-owner" / "memory" / "long_term" / "memory.jsonl")
    assert _recall(other, "Cedar Observatory") == []


@pytest.mark.parametrize("tiered", [False, True])
def test_legacy_relocation_recalls_without_rewriting_the_source(tmp_path, monkeypatch, tiered):
    store = JsonlMemory(_make_owner_home(tmp_path) / "memory" / "long_term" / "memory.jsonl")
    relocated = {"relocated_from": "USER.md#old"}
    # 写出未归一的旧版记录，以同时验证原始文件和冷热分片。
    with monkeypatch.context() as patch:
        patch.setattr(recall, "normalize_dream_relocation", lambda record: record)
        legacy = store.add("user", "Cedar Observatory observation", kind="fact",
                           source="user-profile-dream", attributes=relocated)
        store.add("user", "Cedar Observatory different source", kind="fact", source="other", attributes=relocated)
        store.add("user", "Cedar Observatory different file", kind="fact", source="user-profile-dream",
                  attributes={"relocated_from": "AGENTS.md#old"})
        store.add("user", "Cedar Observatory project fact", kind="fact", source="user-profile-dream",
                  attributes={**relocated, "scope_type": "project", "scope_key": "project:other"})
        if tiered:
            store.migrate_tiers()
    authority = MemoryGenerationStore(store.path)
    root = authority.manifest()
    sources = [store.path] if root is None else [store.path.parent / part["path"] for part in root["segments"]]
    original = {path: path.read_bytes() for path in sources}
    [old_payload] = [row for raw in original.values()
                     for line in raw.splitlines() if (row := json.loads(line))["entry_id"] == legacy.entry_id]
    assert "scope_type" not in old_payload["attributes"]
    if tiered:
        assert store.rebuild_read_index()["status"] == "complete"
        index = MemoryReadIndex(store.path)
        candidates, _ = index.query("Cedar Observatory")
        [normalized] = index.resolve([row for row in candidates if row["id"] == legacy.entry_id])
        assert (normalized.attributes["scope_type"], normalized.attributes["scope_key"]) == ("personal", "personal")
    assert [row.entry_id for row in _recall(store, "Cedar Observatory")] == [legacy.entry_id]
    assert authority.manifest() == root
    assert all(path.read_bytes() == data for path, data in original.items())

    store.add("user", "A later unrelated fact", kind="fact")
    root = authority.manifest()
    if root is None:
        rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    else:
        rows = [row for segment in root["segments"] for row in authority.segment_rows(segment)]
    [persisted] = [row for row in rows if row["entry_id"] == legacy.entry_id]
    assert persisted["attributes"]["scope_type"] == "personal"
    assert persisted["version"] == legacy.version


def _write_switch(config_path, enabled):
    incoming = config_path.with_suffix(".next")
    incoming.write_text(
        f"memory_curator_model_profile: aux-profile\nmemory_curator_enabled: {str(enabled).lower()}\n",
        encoding="utf-8",
    )
    incoming.replace(config_path)


def test_external_extraction_toggle_between_plan_and_move_defers(tmp_path):
    home, service = _retention_owner(tmp_path, profile="aux-profile")
    config_path = tmp_path / "agent.yaml"
    _write_switch(config_path, False)
    config = load_config(config_path)
    service.config = config
    thread, _ = _old_thread(home, _ROWS)
    report, [action], _ = _plan_all(service)
    assert action.record_id == thread.thread_id and action.path.is_file()
    assert action.curator_switch.state == "disabled"

    _write_switch(config_path, True)
    assert config.memory_curator_enabled is False

    applied = retention_apply.execute_retention_plan(
        home=home, candidates=service.candidates, plan=replace(report, actions=(action,)), now=_NOW, config=config,
    )
    assert [row.status for row in applied.actions] == ["state_changed"]
    assert action.path.is_file()
    assert not action.destination.exists()
    current = read_live_extraction_switch(home, config)
    assert current.state == "enabled" and current.source_generation != action.curator_switch.source_generation


@pytest.mark.parametrize("failure", ["missing", "invalid", "same_values_new_generation"])
def test_retention_rejects_unreadable_or_replaced_generation(tmp_path, failure):
    home, service = _retention_owner(tmp_path, profile="aux-profile")
    path = tmp_path / "agent.yaml"
    _write_switch(path, False)
    service.config = load_config(path)
    thread, rows = _old_thread(home, _ROWS)
    _write_curator_state(home, {thread.thread_id: rows[-1].message_id})
    report, [action], _ = _plan_all(service)
    if failure == "missing":
        path.unlink()
    elif failure == "invalid":
        path.write_text("memory_curator_enabled: [unterminated\n", encoding="utf-8")
    else:
        _write_switch(path, False)
    result = read_live_extraction_switch(home, service.config)
    if failure == "same_values_new_generation":
        assert result.state == "disabled" and result.source_generation != action.curator_switch.source_generation
    else:
        assert result.state == "unknown"
        newer, conversations, _ = _plan_all(service)
        assert conversations == [] and newer.curator_switch.state == "unknown"
    applied = retention_apply.execute_retention_plan(
        home=home, candidates=service.candidates, plan=replace(report, actions=(action,)),
        now=_NOW, config=service.config,
    )
    assert [item.status for item in applied.actions] == ["state_changed"]
    assert action.path.is_file() and not action.destination.exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_stable_generation_moves_fully_curated_thread(tmp_path, monkeypatch, enabled):
    home, service = _retention_owner(tmp_path, profile="aux-profile")
    path = tmp_path / "agent.yaml"
    _write_switch(path, enabled)
    service.config = load_config(path)
    thread, rows = _old_thread(home, _ROWS)
    end = rows[-1].message_id
    _write_curator_state(home, {thread.thread_id: end})
    report, [action], _ = _plan_all(service)
    probes = []
    original = MessageStore.after_slice_report

    def observed(messages, thread_id, *, after_message_id, limit, budget):
        probes.append((thread_id, after_message_id))
        return original(messages, thread_id, after_message_id=after_message_id, limit=limit, budget=budget)

    monkeypatch.setattr(MessageStore, "after_slice_report", observed)
    applied = retention_apply.execute_retention_plan(
        home=home, candidates=service.candidates, plan=replace(report, actions=(action,)),
        now=_NOW, config=service.config,
    )
    assert [item.status for item in applied.actions] == ["trashed"]
    assert ((thread.thread_id, end) in probes) is enabled
    assert not action.path.exists() and action.destination.is_dir()


def test_home_doctor_uses_live_config_for_its_read_only_plan(tmp_path):
    home, _service = _retention_owner(tmp_path, profile="aux-profile")
    path = tmp_path / "agent.yaml"
    _write_switch(path, False)
    config = load_config(path)
    thread, _ = _old_thread(home, _ROWS)
    _write_switch(path, True)
    report = build_home_doctor_report(home, config=config)["retention"]
    assert report["curator_switch"]["state"] == "enabled"
    assert not any(row["record_id"] == thread.thread_id for row in report["actions"] if row["category"] == "conversation")
