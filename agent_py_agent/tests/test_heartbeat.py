"""通用心跳/存活检测单测:进程探活 + start_time 防 PID 复用 + is_dead 看门狗判据。"""

import os
import time

from agent.common.heartbeat import (
    heartbeat_record,
    is_dead,
    process_alive,
    process_start_time,
)


def test_current_process_is_alive():
    assert process_alive(os.getpid()) is True


def test_nonexistent_pid_not_alive():
    assert process_alive(0) is False
    assert process_alive(2_000_000_000) is False  # 几乎不可能存在的大 PID


def test_start_time_fingerprint_matches_self():
    pid = os.getpid()
    assert process_alive(pid, start_time=process_start_time(pid)) is True  # 真指纹匹配→活


def test_pid_reuse_detected_via_mismatched_start_time():
    pid = os.getpid()
    st = process_start_time(pid)
    if st is None:
        return  # 该平台取不到启动指纹→保守判活,跳过此用例
    assert process_alive(pid, start_time="different-" + st) is False  # 指纹不符→判非活(防复用)


def test_heartbeat_record_shape():
    rec = heartbeat_record()
    assert rec["pid"] == os.getpid()
    assert "start_time" in rec
    assert rec["heartbeat_at"] > 0


def test_is_dead_fresh_alive():
    rec = heartbeat_record()
    assert is_dead(rec, now=rec["heartbeat_at"] + 1, max_age=60) is False  # 活+心跳新鲜


def test_is_dead_stale_heartbeat():
    rec = heartbeat_record()
    assert is_dead(rec, now=rec["heartbeat_at"] + 100, max_age=60) is True  # 活但心跳超时


def test_is_dead_process_gone():
    rec = {"pid": 2_000_000_000, "start_time": None, "heartbeat_at": time.time()}
    assert is_dead(rec, now=time.time(), max_age=60) is True  # 进程不存在→死
