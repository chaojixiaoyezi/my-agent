from __future__ import annotations

"""AGENTS.md 清理迁移（设计 v2 §3.6）：归属判定、计划摘要、事务恢复与快照失效。

口径：归属按「当前条目最后一次有效写入的来源」判定——「自动添加→用户删除→用户重新添加
相同文字」判为用户条目；无事件、最后是 remove 或最后写入之后有 rollback 一律歧义保留。
apply 绑定 dry-run 计划摘要，中途变化拒绝；崩溃可幂等续作；先快照（含 backups）后归档再
删除；收尾让受影响线程的冻结人格快照受控失效。空跑只给计数和 entry_id，不打印正文。
"""

import functools
import inspect
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import persona_repository as persona_repository_module
from agent_py_agent.agent.capability.persona_repository import (
    PersonaBatchMutationRequest,
    PersonaMutationRequest,
    PersonaRepository,
    persona_entry_id,
)
from agent_py_agent.agent.memory_store import agents_migration as migration_module
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
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.learning_exclusions import (
    curator_learning_exclusions_revision,
)
from agent_py_agent.agent.prompting_parts import builder as prompt_builder
from agent_py_agent.agent.prompting_parts.thread_persona import (
    RenderedPersonaSections,
    ThreadPersonaScope,
    expire_thread_personas_containing,
    freeze_thread_persona,
)
from agent_py_agent.agent.user_space.owner_policy import (
    LearningExclusionEntry,
    append_learning_exclusions,
    read_learning_exclusions_revision,
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


# LLM: 删除正式提交后报告累计量，恢复不得追加第二次删除或归档；故障仍发生在耐久进度回调前。
# 函数用途: 核对崩溃续作的实际累计删除数与幂等文件结果。
def test_partial_failure_recovers_on_rerun(tmp_path: Path, monkeypatch):
    home, repo, candidates = _env(tmp_path)
    for index in range(1, 41):
        _promotion_add(repo, f"自动条目 {index}。", f"scale-candidate-{index}")
    original = migration_module._remove_batch

    def flaky(*args):
        # 一次性删除已提交、进度记录尚未写入的崩溃窗口；不依赖已取消的第二批。
        original(*args)
        raise RuntimeError("injected batch failure")

    monkeypatch.setattr(migration_module, "_remove_batch", flaky)
    with pytest.raises(RuntimeError, match="injected batch failure"):
        _apply(home, repo, candidates, migration_id="m1")

    directory = home.owner_home_dir / "persona" / "migrations" / "agents-migration-m1"
    record = json.loads((directory / "record.json").read_text(encoding="utf-8"))
    assert record["status"] == "in_progress"
    # 全部自动条目已删，续作不能重删、重复归档或误碰基线。
    assert len(repo.list_entries("agents")["entries"]) == 1
    monkeypatch.setattr(migration_module, "_remove_batch", original)
    committed = repo.versions_path.read_bytes()
    archived = (directory / "archive.jsonl").read_bytes()

    report = apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(migration_id="m1"),
    )

    assert report["removed_count"] == 40  # 同迁移累计提交量，不是本次重试新删量。
    assert repo.versions_path.read_bytes() == committed
    assert (directory / "archive.jsonl").read_bytes() == archived
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


def test_learning_exclusions_revision_tracks_appends(tmp_path: Path):
    home, _repo, _candidates = _env(tmp_path)
    assert read_learning_exclusions_revision(home) == 0
    assert curator_learning_exclusions_revision(home) == 0

    assert (
        append_learning_exclusions(
            home, (LearningExclusionEntry(channel_conversation_id="chat-rev-1"),)
        )
        == 1
    )
    assert read_learning_exclusions_revision(home) == 1
    assert curator_learning_exclusions_revision(home) == 1

    # 重复追加同一条不新增，修订号不动（幂等追加不产生假代次）。
    assert (
        append_learning_exclusions(
            home, (LearningExclusionEntry(channel_conversation_id="chat-rev-1"),)
        )
        == 0
    )
    assert read_learning_exclusions_revision(home) == 1

    assert (
        append_learning_exclusions(
            home, (LearningExclusionEntry(channel_conversation_id="chat-rev-2"),)
        )
        == 1
    )
    assert curator_learning_exclusions_revision(home) == 2


