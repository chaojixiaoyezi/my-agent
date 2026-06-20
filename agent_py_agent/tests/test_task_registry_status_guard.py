"""审计 #6 修复真测:TaskRegistry 状态写入终态保护 + CAS(防丢更新/迟到写把终态复活)。

真起 LocalStore(真 SQLite):终态(done/cancelled…)改不回非终态;failed 可重试;CAS 防并发/迟到写覆盖。
学 长期助手 VALID_TRANSITIONS 状态机 + claw owner-lease 任务层的"原子改、拒非法转移"思路。
"""

from __future__ import annotations

from agent_py_agent.agent.local_storage import LocalStore


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
