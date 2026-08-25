"""P1 中途账本准:task_progress 读账前先跑派工路 coverage 对账——子代理 DONE 后模型
中途看账即真进度(covers 绑定项已按 id 打勾),不用等收口门。

契约:只在主代理语境、读的就是本 run 的账时对账;子代理读自己的账不沾;update 路不掺和;
对账失败读账照常(永不抛错由 reconcile 自身+外层双兜)。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.task_identity import (
    task_path_progress_ledger_id,
)
from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

_RUN_ID = "run-parent-1"


def _agent(home: Path, task_root: Path):
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(home)),
        root=str(home),
        _main_agent_run_id=_RUN_ID,
        _current_run_params=SimpleNamespace(
            run_id=_RUN_ID,
            task_id=_RUN_ID,
            source="cli_run",
            context_scope="default",
            task_attributes={"run_workspace": {"task_root": str(task_root)}},
        ),
    )


def _seed_coverage(home: Path, titles: list[str]) -> None:
    targets = [
        {
            "id": f"req-{index:02d}",
            "title": title,
            "status": "pending",
            "coverage_kind": "requirement_item",
            "source_ref": "auto:requirement-enumeration",
        }
        for index, title in enumerate(titles, start=1)
    ]
    write_task_progress(home, _RUN_ID, {"coverage": {"goal": "需求枚举项对账", "targets": targets}})


def _write_done_child(task_root: Path, child_id: str, covers: list[str]) -> None:
    agent_dir = task_root / "work" / "agents" / child_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": child_id,
        "id": child_id,
        "parent_id": _RUN_ID,
        "status": "DONE",
        "attributes": {"covers": covers},
    }
    (agent_dir / "canonical_state.json").write_text(json.dumps(payload), encoding="utf-8")


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    task_root = tmp_path / "task"
    home.mkdir()
    task_root.mkdir()
    return home, task_root


def _coverage_statuses(payload_text: str) -> dict[str, str]:
    payload = json.loads(payload_text)
    return {t["id"]: t["status"] for t in (payload.get("coverage") or {}).get("targets", [])}


def test_read_reconciles_covers_bindings_midrun(tmp_path):
    """子代理 DONE 后主代理中途读账:covers 绑定项当场按 id 打勾,不等收口门。"""
    home, task_root = _setup(tmp_path)
    _seed_coverage(home, ["注册登录", "全文搜索"])
    _write_done_child(task_root, "sub-a", covers=["req-01"])

    result = TaskProgressTool(_agent(home, task_root)).execute({"action": "read"})

    assert result.ok is True
    statuses = _coverage_statuses(result.output)
    assert statuses["req-01"] == "done"  # 中途读账即真进度
    assert statuses["req-02"] == "pending"
    # 账本落盘,不只是本次读的投影。
    persisted = read_task_progress(home, _RUN_ID)["coverage"]["targets"]
    assert {t["id"]: t["status"] for t in persisted}["req-01"] == "done"


def test_update_returns_canonical_done_child_state_in_same_call(tmp_path):
    """A stale model update cannot overwrite the exact child DONE projection in its own receipt."""
    home, task_root = _setup(tmp_path)
    write_task_progress(
        home,
        _RUN_ID,
        {
            "items": [
                {
                    "id": "sub-a",
                    "title": "子代理 A",
                    "status": "in_progress",
                }
            ]
        },
    )
    _write_done_child(task_root, "sub-a", covers=[])

    result = TaskProgressTool(_agent(home, task_root)).execute(
        {"action": "update", "items": [{"id": "sub-a", "status": "in_progress"}]}
    )

    payload = json.loads(result.output)
    assert result.ok is True
    assert payload["items"][0]["status"] == "done"


def test_read_in_subagent_context_does_not_touch_parent_ledger(tmp_path):
    """子代理线程读账(读的是自己的账)→ 不跑父账对账,父账一字不动。"""
    home, task_root = _setup(tmp_path)
    _seed_coverage(home, ["注册登录"])
    _write_done_child(task_root, "sub-a", covers=["req-01"])
    agent = _agent(home, task_root)
    agent._current_subagent_run_id = "sub-a"  # runner context: 当前线程在跑子代理

    result = TaskProgressTool(agent).execute({"action": "read"})

    assert result.ok is True
    persisted = read_task_progress(home, _RUN_ID)["coverage"]["targets"]
    assert {t["id"]: t["status"] for t in persisted}["req-01"] == "pending"


def test_read_explicit_other_run_id_does_not_reconcile(tmp_path):
    """显式 run_id 读别的账(比如查历史 run)→ 账本键与本 run 不同,不对账。"""
    home, task_root = _setup(tmp_path)
    other = "run-other-9"
    targets = [{"id": "req-01", "title": "注册登录", "status": "pending"}]
    write_task_progress(home, other, {"coverage": {"targets": targets}})
    _write_done_child(task_root, "sub-a", covers=["req-01"])

    result = TaskProgressTool(_agent(home, task_root)).execute({"action": "read", "run_id": other})

    assert result.ok is True
    persisted = read_task_progress(home, other)["coverage"]["targets"]
    assert {t["id"]: t["status"] for t in persisted}["req-01"] == "pending"


def test_read_explicit_current_task_id_uses_canonical_task_path_ledger(tmp_path):
    """后台恢复轮误传当前 task_id 时仍读原 Todo，不生成一份空的 request-id 视图。"""
    home, task_root = _setup(tmp_path)
    task_id = "gwreq-current-task"
    ledger_id = task_path_progress_ledger_id(task_root)
    write_task_progress(
        home,
        ledger_id,
        {"items": [{"id": "C1", "title": "实现核心功能", "status": "in_progress"}]},
    )
    store = SimpleNamespace(
        load_task_link=lambda selected: (
            SimpleNamespace(task_path=str(task_root)) if selected == task_id else None
        )
    )
    agent = SimpleNamespace(
        conversation_store=store,
        home_paths=SimpleNamespace(owner_home_dir=str(home)),
        root=str(home),
        _current_run_params=SimpleNamespace(
            run_id="bg-main-attempt",
            task_id=task_id,
            source="background_main_agent",
            context_scope="default",
            task_attributes={
                "conversation_thread_id": "thread-1",
                "conversation_task_id": task_id,
            },
        ),
    )

    result = TaskProgressTool(agent).execute({"action": "read", "run_id": task_id})

    payload = json.loads(result.output)
    assert result.ok is True
    assert payload["run_id"] == ledger_id
    assert [(item["id"], item["status"]) for item in payload["items"]] == [
        ("C1", "in_progress")
    ]


def test_read_without_run_params_still_reads(tmp_path):
    """没有 _current_run_params(极端/测试环境)→ 跳过对账,读账照常不抛。"""
    home, task_root = _setup(tmp_path)
    _seed_coverage(home, ["注册登录"])
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(home)),
        root=str(home),
        _main_agent_run_id=_RUN_ID,
    )

    result = TaskProgressTool(agent).execute({"action": "read"})

    assert result.ok is True
    assert _coverage_statuses(result.output)["req-01"] == "pending"
