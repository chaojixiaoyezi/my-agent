"""审计 #6 修复真测:TaskRegistry 状态写入终态保护 + CAS(防丢更新/迟到写把终态复活)。

真起 LocalStore(真 SQLite):终态(done/cancelled…)改不回非终态;failed 可重试;CAS 防并发/迟到写覆盖。
学 长期助手 VALID_TRANSITIONS 状态机 + 参考实现 owner-lease 任务层的"原子改、拒非法转移"思路。
"""

from __future__ import annotations

from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.task_registry.registry import _FINAL_TASK_STATUSES


def _reg(tmp_path):
    return LocalStore(tmp_path / "local.db").task_registry


def test_final_status_not_resurrected(tmp_path) -> None:
    reg = _reg(tmp_path)
    reg.register_task("t1", status="running", goal="g")
    assert reg.update_task_status("t1", "done") is True  # running → done OK
    assert reg.update_task_status("t1", "running") is False  # done → running 拒绝(终态不复活)
    assert reg.lookup_task("t1")["status"] == "done"  # 状态仍 done,没被改回


def test_failed_is_retryable_not_final(tmp_path) -> None:
    reg = _reg(tmp_path)
    reg.register_task("t2", status="running", goal="g")
    assert reg.update_task_status("t2", "failed") is True
    assert reg.update_task_status("t2", "running") is True  # failed → running 允许(可重试,非终态)
    assert reg.lookup_task("t2")["status"] == "running"


def test_cas_prevents_lost_update(tmp_path) -> None:
    reg = _reg(tmp_path)
    reg.register_task("t3", status="running", goal="g")
    # 迟到写:以为还是 pending,实际已是 running → CAS 不匹配,不覆盖(防丢更新)
    assert reg.update_task_status("t3", "blocked", expected_status="pending") is False
    assert reg.lookup_task("t3")["status"] == "running"
    assert reg.update_task_status("t3", "blocked", expected_status="running") is True  # 预期匹配 → 改
    assert reg.lookup_task("t3")["status"] == "blocked"


def test_terminal_to_terminal_allowed(tmp_path) -> None:
    reg = _reg(tmp_path)
    reg.register_task("t4", status="running", goal="g")
    reg.update_task_status("t4", "done")
    assert reg.update_task_status("t4", "cancelled") is True  # 终态→终态(非复活)允许
    assert reg.lookup_task("t4")["status"] == "cancelled"


# ---------------------------------------------------------------- P0-1 register_task 终态守卫(HANDOFF 文档线)

def test_register_task_rejects_resurrecting_terminal(tmp_path) -> None:
    """P0-1: 终态任务再 register 非终态 -> 抛 ValueError, 状态不复活。"""
    reg = _reg(tmp_path)
    reg.register_task("p0-1", status="running", goal="g")
    reg.update_task_status("p0-1", "done")
    try:
        reg.register_task("p0-1", status="running", goal="复活")
    except ValueError:
        pass
    else:
        raise AssertionError("终态任务 register 非终态应抛 ValueError")
    assert reg.lookup_task("p0-1")["status"] == "done"  # 状态仍终态, 没被复活


def test_register_task_terminal_idempotent(tmp_path) -> None:
    """P0-1: 终态任务再 register 同族终态 -> 允许(终态幂等确认, 非复活)。"""
    reg = _reg(tmp_path)
    reg.register_task("p0-2", status="running", goal="g")
    reg.update_task_status("p0-2", "done")
    reg.register_task("p0-2", status="done", goal="确认")  # 不抛
    assert reg.lookup_task("p0-2")["status"] == "done"


def test_register_task_overwrites_non_terminal(tmp_path) -> None:
    """P0-1: 非终态任务 register 覆盖 -> 正常(register 语义不变)。"""
    reg = _reg(tmp_path)
    reg.register_task("p0-3", status="pending", goal="g")
    reg.register_task("p0-3", status="running", goal="g2")  # 非终态覆盖正常
    assert reg.lookup_task("p0-3")["status"] == "running"


def test_register_task_new_task_normal(tmp_path) -> None:
    """P0-1: 新任务 register -> 正常创建(回归)。"""
    reg = _reg(tmp_path)
    reg.register_task("p0-4", status="pending", goal="g")
    assert reg.lookup_task("p0-4")["status"] == "pending"


# ---------------------------------------------------------------- P0-1 收口(seq1562): 原子条件 UPSERT 交错测试
# 新实现无「先 SELECT 再 UPDATE」步骤——单条 UPSERT 在 SQLite 中天然原子,
# TOCTOU 窗口不存在。下面两条真实 SQLite 连接验证: 已提交终态对「迟到
# UPSERT 非终态」免疫(WHERE 拒绝), 与连接交错顺序无关。

def _terminal_marks() -> str:
    return ",".join("?" for _ in sorted(_FINAL_TASK_STATUSES))


