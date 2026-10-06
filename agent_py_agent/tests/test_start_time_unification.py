# LLM: starttime2 的回归锚点——启动指纹"读取统一 + 双口径比较"（schedstart 修正 + starttime2 统一）。
#   读取统一走 common.heartbeat.process_start_time（Linux /proc ticks、macOS ps lstart）；比较走
#   heartbeat.start_time_matches：旧数字记录与新字符串指纹都可核验，核验不了不判死（宁多等 lease）。
#   本文件覆盖：ps 返回码/超时/空输出口径、/proc 解析（含 comm 空格）、比较矩阵、tool_operations 判活三态。
# 模块用途: 钉住跨平台启动指纹读取与判活语义，防止 PID 复用误判与"格式差异当死亡证据"误杀活进程。
from __future__ import annotations

import os
import subprocess
import time

import pytest

from agent_py_agent.agent.common import heartbeat as hb
from agent_py_agent.agent.common.heartbeat import process_start_time, start_time_matches
from agent_py_agent.agent.local_storage import tool_operations
from agent_py_agent.agent.local_storage.tool_operations import (
    ToolOperationRecord,
    _operation_holder_is_live,
    _reconciliation_marker_is_live,
)
from agent_py_agent.agent.runtime_db.operations import holder_is_alive

_FAKE_PID = 4_000_000_000  # 不存在的 pid：/proc 与真实 ps 都读不到，保证走打桩路径


class _CompletedPs:
    """subprocess.run 的替身：只需要 returncode 与 stdout 两个字段。"""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


# ---------------------------------------------------------------- heartbeat：ps 读取（macOS 等无 /proc 平台）


def test_ps_success_returns_fingerprint(monkeypatch):
    """fake ps 返回码 0 且有输出：指纹可用。"""
    monkeypatch.setattr(
        hb.subprocess, "run", lambda *_a, **_k: _CompletedPs(0, "Fri Oct  3 09:00:00 2026\n")
    )
    assert process_start_time(_FAKE_PID) == "Fri Oct  3 09:00:00 2026"


def test_ps_nonzero_returncode_is_unverifiable(monkeypatch):
    """fake ps 返回码非 0：即使 stdout 有内容也不可核验（返回 None）——初审阻塞 2 的钉点。"""
    monkeypatch.setattr(
        hb.subprocess, "run", lambda *_a, **_k: _CompletedPs(1, "ps: No such process\n")
    )
    assert process_start_time(_FAKE_PID) is None


def test_ps_timeout_is_unverifiable(monkeypatch):
    """fake ps 超时：不可核验（返回 None）。"""

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=5)

    monkeypatch.setattr(hb.subprocess, "run", _timeout)
    assert process_start_time(_FAKE_PID) is None


def test_ps_empty_output_is_unverifiable(monkeypatch):
    """fake ps 返回码 0 但输出为空：不可核验（返回 None）。"""
    monkeypatch.setattr(hb.subprocess, "run", lambda *_a, **_k: _CompletedPs(0, ""))
    assert process_start_time(_FAKE_PID) is None


# ---------------------------------------------------------------- heartbeat：/proc 解析（Linux 读取）


def test_proc_starttime_parsed_from_tail_after_comm(tmp_path):
    """Linux /proc/<pid>/stat：从最后一个 `)` 之后数第 20 个字段取 starttime。"""
    stat = tmp_path / "stat"
    stat.write_text(
        "1234 (python3) S 1 1234 1234 0 -1 4194304 100 0 0 0 1 2 0 0 20 0 1 0 424242 0 0\n",
        encoding="utf-8",
    )
    assert hb._read_proc_starttime(str(stat)) == "424242"


def test_proc_starttime_handles_comm_with_spaces(tmp_path):
    """comm 含空格时（`(my app)`）仍按最后一个 `)` 切分，字段不错位。"""
    stat = tmp_path / "stat"
    stat.write_text(
        "1234 (my app) S 1 1234 1234 0 -1 4194304 100 0 0 0 1 2 0 0 20 0 1 0 777777 0 0\n",
        encoding="utf-8",
    )
    assert hb._read_proc_starttime(str(stat)) == "777777"


