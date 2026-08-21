from __future__ import annotations

"""RUN-01 回归：崩溃悬挂 attempt 按进程死亡证明归 unknown（2026-08-15 C1 真机发现）。

覆盖四象限：
- 死 pid（ProcessLookupError）→ 归 unknown（死亡证明）
- 活 pid + start_time 匹配 → 保持（同进程存活，fail-closed）
- 无 pid 记录（旧数据 metadata={}）→ 保持（fail-closed 不猜）
- 已终态 attempt → 不动
"""

import json
import os
import sqlite3
import time
import uuid

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def _make_repo(tmp_path) -> RuntimeRepository:
    db_path = tmp_path / f"runtime-{uuid.uuid4().hex[:8]}.db"
    repo = RuntimeRepository(str(db_path))
    return repo


def _seed_run(
    repo: RuntimeRepository, *, status: str, metadata: dict | None, attempt_status: str = "running"
) -> str:
    """种一条 agent_run + attempt，返回 agent_run_id。"""
    run_id = f"run-{uuid.uuid4().hex}"
    agent_run_id = f"agentrun-{uuid.uuid4().hex}"
    task_run_id = f"taskrun-{uuid.uuid4().hex}"
    attempt_id = f"attempt-{uuid.uuid4().hex}"
    now = time.time()
    with repo._runtime_connection() as conn:
        conn.execute(
            "INSERT INTO task_runs(task_run_id, task_id, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (task_run_id, run_id, "created", now, now),
        )
        conn.execute(
            "INSERT INTO agent_runs(agent_run_id, task_run_id, run_id, role, status, current_attempt_id, "
            "current_attempt_generation, workspace_epoch, created_at, updated_at) VALUES(?,?,?,?,?,?,1,1,?,?)",
            (agent_run_id, task_run_id, run_id, "main", status, attempt_id, now, now),
        )
        conn.execute(
            "INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation, status, started_at, metadata_json) "
            "VALUES(?,?,1,?,?,?)",
            (attempt_id, agent_run_id, attempt_status, now, json.dumps(metadata or {})),
        )
        conn.commit()
    return agent_run_id


# LLM: 死 pid 是唯一明确死亡证明（ProcessLookupError），必须归 unknown。
# 函数用途: 验证崩溃进程（pid 查无）的悬挂 attempt 被调和为 unknown。
def test_dead_pid_recovered_to_unknown(tmp_path):
    repo = _make_repo(tmp_path)
    # 用"几乎不可能存活"的 pid；若本机恰好存在则跳过（避免误杀真实进程）
    dead_pid = 999999999
    try:
        os.kill(dead_pid, 0)
        import pytest
        pytest.skip("pid 竟然存活，跳过")
    except ProcessLookupError:
        pass
    agent_run_id = _seed_run(repo, status="created", metadata={"runner_pid": dead_pid, "runner_start_time": None})
    recovered = repo.recover_stale_attempts()
    assert recovered == [agent_run_id]
    with repo._runtime_connection() as conn:
        row = conn.execute("SELECT status FROM agent_runs WHERE agent_run_id=?", (agent_run_id,)).fetchone()
        assert row["status"] == "unknown"
        attempt = conn.execute(
            "SELECT status, ended_at FROM agent_attempts WHERE agent_run_id=?",
            (agent_run_id,),
        ).fetchone()
        assert attempt["status"] == "unknown"
        assert attempt["ended_at"] > 0


def test_stale_main_run_blocks_auto_mount_until_explicit_recovery(tmp_path):
    """崩溃归 unknown 后暴露结构化暂停；人工恢复同时复原 run 并允许新 attempt。"""
    repo = _make_repo(tmp_path)
    task_id = "task-stale-main"
    rec = repo.record_run_creation(
        owner_id="local/main",
        goal="继续旧任务",
        conversation_task_id=task_id,
        thread_id="thread-stale-main",
        run_id="run-stale-main",
        role="main",
    )
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_attempts SET metadata_json = ? WHERE attempt_id = ?",
            (
                json.dumps({"runner_pid": 999999999, "runner_start_time": None}),
                rec["attempt_id"],
            ),
        )

    assert repo.recover_stale_attempts() == [rec["agent_run_id"]]
    assert repo.main_agent_recovery_block_for_task(task_id) == {
        "schema_version": "main-agent-recovery-block.v1",
        "reason": "unknown_run_status",
        "task_id": task_id,
        "agent_run_id": rec["agent_run_id"],
        "attempt_id": rec["attempt_id"],
        "run_status": "unknown",
        "attempt_status": "unknown",
    }

    recovered = repo.recover_attempt_unknown(
        rec["attempt_id"],
        operator="human-checker",
        effect_disposition="confirmed_noop",
        reason="已核对没有遗留副作用",
    )

    assert recovered["recovered"] is True
    assert recovered["run_status"] == "created"
    assert repo.main_agent_recovery_block_for_task(task_id) is None
    next_attempt = repo.create_attempt(rec["agent_run_id"])
    assert next_attempt["attempt_id"] != rec["attempt_id"]


# LLM: 活 pid（本进程）且 start_time 匹配 = 同进程存活，fail-closed 保持原态。
# 函数用途: 验证并发运行中的真实 attempt 不会被误调和。
def test_alive_pid_with_matching_start_time_kept(tmp_path):
    repo = _make_repo(tmp_path)
    from agent_py_agent.agent.scheduler.repository import _process_start_time

    agent_run_id = _seed_run(
        repo,
        status="running",
        metadata={"runner_pid": os.getpid(), "runner_start_time": _process_start_time(os.getpid())},
    )
    assert repo.recover_stale_attempts() == []
    with repo._runtime_connection() as conn:
        row = conn.execute("SELECT status FROM agent_runs WHERE agent_run_id=?", (agent_run_id,)).fetchone()
        assert row["status"] == "running"


