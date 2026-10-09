from __future__ import annotations

"""AGENTS.md 清理迁移：混合文件计划、执行、幂等、快照恢复与故障收敛（2026-10-08 第 0 期）。

口径：只移出「仍在本文件里且来源为 memory-promotion」的条目；agent_tool、system_baseline
（基线快照）与任何其它来源记录一律保护；执行先快照后删除，删除走 PersonaRepository 的
CAS 批量通道；空跑只给计数和 entry_id，不打印正文。
"""

import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaBatchMutationRequest,
    PersonaMutationRequest,
    PersonaRepository,
    persona_entry_id,
)
from agent_py_agent.agent.memory_store.agents_migration import (
    AgentsCleanupRequest,
    apply_agents_cleanup,
    plan_agents_cleanup,
)
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService

_ENTRY_ONE = "自动条目一。"
_ENTRY_TWO_OLD = "自动条目二（旧）。"
_ENTRY_TWO_NEW = "自动条目二（新）。"
_TOOL_ENTRY = "工具写入的条目。"
_BASELINE_ENTRY = "基线条目：只读保护。"


# 函数用途: 构造隔离 owner：基线 AGENTS.md、PersonaRepository、候选账本与 home 路径包。
def _env(tmp_path: Path):
    owner = tmp_path / "owner"
    owner.mkdir()
    for name in ("SOUL.md", "USER.md"):
        (owner / name).write_text(f"# {name}\n", encoding="utf-8")
    (owner / "AGENTS.md").write_text(f"# AGENTS\n- {_BASELINE_ENTRY}\n", encoding="utf-8")
    candidates = CandidateService(owner / "memory" / "candidates.jsonl")
    repo = PersonaRepository(
        owner_home=owner,
        soul_path=owner / "SOUL.md",
        user_path=owner / "USER.md",
        agents_path=owner / "AGENTS.md",
    )
    home = SimpleNamespace(
        owner_home_dir=owner,
        owner_agents_md=owner / "AGENTS.md",
        owner_memory_candidates_jsonl=owner / "memory" / "candidates.jsonl",
        owner_memory_policy_json=owner / "memory_policy.json",
    )
    return home, repo, candidates


# 函数用途: 观察一条真实候选（供归档出处使用）。
def _candidate(candidates, content, index):
    ref = {
        "message_id": f"message-{index}",
        "thread_id": f"thread-{index}",
        "role": "user",
        "content_hash": "sha256:x",
        "quote": content,
    }
    return candidates.observe(
        CandidateObservation(
            candidate_type="working_agreement",
            content=content,
            subject_key=f"persona.agents.rule.{index}",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            source_message_refs=(ref,),
            promotion_target="agents",
            promotion_mode="auto_eligible",
        )
    )


# 函数用途: 用一条 promotion 来源写入 AGENTS 条目。
def _promotion_add(repo, content, candidate_id):
    return repo.mutate(
        PersonaMutationRequest(
            target="agents",
            action="add",
            content=content,
            source=f"memory-promotion:memory-promotion-service:{candidate_id}",
        )
    )


# 函数用途: 造混合文件：基线 + agent_tool + 两条自动条目（其中一条被 replace 过）。
def _mixed(tmp_path: Path):
    home, repo, candidates = _env(tmp_path)
    first = _candidate(candidates, _ENTRY_ONE, 1)
    second = _candidate(candidates, _ENTRY_TWO_OLD, 2)
    repo.mutate(
        PersonaMutationRequest(target="agents", action="add", content=_TOOL_ENTRY, source="agent_tool")
    )
    _promotion_add(repo, _ENTRY_ONE, first.candidate_id)
    _promotion_add(repo, _ENTRY_TWO_OLD, second.candidate_id)
    repo.mutate(
        PersonaMutationRequest(
            target="agents",
            action="replace",
            entry_id=persona_entry_id("agents", _ENTRY_TWO_OLD),
            content=_ENTRY_TWO_NEW,
            source=f"memory-promotion:memory-promotion-service:{second.candidate_id}",
        )
    )
    return home, repo, candidates, first, second


