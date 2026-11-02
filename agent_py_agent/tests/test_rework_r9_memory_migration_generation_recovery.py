# LLM: 只用 pytest 临时 owner 与真实迁移、正式库、候选、Daily 仓库；每个场景至多一次普通模拟 I/O 错误，
#   “重启”即在同一 home 上重新构造全部服务对象，不替换迁移算法或删除协议。
# 模块用途: 回归 P1-R9-13——canonical 变更前落 prepared 恢复日志；删除已提交后的失败向前收敛，热备份不覆盖永久墓碑。
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store import migration as migration_module
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent
from agent_py_agent.tests.test_memory_migration_v2 import _runtime


# 函数用途: 返回报告中的结构化错误码集合。
def _codes(report) -> set[str]:
    return {error.error_code for error in report.errors}


# LLM: 一条事实走候选替代、一条对话走 Daily 替代、一份旧 lesson 触发派生路由重建，覆盖两类确定性替代。
# 函数用途: 在新 owner 中写入两条旧正式记录与一份旧 lesson 正文。
def _legacy_owner(tmp_path: Path):
    runtime = _runtime(tmp_path)
    home, _candidates, long_term = runtime[:3]
    fact = long_term.add("system", "旧正式事实：发布前先跑本地门禁。", kind="fact")
    talk = long_term.add("user", "旧对话：周三复盘发布节奏。", kind="dialogue")
    lesson = home.owner_memory_lessons_dir / "legacy.md"
    lesson.write_text("# 旧 lesson\n\n先读合同再改代码。\n", encoding="utf-8")
    return runtime, fact, talk


# LLM: 只让 complete marker 的第一次原子写失败一次；备份清单与恢复日志照常落盘。
# 函数用途: 安装单次 marker 模拟 I/O 错误并返回计数。
def _fail_marker_once(monkeypatch, marker_path: Path) -> dict[str, int]:
    original = migration_module.write_json_file_atomic
    calls = {"failed": 0}

    def flaky(path, payload, *args, **kwargs):
        if Path(path) == marker_path and not calls["failed"]:
            calls["failed"] += 1
            raise OSError("simulated marker write error")
        return original(path, payload, *args, **kwargs)

    monkeypatch.setattr(migration_module, "write_json_file_atomic", flaky)
    return calls


# 函数用途: 读取日志中全部确定性候选与 Daily 替代身份。
def _journal_ids(journal: dict) -> tuple[set[str], set[tuple[str, str]]]:
    candidates = {cid for item in journal["formal"] for cid in item["candidates"]}
    daily = {(event["day"], event["event_id"]) for item in journal["formal"] for event in item["daily"]}
    return candidates, daily


# 函数用途: 读取 Daily 仓库中指定日期集合的全部事件 ID。
def _daily_ids(daily, days) -> set[tuple[str, str]]:
    return {(day, event.event_id) for day in days for event in daily.list(day=day)}


# LLM: 删除已提交后 marker 写失败：旧正式记录保持永久墓碑，候选/Daily 替代不被热备份回滚，日志保持 pending；
#   重启后向前收敛签发 recovered complete，再次重启即稳态，替代不重复。
# 函数用途: 验证单次最终 marker 失败与重复重启恢复。
def test_final_marker_failure_keeps_tombstones_and_recovers_forward(tmp_path, monkeypatch):
    (home, candidates, long_term, daily, _lessons, migration), fact, talk = _legacy_owner(tmp_path)
    calls = _fail_marker_once(monkeypatch, migration.marker_path)

    first = migration.apply()

    assert calls["failed"] == 1
    assert first.applied is False
    assert {"MEMORY_MIGRATION_APPLY_FAILED", "MEMORY_MIGRATION_RECOVERY_PENDING"} <= _codes(first)
    active = {record.entry_id for record in long_term.all_including_expired()}
    assert fact.entry_id not in active and talk.entry_id not in active
    for record in (fact, talk):
        tombstone = long_term.committed_removal_record(record.entry_id)
        assert (tombstone.source, tombstone.version) == ("memory-migration-v2", record.version + 1)
    journal = json.loads(migration.journal_path.read_text(encoding="utf-8"))
    assert (journal["status"], journal["run_id"]) == ("prepared", first.run_id)
    candidate_ids, daily_ids = _journal_ids(journal)
    assert candidate_ids and daily_ids
    assert candidate_ids <= {row.candidate_id for row in candidates.list()}
    assert daily_ids <= _daily_ids(daily, {day for day, _ in daily_ids})
    records_copy = Path(first.backup_dir) / "long_term_records.jsonl"
    if os.name == "posix":
        assert stat.S_IMODE(records_copy.stat().st_mode) == 0o600
    assert not migration.marker_path.exists()
    assert migration.plan().already_current is False
    before_candidates = candidates.path.read_bytes()

    _home, candidates, long_term, daily, _lessons, restarted = _runtime(tmp_path)
    second = restarted.apply()

    assert second.ok and second.applied and second.run_id == first.run_id
    assert (second.removed_legacy_long_term, second.migrated_candidates, second.migrated_daily_events) == (
        2, len(candidate_ids), len(daily_ids))
    marker = json.loads(restarted.marker_path.read_text(encoding="utf-8"))
    assert (marker["status"], marker["run_id"], marker["recovered"]) == ("complete", first.run_id, True)
    assert not restarted.journal_path.exists()
    assert candidates.path.read_bytes() == before_candidates
    assert long_term.committed_removal_record(fact.entry_id).source == "memory-migration-v2"

    third = _runtime(tmp_path)[-1].apply()
    assert third.ok and third.already_current and third.run_id == first.run_id
    assert candidates.path.read_bytes() == before_candidates
    assert len(daily.list(day=next(iter(daily_ids))[0])) == len(daily_ids)