_USER_WRITER_CODE = """
import json, sys
from pathlib import Path
from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
)
owner = Path(sys.argv[1])
repo = PersonaRepository(
    owner_home=owner,
    soul_path=owner / "SOUL.md",
    user_path=owner / "USER.md",
    agents_path=owner / "AGENTS.md",
)
result = repo.mutate(
    PersonaMutationRequest(
        target="agents", action="add", content=sys.argv[2], source="user_manual"
    )
)
print(json.dumps({"entry_id": result.get("entry_id"), "sha256": result.get("sha256")}))
"""

# 迁移线程要替换的原函数引用；monkeypatch 只改模块属性，不影响这里的原始引用。
_ORIGINAL_ADMIT = persona_repository_module._admit_persona_mutation


# LLM: 迁移线程第一次进入 _admit_persona_mutation 时暂停（用真实事件），让子进程有机会去抢锁；
#   state=(paused, release) 经 partial 传入；用 *args 透传产品调用签名（保持参数预算）。
# 函数用途: 返回给 monkeypatch 用的替换函数（在迁移线程内暂停一次）。
def _pause_admit_impl(*args, state):
    paused, release = state
    if threading.current_thread().name == "r3-migration" and not paused.is_set():
        paused.set()
        assert release.wait(10)
    return _ORIGINAL_ADMIT(*args)


# 函数用途: 在线程里执行一次 apply；context=(migration_id, sink)，结果写进 sink。
def _apply_migration_sink(home, repo, candidates, context):
    migration_id, sink = context
    try:
        sink["report"] = _apply(home, repo, candidates, migration_id=migration_id)
    except BaseException as exc:
        sink["error"] = repr(exc)