def test_proc_starttime_missing_fields_returns_none(tmp_path):
    """stat 字段不足（损坏数据）：返回 None（不可核验），不猜。"""
    stat = tmp_path / "stat"
    stat.write_text("1234 (python3) S 1 2\n", encoding="utf-8")
    assert hb._read_proc_starttime(str(stat)) is None


# ---------------------------------------------------------------- heartbeat：新旧指纹比较矩阵


@pytest.mark.parametrize(
    "recorded,current,expected",
    [
        ("123", "123", True),  # 新格式字符串：相同
        ("123", "124", False),  # 新格式字符串：不同 → 判死
        (123.0, "123", True),  # 旧数字记录 vs 新字符串指纹：数值匹配 → 活
        (123, "123", True),
        (123.0, "124", False),  # 旧数字记录 vs 不同字符串 → 判死
        ("Fri Oct  3 09:00:00 2026", "Fri Oct  3 09:00:00 2026", True),  # macOS lstart 相同
        ("Fri Oct  3 09:00:00 2026", "Sat Oct  4 10:00:00 2026", False),  # macOS lstart 不同 → 判死
        (123.0, "Fri Oct  3 09:00:00 2026", True),  # 跨表示不可核验 → 不判死
        (123.0, None, True),  # 当前读不到 → 不判死
        (None, "123", True),  # 记录缺失 → 不判死
        (float("nan"), "123", True),  # 非有限值 → 不判死
    ],
)
def test_start_time_matches_matrix(recorded, current, expected):
    assert start_time_matches(recorded, current) is expected


# ---------------------------------------------------------------- tool_operations：持有者判活三态


def _record_with_token(pid: int, token: str) -> ToolOperationRecord:
    return ToolOperationRecord(
        owner_id="owner-a",
        run_id="run-1",
        task_id="run-1",
        operation_id="op-1",
        tool="t",
        args_hash="h",
        idempotency_key="k",
        idempotency_scope="operation",
        idempotency_namespace="t",
        status="RUNNING",
        holder_id="holder-1",
        holder_host="h",
        holder_pid=pid,
        holder_process_start_token=token,
        generation=1,
        lease_expires_at=time.time() + 3600.0,
    )


@pytest.fixture
def _local_holder(monkeypatch):
    monkeypatch.setattr(tool_operations, "tool_operation_host_id", lambda: "h")


def test_holder_live_when_fingerprint_matches(monkeypatch, _local_holder):
    """同一进程启动指纹一致 → 活。"""
    monkeypatch.setattr(tool_operations, "process_start_time", lambda _pid: "999")
    assert _operation_holder_is_live(_record_with_token(os.getpid(), "999"), time.time()) is True


def test_holder_dead_when_fingerprint_mismatches(monkeypatch, _local_holder):
    """PID 复用（启动指纹不同）→ 死，可接管。"""
    monkeypatch.setattr(tool_operations, "process_start_time", lambda _pid: "1000")
    assert _operation_holder_is_live(_record_with_token(os.getpid(), "999"), time.time()) is False


def test_holder_conservatively_live_when_fingerprint_unreadable(monkeypatch, _local_holder):
    """读不到启动指纹（如沙箱禁 ps）→ 保持原语义：保守判活，不接管。"""
    monkeypatch.setattr(tool_operations, "process_start_time", lambda _pid: None)
    assert _operation_holder_is_live(_record_with_token(os.getpid(), "999"), time.time()) is True


def test_holder_conservatively_live_when_token_missing(monkeypatch, _local_holder):
    """旧记录 token 为空 → 不看指纹（空值=不可核验），保守判活。"""
    monkeypatch.setattr(tool_operations, "process_start_time", lambda _pid: "anything")
    assert _operation_holder_is_live(_record_with_token(os.getpid(), ""), time.time()) is True


# ---------------------------------------------------------------- runtime_db 读端：holder_is_alive（锁接管判死）