# LLM: 替代账本漂移（候选账本被换回迁移前副本、Daily 分片丢失）后，恢复只从摘要相符的私有备份按日志 ID 重建，
#   不复活旧正式记录、不生成日志外的新身份。
# 函数用途: 验证缺失替代由经核验备份确定性补齐。
def test_restart_rebuilds_missing_replacements_from_verified_backup(tmp_path, monkeypatch):
    (home, candidates, long_term, daily, _lessons, migration), fact, _talk = _legacy_owner(tmp_path)
    pre_migration = candidates.path.read_bytes() if candidates.path.exists() else b""
    _fail_marker_once(monkeypatch, migration.marker_path)
    first = migration.apply()
    journal = json.loads(migration.journal_path.read_text(encoding="utf-8"))
    candidate_ids, daily_ids = _journal_ids(journal)
    candidates.path.write_bytes(pre_migration)
    for day in {day for day, _ in daily_ids}:
        (home.owner_memory_daily_dir / f"{day}.jsonl").unlink()

    _home, candidates, long_term, daily, _lessons, restarted = _runtime(tmp_path)
    report = restarted.apply()

    assert report.ok and report.applied and report.run_id == first.run_id
    assert {row.candidate_id for row in candidates.list()} == candidate_ids
    assert _daily_ids(daily, {day for day, _ in daily_ids}) == daily_ids
    assert fact.entry_id not in {record.entry_id for record in long_term.all_including_expired()}
    assert not restarted.journal_path.exists()


# LLM: 私有记录副本摘要不符时替代无法核验：保持 pending，不写候选/Daily，不签发 complete，墓碑不动。
# 函数用途: 验证无法证明的恢复不会制造替代或完成标记。
def test_unverified_backup_keeps_recovery_pending(tmp_path, monkeypatch):
    (home, candidates, long_term, _daily, _lessons, migration), fact, _talk = _legacy_owner(tmp_path)
    pre_migration = candidates.path.read_bytes() if candidates.path.exists() else b""
    _fail_marker_once(monkeypatch, migration.marker_path)
    first = migration.apply()
    candidates.path.write_bytes(pre_migration)
    records_copy = Path(first.backup_dir) / "long_term_records.jsonl"
    records_copy.write_bytes(records_copy.read_bytes().replace("发布前".encode(), "发布后".encode()))

    _home, candidates, long_term, _daily, _lessons, restarted = _runtime(tmp_path)
    report = restarted.apply()

    assert report.applied is False and "MEMORY_MIGRATION_RECOVERY_PENDING" in _codes(report)
    assert candidates.path.read_bytes() == pre_migration
    assert restarted.journal_path.exists() and not restarted.marker_path.exists()
    assert long_term.committed_removal_record(fact.entry_id).source == "memory-migration-v2"