# LLM: 旧数据无 pid 身份记录（修复前形态）→ 无法证实死亡，保持原态（不猜）。
# 函数用途: 验证无身份的旧悬挂 attempt 保持 running，不会被误回收。
def test_no_pid_metadata_kept(tmp_path):
    repo = _make_repo(tmp_path)
    agent_run_id = _seed_run(repo, status="created", metadata={})
    assert repo.recover_stale_attempts() == []
    with repo._runtime_connection() as conn:
        row = conn.execute("SELECT status FROM agent_runs WHERE agent_run_id=?", (agent_run_id,)).fetchone()
        assert row["status"] == "created"


# LLM: 已终态 attempt 不在调和范围（JOIN 只取 running/created）。
# 函数用途: 验证 done attempt 不动。
def test_terminal_attempt_untouched(tmp_path):
    repo = _make_repo(tmp_path)
    agent_run_id = _seed_run(
        repo, status="done", metadata={"runner_pid": 999999999, "runner_start_time": None}, attempt_status="done"
    )
    assert repo.recover_stale_attempts() == []
    with repo._runtime_connection() as conn:
        row = conn.execute("SELECT status FROM agent_runs WHERE agent_run_id=?", (agent_run_id,)).fetchone()
        assert row["status"] == "done"


# LLM: 新 attempt 创建必须落 runner 身份（修复核心），供后续崩溃调和使用。
# 函数用途: 验证 create_attempt/record_run_creation 路径写入 pid 元数据。
def test_new_attempt_records_runner_identity(tmp_path):
    repo = _make_repo(tmp_path)
    run_id = f"run-{uuid.uuid4().hex}"
    agent_run_id = f"agentrun-{uuid.uuid4().hex}"
    task_run_id = f"taskrun-{uuid.uuid4().hex}"
    now = time.time()
    with repo._runtime_connection() as conn:
        conn.execute(
            "INSERT INTO task_runs(task_run_id, task_id, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (task_run_id, run_id, "created", now, now),
        )
        conn.execute(
            "INSERT INTO agent_runs(agent_run_id, task_run_id, run_id, role, status, current_attempt_id, "
            "current_attempt_generation, workspace_epoch, created_at, updated_at) VALUES(?,?,?,?,?,?,0,1,?,?)",
            (agent_run_id, task_run_id, run_id, "main", "created", "", now, now),
        )
        conn.commit()
    repo.create_attempt(agent_run_id)
    with repo._runtime_connection() as conn:
        row = conn.execute(
            "SELECT metadata_json FROM agent_attempts WHERE agent_run_id=?",
            (agent_run_id,),
        ).fetchone()
        meta = json.loads(row["metadata_json"])
        assert int(meta["runner_pid"]) == os.getpid()


# LLM: SANDBOX-01(2026-08-15 真机): 任务收口必须把 work/.sandbox-tmp 产物发布到
# output/.sandbox-tmp/ 并记录 timeline 事实, 否则"模型视角完成、用户视角产物消失"。
# 函数用途: 验证收口发布函数复制产物且写 timeline 事件。
def test_publish_sandbox_tmp_outputs(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        _publish_sandbox_tmp_outputs,
    )

    root = tmp_path / "task"
    work = root / "work"
    output = root / "output"
    sandbox_tmp = work / ".sandbox-tmp"
    (sandbox_tmp / "dsh-stability" / "proj").mkdir(parents=True)
    (sandbox_tmp / "dsh-stability" / "proj" / "main.py").write_text("print(1)", encoding="utf-8")
    (sandbox_tmp / "keep.txt").write_text("keep", encoding="utf-8")

    published = _publish_sandbox_tmp_outputs(root, run_id="run-test")

    assert len(published) == 2
    assert (output / ".sandbox-tmp" / "dsh-stability" / "proj" / "main.py").is_file()
    assert (output / ".sandbox-tmp" / "keep.txt").is_file()
    timeline = (work / "timeline.jsonl").read_text(encoding="utf-8")
    assert "sandbox_tmp_published" in timeline


# LLM: SANDBOX-01 适配——tmp 根在任务根(working_dir 未指定时的沙箱 workspace)时也发布。
# 函数用途: 验证任务根/.sandbox-tmp 的产物被收口发布。
def test_publish_sandbox_tmp_at_task_root(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        _publish_sandbox_tmp_outputs,
    )

    root = tmp_path / "task"
    (root / "work").mkdir(parents=True)
    (root / "output").mkdir(parents=True)
    sandbox_tmp = root / ".sandbox-tmp"
    sandbox_tmp.mkdir()
    (sandbox_tmp / "notes.txt").write_text("hi", encoding="utf-8")

    published = _publish_sandbox_tmp_outputs(root, run_id="run-test")

    assert published == ["notes.txt"]
    assert (root / "output" / ".sandbox-tmp" / "notes.txt").is_file()


# LLM: 无 .sandbox-tmp 时发布为空且不产生 timeline 噪音。
# 函数用途: 验证空发布路径幂等无副作用。
def test_publish_sandbox_tmp_empty_noop(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        _publish_sandbox_tmp_outputs,
    )

    root = tmp_path / "task"
    (root / "work").mkdir(parents=True)
    assert _publish_sandbox_tmp_outputs(root, run_id="run-test") == []
    assert not (root / "work" / "timeline.jsonl").exists()
