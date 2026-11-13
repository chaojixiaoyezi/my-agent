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

from agent_py_agent.agent.capability import self_map_memory, self_map_skills, skill_feedback_tool
from agent_py_agent.agent.capability import skill_learning_versions as versions_module
from agent_py_agent.agent.capability.skill_learning import (
    SkillLearningRuntime,
    SkillLearningService,
    SkillLearningSettings,
)
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    control_learning,
    maintain_learning,
    observe_learning,
)
from agent_py_agent.agent.capability.skill_learning_prompt import OUTPUT_SCHEMA_VERSION
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_proposals import lesson_carrier
from agent_py_agent.agent.capability.skill_usage_receipts import aggregate_key, read_usage_aggregate
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store import _jsonl_indexing as indexing_module
from agent_py_agent.agent.memory_store import lessons as lessons_module
from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.owner_wake_discovery import discover_owner_home_page
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.live_switches import register_live_config
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.agent.tooling.self_map_registry import SelfMapReadContext
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_skill_learning import BODY, _FakeBackend
from agent_py_agent.tests.test_sm_w3_intake_runtime import _finalize_ctx, _skill_candidate
from agent_py_agent.tests.test_sm_w9_feedback_awareness import (
    _activate_main_turn,
    _make_real_agent,
    _publish_skill,
    _record_delivery,
)
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
# Stage B：W0..W9 全部合入后的真实组合根全链路（SimpleAgent + 真实工具 + 真实存储）
# ---------------------------------------------------------------------------


def _self_map_ctx(agent: object) -> SelfMapReadContext:
    inv = ToolInvocationContext(agent.tools.runtime_snapshot(allowed_tools=["self_map", "skill_search", "remember"]))
    return SelfMapReadContext(agent, inv, "admin", False, "", "main", agent.home_paths.owner_id, "admin")


def _ineffective_output(name: str) -> str:
    return json.dumps(
        {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "decision": "ineffective",
            "reason": "已交付的方法误导了本轮执行",
            "update_target": name,
            "name": name,
            "description": "",
            "when_to_use": "",
            "tags": [],
            "body": "",
        },
        ensure_ascii=False,
    )


def test_stage_b_v2_1_live_user_config_reaches_service_and_self_map(tmp_path: Path) -> None:
    """V2-1：主会话 UserConfigTool 写入后，同一 Agent 的学习服务与 self_map 在下一次读取即反映，无需重启。"""
    cfg_path = tmp_path / "user_config.yaml"
    cfg_path.write_text('agent_name: "w10-stage-b"\n', encoding="utf-8")
    before = cfg_path.stat()
    os.utime(cfg_path, ns=(before.st_atime_ns, before.st_mtime_ns - 5_000_000_000))
    config = load_config(cfg_path)
    config.model_backend = "echo"
    config.my_agent_home = str(tmp_path / "home")
    config.my_agent_owner_provider = "local"
    config.my_agent_owner_kind = "main"
    config.my_agent_owner_id = "local/main"
    config.execution_mode = "managed"
    register_live_config(config)
    agent, _repo, store = _make_real_agent(tmp_path, config=config)
    _publish_skill(store, "live-policy-skill")
    tool = UserConfigTool(agent)

    assert isinstance(agent.skill_learning, SkillLearningService)  # 出厂即开启
    assert agent.skill_learning._live_state()[1].maintenance_mode == "autonomous"

    def _set(key: str, value: str) -> None:
        out = tool.execute({"action": "set", "key": key, "value": value, "reason": "w10 stage b"})
        assert out.ok, (key, out.output)

    _set("enable_self_learning", "false")
    assert agent.skill_learning is None
    assert "enable_self_learning=False" in self_map_skills.skill_projection(_self_map_ctx(agent)).summary

    _set("enable_self_learning", "true")
    _set("self_learning_maintenance_mode", "observe")
    _set("self_learning_max_skills", "12")
    service = agent.skill_learning
    assert isinstance(service, SkillLearningService)
    settings = service._live_state()[1]
    assert (settings.maintenance_mode, settings.max_skills) == ("observe", 12)
    projection = self_map_skills.skill_projection(_self_map_ctx(agent))
    assert projection.counts["max_learned_skills"] == 12
    assert projection.counts["live_learned_count"] == 1
    assert "maintenance_mode=observe" in projection.summary

    reset = tool.execute({"action": "reset", "key": "self_learning_maintenance_mode", "reason": "w10 stage b"})
    assert reset.ok, reset.output
    assert agent.skill_learning._live_state()[1].maintenance_mode == "autonomous"