# 函数用途: 读 AGENTS.md 当前文本。
def _agents_text(repo) -> str:
    return repo.path_for("agents").read_text(encoding="utf-8")


def test_plan_counts_mixed_entries(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)

    plan = plan_agents_cleanup(persona=repo)
    payload = plan.to_dict()

    assert payload["entries_total"] == 4
    assert payload["promotion_entries"] == 2
    assert payload["protected_entries"] == 1
    assert payload["removable_count"] == 2
    assert set(payload["removable_entry_ids"]) == {
        persona_entry_id("agents", _ENTRY_ONE),
        persona_entry_id("agents", _ENTRY_TWO_NEW),
    }
    versions = {item["entry_id"]: item["version"] for item in payload["removable"]}
    assert versions[persona_entry_id("agents", _ENTRY_TWO_NEW)] > versions[
        persona_entry_id("agents", _ENTRY_ONE)
    ]
    assert {item["candidate_id"] for item in payload["removable"]} == {
        first.candidate_id,
        second.candidate_id,
    }


def test_dry_run_payload_has_no_entry_content(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)

    payload = plan_agents_cleanup(persona=repo).to_dict()
    text = json.dumps(payload, ensure_ascii=False)

    assert all("content" not in item for item in payload["removable"])
    for entry in (_ENTRY_ONE, _ENTRY_TWO_NEW, _TOOL_ENTRY, _BASELINE_ENTRY):
        assert entry not in text


def test_entry_with_non_promotion_record_is_protected(tmp_path: Path):
    # 同一条目既有 promotion 记录、又有 agent_tool 记录（用户写回同内容）时必须保护。
    home, repo, candidates = _env(tmp_path)
    candidate = _candidate(candidates, "双记录条目。", 1)
    _promotion_add(repo, "双记录条目。", candidate.candidate_id)
    entry_id = persona_entry_id("agents", "双记录条目。")
    repo.mutate(
        PersonaMutationRequest(
            target="agents",
            action="replace",
            entry_id=entry_id,
            content="双记录条目。",
            source="agent_tool",
        )
    )

    plan = plan_agents_cleanup(persona=repo)
    assert plan.removable == ()
    report = apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(migration_id="m2"),
    )

    assert report["removed_count"] == 0
    assert "双记录条目。" in repo.path_for("agents").read_text(encoding="utf-8")


def test_apply_removes_only_promotion_entries(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)

    report = apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(migration_id="m1", conversations=("chat-excluded",)),
    )

    assert report["removable_count"] == 2
    assert report["removed_count"] == 2
    remaining = repo.list_entries("agents")["entries"]
    assert {item["content"] for item in remaining} == {_BASELINE_ENTRY, _TOOL_ENTRY}
    archive_path = Path(str(report["archive_ref"]))
    rows = [json.loads(line) for line in archive_path.read_text(encoding="utf-8").splitlines()]
    assert {row["entry_id"] for row in rows} == {
        persona_entry_id("agents", _ENTRY_ONE),
        persona_entry_id("agents", _ENTRY_TWO_NEW),
    }
    by_id = {row["entry_id"]: row for row in rows}
    assert by_id[persona_entry_id("agents", _ENTRY_ONE)]["candidate_id"] == first.candidate_id
    assert by_id[persona_entry_id("agents", _ENTRY_ONE)]["content"] == _ENTRY_ONE
    assert by_id[persona_entry_id("agents", _ENTRY_ONE)]["source_message_refs"][0][
        "message_id"
    ] == "message-1"
    assert by_id[persona_entry_id("agents", _ENTRY_TWO_NEW)]["candidate_id"] == second.candidate_id
    record = json.loads((Path(str(report["snapshot_dir"])) / "record.json").read_text(encoding="utf-8"))
    assert record["migration_id"] == "m1"
    assert record["removed_count"] == 2
    assert record["conversations"] == ["chat-excluded"]
    versions = repo.history("agents")
    assert any(
        row.get("source") == "migration:m1" and row.get("action") == "batch" for row in versions
    )
    policy = json.loads((home.owner_memory_policy_json).read_text(encoding="utf-8"))
    assert policy["learning_exclusions"][0]["channel_conversation_id"] == "chat-excluded"


