"""W6 Lesson/HOT 生命周期、热度路由与永久非破坏性归档综合行为测试。

全部使用 pytest tmp_path、临时测试替身与线程事件屏障，不触碰真实宿主目录或网络。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.common import global_write_commit as commits
from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.memory_store import lessons as lesson_module
from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate
from agent_py_agent.agent.memory_store.jsonl import MemorySubjectConflict
from agent_py_agent.agent.memory_store.lessons import (
    HotRuleRepository,
    LessonHeatUnavailableError,
    LessonRecord,
    LessonRepository,
    LessonRoutingPendingError,
    _render_lesson,
)
from agent_py_agent.tests.test_local_store_coordinator_io import Door


def _repo(tmp_path: Path, *, coordinator_io: bool = False) -> tuple[LessonRepository, HotRuleRepository]:
    memory_dir = tmp_path / "memory"
    return (
        LessonRepository(
            memory_dir / "lessons",
            memory_dir / "routing" / "INDEX.md",
            coordinator_io=coordinator_io,
        ),
        HotRuleRepository(memory_dir / "memory-hot.md", coordinator_io=coordinator_io),
    )


def _cand(
    cid: str,
    *,
    subject_key: str = "testing.lifecycle",
    content: str = "先复现失败边界，再实施最小修改。",
    scope_type: str = "personal",
    scope_key: str = "personal",
    occurrence_count: int = 2,
    proposed_action: str = "add",
    promotion_target: str = "lesson",
    candidate_type: str = "lesson",
    target_entry_id: str = "",
) -> MemoryCandidate:
    now_iso = "2026-10-01T00:00:00+00:00"
    return MemoryCandidate(
        schema_version="my-agent.candidate.v1",
        candidate_id=cid,
        candidate_type=candidate_type,
        content=content,
        subject_key=subject_key,
        scope={"scope_type": scope_type, "scope_key": scope_key},
        origin="reviewed",
        evidence_refs=[{"kind": "task", "id": f"t-{cid}-1"}, {"kind": "run", "id": f"r-{cid}-1"}],
        source_message_refs=[],
        source_tool_refs=[],
        source_artifact_refs=[],
        source_task_ids=[f"task-{cid}-1", f"task-{cid}-2"],
        source_run_ids=[f"run-{cid}-1", f"run-{cid}-2"],
        observed_at=now_iso,
        last_observed_at=now_iso,
        valid_from=now_iso,
        valid_until="",
        occurrence_count=occurrence_count,
        confidence=0.92,
        proposed_action=proposed_action,
        target_entry_id=target_entry_id,
        conflicts_with=[],
        promotion_target=promotion_target,
        status="approved",
        reviewer="admin",
        review_note="",
        created_at=now_iso,
        updated_at=now_iso,
        promoted_at="",
        promotion_ref="",
    )


def _hot_cand(
    cid: str,
    *,
    target_lesson_id: str,
    subject_key: str = "testing.lifecycle",
    rule: str = "提交前跑定向测试。",
    occurrence_count: int = 3,
) -> MemoryCandidate:
    return _cand(
        cid,
        subject_key=subject_key,
        content=rule,
        occurrence_count=occurrence_count,
        promotion_target="hot",
        candidate_type="hot_rule",
        target_entry_id=target_lesson_id,
    )


def _make_record(
    index: int,
    *,
    subject_key: str | None = None,
    content: str | None = None,
    updated_at: str = "2026-10-01T00:00:00+00:00",
    created_at: str = "2026-10-01T00:00:00+00:00",
    status: str = "active",
    revision: int = 1,
    superseded_by: str = "",
    superseded_at: str = "",
) -> tuple[LessonRecord, str]:
    lid = f"lesson-{index:04d}{index:04x}"
    subj = subject_key or f"subj.case{index:04d}"
    fname = f"subj-case{index:04d}-{lid[-8:]}.md"
    rec = LessonRecord(
        schema_version=lesson_module.LESSON_SCHEMA_VERSION,
        lesson_id=lid,
        candidate_id=f"cand-{index}",
        subject_key=subj,
        scope={"scope_type": "personal", "scope_key": "personal"},
        content=content or f"教训条目 #{index}",
        occurrence_count=2,
        evidence_groups=(f"run:r{index}", f"task:t{index}"),
        source_task_ids=(f"t{index}",),
        source_run_ids=(f"r{index}",),
        created_at=created_at,
        updated_at=updated_at,
        path=f"memory/lessons/{fname}",
        status=status,
        revision=revision,
        superseded_by=superseded_by,
        superseded_at=superseded_at,
    )
    return rec, fname


def _seed_raw_lesson(repo: LessonRepository, rec: LessonRecord) -> Path:
    path = repo.lessons_dir / Path(rec.path).name
    path.write_text(_render_lesson(rec), encoding="utf-8")
    return path


def _assert_valid_heat_schema(heat: dict[str, object]) -> None:
    required_top = {
        "schema_version",
        "generation",
        "updated_at",
        "rows",
        "routed_ids",
        "lessons_archived_count",
        "archive_count_coverage",
    }
    assert required_top <= set(heat)
    assert heat["schema_version"] == lesson_module.LESSON_HEAT_SCHEMA_VERSION
    assert isinstance(heat["generation"], str) and heat["generation"]
    assert isinstance(heat["updated_at"], (int, float))
    assert isinstance(heat["routed_ids"], list)
    cov = heat["archive_count_coverage"]
    assert isinstance(cov, dict) and cov["status"] in {"complete", "partial", "unavailable"}
    rows = heat["rows"]
    assert isinstance(rows, dict)
    expected_row_keys = {
        "path",
        "subject_key",
        "scope_type",
        "scope_key",
        "status",
        "revision",
        "created_at",
        "updated_at",
        "last_hit_at",
        "hits",
        "unrouted_at",
        "superseded_at",
    }
    for lid, row in rows.items():
        assert isinstance(lid, str) and lid
        assert set(row) == expected_row_keys
        assert row["status"] in {"active", "superseded"}
        assert isinstance(row["revision"], int) and row["revision"] >= 1
        assert isinstance(row["hits"], int) and row["hits"] >= 0


# ---------------------------------------------------------------------------
# 1. read_refs 批量规范直读与边界校验
# ---------------------------------------------------------------------------


def test_read_refs_empty_and_deduped_disk_reads(tmp_path: Path, monkeypatch) -> None:
    repo, _ = _repo(tmp_path)
    r1 = repo.promote(_cand("r1", subject_key="alpha.topic", content="第一条规范内容"))
    r2 = repo.promote(_cand("r2", subject_key="beta.topic", content="第二条规范内容"))

    calls: list[tuple[Path, tuple[str, ...]]] = []
    real_read = lesson_module.read_text_beneath

    def counting_read(root: Path, rel: tuple[str, ...]) -> str | None:
        calls.append((Path(root), rel))
        return real_read(root, rel)

    monkeypatch.setattr(lesson_module, "read_text_beneath", counting_read)

    # 空输入必须零磁盘 I/O。
    assert repo.read_refs([]) == {}
    assert calls == []

    # 同一文件多种合法 ref（带与不带 #lesson_id 重复）只允许打开一次。
    refs = [
        r1.path,
        f"{r1.path}#{r1.lesson_id}",
        r1.path,
        r2.path,
        f"{r2.path}#{r2.lesson_id}",
    ]
    loaded = repo.read_refs(refs)
    assert set(loaded) == {r1.path, f"{r1.path}#{r1.lesson_id}", r2.path, f"{r2.path}#{r2.lesson_id}"}
    assert loaded[r1.path] == r1
    assert loaded[f"{r1.path}#{r1.lesson_id}"] == r1
    assert loaded[r2.path] == r2
    assert len(calls) == 2


@pytest.mark.parametrize(
    "bad_ref",
    [
        "",
        " ",
        "../lessons/a-01234567.md",
        "memory/lessons/../hot.md",
        "memory/lessons/sub/a-01234567.md",
        "memory/lessons/archive/2026-01/a-01234567.md",
        "memory/other/a-01234567.md",
        "memory/lessons/topic.bak.md",
        "memory/lessons/topic.md",
        "memory/lessons/a-01234567.md#",
        "memory/lessons/a-01234567.md#a/b",
        "memory/lessons/a-01234567.md#a#b",
        "memory/lessons/a-01234567.md\n",
        "memory/lessons/a-01234567.md\x00",
        "memory\\lessons\\a-01234567.md",
    ],
)
def test_read_refs_rejects_invalid_refs_even_mixed_with_valid(tmp_path: Path, bad_ref: str) -> None:
    repo, _ = _repo(tmp_path)
    valid = repo.promote(_cand("mix1", subject_key="valid.topic"))
    with pytest.raises(ValueError):
        repo.read_refs([valid.path, bad_ref])


def test_read_refs_rejects_symlinks_missing_is_omitted_and_mismatches_fail(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    rec = repo.promote(_cand("sym1", subject_key="sym.topic"))
    missing_ref = "memory/lessons/missing-deadbeef.md"
    assert repo.read_refs([missing_ref]) == {}

    # 片段 ID 不匹配直接报错。
    with pytest.raises(ValueError, match="LESSON_HEADER_ID_MISMATCH"):
        repo.read_refs([f"{rec.path}#lesson-wrongid"])

    # 头部 path 与文件名不一致直接报错。
    tampered_name = "tampered-01234567.md"
    (repo.lessons_dir / tampered_name).write_text(_render_lesson(rec), encoding="utf-8")
    with pytest.raises(ValueError, match="LESSON_HEADER_PATH_MISMATCH"):
        repo.read_refs([f"memory/lessons/{tampered_name}"])
    (repo.lessons_dir / tampered_name).unlink()

    # 未迁移环境下 legacy 无 marker 文件容忍跳过；migration.json 存在后损坏直接报错。
    legacy_name = "legacy-01234567.md"
    (repo.lessons_dir / legacy_name).write_text("# legacy note\n", encoding="utf-8")
    assert repo.read_refs([f"memory/lessons/{legacy_name}"]) == {}
    (repo.lessons_dir.parent / "migration.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="requires migration"):
        repo.read_refs([f"memory/lessons/{legacy_name}"])
    (repo.lessons_dir / legacy_name).unlink()

    # 符号链接文件与符号链接目录均以 NoFollowPathError 拒绝。
    outside = tmp_path / "outside.md"
    outside.write_text("external", encoding="utf-8")
    sym_file = repo.lessons_dir / "symlinked-01234567.md"
    try:
        os.symlink(outside, sym_file)
    except OSError:
        pytest.skip("当前环境不支持创建 symlink")
    with pytest.raises(NoFollowPathError):
        repo.read_refs(["memory/lessons/symlinked-01234567.md"])


# ---------------------------------------------------------------------------
# 2. note_hits 零磁盘 I/O 与维护并发交错不丢信号
# ---------------------------------------------------------------------------


def test_note_hits_zero_disk_io_dedupes_and_survives_concurrent_maintain(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _ = _repo(tmp_path)
    r1 = repo.promote(_cand("h1", subject_key="hit.one", content="命中第一条"))
    r2 = repo.promote(_cand("h2", subject_key="hit.two", content="命中第二条"))

    io_events: list[str] = []
    real_write = lesson_module.write_private_text_file_atomic_unlocked
    real_read = lesson_module.read_text_beneath

    def spy_write(path, *args, **kwargs):
        io_events.append(f"write:{Path(path).name}")
        return real_write(path, *args, **kwargs)

    def spy_read(root, rel):
        io_events.append(f"read:{rel}")
        return real_read(root, rel)

    monkeypatch.setattr(lesson_module, "write_private_text_file_atomic_unlocked", spy_write)
    monkeypatch.setattr(lesson_module, "read_text_beneath", spy_read)

    hit_t1 = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
    repo.note_hits([r1.lesson_id, r1.lesson_id, " ", r2.lesson_id], at=hit_t1)
    assert io_events == [], "note_hits 不允许做任何同步磁盘读写"

    # 恢复原读写后做一次并发 maintain 交错验证：在 _drain_pending_hits 之后写入窗口追加的命中必须保留至下一轮。
    monkeypatch.undo()
    inside_compute = threading.Event()
    proceed_maintain = threading.Event()
    errors: list[BaseException] = []
    real_compute = lesson_module._compute_routing_from_rows

    def slow_compute(*args, **kwargs):
        inside_compute.set()
        assert proceed_maintain.wait(timeout=5.0)
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(lesson_module, "_compute_routing_from_rows", slow_compute)

    def run_maintain() -> None:
        try:
            repo.maintain(now=datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=run_maintain)
    worker.start()
    assert inside_compute.wait(timeout=5.0)
    hit_t2 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    repo.note_hits([r2.lesson_id], at=hit_t2)
    proceed_maintain.set()
    worker.join(timeout=5.0)
    assert not worker.is_alive() and not errors
    monkeypatch.undo()

    heat_mid = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert heat_mid["rows"][r1.lesson_id]["hits"] == 1
    assert heat_mid["rows"][r2.lesson_id]["hits"] == 1

    # 下一轮 maintain 吸收并发期间暂存的 hit_t2。
    repo.maintain(now=datetime(2026, 10, 5, 12, 1, tzinfo=timezone.utc))
    heat_after = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert heat_after["rows"][r1.lesson_id]["hits"] == 1
    assert heat_after["rows"][r2.lesson_id]["hits"] == 2
    assert heat_after["rows"][r2.lesson_id]["last_hit_at"] == pytest.approx(hit_t2.timestamp())


# ---------------------------------------------------------------------------
# 3. active_for_subject + promote_with_receipt 冲突、scope 规范与并发互斥
# ---------------------------------------------------------------------------


def test_active_for_subject_fail_closed_and_alias_scope_conflict(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    assert repo.active_for_subject("empty.topic", {"scope_type": "personal", "scope_key": "personal"}) is None

    # project scope 规范别名 project:alpha / task:alpha / alpha 等价对齐。
    first, changed1 = repo.promote_with_receipt(
        _cand(
            "scope-a",
            subject_key="deploy.scope",
            content="先构建产物再验证端口。",
            scope_type="project",
            scope_key="alpha",
        )
    )
    assert changed1 is True
    found = repo.active_for_subject("deploy.scope", {"scope_type": "project", "scope_key": "project:alpha"})
    assert found == first

    # 同主题、同规范正文但不同 candidate_id 幂等复用既有记录。
    reused, changed2 = repo.promote_with_receipt(
        _cand(
            "scope-b",
            subject_key="deploy.scope",
            content="  先构建产物再验证端口。  ",
            scope_type="project",
            scope_key="task:alpha",
        )
    )
    assert changed2 is False and reused.lesson_id == first.lesson_id

    # 同主题（别名 scope）、不同正文触发 MemorySubjectConflict。
    with pytest.raises(MemorySubjectConflict) as conflict:
        repo.promote_with_receipt(
            _cand(
                "scope-c",
                subject_key="deploy.scope",
                content="完全不同的替代方案正文。",
                scope_type="project",
                scope_key="task:alpha",
            )
        )
    assert conflict.value.subject_key == "deploy.scope"
    assert conflict.value.entry_id == first.lesson_id

    # 损坏 lesson_heat.json 时必须 fail-closed；执行 maintain() 自愈后恢复。
    repo.heat_path.write_text("{corrupt json", encoding="utf-8")
    with pytest.raises(LessonHeatUnavailableError):
        repo.active_for_subject("deploy.scope", {"scope_type": "project", "scope_key": "alpha"})

    # 删掉 lesson_heat.json 但仍存在规范 .md 文件时同样 fail-closed，避免误判“无同主题”。
    repo.heat_path.unlink()
    with pytest.raises(LessonHeatUnavailableError):
        repo.active_for_subject("deploy.scope", {"scope_type": "project", "scope_key": "alpha"})

    repo.maintain()
    assert repo.active_for_subject("deploy.scope", {"scope_type": "project", "scope_key": "alpha"}) == first


def test_concurrent_promote_same_subject_allows_only_one_active_lesson(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    barrier = threading.Barrier(2)
    results: list[tuple[int, str, str | None]] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait(timeout=5.0)
        try:
            rec, changed = repo.promote_with_receipt(
                _cand(
                    f"conc-{idx}",
                    subject_key="conc.subject",
                    content=f"并发分支 {idx} 的独立内容。",
                )
            )
            outcome = (idx, "ok" if changed else "reused", rec.lesson_id)
        except MemorySubjectConflict as exc:
            outcome = (idx, "conflict", exc.entry_id)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    statuses = sorted(item[1] for item in results)
    assert statuses == ["conflict", "ok"]
    assert len(repo.list()) == 1


# ---------------------------------------------------------------------------
# 4. supersede_with_receipt CAS、HOT 退役、协调门重放与并发竞争
# ---------------------------------------------------------------------------


def test_supersede_validation_lifecycle_and_hot_retirement(tmp_path: Path) -> None:
    repo, hot = _repo(tmp_path)
    old = repo.promote(_cand("sup-1", subject_key="net.timeout", content="旧规则正文：超时重试 3 次。"))
    other = repo.promote(_cand("keep-1", subject_key="disk.sync", content="另一主题保留不变。"))

    hot_old, _ = hot.promote_with_receipt(
        _hot_cand("hot-old", target_lesson_id=old.lesson_id, subject_key="net.timeout", rule="旧 HOT：超时重试 3 次。"),
        lesson=old,
    )
    hot_other, _ = hot.promote_with_receipt(
        _hot_cand("hot-other", target_lesson_id=other.lesson_id, subject_key="disk.sync", rule="保留 HOT：落盘执行 fsync。"),
        lesson=other,
    )
    assert {r.hot_id for r in hot.list()} == {hot_old.hot_id, hot_other.hot_id}

    cand_new = _cand("sup-2", subject_key="net.timeout", content="新规则正文：超时使用指数退避。")

    # 未传 expected_revision / 传错误版本 / 传 remove / 错 scope / 不存在目标均明确拒绝。
    with pytest.raises(ValueError, match="SUPERSESSION_EXPECTED_REVISION_REQUIRED"):
        repo.supersede_with_receipt(cand_new, target_lesson_id=old.lesson_id, hot=hot, expected_revision=None)
    with pytest.raises(ValueError, match="LESSON_REVISION_CONFLICT"):
        repo.supersede_with_receipt(cand_new, target_lesson_id=old.lesson_id, hot=hot, expected_revision=99)
    with pytest.raises(KeyError):
        repo.supersede_with_receipt(cand_new, target_lesson_id="lesson-nonexistent", hot=hot, expected_revision=1)
    with pytest.raises(ValueError, match="SUPERSESSION_SCOPE_OR_SUBJECT_MISMATCH"):
        repo.supersede_with_receipt(
            _cand("sup-bad-subj", subject_key="wrong.subject", content="不匹配主题"),
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )
    with pytest.raises(ValueError, match="LESSON_ACTION_UNSUPPORTED"):
        repo.supersede_with_receipt(
            _cand("sup-rem", subject_key="net.timeout", content="remove action", proposed_action="remove"),
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )

    # 正常替代：产生 rev=2 新教训、旧教训保留原正文改 status=superseded、旧 HOT 退役且不抄给继任者。
    successor, changed = repo.supersede_with_receipt(
        cand_new,
        target_lesson_id=old.lesson_id,
        hot=hot,
        expected_revision=old.revision,
    )
    assert changed is True
    assert successor.status == "active"
    assert successor.revision == old.revision + 1
    assert successor.supersedes == (old.lesson_id,)
    assert successor.superseded_by == ""

    old_after = repo.get(old.lesson_id)
    assert old_after.status == "superseded"
    assert old_after.superseded_by == successor.lesson_id
    assert old_after.superseded_at == successor.created_at
    assert old_after.content == old.content

    assert repo.active_for_subject("net.timeout", old.scope) == successor
    hot_remaining = hot.list()
    assert [row.hot_id for row in hot_remaining] == [hot_other.hot_id]
    assert "旧 HOT" not in hot.path.read_text(encoding="utf-8")

    # 再次对已被替代的旧 lesson 发起另一份 candidate 替代，必须冲突拒绝。
    with pytest.raises(ValueError, match="LESSON_REVISION_CONFLICT"):
        repo.supersede_with_receipt(
            _cand("sup-3", subject_key="net.timeout", content="迟来的第二次替代。"),
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )


@pytest.mark.parametrize("offset", [1, 2, 3, 4, 5, 6, 7])
def test_supersede_coordinator_interruption_and_replay_converge_idempotently(
    tmp_path: Path, offset: int
) -> None:
    door = Door()
    with commits.global_write_commit_scope(door):
        repo, hot = _repo(tmp_path / "case", coordinator_io=True)
        old, _ = repo.promote_with_receipt(_cand("door-old", subject_key="door.sup", content="初始内容"))
        hot.promote_with_receipt(
            _hot_cand("door-hot", target_lesson_id=old.lesson_id, subject_key="door.sup", rule="旧规则要退役。"),
            lesson=old,
        )

    cand_new = _cand("door-new", subject_key="door.sup", content="替代后新内容。")
    door.reject_at = door.calls + offset
    with commits.global_write_commit_scope(door), pytest.raises(RuntimeError):
        repo.supersede_with_receipt(
            cand_new,
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )

    # 撤掉阻塞门后重放相同调用，必须收敛到完全一致的确定性结果，且绝不报错或产生第三份教训。
    door.reject_at = None
    with commits.global_write_commit_scope(door):
        rec_replay, _ = repo.supersede_with_receipt(
            cand_new,
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )

    expected_sid = (
        "lesson-"
        + hashlib.sha256(
            f"{cand_new.candidate_id}\0supersede\0{old.lesson_id}\0{old.revision}".encode()
        ).hexdigest()[:20]
    )
    assert rec_replay.lesson_id == expected_sid
    assert repo.get(old.lesson_id).status == "superseded"
    assert repo.get(old.lesson_id).superseded_by == expected_sid
    assert repo.active_for_subject("door.sup", old.scope) == rec_replay
    assert hot.list() == []
    assert expected_sid in repo.routing_index_path.read_text(encoding="utf-8")


def test_supersede_routing_pending_error_replay(tmp_path: Path, monkeypatch) -> None:
    repo, hot = _repo(tmp_path)
    old = repo.promote(_cand("pend-1", subject_key="pend.subj", content="旧正文"))
    cand_new = _cand("pend-2", subject_key="pend.subj", content="新正文")

    real_write = lesson_module.write_private_text_file_atomic_unlocked

    def fail_index(path, *args, **kwargs):
        if Path(path) == repo.routing_index_path:
            raise OSError("disk full on routing index")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(lesson_module, "write_private_text_file_atomic_unlocked", fail_index)
    with pytest.raises(LessonRoutingPendingError) as caught:
        repo.supersede_with_receipt(
            cand_new,
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )
    assert caught.value.formal_memory_changed is True

    monkeypatch.undo()
    replayed, changed = repo.supersede_with_receipt(
        cand_new,
        target_lesson_id=old.lesson_id,
        hot=hot,
        expected_revision=old.revision,
    )
    assert changed is False
    assert repo.active_for_subject("pend.subj", old.scope) == replayed


def test_concurrent_supersede_race_has_single_winner(tmp_path: Path) -> None:
    repo, hot = _repo(tmp_path)
    old = repo.promote(_cand("race-old", subject_key="race.subj", content="原始竞争正文"))
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, str]] = []
    lock = threading.Lock()

    def contender(idx: int) -> None:
        barrier.wait(timeout=5.0)
        try:
            rec, _ = repo.supersede_with_receipt(
                _cand(f"race-{idx}", subject_key="race.subj", content=f"竞争者 {idx}"),
                target_lesson_id=old.lesson_id,
                hot=hot,
                expected_revision=old.revision,
            )
            tag = ("ok", rec.lesson_id)
        except ValueError as exc:
            assert "LESSON_REVISION_CONFLICT" in str(exc)
            tag = ("conflict", "")
        with lock:
            outcomes.append((idx, tag[0]))

    threads = [threading.Thread(target=contender, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    assert sorted(status for _, status in outcomes) == ["conflict", "ok"]
    active = [r for r in repo.list() if r.status == "active"]
    assert len(active) == 1 and active[0].revision == 2


# ---------------------------------------------------------------------------
# 5. HOT 置顶、归档双向守卫与不完整 pin_loader 拒归档
# ---------------------------------------------------------------------------


def test_hot_pin_protection_and_archive_before_hot_guard(tmp_path: Path) -> None:
    repo, hot = _repo(tmp_path)
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    cold_pinned, _ = _make_record(0, subject_key="pinned.subj", content="被 HOT 置顶的冷教训")
    cold_unpinned, _ = _make_record(1, subject_key="unpinned.subj", content="未被置顶的冷教训")
    _seed_raw_lesson(repo, cold_pinned)
    _seed_raw_lesson(repo, cold_unpinned)

    # 先让一条被 HOT 引用，另一条通过超量路由跌出 INDEX.md。
    for i in range(2, 50):
        rec, _ = _make_record(
            i,
            updated_at=f"2026-10-02T00:00:{i:02d}+00:00",
            content="填充路由预算的长文本内容。" * 4,
        )
        _seed_raw_lesson(repo, rec)

    # HOT-before-archive：即便不传显式 pin_loader，或由动态 pin_loader 读取 HOT，置顶教训不得归档。
    hot.promote_with_receipt(
        _hot_cand("hot-pin-1", target_lesson_id=cold_pinned.lesson_id, subject_key="pinned.subj", rule="始终置顶保留"),
        lesson=cold_pinned,
    )
    repo.maintain(now=t0, archive_days=15, batch_ms=5000)

    # 不完整 pin_loader 必须拒绝一切归档并标记 coverage=partial。
    res_partial = repo.maintain(
        now=t0 + timedelta(days=20),
        archive_days=15,
        pin_loader=lambda: ([], False),
        batch_ms=5000,
    )
    assert res_partial["archived"] == 0
    assert res_partial["coverage"]["status"] == "partial"
    assert (repo.lessons_dir / Path(cold_unpinned.path).name).exists()

    # 完整动态 pin_loader 允许冷条目归档，但保住 HOT 置顶条目。
    res_ok = repo.maintain(
        now=t0 + timedelta(days=20),
        archive_days=15,
        pin_loader=lambda: ([row.lesson_ref for row in hot.list()], True),
        batch_ms=5000,
    )
    assert res_ok["coverage"]["status"] == "complete"
    assert (repo.lessons_dir / Path(cold_pinned.path).name).exists()
    assert not (repo.lessons_dir / Path(cold_unpinned.path).name).exists()

    # Archive-before-HOT：对已归档冷教训继续晋升 HOT 或替代必须明确拒绝。
    with pytest.raises(ValueError, match="HOT_TARGET_ARCHIVED_OR_MISSING"):
        hot.promote_with_receipt(
            _hot_cand("hot-after-arch", target_lesson_id=cold_unpinned.lesson_id, subject_key="unpinned.subj", rule="不能绑已归档教训"),
            lesson=cold_unpinned,
        )
    with pytest.raises(KeyError):
        repo.supersede_with_receipt(
            _cand("sup-after-arch", subject_key="unpinned.subj", content="不能替代已归档"),
            target_lesson_id=cold_unpinned.lesson_id,
            hot=hot,
            expected_revision=cold_unpinned.revision,
        )


# ---------------------------------------------------------------------------
# 6. 15 天边界、命中与回流重置时钟、锁隔离、双活性收敛与不递归归档读
# ---------------------------------------------------------------------------


def test_maintain_15_day_boundaries_clocks_locks_permanent_archive_and_non_recursive_reads(
    tmp_path: Path,
) -> None:
    repo, hot = _repo(tmp_path)
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

    # 构造 50 条足以挤出尾部冷教训的活跃记录 + 1 条已替代教训。
    for i in range(50):
        rec, _ = _make_record(
            i,
            updated_at=f"2026-10-01T01:00:{i:02d}+00:00",
            content="填充用教训正文。" * 6,
        )
        _seed_raw_lesson(repo, rec)

    cold_rec, cold_fname = _make_record(900, subject_key="cold.clock", content="冷教训主体")
    _seed_raw_lesson(repo, cold_rec)
    sup_rec, sup_fname = _make_record(
        901,
        subject_key="sup.clock",
        status="superseded",
        superseded_by="lesson-successor",
        superseded_at=t0.isoformat(timespec="seconds"),
    )
    _seed_raw_lesson(repo, sup_rec)
    # 故意放置一个活跃/遗留 .lock 文件，严禁被归档搬走。
    stale_lock = repo.lessons_dir / f"{cold_fname}.lock"
    stale_lock.write_text('{"pid": 999999}', encoding="utf-8")

    # 首次 maintain 对缺失时间戳做保守重建：时钟自 t0 开始，当天两者均保留。
    repo.maintain(now=t0, archive_days=15, batch_ms=5000)
    assert (repo.lessons_dir / cold_fname).exists()
    assert (repo.lessons_dir / sup_fname).exists()

    # 差 1 秒满 15 天仍保留。
    just_before = t0 + timedelta(days=15) - timedelta(seconds=1)
    res_before = repo.maintain(now=just_before, archive_days=15, batch_ms=5000)
    assert res_before["archived"] == 0
    assert (repo.lessons_dir / cold_fname).exists()
    assert (repo.lessons_dir / sup_fname).exists()

    # 第 10 天给 cold_rec 一次命中，刷新活跃时钟：26 条未命中的出局填充 + sup_rec 满 15 天归档，cold_rec 仍保留。
    hit_time = t0 + timedelta(days=10)
    repo.note_hits([cold_rec.lesson_id], at=hit_time)
    at_15d = t0 + timedelta(days=15)
    res_15d = repo.maintain(now=at_15d, archive_days=15, batch_ms=5000)
    assert res_15d["archived"] == 27
    assert not (repo.lessons_dir / sup_fname).exists()
    sup_archive_path = repo.lessons_dir / "archive" / f"{at_15d.year:04d}-{at_15d.month:02d}" / sup_fname
    assert sup_archive_path.exists()
    assert (repo.lessons_dir / cold_fname).exists()
    assert stale_lock.exists(), ".lock 侧车严禁被搬进 archive/"
    assert not list((repo.lessons_dir / "archive").rglob("*.lock"))

    # 令 cold_rec 多次命中回流 INDEX.md 后再冷却，时钟从重新出局时重新算，而非继承旧 unrouted_at。
    reenter_time = t0 + timedelta(days=16)
    repo.note_hits([cold_rec.lesson_id], at=reenter_time)
    repo.maintain(now=reenter_time, archive_days=15, batch_ms=5000)
    heat_reenter = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert cold_rec.lesson_id in heat_reenter["routed_ids"]
    assert heat_reenter["rows"][cold_rec.lesson_id]["unrouted_at"] == 0.0

    # archive_days=0 仅关闭自动归档，仍同步 heat 与归档总数。
    res_disabled = repo.maintain(now=t0 + timedelta(days=100), archive_days=0, batch_ms=5000)
    assert res_disabled["archived"] == 0
    assert (repo.lessons_dir / cold_fname).exists()
    assert res_disabled["lessons_archived_count"] == 27

    # 真正进入 +10 年后（期间不再命中），若前面未归档的冷教训超过窗口可被归档，但已有归档文件永久保留不删。
    far_future = t0 + timedelta(days=3650)
    repo.maintain(now=far_future, archive_days=15, batch_ms=5000)
    assert sup_archive_path.exists(), "永久归档文件十年后仍必须保留"

    # 活跃读接口（list/get/read_refs/rebuild_routing_index）严禁递归进 archive/。
    # 在 archive 内放一个不可读/损坏文件也绝不能拖垮活跃读。
    (repo.lessons_dir / "archive" / "2020-01").mkdir(parents=True, exist_ok=True)
    (repo.lessons_dir / "archive" / "2020-01" / "corrupt-deadbeef.md").write_text("bad", encoding="utf-8")
    live_ids = {r.lesson_id for r in repo.list()}
    assert sup_rec.lesson_id not in live_ids
    with pytest.raises(KeyError):
        repo.get(sup_rec.lesson_id)
    assert repo.read_refs([sup_rec.path]) == {}
    repo.rebuild_routing_index()
    idx_text = repo.routing_index_path.read_text(encoding="utf-8")
    assert sup_rec.lesson_id not in idx_text


def test_maintain_reconciles_manual_restore_duplicate_active_authority(tmp_path: Path) -> None:
    repo, hot = _repo(tmp_path)
    rev1 = repo.promote(_cand("dup-1", subject_key="dup.restore", content="版本 1 旧内容"))
    rev2, _ = repo.supersede_with_receipt(
        _cand("dup-2", subject_key="dup.restore", content="版本 2 新内容"),
        target_lesson_id=rev1.lesson_id,
        hot=hot,
        expected_revision=rev1.revision,
    )
    later = datetime.now(timezone.utc) + timedelta(days=20)
    repo.maintain(now=later, archive_days=15)
    archived = repo.lessons_dir / "archive" / f"{later.year:04d}-{later.month:02d}" / Path(rev1.path).name
    assert archived.exists()

    # 模拟运维把旧版 rev1 文件手工改回 active 拷贝回 lessons/，造成同一 subject/scope 双活。
    restored_rev1 = _render_lesson(
        LessonRecord(
            **{
                **rev1.__dict__,
                "status": "active",
                "superseded_by": "",
                "superseded_at": "",
            }
        )
    )
    (repo.lessons_dir / Path(rev1.path).name).write_text(restored_rev1, encoding="utf-8")
    repo.maintain(now=later, archive_days=15)

    assert repo.get(rev2.lesson_id).status == "active"
    assert repo.get(rev1.lesson_id).status == "superseded"
    assert repo.get(rev1.lesson_id).superseded_by == rev2.lesson_id
    assert repo.active_for_subject("dup.restore", rev2.scope) == repo.get(rev2.lesson_id)


# ---------------------------------------------------------------------------
# 7. 大规模有界切片、schema 校验与 Rule #6 缺失自愈
# ---------------------------------------------------------------------------


def test_maintain_bounded_slices_scale_and_rule6_self_healing(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    # 创建 260 条活跃 lesson + 120 条已归档文件，按小切片推进，直到 backlog=False。
    for i in range(260):
        rec, _ = _make_record(i, updated_at=f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00")
        _seed_raw_lesson(repo, rec)
    bucket = repo.lessons_dir / "archive" / "2026-08"
    bucket.mkdir(parents=True, exist_ok=True)
    for j in range(120):
        rec, fname = _make_record(1000 + j)
        (bucket / fname).write_text(_render_lesson(rec), encoding="utf-8")

    now_dt = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)
    slices: list[dict[str, object]] = []
    for _ in range(30):
        step = repo.maintain(now=now_dt, archive_days=15, batch_count=60, batch_bytes=16_384)
        slices.append(step)
        assert step["scanned"] <= 60
        if not step["backlog"]:
            break
    assert slices[-1]["backlog"] is False
    assert slices[0]["backlog"] is True
    assert slices[-1]["coverage"]["status"] == "complete"
    assert slices[-1]["lessons_active_count"] == 260
    assert slices[-1]["lessons_archived_count"] == 120

    heat = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    _assert_valid_heat_schema(heat)

    # 极端字节预算（1 字节）也必须每次至少推进 1 条，绝不卡死零前进。
    tiny_slice = repo.maintain(now=now_dt, archive_days=0, batch_bytes=1, batch_count=100)
    assert tiny_slice["scanned"] >= 1

    # AGENTS.md 第 6 条：任意删除 lesson_heat.json、INDEX.md 或单条 lesson 文件均平滑自愈不报错（默认切片内分步完成全量自愈）。
    first_file = next(repo.lessons_dir.glob("*.md"))
    first_file.unlink()
    repo.heat_path.unlink()
    repo.routing_index_path.unlink()
    healed: dict[str, object] = {}
    for _ in range(10):
        healed = repo.maintain(now=now_dt, archive_days=0)
        if not healed["backlog"]:
            break
    assert healed["backlog"] is False
    assert healed["coverage"]["status"] == "complete"
    assert healed["lessons_active_count"] == 259
    assert healed["lessons_archived_count"] == 120
    assert repo.heat_path.exists() and repo.routing_index_path.exists()


def test_maintain_invalidates_stale_archive_count_when_archive_move_stops_mid_slice(
    tmp_path: Path,
) -> None:
    repo, _ = _repo(tmp_path)
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    bucket = repo.lessons_dir / "archive" / "2026-08"
    bucket.mkdir(parents=True, exist_ok=True)
    for j in range(3):
        rec, fname = _make_record(2000 + j)
        (bucket / fname).write_text(_render_lesson(rec), encoding="utf-8")

    # 在 lessons/ 中放置 6 条已替代待归档教训，并先记录完整归档计数 3。
    for i in range(6):
        rec, _ = _make_record(
            i,
            status="superseded",
            superseded_by="lesson-successor",
            superseded_at=t0.isoformat(timespec="seconds"),
        )
        _seed_raw_lesson(repo, rec)

    initial = repo.maintain(now=t0, archive_days=15)
    assert initial["lessons_archived_count"] == 3
    assert initial["coverage"]["status"] == "complete"

    # 分片执行 archive_move（6 条 active 扫描消耗 6 次后余 2 次预算），在搬走 2 条后耗尽并作废旧归档计数。
    mid = repo.maintain(now=t0 + timedelta(days=20), archive_days=15, batch_count=8)
    assert mid["cursor"]["phase"] == "archive_move"
    assert mid["archived"] == 2
    assert mid["backlog"] is True
    assert mid["lessons_archived_count"] is None
    heat_mid = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert heat_mid["lessons_archived_count"] is None
    assert heat_mid["archive_count_coverage"]["status"] == "partial"
    assert heat_mid["archive_count_coverage"]["reason"] == "archive_moved_pending_recount"

    # 验证 batch_bytes=1 在 archive_move 阶段也会真实计字节并仅推进 1 条。
    byte_slice = repo.maintain(
        now=t0 + timedelta(days=20),
        archive_days=15,
        batch_count=100,
        batch_bytes=1,
    )
    assert byte_slice["archived"] == 1
    assert byte_slice["backlog"] is True
    assert byte_slice["lessons_archived_count"] is None

    final: dict[str, object] = {}
    for _ in range(10):
        final = repo.maintain(now=t0 + timedelta(days=20), archive_days=15, batch_count=2)
        if not final["backlog"]:
            break
    assert final["backlog"] is False
    assert final["coverage"]["status"] == "complete"
    assert final["lessons_archived_count"] == 9


def test_maintain_preserves_note_hits_for_unscanned_tail_lessons_during_sliced_active_scan(
    tmp_path: Path,
) -> None:
    repo, _ = _repo(tmp_path)
    records = []
    for i in range(10):
        rec, _ = _make_record(i, updated_at=f"2026-10-01T00:00:{i:02d}+00:00")
        _seed_raw_lesson(repo, rec)
        records.append(rec)

    tail_rec = records[-1]
    hit_dt = datetime(2026, 10, 5, 8, 30, tzinfo=timezone.utc)
    repo.note_hits([tail_rec.lesson_id], at=hit_dt)

    # 第一片仅扫描前 4 条即停止在 phase='active'，尾部命中的信号必须回填并保留到扫全之后。
    t0 = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
    first = repo.maintain(now=t0, archive_days=0, batch_count=4)
    assert first["cursor"]["phase"] == "active"
    assert first["backlog"] is True

    last: dict[str, object] = {}
    for _ in range(10):
        last = repo.maintain(now=t0, archive_days=0, batch_count=4)
        if not last["backlog"]:
            break
    assert last["backlog"] is False
    assert last["coverage"]["status"] == "complete"
    heat = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert heat["rows"][tail_rec.lesson_id]["hits"] == 1
    assert heat["rows"][tail_rec.lesson_id]["last_hit_at"] == pytest.approx(hit_dt.timestamp())


def test_supersede_wraps_hot_projection_failure_in_lesson_routing_pending_error(
    tmp_path: Path, monkeypatch
) -> None:
    repo, hot = _repo(tmp_path)
    old = repo.promote(_cand("hotfail-old", subject_key="hotfail.subj", content="旧正文"))
    hot.promote_with_receipt(
        _hot_cand("hotfail-rule", target_lesson_id=old.lesson_id, subject_key="hotfail.subj", rule="待退役 HOT 规则"),
        lesson=old,
    )
    cand_new = _cand("hotfail-new", subject_key="hotfail.subj", content="新正文")
    expected_sid = (
        "lesson-"
        + hashlib.sha256(
            f"{cand_new.candidate_id}\0supersede\0{old.lesson_id}\0{old.revision}".encode()
        ).hexdigest()[:20]
    )

    real_write = lesson_module.write_private_text_file_atomic_unlocked

    def fail_hot(path, *args, **kwargs):
        if Path(path) == hot.path:
            raise OSError("simulated hot file write failure")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(lesson_module, "write_private_text_file_atomic_unlocked", fail_hot)
    with pytest.raises(LessonRoutingPendingError) as caught:
        repo.supersede_with_receipt(
            cand_new,
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )
    assert caught.value.formal_memory_changed is True
    assert caught.value.promotion_ref.endswith("#" + expected_sid)
    assert isinstance(caught.value.__cause__, OSError)
    assert repo.get(old.lesson_id).status == "superseded"
    assert repo.get(expected_sid).status == "active"

    # 恢复写能力后重放幂等完成 HOT 退役与 heat/routing 对账。
    monkeypatch.undo()
    replayed, changed = repo.supersede_with_receipt(
        cand_new,
        target_lesson_id=old.lesson_id,
        hot=hot,
        expected_revision=old.revision,
    )
    assert changed is False and replayed.lesson_id == expected_sid
    assert hot.list() == []
    assert repo.active_for_subject("hotfail.subj", old.scope) == replayed
