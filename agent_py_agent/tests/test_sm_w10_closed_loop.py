"""W10 闭环集成测试：在已合入 W0/W1/W2/W4/W5/W6/W7/W8 的同一提交上串起真实 provider 验证跨切片组合行为。

覆盖范围（Stage A，均使用真实文件/真实 SQLite/真实锁，不替换被测合同）：
1. 275 -> 50 收敛：每 owner 50 条只计机器托管条目，豁免层（固定、用户改过、活跃引用、手写/工作区 Skill）不动，
   已知用过的保留，最冷的“确证从未使用”条目先被换出，每轮工作量受批量预算约束，换出不加黑名单且获得完整宽限期。
2. 单故障崩溃恢复：容量收敛中途子进程在归档搬迁后、登记提交前硬退出，重启后有界轮次内前向收敛，不丢不重。
3. 5,000 owner 身份隔离：同名同正文在不同 owner 下拥有独立 incarnation/activation 与回执键；
   语义指纹 memo 有界，单个 owner 变化不影响其它 owner。
4. 规模工作计数：零引用零 lesson 打开；按 ID 刷新只读选中条目且不走 all()；scope_pairs 命中不重扫。
5. 互斥载体与 lesson 纠正：过程性候选只走 Skill 载体、陈述性候选只走 lesson；lesson 替代保留旧正文并退役旧 HOT。

Stage B 挂钩：W3（组合根 live 接线）与 W9（skill_feedback / self_map）合入后，在文件末尾
“Stage B live composition” 区块补充真实组合根全链路用例（见该区块说明）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_learning_versions as versions_module
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    control_learning,
    maintain_learning,
)
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_proposals import lesson_carrier
from agent_py_agent.agent.capability.skill_usage_receipts import aggregate_key
from agent_py_agent.agent.memory_store import _jsonl_indexing as indexing_module
from agent_py_agent.agent.memory_store import lessons as lessons_module
from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_skill_learning import BODY
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision

REPO_ROOT = Path(__file__).resolve().parents[2]

# 与出厂默认值一致的生命周期参数（7d 冷藏 / 15d 闲置归档 / 归档后 15d 清除）。
COOL_SECONDS = 604_800
ARCHIVE_SECONDS = 1_296_000
EVICT_SECONDS = 1_296_000
MAX_SKILLS = 50
BATCH_ENTRIES = 128


@pytest.fixture(autouse=True)
def _isolated_learning_memo():
    with versions_module._LEARNING_MEMO_LOCK:
        versions_module._LEARNING_STATE_MEMO.clear()
    yield
    with versions_module._LEARNING_MEMO_LOCK:
        versions_module._LEARNING_STATE_MEMO.clear()


def _store_with_runtime(root: Path) -> tuple[object, RuntimeRepository, SkillLearningStore]:
    home = ensure_my_agent_home(root)
    repo = RuntimeRepository(runtime_db_path(home.owner_home_dir))
    store = SkillLearningStore.for_home(home, usage_runtime_provider=lambda: repo)
    return home, repo, store


def _publish(store: SkillLearningStore, name: str):
    dec = replace(
        decision(name),
        body=f"{BODY}\n\n# skill:{name}",
        when_to_use=f"when handling {name}",
        tags=(name, "csv"),
    )
    return publish_learned_skill(store, PublishRequest(dec))


def _patch_maint(store: SkillLearningStore, name: str, **updates: object) -> None:
    with store.state_lock():
        reg = store.load_registry()
        rec = reg.skills[name]
        store.save_registry(replace(reg, skills={**reg.skills, name: replace(rec, maintenance={**rec.maintenance, **updates})}))


def _converge(store: SkillLearningStore, *, now: float, protected_ids=(), max_passes: int) -> list[dict[str, object]]:
    """在固定时钟下反复执行有界自治维护直到无积压，返回每轮报告；超过轮次上限视为未收敛。"""
    reports: list[dict[str, object]] = []
    for _ in range(max_passes):
        report = maintain_learning(
            store,
            mode="autonomous",
            cool_seconds=COOL_SECONDS,
            archive_seconds=ARCHIVE_SECONDS,
            evict_seconds=EVICT_SECONDS,
            max_skills=MAX_SKILLS,
            protected_ids=protected_ids,
            protection_complete=True,
            now=now,
            batch_count=BATCH_ENTRIES,
            batch_ms=5000,
        )
        reports.append(report)
        if not report["backlog"]:
            return reports
    raise AssertionError(f"maintenance did not converge within {max_passes} passes")


def _machine_live(store: SkillLearningStore) -> set[str]:
    reg = store.load_registry()
    return {
        name
        for name, rec in reg.skills.items()
        if rec.layout != "owner"
        and rec.maintenance.get("state") != "archived"
        and rec.maintenance.get("ownership") not in {"owner_layout", "user_modified"}
    }


def _candidate(cid: str, content: str, *, subject_key: str = "closed.loop", scope_key: str = "personal",
               scope_type: str = "personal", candidate_type: str = "lesson", promotion_target: str = "lesson",
               target_entry_id: str = "", occurrence_count: int = 2, applies_when: str = "") -> MemoryCandidate:
    now_iso = "2026-10-01T00:00:00+00:00"
    scope = {"scope_type": scope_type, "scope_key": scope_key}
    if applies_when:
        scope["applies_when"] = applies_when
    return MemoryCandidate(
        schema_version="my-agent.candidate.v1",
        candidate_id=cid,
        candidate_type=candidate_type,
        content=content,
        subject_key=subject_key,
        scope=scope,
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
        proposed_action="add",
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


# ---------------------------------------------------------------------------
# 1. 275 -> 50 收敛：豁免层不动、已知使用保留、最冷未采用先换出、每轮有界
# ---------------------------------------------------------------------------


def test_275_to_50_convergence_preserves_exempt_layers_and_displaces_coldest_unadopted(tmp_path: Path) -> None:
    home, _repo, store = _store_with_runtime(tmp_path / "home")
    names = [f"sk-{index:03d}" for index in range(275)]
    for name in names:
        _publish(store, name)

    now = time.time() + 60.0
    known_used = names[:5]
    pinned, user_modified, active_ref = names[5], names[6], names[7]
    for name in known_used:
        _patch_maint(store, name, ever_used=True, last_used_at=now - 3600.0, independent_uses=3)
    control_learning(store, pinned, operation="pin")
    edited = store.skill_path(user_modified)
    edited.write_text(edited.read_text(encoding="utf-8") + "\n用户自己补充的一段说明。\n", encoding="utf-8")
    edited_bytes = edited.read_bytes()

    # 非托管层：owner 手写 Skill 与工作区 Skill 不进入登记，也绝不被自动生命周期触碰。
    handwritten = home.owner_home_dir / "skills" / "handwritten-notes" / "SKILL.md"
    handwritten.parent.mkdir(parents=True)
    handwritten.write_text("---\nname: handwritten-notes\ndescription: 手写\n---\n\n手写正文。\n", encoding="utf-8")
    workspace_skill = tmp_path / "workspace" / ".agent" / "skills" / "ws-skill" / "SKILL.md"
    workspace_skill.parent.mkdir(parents=True)
    workspace_skill.write_text("---\nname: ws-skill\ndescription: 工作区\n---\n\n工作区正文。\n", encoding="utf-8")
    exempt_bytes = {handwritten: handwritten.read_bytes(), workspace_skill: workspace_skill.read_bytes()}
    blocked_before = store.load_registry().blocked_names

    reports = _converge(store, now=now, protected_ids=(f"owner:{active_ref}",), max_passes=15)

    # 每轮实际状态变更受同一批量预算约束（工作计数而非墙钟）。
    assert all(len(report["changed"]) <= BATCH_ENTRIES for report in reports)
    live = _machine_live(store)
    assert len(live) == MAX_SKILLS
    assert set(known_used) | {pinned, active_ref} <= live

    registry = store.load_registry()
    archived = {name for name, rec in registry.skills.items() if rec.maintenance.get("state") == "archived"}
    # 从未使用池按 baseline 由旧到新换出：保留最新的 43 条，换出 sk-008..sk-231。
    assert archived == set(names[8:232])
    assert live == set(known_used) | {pinned, active_ref} | set(names[232:])
    for name in sorted(archived)[:3] + sorted(archived)[-3:]:
        rec = registry.skills[name]
        assert rec.maintenance["archived_at"] == now  # 容量换出获得完整的归档宽限起点
        assert store.archived_skill_path(name).is_file()
        assert not store.skill_path(name).exists()
    # 容量换出不是用户删除，不加入重名/同族黑名单。
    assert registry.blocked_names == blocked_before
    assert registry.blocked_task_families == ()

    # 用户改过的条目归属转为 user_modified，正文字节不变；手写/工作区 Skill 完全未被触碰。
    assert registry.skills[user_modified].maintenance["ownership"] == "user_modified"
    assert edited.read_bytes() == edited_bytes
    assert all(path.read_bytes() == data for path, data in exempt_bytes.items())
    assert "handwritten-notes" not in registry.skills

    # 已收敛后再完整跑一遍（模拟下一次维护准入，仍按批量预算分片）不产生任何新的状态变更。
    settled = _converge(store, now=now + 1.0, protected_ids=(f"owner:{active_ref}",), max_passes=15)
    assert all(report["changed"] == [] for report in settled)
    assert len(_machine_live(store)) == MAX_SKILLS


# ---------------------------------------------------------------------------
# 2. 单故障崩溃恢复：容量归档搬迁完成后、登记提交前进程硬退出
# ---------------------------------------------------------------------------


def test_cap_enforcement_crash_after_archive_move_recovers_without_loss_or_duplicate(tmp_path: Path) -> None:
    home, _repo, store = _store_with_runtime(tmp_path / "home")
    names = [f"crash-{index}" for index in range(8)]
    for name in names:
        _publish(store, name)
    now = time.time() + 60.0

    script = f"""
