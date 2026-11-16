"""D-8 回归：超出 self_learning_max_skills 的自学 Skill 在保护名单时有时无的维护片下仍要收敛到上限。

现场（2026-11-16 local/main）：registry 284 条、250 条在役托管、上限 50。容量换出只在一整轮维护的末尾运行，
且要求那一片 protection_complete；任一片保护不完整就跳过容量阶段、清空游标，下一轮要重新走完
usage/ownership/lifecycle 才能回到容量阶段。负载下保护名单时常不完整，于是 4 小时内没有一条被换出。
修复：超额而本片保护或使用证据不完整时，游标停在容量阶段，下一片直接重试；保护规则本身不变，
固定（pinned）与活跃引用条目照旧不动，换出只归档不删除。
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from agent_py_agent.agent.capability.skill_learning_maintenance import (
    control_learning,
    maintain_learning,
)
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_skill_learning import BODY
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision

MAX_SKILLS = 3
SKILL_COUNT = 8


def _store(root: Path) -> SkillLearningStore:
    home = ensure_my_agent_home(root)
    repo = RuntimeRepository(runtime_db_path(home.owner_home_dir))
    return SkillLearningStore.for_home(home, usage_runtime_provider=lambda: repo)


def _seed(store: SkillLearningStore) -> list[str]:
    names = [f"d8-skill-{index:02d}" for index in range(SKILL_COUNT)]
    for name in names:
        dec = replace(decision(name), body=f"{BODY}\n\n# skill:{name}", when_to_use=f"when handling {name}")
        publish_learned_skill(store, PublishRequest(dec))
    return names


def _tick(store: SkillLearningStore, *, now: float, complete: bool, protected=()) -> dict[str, object]:
    # batch_count=1：每片只推进一个条目或一次换出，与生产 25ms 片内只够一次归档的节奏一致且不依赖墙钟。
    return maintain_learning(
        store,
        mode="autonomous",
        max_skills=MAX_SKILLS,
        protection_complete=complete,
        protected_ids=protected,
        now=now,
        batch_count=1,
        batch_ms=5000,
    )


def _live(store: SkillLearningStore) -> set[str]:
    return {
        name for name, rec in store.load_registry().skills.items()
        if rec.maintenance.get("state") != "archived"
    }


def _run_to_cap_stage(store: SkillLearningStore, *, now: float, protected=()) -> None:
    for _ in range(SKILL_COUNT * 4):
        if _tick(store, now=now, complete=True, protected=protected)["changed"]:
            return
    raise AssertionError("maintenance never reached the cap stage")


def test_incomplete_protection_slice_keeps_the_cap_backlog_for_the_next_slice(tmp_path: Path) -> None:
    store = _store(tmp_path / "home")
    _seed(store)
    now = time.time() + 60.0
    _run_to_cap_stage(store, now=now)
    live_before = _live(store)
    assert len(live_before) == SKILL_COUNT - 1

    held = _tick(store, now=now, complete=False)
    # 保护不完整的一片不换出任何条目，但容量积压不能被当作整轮完成而清掉。
    assert held["changed"] == []
    assert held["backlog"] is True
    assert held["cursor"].get("phase") == "lifecycle"
    assert store.load_registry().maintenance_cursor.get("phase") == "lifecycle"
    assert _live(store) == live_before

    resumed = _tick(store, now=now, complete=True)
    # 下一片保护完整时立即继续换出，而不是从 usage 阶段重走整轮。
    assert len(resumed["changed"]) == 1
    assert len(_live(store)) == len(live_before) - 1


def test_alternating_protection_converges_to_cap_without_touching_pinned_or_active(tmp_path: Path) -> None:
    store = _store(tmp_path / "home")
    names = _seed(store)
    pinned, active = names[0], names[1]
    control_learning(store, pinned, operation="pin")
    protected = (f"owner:{active}",)
    now = time.time() + 60.0

    ticks = 0
    for index in range(SKILL_COUNT * 6):
        ticks = index + 1
        report = _tick(store, now=now, complete=index % 2 == 0, protected=protected)
        if not report["backlog"] and len(_live(store)) <= MAX_SKILLS:
            break
    live = _live(store)
    assert len(live) == MAX_SKILLS, f"not converged after {ticks} ticks: {sorted(live)}"
    assert {pinned, active} <= live

    registry = store.load_registry()
    archived = sorted(name for name, rec in registry.skills.items() if rec.maintenance.get("state") == "archived")
    assert len(archived) == SKILL_COUNT - MAX_SKILLS
    # 容量换出只归档：正文仍在 archived/，登记条目还在，不进重名黑名单。
    assert all(store.archived_skill_path(name).is_file() for name in archived)
    assert set(registry.skills) == set(names)
    assert registry.blocked_names == ()


def test_at_cap_an_incomplete_slice_still_finishes_the_pass(tmp_path: Path) -> None:
    store = _store(tmp_path / "home")
    _seed(store)
    now = time.time() + 60.0
    for _ in range(SKILL_COUNT * 8):
        if not _tick(store, now=now, complete=True)["backlog"]:
            break
    assert len(_live(store)) == MAX_SKILLS

    # 不超额时保护不完整不应把游标钉住，否则维护会一直判为到期而空转。
    for _ in range(SKILL_COUNT * 4):
        report = _tick(store, now=now + 1.0, complete=False)
        if not report["backlog"]:
            break
    assert report["backlog"] is False
    assert store.load_registry().maintenance_cursor == {}
    assert len(_live(store)) == MAX_SKILLS