def test_upsert_atomic_rejects_late_write_over_terminal(tmp_path) -> None:
    """连接 B 已提交终态后, 连接 A 迟到 UPSERT 非终态被 WHERE 拒绝。"""
    import sqlite3

    db_path = tmp_path / "interleave.db"
    conn_a = sqlite3.connect(db_path)
    conn_b = sqlite3.connect(db_path)
    conn_a.execute(
        """CREATE TABLE task_registry (
            task_id TEXT PRIMARY KEY, session_id TEXT, user_id TEXT,
            status TEXT, goal TEXT, created_at REAL, updated_at REAL)"""
    )
    conn_a.execute(
        "INSERT INTO task_registry VALUES ('t', '', '', 'running', 'g', 1, 1)"
    )
    conn_a.commit()

    conn_b.execute("UPDATE task_registry SET status='done', updated_at=2 WHERE task_id='t'")
    conn_b.commit()  # B 连接先提交终态

    marks = _terminal_marks()
    cursor = conn_a.execute(
        f"""INSERT INTO task_registry (task_id, session_id, user_id, status, goal, created_at, updated_at)
            VALUES ('t', '', '', 'running', 'g', 1, 2)
            ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
            WHERE excluded.status IN ({marks}) OR status NOT IN ({marks})""",
        (*sorted(_FINAL_TASK_STATUSES), *sorted(_FINAL_TASK_STATUSES)),
    )
    conn_a.commit()
    assert cursor.rowcount == 0  # WHERE 拒绝(当前 done 终态 + 新值 running 非终态)
    final = conn_b.execute("SELECT status FROM task_registry WHERE task_id='t'").fetchone()
    assert final[0] == "done"  # 终态未被迟到写覆盖


def test_upsert_atomic_rejects_insert_after_late_terminal(tmp_path) -> None:
    """连接 B 先插入终态后, 连接 A 迟到 UPSERT 非终态被拒绝(不复活)。"""
    import sqlite3

    db_path = tmp_path / "interleave2.db"
    conn_a = sqlite3.connect(db_path)
    conn_b = sqlite3.connect(db_path)
    conn_a.execute(
        """CREATE TABLE task_registry (
            task_id TEXT PRIMARY KEY, session_id TEXT, user_id TEXT,
            status TEXT, goal TEXT, created_at REAL, updated_at REAL)"""
    )
    conn_a.commit()

    conn_b.execute(
        "INSERT INTO task_registry VALUES ('t', '', '', 'cancelled', 'g', 1, 1)"
    )
    conn_b.commit()  # B 先插入终态

    marks = _terminal_marks()
    cursor = conn_a.execute(
        f"""INSERT INTO task_registry (task_id, session_id, user_id, status, goal, created_at, updated_at)
            VALUES ('t', '', '', 'pending', 'g', 1, 2)
            ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
            WHERE excluded.status IN ({marks}) OR status NOT IN ({marks})""",
        (*sorted(_FINAL_TASK_STATUSES), *sorted(_FINAL_TASK_STATUSES)),
    )
    conn_a.commit()
    assert cursor.rowcount == 0  # 冲突且 WHERE 拒绝(当前 cancelled 终态)
    final = conn_b.execute("SELECT status FROM task_registry WHERE task_id='t'").fetchone()
    assert final[0] == "cancelled"  # 迟到 pending 未复活


# ---------------------------------------------------------------- P0-1 收口(seq1584/1585): 生产入口交错测试

def test_production_entry_late_register_rejected_after_terminal(tmp_path) -> None:
    """走生产入口 TaskRegistry.register_task: B 已提交终态后, A 迟到
    register 非终态被拒(抛 ValueError), 覆盖生产入口/连接配置/异常路径。"""
    reg_a = _reg(tmp_path)
    reg_b = _reg(tmp_path)
    reg_a.register_task("prod-1", status="pending", goal="g")
    assert reg_b.update_task_status("prod-1", "done") is True  # B 提交终态
    try:
        reg_a.register_task("prod-1", status="running", goal="迟到")  # A 迟到写
    except ValueError:
        pass
    else:
        raise AssertionError("生产入口迟到 register 非终态应抛 ValueError")
    assert reg_b.lookup_task("prod-1")["status"] == "done"  # 终态未被覆盖


def test_production_entry_terminal_idempotent_via_second_instance(tmp_path) -> None:
    """走生产入口: 终态任务再 register 同族终态(另一实例) -> 允许, 不抛。"""
    reg_a = _reg(tmp_path)
    reg_b = _reg(tmp_path)
    reg_a.register_task("prod-2", status="running", goal="g")
    reg_a.update_task_status("prod-2", "done")
    reg_b.register_task("prod-2", status="done", goal="确认")  # 幂等终态, 不抛
    assert reg_b.lookup_task("prod-2")["status"] == "done"


# ---------------------------------------------------------------- P0-1 收口(seq1587/1588): 第二类生产入口竞态

def test_production_entry_late_register_after_external_terminal_insert(tmp_path) -> None:
    """第二类竞态走生产入口: 另一连接(raw SQL)先插入终态, 生产入口
    register_task 迟到写非终态被拒——「无行→迟到终态」由生产入口覆盖。"""
    import sqlite3

    reg = _reg(tmp_path)
    db_path = tmp_path / "local.db"
    conn_b = sqlite3.connect(db_path)  # 另一条真实连接
    conn_b.execute(
        "INSERT INTO task_registry (task_id, session_id, user_id, status, goal, created_at, updated_at) "
        "VALUES ('prod-3', '', '', 'cancelled', 'g', 1, 1)"
    )
    conn_b.commit()  # B 先插入终态(生产入口从未见过该行)
    try:
        reg.register_task("prod-3", status="pending", goal="迟到")  # A 生产入口迟到写
    except ValueError:
        pass
    else:
        raise AssertionError("无行视图下迟到 register 非终态应抛 ValueError")
    assert reg.lookup_task("prod-3")["status"] == "cancelled"  # 终态未被复活