import os
from pathlib import Path
from agent_py_agent.agent.capability.skill_learning_maintenance import maintain_learning
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

home = ensure_my_agent_home(Path({str(home.root)!r}))
repo = RuntimeRepository(runtime_db_path(home.owner_home_dir))
store = SkillLearningStore.for_home(home, usage_runtime_provider=lambda: repo)
original_move = store._move_dir_nofollow

def crash_after_move(*args, **kwargs):
    original_move(*args, **kwargs)
    os._exit(77)

store._move_dir_nofollow = crash_after_move
maintain_learning(store, mode="autonomous", cool_seconds={COOL_SECONDS}, archive_seconds={ARCHIVE_SECONDS},
                  evict_seconds={EVICT_SECONDS}, max_skills=5, protection_complete=True, now={now!r}, batch_ms=5000)
"""
    env = {**os.environ, "HOME": str(home.root), "MY_AGENT_HOME": str(home.root), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, check=False, timeout=120)
    assert proc.returncode == 77, proc.stdout + proc.stderr
    crashed = SkillLearningStore.for_home(home).load_registry()
    assert crashed.pending_operation is not None  # 搬迁前已持久化可恢复意图

    # 重启：全新 store 实例在有界轮次内前向收敛到 5 条在役，记录总数不变。
    fresh = SkillLearningStore.for_home(home, usage_runtime_provider=lambda: RuntimeRepository(runtime_db_path(home.owner_home_dir)))
    for _ in range(6):
        report = maintain_learning(
            fresh, mode="autonomous", cool_seconds=COOL_SECONDS, archive_seconds=ARCHIVE_SECONDS,
            evict_seconds=EVICT_SECONDS, max_skills=5, protection_complete=True, now=now + 1.0, batch_ms=5000,
        )
        if not report["backlog"]:
            break
    else:
        raise AssertionError("restart recovery did not converge within 6 passes")
    registry = fresh.load_registry()
    assert registry.pending_operation is None
    assert set(registry.skills) == set(names)
    live = {name for name, rec in registry.skills.items() if rec.maintenance.get("state") != "archived"}
    assert len(live) == 5
    for name, rec in registry.skills.items():
        archived_file = fresh.archived_skill_path(name).is_file()
        live_file = fresh.skill_path(name).is_file()
        # 每条恰好只有一个物理位置，与登记状态一致。
        assert archived_file != live_file
        assert archived_file == (rec.maintenance.get("state") == "archived")


# ---------------------------------------------------------------------------
# 3. 5,000 owner 身份与缓存隔离
# ---------------------------------------------------------------------------


def test_5000_owner_identity_separation_and_bounded_catalog_memo(tmp_path: Path) -> None:
    home_a, repo_a, store_a = _store_with_runtime(tmp_path / "owner-a")
    home_b, repo_b, store_b = _store_with_runtime(tmp_path / "owner-b")
    _publish(store_a, "shared-name")
    _publish(store_b, "shared-name")
    rec_a = store_a.load_registry().skills["shared-name"]
    rec_b = store_b.load_registry().skills["shared-name"]
    # 同名同正文：内容 hash 相同，但身份是各 owner 独立生成的不透明 UUID，回执命名空间不会互相串号。
    assert rec_a.sha256 == rec_b.sha256
    assert rec_a.maintenance["incarnation_id"] != rec_b.maintenance["incarnation_id"]
    assert rec_a.maintenance["activation_id"] != rec_b.maintenance["activation_id"]
    key_a = aggregate_key(rec_a.maintenance["incarnation_id"], rec_a.maintenance["activation_id"], rec_a.sha256)
    key_b = aggregate_key(rec_b.maintenance["incarnation_id"], rec_b.maintenance["activation_id"], rec_b.sha256)
    assert key_a != key_b
    assert repo_a.db_path != repo_b.db_path

    template = store_a.registry_path.read_bytes()
    owners = []
    for index in range(5000):
        owner_home = tmp_path / "owners" / f"o{index:04d}"
        learning_dir = owner_home / "data" / "skill_learning"
        learning_dir.mkdir(parents=True)
        (learning_dir / "registry.json").write_bytes(template)
        owners.append(SimpleNamespace(owner_home_dir=owner_home, owner_skill_learning_dir=learning_dir))

    fingerprints = [versions_module.learning_state_fingerprint(owner) for owner in owners]
    capacity = versions_module._memo_capacity()
    # 缓存按 owner 路径分键且严格有界：5,000 个 owner 走完后驻留条目不超过 resident_owners 配额。
    assert len(versions_module._LEARNING_STATE_MEMO) <= capacity < len(owners)
    assert all(fp and fp != "learning-state-unavailable" for fp in fingerprints)
    # 指纹包含 owner 规范数据目录与 Skill 根：登记字节完全相同的不同 owner 也不会共享快照缓存键。
    assert len(set(fingerprints)) == len(owners)

    # 只修改其中一个 owner 的语义状态，不影响任何其它 owner 的指纹；被 LRU 淘汰的 owner 重新访问结果不变。
    changed_owner, untouched_recent, untouched_evicted = owners[-1], owners[-2], owners[0]
    mutated = json.loads(template)
    mutated["skills"]["shared-name"]["maintenance"]["state"] = "disabled"
    (changed_owner.owner_skill_learning_dir / "registry.json").write_text(json.dumps(mutated), encoding="utf-8")
    assert versions_module.learning_state_fingerprint(changed_owner) != fingerprints[-1]
    assert versions_module.learning_state_fingerprint(untouched_recent) == fingerprints[-2]
    assert versions_module.learning_state_fingerprint(untouched_evicted) == fingerprints[0]
    assert len(versions_module._LEARNING_STATE_MEMO) <= capacity


# ---------------------------------------------------------------------------
# 4. 规模工作计数：读取量只与选中/引用集合成正比，与总历史无关
# ---------------------------------------------------------------------------


def test_recall_work_counters_are_independent_of_total_history(tmp_path: Path, monkeypatch) -> None:
    lessons, hot = (
        LessonRepository(tmp_path / "memory" / "lessons", tmp_path / "memory" / "routing" / "INDEX.md"),
        HotRuleRepository(tmp_path / "memory" / "memory-hot.md"),
    )
    promoted = [lessons.promote(_candidate(f"lesson-{index}", f"第 {index} 条陈述经验。", subject_key=f"subject.{index}"))
                for index in range(30)]
    assert hot.list() == []

    opens: list[tuple[str, ...]] = []
    original_read = lessons_module.read_text_beneath

    def counting_read(root, parts, *args, **kwargs):
        opens.append(tuple(parts))
        return original_read(root, parts, *args, **kwargs)

    monkeypatch.setattr(lessons_module, "read_text_beneath", counting_read)
    assert lessons.read_refs([]) == {}
    assert opens == []  # 零引用零打开
    loaded = lessons.read_refs([promoted[7].path])
    assert list(loaded.values())[0].lesson_id == promoted[7].lesson_id
    assert len(opens) == 1  # 一个引用只打开一次，与 30 条 lesson 总量无关

    memory = JsonlMemory(tmp_path / "memory" / "long_term.jsonl")
    for index in range(200):
        memory.add_record(MemoryRecord(
            role="user", entry_id=f"fact-{index}", kind="fact", content=f"事实 {index}",
            attributes={"scope_type": "project", "scope_key": f"project:p{index % 4}"},
        ))

    def forbidden_all():
        raise AssertionError("selected-ID refresh must not materialize the whole library")

    monkeypatch.setattr(memory, "all", forbidden_all)
    selected = memory.read_ids(["fact-150", "fact-3", "missing", "fact-150"])
    assert [row.entry_id for row in selected] == ["fact-150", "fact-3"]

    scans: list[int] = []
    original_scan = indexing_module.bounded_authority_records

    def counting_scan(*args, **kwargs):
        scans.append(1)
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(indexing_module, "bounded_authority_records", counting_scan)
    first = memory.scope_pairs("project")
    scans_after_first = len(scans)
    second = memory.scope_pairs("project")
    assert first == second == tuple(("project", f"project:p{index}") for index in range(4))
    assert len(scans) == scans_after_first  # generation 未变时命中缓存，不重扫权威


# ---------------------------------------------------------------------------
# 5. 互斥载体分类与 lesson 纠正 / HOT 退役
# ---------------------------------------------------------------------------


def test_exclusive_carrier_and_lesson_correction_retire_obsolete_hot(tmp_path: Path) -> None:
    declarative = _candidate("carrier-decl", "写配置前先确认目标文件是否存在。", scope_type="project",
                             scope_key="project:alpha", applies_when="修改配置")
    procedural = _candidate("carrier-proc", "1. 先读取当前版本。\n2. 用精确版本做比较交换写入。",
                            scope_type="project", scope_key="project:alpha", applies_when="并发更新")
    assert lesson_carrier(declarative) == "lesson"
    assert lesson_carrier(procedural) == "skill"

    lessons = LessonRepository(tmp_path / "memory" / "lessons", tmp_path / "memory" / "routing" / "INDEX.md")
    hot = HotRuleRepository(tmp_path / "memory" / "memory-hot.md")
    old = lessons.promote(_candidate("fix-1", "旧经验：超时固定重试 3 次。", subject_key="net.timeout"))
    hot_rule, _ = hot.promote_with_receipt(
        _candidate("hot-1", "旧 HOT：超时固定重试 3 次。", subject_key="net.timeout", candidate_type="hot_rule",
                   promotion_target="hot", target_entry_id=old.lesson_id, occurrence_count=3),
        lesson=old,
    )
    assert [row.hot_id for row in hot.list()] == [hot_rule.hot_id]

    successor, changed = lessons.supersede_with_receipt(
        _candidate("fix-2", "新经验：超时改用指数退避。", subject_key="net.timeout"),
        target_lesson_id=old.lesson_id,
        hot=hot,
        expected_revision=old.revision,
    )
    assert changed is True and successor.revision == old.revision + 1
    previous = lessons.get(old.lesson_id)
    assert previous.status == "superseded" and previous.content == old.content  # 旧正文永久保留
    assert lessons.active_for_subject("net.timeout", old.scope) == successor
    assert hot.list() == []  # 旧 HOT 退役且不迁移给继任者


# ---------------------------------------------------------------------------
# Stage B live composition（W3 + W9 合入后补充）
# ---------------------------------------------------------------------------
# 待 W3（SkillLearningService live config_provider / usage_runtime_provider / 载体回调的组合根接线）
# 与 W9（skill_feedback 四动作、self_map SkillTelemetry/MemoryTelemetry）合入后，在此区块追加：
#   - 真实 SimpleAgent 组合根：enable_self_learning false -> true -> false 与 maintenance_mode 切换即时生效；
#   - 主会话 skill_feedback(ineffective) + 后台总结 + 包检查三通道同任务只记 1 击，3/4/5 阶梯与第三击回滚/退役；
#   - 子代理调用 skill_feedback 被拒（effect_outcome="not_started"），restore/remove 的副作用回执真实；
#   - self_map 遥测报告 live 规则与部分覆盖（unknown 为 null + reason，不当作 0）。
# 这些用例必须走真实 provider，不以替身实现代替尚未合入的切片。
