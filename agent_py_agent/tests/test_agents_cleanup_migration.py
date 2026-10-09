from __future__ import annotations

"""AGENTS.md 清理迁移（设计 v2 §3.6）：归属判定、计划摘要、事务恢复与快照失效。

口径：归属按「当前条目最后一次有效写入的来源」判定——「自动添加→用户删除→用户重新添加
相同文字」判为用户条目；无事件、最后是 remove 或最后写入之后有 rollback 一律歧义保留。
apply 绑定 dry-run 计划摘要，中途变化拒绝；崩溃可幂等续作；先快照（含 backups）后归档再
删除；收尾让受影响线程的冻结人格快照受控失效。空跑只给计数和 entry_id，不打印正文。
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
    AgentsCleanupError,
    AgentsCleanupRequest,
    apply_agents_cleanup,
    plan_agents_cleanup,
)
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.prompting_parts.thread_persona import (
    RenderedPersonaSections,
    ThreadPersonaScope,
    freeze_thread_persona,
)

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


# 函数用途: 计算当前 dry-run 计划摘要，供 apply 携带。
def _digest(repo) -> str:
    return plan_agents_cleanup(persona=repo).plan_digest


# 函数用途: 用当前计划摘要执行一次迁移（测试默认路径）。
def _apply(home, repo, candidates, **kwargs):
    migration_id = str(kwargs.pop("migration_id"))
    return apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(
            migration_id=migration_id,
            plan_digest=_digest(repo),
            **kwargs,
        ),
    )


def test_plan_counts_mixed_entries(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)

    plan = plan_agents_cleanup(persona=repo)
    payload = plan.to_dict()

    assert payload["entries_total"] == 4
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
    assert payload["plan_digest"]


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
    report = _apply(home, repo, candidates, migration_id="m2")

    assert report["removed_count"] == 0
    assert "双记录条目。" in _agents_text(repo)


def test_user_readded_same_text_is_kept(tmp_path: Path):
    # mc4 红测场景：自动添加 → 用户删除 → 用户重新添加相同文字，entry_id 相同但归属是用户。
    home, repo, candidates = _env(tmp_path)
    candidate = _candidate(candidates, "被用户接管的约定。", 1)
    entry_id = persona_entry_id("agents", "被用户接管的约定。")
    _promotion_add(repo, "被用户接管的约定。", candidate.candidate_id)
    repo.mutate(
        PersonaMutationRequest(
            target="agents", action="remove", entry_id=entry_id, source="agent_tool"
        )
    )
    repo.mutate(
        PersonaMutationRequest(
            target="agents",
            action="add",
            content="被用户接管的约定。",
            source="agent_tool",
        )
    )

    plan = plan_agents_cleanup(persona=repo)

    assert plan.removable == ()
    assert entry_id not in plan.ambiguous_entry_ids
    report = _apply(home, repo, candidates, migration_id="m-readd")
    assert report["removed_count"] == 0
    assert "被用户接管的约定。" in _agents_text(repo)


def test_hand_edited_entry_is_ambiguous_and_kept(tmp_path: Path):
    # 用户直接编辑文件加的行没有任何版本事件，归属不明 → 歧义保留并列出。
    home, repo, candidates = _env(tmp_path)
    _promotion_add(repo, "自动条目。", "hand-candidate-1")
    path = repo.path_for("agents")
    path.write_text(path.read_text(encoding="utf-8") + "- 手工加的条目。\n", encoding="utf-8")

    plan = plan_agents_cleanup(persona=repo)

    manual_id = persona_entry_id("agents", "手工加的条目。")
    assert manual_id in plan.ambiguous_entry_ids
    assert all(item["entry_id"] != manual_id for item in plan.removable)
    assert {item["entry_id"] for item in plan.removable} == {
        persona_entry_id("agents", "自动条目。")
    }


def test_entry_touched_by_rollback_is_ambiguous(tmp_path: Path):
    # rollback 晚于最后写入时，当前内容可能来自被恢复的旧版本 → 歧义保留。
    home, repo, candidates = _env(tmp_path)
    _promotion_add(repo, "自动条目 A。", "rollback-candidate-a")
    _promotion_add(repo, "自动条目 B。", "rollback-candidate-b")
    repo.mutate(
        PersonaMutationRequest(
            target="agents", action="rollback", rollback_version=1, source="agent_tool"
        )
    )

    plan = plan_agents_cleanup(persona=repo)

    entry_a = persona_entry_id("agents", "自动条目 A。")
    assert entry_a in plan.ambiguous_entry_ids
    assert plan.removable == ()
    report = _apply(home, repo, candidates, migration_id="m-rollback")
    assert report["removed_count"] == 0
    assert "自动条目 A。" in _agents_text(repo)


def test_apply_removes_only_promotion_entries(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)

    report = _apply(home, repo, candidates, migration_id="m1", conversations=("chat-excluded",))

    assert report["planned_count"] == 2
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
    assert record["status"] == "completed"
    assert record["removed_count"] == 2
    assert record["conversations"] == ["chat-excluded"]
    versions = repo.history("agents")
    assert any(
        row.get("source") == "migration:m1" and row.get("action") == "batch" for row in versions
    )
    policy = json.loads((home.owner_memory_policy_json).read_text(encoding="utf-8"))
    assert policy["learning_exclusions"][0]["channel_conversation_id"] == "chat-excluded"


def test_apply_requires_digest_and_rejects_stale_plan(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)

    with pytest.raises(AgentsCleanupError) as missing:
        apply_agents_cleanup(
            home_paths=home,
            persona=repo,
            candidate_service=candidates,
            request=AgentsCleanupRequest(migration_id="m1"),
        )
    assert missing.value.reason_code == "AGENTS_CLEANUP_PLAN_DIGEST_REQUIRED"

    digest = _digest(repo)
    repo.mutate(
        PersonaMutationRequest(
            target="agents", action="add", content="用户新加的条目。", source="agent_tool"
        )
    )
    with pytest.raises(AgentsCleanupError) as stale:
        apply_agents_cleanup(
            home_paths=home,
            persona=repo,
            candidate_service=candidates,
            request=AgentsCleanupRequest(migration_id="m1", plan_digest=digest),
        )
    assert stale.value.reason_code == "AGENTS_CLEANUP_PLAN_STALE"
    assert _ENTRY_ONE in _agents_text(repo)


def test_apply_is_idempotent_on_rerun(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)
    _apply(home, repo, candidates, migration_id="m1")
    before = _agents_text(repo)

    report = _apply(home, repo, candidates, migration_id="m1")

    assert report["already_completed"] is True
    assert report["removed_count"] == 2
    assert _agents_text(repo) == before
    snapshot_meta = json.loads(
        (Path(str(report["snapshot_dir"])) / "snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot_meta["files"]["agents.md"]["sha256"]


def test_snapshot_restores_original_bytes(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)
    before = repo.path_for("agents").read_bytes()

    report = _apply(home, repo, candidates, migration_id="m1")

    snapshot_dir = Path(str(report["snapshot_dir"])) / "snapshot"
    assert (snapshot_dir / "agents.md").read_bytes() == before
    assert (snapshot_dir / "versions.jsonl").exists()
    assert list((snapshot_dir / "backups").rglob("*.md"))
    shutil.copyfile(snapshot_dir / "agents.md", repo.path_for("agents"))
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
        _apply(home, repo, candidates, migration_id="m1")

    directory = home.owner_home_dir / "persona" / "migrations" / "agents-migration-m1"
    record = json.loads((directory / "record.json").read_text(encoding="utf-8"))
    assert record["status"] == "in_progress"
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
    assert report["status"] == "completed"
    remaining = repo.list_entries("agents")["entries"]
    assert {item["content"] for item in remaining} == {_BASELINE_ENTRY}
    archive_rows = [
        json.loads(line)
        for line in (directory / "archive.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(archive_rows) == 40
    final = json.loads((directory / "record.json").read_text(encoding="utf-8"))
    assert final["status"] == "completed"
    assert final["remaining_count"] == 0


def test_expire_thread_snapshots_after_cleanup(tmp_path: Path):
    home, repo, candidates = _env(tmp_path)
    _promotion_add(repo, "会被清掉的约定。", "expire-candidate-1")
    scope = ThreadPersonaScope(
        thread_id="thread-1",
        compact_generation=1,
        model_profile_id="model-a",
        protocol=("backend", "protocol"),
    )
    frozen = RenderedPersonaSections(
        sections={
            "agents": _agents_text(repo),
            "soul": "# SOUL.md\n",
            "user": "# USER.md\n",
            "diagnostics": "",
        },
        truncated_targets=frozenset(),
    )
    freeze_thread_persona(home.owner_home_dir, scope, frozen)
    snapshot_path = home.owner_home_dir / "persona" / "thread_prefix" / "thread-1.json"
    assert snapshot_path.exists()

    report = _apply(home, repo, candidates, migration_id="m1")

    assert report["expired_thread_snapshots"] == 1
    assert not snapshot_path.exists()
    # 下一回合重新冻结后，固定前缀不再出现旧条目（失效后按当前内容重建）。
    after = RenderedPersonaSections(
        sections={**frozen.sections, "agents": _agents_text(repo)},
        truncated_targets=frozenset(),
    )
    freeze_thread_persona(home.owner_home_dir, scope, after)
    saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert "会被清掉的约定。" not in saved["sections"]["agents"]


def test_archived_entries_leave_no_live_surfaces(tmp_path: Path):
    home, repo, candidates, first, second = _mixed(tmp_path)

    report = _apply(home, repo, candidates, migration_id="m1", conversations=("chat-1",))

    archive_path = Path(str(report["archive_ref"]))
    assert archive_path.exists()
    agents_text = _agents_text(repo)
    for entry in (_ENTRY_ONE, _ENTRY_TWO_NEW):
        assert entry not in agents_text
    stored = candidates.get(first.candidate_id)
    assert stored.status == "pending_review"
    policy = json.loads((home.owner_memory_policy_json).read_text(encoding="utf-8"))
    assert policy["learning_exclusions"][0]["channel_conversation_id"] == "chat-1"


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
    report = _apply(home, repo, candidates, migration_id="scale")
    elapsed = time.perf_counter() - start
    print(f"\nSAME_SCALE_CLEANUP_ELAPSED={elapsed:.3f}s", flush=True)

    assert report["planned_count"] == 475
    assert report["removed_count"] == 475
    remaining = repo.list_entries("agents")["entries"]
    assert len(remaining) == 14
    assert all("自动条目" not in item["content"] for item in remaining)