def test_apply_is_idempotent_on_rerun(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)
    apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(migration_id="m1"),
    )
    before = _agents_text(repo)

    plan = plan_agents_cleanup(persona=repo)
    report = apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(migration_id="m1"),
    )

    assert plan.removable == ()
    assert report["removed_count"] == 0
    assert "nothing_to_remove" in report["notes"]
    assert _agents_text(repo) == before
    snapshot_meta = json.loads(
        (Path(str(report["snapshot_dir"])) / "snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot_meta["files"]["agents"]["sha256"]


def test_snapshot_restores_original_bytes(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)
    before = repo.path_for("agents").read_bytes()

    report = apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(migration_id="m1"),
    )

    snapshot_agents = Path(str(report["snapshot_dir"])) / "snapshot" / "agents"
    assert snapshot_agents.read_bytes() == before
    shutil.copyfile(snapshot_agents, repo.path_for("agents"))
    assert repo.path_for("agents").read_bytes() == before


def test_partial_failure_recovers_on_rerun(tmp_path: Path, monkeypatch):
    home, repo, candidates = _env(tmp_path)
    for index in range(1, 41):
        _promotion_add(repo, f"自动条目 {index}。", f"scale-candidate-{index}")
    original = PersonaRepository.mutate_batch
    calls = {"count": 0}

    def flaky(self, request):
        if calls["count"] >= 1:
            raise RuntimeError("injected batch failure")
        calls["count"] += 1
        return original(self, request)

    monkeypatch.setattr(PersonaRepository, "mutate_batch", flaky)
    with pytest.raises(RuntimeError, match="injected batch failure"):
        apply_agents_cleanup(
            home_paths=home,
            persona=repo,
            candidate_service=candidates,
            request=AgentsCleanupRequest(migration_id="m1"),
        )

    directory = home.owner_home_dir / "persona" / "migrations" / "agents-migration-m1"
    assert (directory / "snapshot.json").exists()
    # 40 条自动条目按每批 32 条删除：第一批已删，剩 8 条自动 + 1 条基线。
    assert len(repo.list_entries("agents")["entries"]) == 9
    monkeypatch.setattr(PersonaRepository, "mutate_batch", original)

    report = apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(migration_id="m1"),
    )

    assert report["removed_count"] == 8
    remaining = repo.list_entries("agents")["entries"]
    assert {item["content"] for item in remaining} == {_BASELINE_ENTRY}


def test_same_scale_cleanup_duration(tmp_path: Path):
    home, repo, candidates = _env(tmp_path)
    for index in range(1, 476):
        _promotion_add(repo, f"自动条目 {index}。", f"scale-candidate-{index}")
    operations = tuple(
        PersonaMutationRequest(target="agents", action="add", content=f"工具条目 {index}。")
        for index in range(1, 14)
    )
    repo.mutate_batch(
        PersonaBatchMutationRequest(target="agents", operations=operations, source="agent_tool")
    )
    assert len(repo.list_entries("agents")["entries"]) == 489

    start = time.perf_counter()
    report = apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(migration_id="scale"),
    )
    elapsed = time.perf_counter() - start
    print(f"\nSAME_SCALE_CLEANUP_ELAPSED={elapsed:.3f}s", flush=True)

    assert report["removable_count"] == 475
    assert report["removed_count"] == 475
    remaining = repo.list_entries("agents")["entries"]
    assert len(remaining) == 14
    assert all("自动条目" not in item["content"] for item in remaining)
