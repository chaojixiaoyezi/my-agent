"""R1-03 补漏 2 红测：run 权威终态后任务级账本自愈闭环。

真机根因（2026-08-12 testbox）：孤儿回收 tick（wake-tick-orphan-reclaim）把
「挂载后无续租」的 attempt 经 settle_agent_run 落成 run 终态（cancelled）——
但 state.json 残留 RUNNING、link 残留 active、task_run 未 closeout（三层任务级
账本都没闭环）→ 发现层 _filter_by_runtime_authority 每 tick 对每个 stale task
写一条 status_conflict 诊断事件（ledger_stale_after_terminal）→ 每秒 2 条无限
增长（soak 判据直接失败）。

修复：发现层自愈投影——run 终态分支把任务级账本投影终态：
  ① state.json → finish_run_workspace（CANCELLED/UNVERIFIED，身份从文件读）
  ② link status → 'cancelled'
  ③ task_run → settle_task_run_terminal（closed_at CAS 幂等）
投影成功不写诊断事件（风暴自然停），失败保留 status_conflict（可观测）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.owner_wake_discovery import unfinished_task_ids
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


@pytest.fixture
def stale_owner(tmp_path):
    """构造「run 权威终态 cancelled + 三层任务级账本残留」的 owner home。"""
    home = tmp_path / "home"
    repo = RuntimeRepository(home / "runtime.db")
    task_id = "req_1786517186493_1025282_0"
    run = repo.record_run_creation(
        owner_id="local/main",
        goal="工具测试",
        conversation_task_id=task_id,
        run_id=task_id,  # gateway 请求身份登记
        role="main",
    )
    repo.settle_agent_run(
        agent_run_id=run["agent_run_id"],
        status="cancelled",
        attempt_id=run["attempt_id"],
        payload={"status": "cancelled", "runtime_status": "cancelled"},
    )
    # 任务工作区：state.json 残留 RUNNING，primary_run_id 是续跑激活身份
    now = 1786517249.0
    task_root = home / "tasks" / "2026-08-12" / "tool-test"
    (task_root / "work").mkdir(parents=True)
    state = {
        "status": "RUNNING",
        "task_id": task_id,
        "primary_run_id": "bg-main-thread-eeb07112a70a4cd5",
        "runtime_status": "",
        "current_step": "",
        "updated_at": _iso(now),
    }
    (task_root / "work" / "state.json").write_text(
        __import__("json").dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    identity = {
        "request_id": "run-1786517249564574101",
        "run_id": "bg-main-thread-eeb07112a70a4cd5",
        "task_id": task_id,
    }
    (task_root / "work" / "run_workspace.json").write_text(
        __import__("json").dumps(identity, ensure_ascii=False), encoding="utf-8"
    )
    # link：生命周期权威仍 active，task_path 指向任务目录
    link_dir = home / "workspace" / "runtime" / "workspaces" / "tool-test" / "conversations" / "tasks"
    link_dir.mkdir(parents=True)
    link = {
        "task_id": task_id,
        "status": "active",
        "task_path": "tasks/2026-08-12/tool-test",
        "thread_id": "thread-1",
        "goal": "工具测试",
    }
    (link_dir / f"{task_id}.json").write_text(
        __import__("json").dumps(link, ensure_ascii=False), encoding="utf-8"
    )
    return {"home": home, "repo": repo, "task_id": task_id, "task_root": task_root,
            "link_path": link_dir / f"{task_id}.json"}


def _read_json(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _stale_conflict_events(repo: RuntimeRepository) -> list[dict]:
    rows = repo._runtime_connect().execute(
        "SELECT event_id FROM runtime_events "
        "WHERE event_type = 'status_conflict' "
        "AND payload_json LIKE '%ledger_stale_after_terminal%'"
    ).fetchall()
    return list(rows)


def test_terminal_run_projects_task_ledger_terminal(stale_owner):
    """run 权威终态 + 账本残留 → 一次 unfinished_task_ids 自愈三层账本，且
    不再写 status_conflict 诊断事件（风暴根因消解）。"""
    home = stale_owner["home"]
    repo = stale_owner["repo"]
    task_id = stale_owner["task_id"]

    remaining = unfinished_task_ids(home)

    # ④ 返回不含该 task（权威终态不驱动）
    assert task_id not in remaining
    # ① state.json 投影终态
    state = _read_json(stale_owner["task_root"] / "work" / "state.json")
    assert state["status"] == "CANCELLED"
    assert state["verification_status"] == "UNVERIFIED"
    assert state["terminal_run_id"]
    # ② link 置终态
    link = _read_json(stale_owner["link_path"])
    assert link["status"] == "cancelled"
    # ③ task_run 终态化（closed_at CAS）
    row = repo._runtime_connect().execute(
        "SELECT status, closed_at FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert row is not None and row["status"] == "cancelled" and row["closed_at"] > 0
    # ⑤ 不再写诊断事件（风暴自然停）
    assert _stale_conflict_events(repo) == []


def test_projection_is_idempotent(stale_owner):
    """二次调用不重复写：文件不再变化、无新事件（幂等收敛）。"""
    home = stale_owner["home"]
    repo = stale_owner["repo"]
    task_id = stale_owner["task_id"]

    unfinished_task_ids(home)
    state_before = _read_json(stale_owner["task_root"] / "work" / "state.json")
    link_before = _read_json(stale_owner["link_path"])
    events_before = len(_stale_conflict_events(repo))

    unfinished_task_ids(home)

    state_after = _read_json(stale_owner["task_root"] / "work" / "state.json")
    link_after = _read_json(stale_owner["link_path"])
    assert state_after == state_before
    assert link_after == link_before
    assert len(_stale_conflict_events(repo)) == events_before
    assert task_id not in unfinished_task_ids(home)


def test_cancelled_old_attempt_cannot_overwrite_new_generation(stale_owner):
    from agent_py_agent.agent.owner_wake_discovery import _project_task_ledger_terminal

    repo = stale_owner["repo"]
    task_id = stale_owner["task_id"]
    old = repo.main_agent_run_for_task(task_id)
    new = repo.create_attempt(old["agent_run_id"])
    projected = _project_task_ledger_terminal(
        stale_owner["home"], repo, task_id, old, stale_owner["link_path"],
    )
    assert projected
    assert repo.main_agent_run_for_task(task_id)["current_attempt_id"] == new["attempt_id"]
    assert _read_json(stale_owner["link_path"])["status"] == "active"
    assert _read_json(stale_owner["task_root"] / "work/state.json")["status"] == "RUNNING"


def test_cancelled_turn_is_not_cancelled_active_goal(stale_owner):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    store = ConversationStore(stale_owner["link_path"].parent.parent)
    thread = store.get_or_create_thread({"canonical_user_id": "test", "channel": "chat", "channel_conversation_id": "goal"})
    link = _read_json(stale_owner["link_path"])
    link["thread_id"] = thread.thread_id
    write_json_file_atomic(stale_owner["link_path"], link)
    goal = store.create_goal({"thread_id": thread.thread_id, "task_id": stale_owner["task_id"], "objective": "持续整理"})
    unfinished_task_ids(stale_owner["home"])
    assert store.load_task_link(goal.task_id).status == "active"
    assert store.load_goal(thread.thread_id).status == "active"
    assert _read_json(stale_owner["task_root"] / "work/state.json")["status"] == "RUNNING"
    assert not store.pending_wake_signals()  # 扫描不得自己创建续跑授权。


def test_non_terminal_run_keeps_driving(tmp_path):
    """run 非终态（created）→ 不投影账本，任务照旧返回驱动（保护既有行为）。"""
    home = tmp_path / "home"
    repo = RuntimeRepository(home / "runtime.db")
    task_id = "req_1786515758774_1022791_0"
    repo.record_run_creation(
        owner_id="local/main",
        goal="canary",
        conversation_task_id=task_id,
        run_id=task_id,
        role="main",
    )
    task_root = home / "tasks" / "2026-08-12" / "canary"
    (task_root / "work").mkdir(parents=True)
    state = {"status": "RUNNING", "task_id": task_id, "primary_run_id": task_id,
             "updated_at": _iso(1786515758.0)}
    (task_root / "work" / "state.json").write_text(
        __import__("json").dumps(state), encoding="utf-8"
    )
    link_dir = home / "workspace" / "runtime" / "workspaces" / "canary" / "conversations" / "tasks"
    link_dir.mkdir(parents=True)
    link = {"task_id": task_id, "status": "active",
            "task_path": "tasks/2026-08-12/canary", "thread_id": "t", "goal": "canary"}
    (link_dir / f"{task_id}.json").write_text(
        __import__("json").dumps(link), encoding="utf-8"
    )

    remaining = unfinished_task_ids(home)

    assert task_id in remaining
    assert _read_json(task_root / "work" / "state.json")["status"] == "RUNNING"
    assert _read_json(link_dir / f"{task_id}.json")["status"] == "active"
    # 未 settle 的 run 不产生 stale 诊断（非终态不是冲突观测）
    assert _stale_conflict_events(repo) == []


def test_done_run_does_not_project_lifecycle_link(tmp_path):
    """done 终态 + link active = 持续任务轮间形态（audit/监控一轮轮跑）：
    本轮 run 完成但任务生命周期还在——绝不投影 link（link 终态化会让
    wake stale 检查误判任务已死而作废 pending wake，真机教训），
    state.json 由下一轮激活接管，task_run 也不 closeout。"""
    home = tmp_path / "home"
    repo = RuntimeRepository(home / "runtime.db")
    task_id = "audit-aggregate-capacity"
    repo.record_run_creation(
        owner_id="local/main",
        goal="持续审计",
        conversation_task_id=task_id,
        run_id=task_id,
        role="main",
    )
    run = repo.main_agent_run_for_task(task_id)
    repo.settle_agent_run(
        agent_run_id=run["agent_run_id"],
        status="done",
        attempt_id=run["current_attempt_id"],
        payload={"status": "done", "runtime_status": "ok"},
    )
    task_root = home / "tasks" / "2026-08-12" / "audit"
    (task_root / "work").mkdir(parents=True)
    state = {"status": "RUNNING", "task_id": task_id, "primary_run_id": task_id,
             "updated_at": _iso(1786517249.0)}
    (task_root / "work" / "state.json").write_text(
        __import__("json").dumps(state), encoding="utf-8"
    )
    link_dir = home / "workspace" / "runtime" / "workspaces" / "audit" / "conversations" / "tasks"
    link_dir.mkdir(parents=True)
    link = {"task_id": task_id, "status": "active",
            "task_path": "tasks/2026-08-12/audit", "thread_id": "t", "goal": "持续审计"}
    (link_dir / f"{task_id}.json").write_text(
        __import__("json").dumps(link), encoding="utf-8"
    )

    remaining = unfinished_task_ids(home)

    assert task_id not in remaining  # R1-03：权威终态不驱动（持续任务靠事件 wake）
    link_after = _read_json(link_dir / f"{task_id}.json")
    assert link_after["status"] == "active"  # 生命周期权威保持 active
    assert _read_json(task_root / "work" / "state.json")["status"] == "RUNNING"
    row = repo._runtime_connect().execute(
        "SELECT closed_at FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert row["closed_at"] == 0


def test_missing_link_task_path_falls_back_to_state_glob(stale_owner):
    """link 缺 task_path（旧 link）→ 按 state.json 的 task_id 反查也能投影。"""
    home = stale_owner["home"]
    repo = stale_owner["repo"]
    task_id = stale_owner["task_id"]
    link = _read_json(stale_owner["link_path"])
    link.pop("task_path", None)
    stale_owner["link_path"].write_text(
        __import__("json").dumps(link, ensure_ascii=False), encoding="utf-8"
    )

    remaining = unfinished_task_ids(home)

    assert task_id not in remaining
    state = _read_json(stale_owner["task_root"] / "work" / "state.json")
    assert state["status"] == "CANCELLED"
    assert _stale_conflict_events(repo) == []


def test_projection_failure_keeps_diagnostic_event(stale_owner):
    """投影失败（身份不可得，finish_run_workspace 无法权威终态化）→ 保留
    status_conflict 诊断事件（可观测，不静默吞掉冲突）。"""
    home = stale_owner["home"]
    repo = stale_owner["repo"]
    task_id = stale_owner["task_id"]
    link = _read_json(stale_owner["link_path"])
    link["task_path"] = "tasks/2026-08-12/no-such-task"
    stale_owner["link_path"].write_text(
        __import__("json").dumps(link, ensure_ascii=False), encoding="utf-8"
    )
    # 身份文件缺失：state.json 仍在（task 仍进 candidates），但权威终态写
    # 因 identity 不可得而失败 → 诊断事件保留。
    (stale_owner["task_root"] / "work" / "run_workspace.json").unlink()

    remaining = unfinished_task_ids(home)

    assert task_id not in remaining  # 权威终态仍不驱动
    assert len(_stale_conflict_events(repo)) == 1  # 诊断事件保留（一次性）
    # 部分自愈仍生效：link 已置终态，下次 tick 不再进候选 → 诊断不重复
    assert _read_json(stale_owner["link_path"])["status"] == "cancelled"


def test_done_run_no_conflict_flood_across_ticks(tmp_path):
    """【红测】done 终态 + link active（持续任务轮间正常形态）重复 maintenance tick
    不产生 status_conflict 洪泛——隔离复现 testbox 42041 条根因。
    当前实现每 tick 对 done/failed 无条件写诊断（projected 恒 False）→ 本测试红。"""
    home = tmp_path / "home"
    repo = RuntimeRepository(home / "runtime.db")
    task_id = "audit-aggregate-capacity"
    repo.record_run_creation(
        owner_id="local/main",
        goal="持续审计",
        conversation_task_id=task_id,
        run_id=task_id,
        role="main",
    )
    run = repo.main_agent_run_for_task(task_id)
    repo.settle_agent_run(
        agent_run_id=run["agent_run_id"],
        status="done",
        attempt_id=run["current_attempt_id"],
        payload={"status": "done", "runtime_status": "ok"},
    )
    task_root = home / "tasks" / "2026-08-12" / "audit"
    (task_root / "work").mkdir(parents=True)
    state = {"status": "RUNNING", "task_id": task_id, "primary_run_id": task_id,
             "updated_at": _iso(1786517249.0)}
    (task_root / "work" / "state.json").write_text(
        __import__("json").dumps(state), encoding="utf-8"
    )
    link_dir = home / "workspace" / "runtime" / "workspaces" / "audit" / "conversations" / "tasks"
    link_dir.mkdir(parents=True)
    link = {"task_id": task_id, "status": "active",
            "task_path": "tasks/2026-08-12/audit", "thread_id": "t", "goal": "持续审计"}
    (link_dir / f"{task_id}.json").write_text(
        __import__("json").dumps(link), encoding="utf-8"
    )

    for _ in range(5):  # 5 个 maintenance tick（testbox 60s 间隔 × 5 分钟）
        unfinished_task_ids(home)

    # 正常轮间形态不是冲突：不写诊断（当前实现红：每 tick 一条）
    assert _stale_conflict_events(repo) == []
    # 链路不受影响：link 仍 active（生命周期在册），run 终态仍不驱动
    assert _read_json(link_dir / f"{task_id}.json")["status"] == "active"


def test_cancelled_projection_failure_bounded_across_ticks(stale_owner):
    """cancelled 投影失败诊断有界：link 终态化后不再进候选，
    多 tick 重复维护不产生无界 runtime_events（锁定既有收敛行为）。"""
    home = stale_owner["home"]
    repo = stale_owner["repo"]
    link = _read_json(stale_owner["link_path"])
    link["task_path"] = "tasks/2026-08-12/no-such-task"
    stale_owner["link_path"].write_text(
        __import__("json").dumps(link, ensure_ascii=False), encoding="utf-8"
    )
    (stale_owner["task_root"] / "work" / "run_workspace.json").unlink()

    for _ in range(3):
        unfinished_task_ids(home)

    assert len(_stale_conflict_events(repo)) == 1  # 有界：只有首次 tick 写


def test_terminal_link_replays_open_task_run_closeout_after_restart(tmp_path):
    """link 与代理树已终态但进程在 TaskRun CAS 前退出时，owner 发现层补账一次。"""
    home = tmp_path / "home"
    repo = RuntimeRepository(home / "runtime.db")
    task_id = "goal-terminal-before-task-run-close"
    rec = repo.record_run_creation(
        owner_id="local/main",
        goal="持续目标已完成",
        conversation_task_id=task_id,
        run_id=task_id,
        role="main",
    )
    repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="done",
        attempt_id=rec["attempt_id"],
    )
    link_dir = (
        home
        / "workspace"
        / "runtime"
        / "workspaces"
        / "goal"
        / "conversations"
        / "tasks"
    )
    link_dir.mkdir(parents=True)
    (link_dir / f"{task_id}.json").write_text(
        __import__("json").dumps(
            {
                "task_id": task_id,
                "status": "completed",
                "thread_id": "thread-goal",
                "goal": "持续目标已完成",
            }
        ),
        encoding="utf-8",
    )

    assert unfinished_task_ids(home) == []
    assert unfinished_task_ids(home) == []

    task_run = repo.get_task_run(rec["task_run_id"])
    assert task_run is not None
    assert task_run["status"] == "done"
    assert task_run["closed_at"] > 0
    events = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE task_run_id = ? "
        "AND event_type = 'task_run.closed'",
        (rec["task_run_id"],),
    ).fetchall()
    assert len(events) == 1


def test_conflicting_duplicate_link_statuses_do_not_close_task_run(tmp_path):
    """同 task 出现 active/completed 冲突投影时保持打开，不能猜哪份链接有权威。"""
    home = tmp_path / "home"
    repo = RuntimeRepository(home / "runtime.db")
    task_id = "conflicting-link-task"
    rec = repo.record_run_creation(
        owner_id="local/main",
        goal="冲突链接",
        conversation_task_id=task_id,
        run_id=task_id,
        role="main",
    )
    repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="done",
        attempt_id=rec["attempt_id"],
    )
    for workspace, status in (("one", "active"), ("two", "completed")):
        link_dir = (
            home
            / "workspace"
            / "runtime"
            / "workspaces"
            / workspace
            / "conversations"
            / "tasks"
        )
        link_dir.mkdir(parents=True)
        (link_dir / f"{task_id}.json").write_text(
            __import__("json").dumps(
                {
                    "task_id": task_id,
                    "status": status,
                    "thread_id": "thread-conflict",
                    "goal": "冲突链接",
                }
            ),
            encoding="utf-8",
        )

    unfinished_task_ids(home)

    task_run = repo.get_task_run(rec["task_run_id"])
    assert task_run is not None
    assert task_run["closed_at"] == 0