def test_holder_is_alive_reads_fingerprint_consistently(monkeypatch):
    """读端与写端同源：指纹一致 → 活；不同 → 死（PID 复用）。"""
    monkeypatch.setattr(
        "agent_py_agent.agent.runtime_db.operations.process_start_time", lambda _pid: "999"
    )
    assert holder_is_alive(os.getpid(), "999") is True
    assert holder_is_alive(os.getpid(), "1000") is False


def test_holder_is_alive_conservative_when_unreadable(monkeypatch):
    """读不到指纹（如沙箱禁 ps / 无 /proc）→ 保守判活，不接管。"""
    monkeypatch.setattr(
        "agent_py_agent.agent.runtime_db.operations.process_start_time", lambda _pid: None
    )
    assert holder_is_alive(os.getpid(), "999") is True


def test_holder_is_alive_conservative_for_legacy_empty_token(monkeypatch):
    """旧记录指纹为空 → 不比较、保守判活（存量迁移口径）。"""
    monkeypatch.setattr(
        "agent_py_agent.agent.runtime_db.operations.process_start_time", lambda _pid: "anything"
    )
    assert holder_is_alive(os.getpid(), "") is True


def test_holder_is_alive_detects_pid_reuse_on_macos_lstart(monkeypatch):
    """macOS：lstart 指纹不同 → 判死（此前读不到 /proc 恒判活，本次起可核对）。"""
    monkeypatch.setattr(
        "agent_py_agent.agent.runtime_db.operations.process_start_time",
        lambda _pid: "Sat Oct  4 10:00:00 2026",
    )
    assert holder_is_alive(os.getpid(), "Fri Oct  3 09:00:00 2026") is False
    assert holder_is_alive(os.getpid(), "Sat Oct  4 10:00:00 2026") is True


# ---------------------------------------------------------------- 锁写读同源（starttime3）


def test_start_token_uses_heartbeat_reader(monkeypatch):
    """写端必须走 heartbeat 读取（打桩它即生效），不再自解析 /proc。"""
    from agent_py_agent.agent.runtime_db import repository as rt_repo

    monkeypatch.setattr(rt_repo, "_proc_start_time", lambda _pid: "777777")
    assert rt_repo.RuntimeRepository._start_token(None) == "777777"


def test_lock_write_and_read_share_fingerprint(monkeypatch):
    """锁写端 _start_token 与读端 holder_is_alive 同源：写端值直接可被读端核验；指纹不同判死。"""
    from agent_py_agent.agent.runtime_db import repository as rt_repo

    monkeypatch.setattr(rt_repo, "_proc_start_time", lambda _pid: "12345")
    token = rt_repo.RuntimeRepository._start_token(None)
    assert token == "12345"
    monkeypatch.setattr(
        "agent_py_agent.agent.runtime_db.operations.process_start_time", lambda _pid: "12345"
    )
    assert holder_is_alive(os.getpid(), token) is True
    monkeypatch.setattr(
        "agent_py_agent.agent.runtime_db.operations.process_start_time", lambda _pid: "99999"
    )
    assert holder_is_alive(os.getpid(), token) is False


def test_lock_macos_lstart_write_then_pid_reuse_detected(monkeypatch):
    """macOS：写端写入 lstart 指纹后读端能识别 pid 复用（此前写端恒空、读端恒判活）。"""
    from agent_py_agent.agent.runtime_db import repository as rt_repo

    lstart = "Fri Oct  3 09:00:00 2026"
    monkeypatch.setattr(rt_repo, "_proc_start_time", lambda _pid: lstart)
    token = rt_repo.RuntimeRepository._start_token(None)
    assert token == lstart
    monkeypatch.setattr(
        "agent_py_agent.agent.runtime_db.operations.process_start_time",
        lambda _pid: "Sat Oct  4 10:00:00 2026",
    )
    assert holder_is_alive(os.getpid(), token) is False
    monkeypatch.setattr(
        "agent_py_agent.agent.runtime_db.operations.process_start_time", lambda _pid: lstart
    )
    assert holder_is_alive(os.getpid(), token) is True