def test_stage_b_v2_2_three_channels_one_strike_per_task_and_third_strike_rollback(tmp_path: Path) -> None:
    """V2-2：同一任务上主会话反馈、包检查归因、后台总结复核三通道只记 1 击；三个不同任务后回滚到已证明版本。"""
    from agent_py_agent.agent.capability.skill_learning_host_feedback import (
        observe_command_verification,
    )

    agent, repo, store = _make_real_agent(tmp_path, enable_self_learning=True)
    name = "three-channel"
    v1 = _publish_skill(store, name)
    for idx in (1, 2):
        _record_delivery(store, v1, task_id=f"establish-{idx}")
        observe_learning(
            store, name, sha256=v1.sha256, task_id=f"establish-{idx}", outcome="effective",
            source=LearningObservationSource(authority="host", receipt_id=f"rcpt-est-{idx}"),
        )
    v2 = _publish_skill(store, name, update_from=store.load_registry().skills[name])
    identity = {
        "incarnation_id": str(v2.maintenance["incarnation_id"]),
        "activation_id": str(v2.maintenance["activation_id"]),
        "content_sha256": v2.sha256,
    }

    def hurt() -> int:
        agg = read_usage_aggregate(repo, **identity)
        return int(agg["hurt_count"]) if agg else 0

    backend = _FakeBackend(_ineffective_output(name))
    summarizer = SkillLearningService(
        store,
        SkillLearningSettings(min_tool_rounds=3, max_skills=50),
        SkillLearningRuntime(
            owner_id="local:main",
            deployment_root=tmp_path / "dep",
            backend=backend,
            snapshot_provider=lambda: SimpleNamespace(entries=(SimpleNamespace(
                name=name, source="owner", description="three channel", path=store.skill_path(name),
                stable_id=f"owner:{name}",
            ),)),
            guard_config=None,
            usage_runtime_provider=lambda: repo,
        ),
    )

    # 任务 1：通道 A（主会话 skill_feedback）+ 通道 C（包检查失败）+ 通道 B（后台总结）同任务只记 1 击。
    claim = _activate_main_turn(agent, tmp_path, run_id="run-1", task_id="task-1", request_id="req-1")
    _record_delivery(store, v2, task_id="task-1")
    agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)
    tool = skill_feedback_tool.SkillFeedbackTool(agent)
    first = tool.execute({"action": "ineffective", "skill_id": f"owner:{name}"})
    assert first.ok and first.effect_outcome == "", first.output
    assert first.result_envelope["skill_feedback"]["lifecycle"]["hurt_count"] == 1
    observe_command_verification(
        agent, agent._current_run_params,
        {"ok": False, "command": "pytest", "exit_code": 1, "target_skill": f"owner:{name}"},
    )
    assert summarizer.enqueue_from_finalize(_finalize_ctx("run-1-bg", root_task_id="task-1", used_skill_ids=(name,))) == "queued"
    summarizer.run_pending()
    assert hurt() == 1

    # 任务 2：仅后台总结通道，独立任务记第 2 击（计数清零、要求 5 次）。
    _record_delivery(store, v2, task_id="task-2")
    backend.responses.append(_ineffective_output(name))
    assert summarizer.enqueue_from_finalize(_finalize_ctx("run-2-bg", root_task_id="task-2", used_skill_ids=(name,))) == "queued"
    summarizer.run_pending()
    assert hurt() == 2
    assert store.load_registry().skills[name].maintenance.get("uses_since_hurt", 0) == 0

    # 任务 3：主会话反馈记第 3 击，立即回滚到已证明有效的 v1，不再记第 4 击。
    _activate_main_turn(agent, tmp_path, run_id="run-3", task_id="task-3", request_id="req-3", prev_claim=claim)
    _record_delivery(store, v2, task_id="task-3")
    agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)
    third = tool.execute({"action": "ineffective", "skill_id": f"owner:{name}"})
    assert third.ok, third.output
    assert third.result_envelope["skill_feedback"]["code"] == "ROLLED_BACK"
    assert hurt() == 3
    current = store.load_registry().skills[name]
    assert current.version == 1 and current.sha256 == v1.sha256
    assert current.maintenance["activation_id"] != identity["activation_id"]  # 回滚产生新的激活身份
    assert name not in store.load_registry().blocked_names