# LLM: 迁移持人格锁（flock）期间用户写入必须等待；真实子进程验证跨进程互斥，释放后写入生效，
#   且清理只删计划内的自动条目、不碰用户新条目（0r3 新增并发用例）。
# 函数用途: 验证迁移执行期间并发 update_persona 等待锁、写入条目在清理后保留。
def test_concurrent_user_write_waits_and_survives_cleanup(tmp_path: Path, monkeypatch):
    home, repo, candidates = _env(tmp_path)
    for index in range(5):
        content = f"并发清理自动条目{index}。"
        candidate = _candidate(candidates, content, 20 + index)
        _promotion_add(repo, content, candidate.candidate_id)
    paused, release = threading.Event(), threading.Event()
    result: dict[str, object] = {}
    monkeypatch.setattr(
        persona_repository_module,
        "_admit_persona_mutation",
        functools.partial(_pause_admit_impl, state=(paused, release)),
    )
    thread = threading.Thread(
        target=_apply_migration_sink,
        args=(home, repo, candidates, ("user-wait", result)),
        name="r3-migration",
    )
    thread.start()
    assert paused.wait(5)
    child = subprocess.Popen(
        [sys.executable, "-c", _USER_WRITER_CODE, str(home.owner_home_dir), "用户并发写入条目。"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.0)
    waited = child.poll() is None
    release.set()
    child.communicate(timeout=15)
    thread.join(15)
    text = _agents_text(repo)
    result["user_entry_present"] = "用户并发写入条目。" in text
    result["automatic_entries_removed"] = "并发清理自动条目" not in text
    result["waited"] = waited
    result["child_returncode"] = child.returncode
    result["migration_alive"] = thread.is_alive()
    assert waited and child.returncode == 0 and not thread.is_alive(), result
    assert result["user_entry_present"] and result["automatic_entries_removed"], result


# LLM: curator 租约是迁移的互斥闸：在途 curator 持租约时迁移结构化拒绝且不写任何东西；
#   释放后可重跑，迁移释放走 commit_success 清空 active_lease 并记录成功时间（0r3 新增）。
# 函数用途: 验证 curator 运行中迁移被拒、释放后成功且租约状态正确收尾。
def test_migration_refuses_while_curator_lease_is_held(tmp_path: Path):
    home, repo, candidates = _env(tmp_path)
    candidate = _candidate(candidates, _ENTRY_ONE, 31)
    _promotion_add(repo, _ENTRY_ONE, candidate.candidate_id)
    store = MemoryCuratorStateStore(home.owner_home_dir / "memory" / "curator" / "state.json")
    acquired = store.acquire(reason="interval", config_revision="test", lease_seconds=300)
    assert acquired is not None
    lease = dict(acquired[1])
    result: dict[str, object] = {}
    try:
        apply_agents_cleanup(
            home_paths=home,
            persona=repo,
            candidate_service=candidates,
            request=AgentsCleanupRequest(migration_id="busy-curator", plan_digest=_digest(repo)),
            curator_state_store=store,
        )
        result["unexpected_success"] = True
    except AgentsCleanupError as exc:
        result["refusal"] = exc.reason_code
    result["entry_kept_while_busy"] = _ENTRY_ONE in _agents_text(repo)
    store.commit_failure(
        lease_id=str(lease.get("lease_id") or ""), failure_code="TEST_RELEASE"
    )
    report = apply_agents_cleanup(
        home_paths=home,
        persona=repo,
        candidate_service=candidates,
        request=AgentsCleanupRequest(migration_id="busy-curator", plan_digest=_digest(repo)),
        curator_state_store=store,
    )
    state = store.load()
    result["removed_count"] = report.get("removed_count")
    result["active_lease_cleared"] = dict(state.active_lease) == {}
    result["last_success_at_set"] = bool(state.last_success_at)
    assert result.get("refusal") == "AGENTS_CLEANUP_CURATOR_BUSY", result
    assert result["entry_kept_while_busy"], result
    assert result["removed_count"] == 1, result
    assert result["active_lease_cleared"], result
    assert result["last_success_at_set"], result
    assert _ENTRY_ONE not in _agents_text(repo), result


# LLM: 冻结与失效必须走同一把线程前缀锁（B 设计）；静态断言两处都引用锁函数，再用并发
#   交错测试证明不会死锁（0r3 新增锁序用例）。
# 函数用途: 静态验证 freeze/expire 都引用线程前缀锁。
def test_thread_prefix_lock_covers_freeze_and_expire():
    freeze_source = inspect.getsource(freeze_thread_persona)
    expire_source = inspect.getsource(expire_thread_personas_containing)
    assert "_thread_prefix_lock_path" in freeze_source
    assert "_thread_prefix_lock_path" in expire_source


# 函数用途: 并发交错 freeze 与 expire 限时完成，验证线程前缀锁不会死锁。
def test_freeze_and_expire_interleave_without_deadlock(tmp_path: Path):
    home, repo, candidates = _env(tmp_path)
    candidate = _candidate(candidates, _ENTRY_ONE, 41)
    _promotion_add(repo, _ENTRY_ONE, candidate.candidate_id)
    builder = SimpleNamespace(
        home_paths=home,
        persona_repository=repo,
        config=SimpleNamespace(
            home_context_enabled=True, thread_prompt_prefix_freeze_enabled=True
        ),
    )
    request = SimpleNamespace(
        thread_persona=ThreadPersonaScope("r3-thread", 1, "test", ("fake", "native"))
    )
    errors: list[str] = []

    def freezer():
        try:
            for _ in range(20):
                prompt_builder._thread_persona_context(builder, request, False)
        except BaseException as exc:
            errors.append(repr(exc))

    def expirer():
        try:
            for _ in range(20):
                expire_thread_personas_containing(home.owner_home_dir, (_ENTRY_ONE,))
        except BaseException as exc:
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=freezer, name="r3-freezer-1"),
        threading.Thread(target=freezer, name="r3-freezer-2"),
        threading.Thread(target=expirer, name="r3-expirer-1"),
        threading.Thread(target=expirer, name="r3-expirer-2"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads), errors
    assert not errors, errors