# LLM: 失败与重启之间的人工审核、无关候选与无关 Daily 都是后续真实写入；恢复只核验替代存在，不重置状态、不覆盖账本。
# 函数用途: 验证恢复保留审核决定与无关写入。
def test_recovery_preserves_review_decisions_and_unrelated_writes(tmp_path, monkeypatch):
    (home, candidates, _long_term, daily, _lessons, migration), _fact, _talk = _legacy_owner(tmp_path)
    _fail_marker_once(monkeypatch, migration.marker_path)
    migration.apply()
    journal = json.loads(migration.journal_path.read_text(encoding="utf-8"))
    candidate_ids, _daily_ids_ = _journal_ids(journal)
    migrated_rows = {row.candidate_id for row in candidates.list()}
    reviewed = sorted(candidate_ids)[0]
    if candidates.get(reviewed).status == "observed":
        candidates.transition(reviewed, "pending_review", reviewer="owner")
    candidates.transition(reviewed, "approved", reviewer="owner")
    unrelated = candidates.observe(CandidateObservation(
        candidate_type="user_preference", content="回复尽量用中文。", subject_key="pref.language",
        scope=MemoryScope("personal", "personal"), origin="user_explicit", observation_id="unrelated-pref",
    ))
    unrelated_event = daily.append(DailyMemoryEvent(
        event_type="decision", summary="无关的后续决定。", actor="main_agent",
        created_at=datetime.now(timezone.utc).isoformat(),
    ))

    _home, candidates, _long_term, daily, _lessons, restarted = _runtime(tmp_path)
    report = restarted.apply()

    assert report.ok and report.applied
    rows = {row.candidate_id: row for row in candidates.list()}
    assert rows[reviewed].status == "approved"
    assert candidate_ids <= migrated_rows and set(rows) == migrated_rows | {unrelated.candidate_id}
    assert unrelated_event.event_id in {event.event_id for event in daily.list(day=unrelated_event.created_at[:10])}


# LLM: 删除提交前的真实并发写（用户改成新版本或用户自己删除）使迁移按陈旧前像失败；回滚恢复候选等旧来源，
#   但正式热文件不再从备份回拷，新版本与真实用户墓碑都保持，日志随回滚成功清除。
# 函数用途: 验证热备份不能覆盖删除前发生的新版本或用户删除。
@pytest.mark.parametrize("change", ["newer_version", "user_deletion"])
def test_rollback_before_deletion_keeps_concurrent_formal_change(tmp_path, monkeypatch, change):
    (home, candidates, long_term, _daily, _lessons, migration), fact, _talk = _legacy_owner(tmp_path)
    pre_migration = candidates.path.read_bytes() if candidates.path.exists() else b""
    original = long_term.remove_migrated_legacy

    def concurrent_change_then_remove(*args, **kwargs):
        if change == "newer_version":
            long_term.replace(fact.entry_id, "用户刚更新的事实：发布前跑完整门禁。")
        else:
            long_term.remove(fact.entry_id, source="owner")
        return original(*args, **kwargs)

    monkeypatch.setattr(long_term, "remove_migrated_legacy", concurrent_change_then_remove)
    report = migration.apply()

    assert report.applied is False and "MEMORY_MIGRATION_APPLY_FAILED" in _codes(report)
    current = {record.entry_id: record for record in long_term.all_including_expired()}
    if change == "newer_version":
        assert current[fact.entry_id].version == fact.version + 1
        assert current[fact.entry_id].content == "用户刚更新的事实：发布前跑完整门禁。"
    else:
        assert fact.entry_id not in current
        assert long_term.committed_removal_record(fact.entry_id).source == "owner"
    assert "MEMORY_MIGRATION_RECOVERY_PENDING" not in _codes(report)
    assert candidates.path.read_bytes() == pre_migration
    assert not migration.journal_path.exists() and not migration.marker_path.exists()


# LLM: 读不出的日志或别的 owner 的日志都是未知恢复状态：保持 pending，不扫描、不迁移、不签发 complete。
# 函数用途: 验证无法确认的未完成代次 fail closed。
@pytest.mark.parametrize("journal", ["{not json", json.dumps({
    "schema_version": "my-agent.memory-migration-journal.v1", "status": "prepared",
    "owner_home": "/elsewhere", "run_id": "memory-v2-x", "formal": [],
})])
def test_unknown_journal_keeps_recovery_pending(tmp_path, journal):
    (home, candidates, long_term, _daily, _lessons, migration), fact, _talk = _legacy_owner(tmp_path)
    migration.journal_path.write_text(journal, encoding="utf-8")

    report = migration.apply()

    assert report.applied is False and _codes(report) == {"MEMORY_MIGRATION_RECOVERY_PENDING"}
    assert fact.entry_id in {record.entry_id for record in long_term.all_including_expired()}
    assert not migration.marker_path.exists()
    assert migration.plan().already_current is False