def test_lock_start_token_empty_when_unreadable(monkeypatch):
    """写端读不到指纹时写空串（不可核验，读端保守判活），不猜。"""
    from agent_py_agent.agent.runtime_db import repository as rt_repo

    monkeypatch.setattr(rt_repo, "_proc_start_time", lambda _pid: None)
    assert rt_repo.RuntimeRepository._start_token(None) == ""
    assert holder_is_alive(os.getpid(), "") is True


# ---------------------------------------------------------------- 旧数字 token 兼容（starttime3）


def test_holder_conservatively_live_for_legacy_numeric_token(monkeypatch, _local_holder):
    """旧数字 token（"12345.0"）与字符串指纹（"12345"）数值等价 → 不判死。"""
    monkeypatch.setattr(tool_operations, "process_start_time", lambda _pid: "12345")
    record = _record_with_token(os.getpid(), "12345.0")
    assert _operation_holder_is_live(record, time.time()) is True


def test_holder_dead_for_legacy_numeric_token_mismatch(monkeypatch, _local_holder):
    """旧数字 token 与不同指纹 → 判死（兼容不等于全放行）。"""
    monkeypatch.setattr(tool_operations, "process_start_time", lambda _pid: "99999")
    record = _record_with_token(os.getpid(), "12345.0")
    assert _operation_holder_is_live(record, time.time()) is False


def test_reconciliation_marker_conservatively_live_for_legacy_numeric_token(monkeypatch, _local_holder):
    """核对标记同口径：旧数字 token 数值等价 → 不判死。"""
    monkeypatch.setattr(tool_operations, "process_start_time", lambda _pid: "12345")
    marker = {
        "lease_expires_at": time.time() + 3600.0,
        "holder": {
            "holder_id": "h1",
            "host": "h",
            "pid": os.getpid(),
            "process_start_token": "12345.0",
        },
    }
    assert _reconciliation_marker_is_live(marker, time.time()) is True


# 3a 10-05 收口（ds6 初审发现）：三态比较只在“同格式且确证不同”时判 different；格式不可比、空串、坏值都是 unverifiable。
LSTART = "Mon Oct  5 10:00:00 2026"


@pytest.mark.parametrize(
    ("recorded", "current", "expected"),
    [
        ("12345", "12345", "same"),
        ("12345.0", "12345", "same"),
        (12345, "12345", "same"),
        ("12345", "12346", "different"),
        (LSTART, LSTART, "same"),
        (LSTART, "Mon Oct  5 10:00:01 2026", "different"),
        ("12345", LSTART, "unverifiable"),
        (LSTART, "12345", "unverifiable"),
        (12345, LSTART, "unverifiable"),
        ("", "12345", "unverifiable"),
        ("   ", LSTART, "unverifiable"),
        (None, "12345", "unverifiable"),
        ("12345", None, "unverifiable"),
        ("nan", "12345", "unverifiable"),
        (float("inf"), "12345", "unverifiable"),
        (True, "1", "unverifiable"),
    ],
)
def test_start_time_relation_matrix(recorded, current, expected):
    assert hb.start_time_relation(recorded, current) == expected
    assert start_time_matches(recorded, current) is (expected != "different")


@pytest.mark.parametrize(
    ("recorded", "current", "expected"),
    [("12345", LSTART, "unverifiable"), ("", LSTART, "unverifiable"), ("12345.0", "12345", "alive"),
     (LSTART, LSTART, "alive"), (LSTART, "Mon Oct  5 10:00:01 2026", "dead")],
)
def test_scheduler_runner_liveness_uses_shared_relation(monkeypatch, recorded, current, expected):
    from agent_py_agent.agent.scheduler import repository as sched_repo

    monkeypatch.setattr(sched_repo, "_process_state", lambda pid: "alive")
    monkeypatch.setattr(sched_repo, "process_start_time", lambda pid: current)
    assert sched_repo._runner_liveness({"runner_pid": 4242, "runner_start_time": recorded}) == expected