def test_stage_b_v2_3_multi_owner_paged_discovery_is_bounded_and_complete(tmp_path: Path, monkeypatch) -> None:
    """V2-3：多 owner 分页发现每页不超过上限、游标走完全集且不重复，发现过程不构造任何 Agent。"""
    home_root = tmp_path / "home"
    users = [f"u{index:02d}" for index in range(25)]
    for uid in users:
        (home_root / "owners" / "providers" / "feishu" / "users" / uid / "data" / "skill_learning").mkdir(parents=True)
    ensure_my_agent_home(home_root)

    def forbidden_agent(*_args, **_kwargs):
        raise AssertionError("cold owner discovery must not construct an Agent")

    monkeypatch.setattr(SimpleAgent, "__init__", forbidden_agent)
    page_limit = 4
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = discover_owner_home_page(home_root / "owners", limit=page_limit, after_cursor=cursor)
        pages += 1
        assert len(page.targets) <= page_limit
        seen.extend(f"{t.identity.provider}:{t.identity.owner_kind}:{t.identity.owner_id}" for t in page.targets)
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages <= 20, "paged discovery did not terminate"
    assert len(seen) == len(set(seen))
    assert {f"feishu:user:{uid}" for uid in users} <= set(seen)
    assert pages >= -(-len(users) // page_limit)


def test_stage_b_v2_4_exclusive_carrier_publishes_without_confirmation_popup(tmp_path: Path) -> None:
    """V2-4：过程性子代理经验只进 Skill 学习队列，记忆侧委派撤回；后台总结直接经闸门发布，不产生待确认提案。"""
    agent, repo, store = _make_real_agent(tmp_path, enable_self_learning=True)
    procedural = _skill_candidate(
        agent.memory_candidates,
        "run-carrier-1",
        body=(
            "# 共享登记表更新规范\n\n## 触发条件\n写共享登记表时触发。\n\n"
            "## 步骤\n1. 先读取当前版本记录。\n2. 用精确版本比较交换落盘。\n"
        ),
    )
    assert lesson_carrier(procedural) == "skill"
    queued = agent.subagents.enqueue_lesson_candidates([procedural])
    assert [row["status"] for row in queued["queued"]] == ["queued"]
    assert queued["refused"] == [] and queued["deferred"] == []
    assert len(store.pending_requests()) == 1

    # 记忆侧自动晋升对同一过程性候选不写 lesson/HOT、不重复入队（已批准候选的委派撤回见 W8 用例）。
    result = agent.memory_promotion.promote(procedural.candidate_id, automatic=True)
    assert result.promoted is False and result.formal_memory_changed is False
    assert agent.memory_lessons.list() == []
    assert len(store.pending_requests()) == 1

    owner = Path(agent.home_paths.owner_home_dir)
    assert not (owner / "data" / "skill_proposals").exists()
    assert not (store.directory / "proposals").exists() or not any((store.directory / "proposals").iterdir())


def test_stage_b_v2_5_real_lesson_heat_telemetry_and_missing_memory_self_heal(tmp_path: Path) -> None:
    """V2-5：真实 lesson 维护写出的热度投影驱动 self_map 遥测（ISO-8601 as_of）；整个 memory 目录被删后读路径静默降级。"""
    import shutil
    from datetime import datetime

    agent, _repo, _store = _make_real_agent(tmp_path, enable_self_learning=True)
    lessons = agent.memory_lessons
    for index in range(2):
        lessons.promote(_candidate(f"heat-{index}", f"第 {index} 条陈述经验。", subject_key=f"heat.subject.{index}"))
    report = lessons.maintain(archive_days=15, pin_loader=lambda: ((), True), batch_ms=5000)
    assert report["coverage"]["status"] in {"complete", "partial"}

    ctx = _self_map_ctx(agent)
    projection = self_map_memory.memory_projection(ctx)
    telemetry = self_map_memory.memory_telemetry(ctx)
    assert projection.errors == ()
    assert projection.counts["lessons_active_count"] == 2
    assert projection.counts["lesson_archive_days"] == 15
    assert datetime.fromisoformat(telemetry["coverage"]["as_of"]).tzinfo is not None

    shutil.rmtree(Path(agent.home_paths.owner_home_dir) / "memory")
    degraded = self_map_memory.memory_projection(_self_map_ctx(agent))
    assert degraded.completeness == "partial"
    assert degraded.counts["lessons_active_count"] is None  # 未知不当作 0
    assert agent.memory.read_ids(["missing-entry"]) == []
    assert agent.memory.scope_pairs("project") == ()
