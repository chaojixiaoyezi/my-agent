"""后台 host 独占日志上限，启动方退出或旧回合取消不影响保护。"""

from __future__ import annotations

import time

from agent_py_agent.agent.tooling.background_process_launch import (
    BackgroundLaunchError,
    start_background_process,
)
from agent_py_agent.agent.tooling.process_registry import process_registry
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests._managed_process_harness import managed_request


def test_host_limits_runaway_log_before_handoff(tmp_path):
    request = managed_request(
        tmp_path, "while True: print('x' * 1000, flush=True)", max_log_bytes=20_000
    )
    try:
        hosted = start_background_process(request)
    except BackgroundLaunchError as exc:
        record = exc.record
    else:
        record = process_registry.attach(
            hosted.record, hosted.process, hosted.store_root
        ).to_record()
        hosted.process.wait(timeout=5)
    current = ProcessSessionStore(request.store_root).load(record["session_id"]).record
    assert current["status"] == "killed"
    assert current["reason"] == "log_limit_exceeded"
    assert current["termination"]["confirmed"] is True


def test_host_lets_small_output_command_finish_with_actual_code(tmp_path):
    request = managed_request(tmp_path, "print('hi-bg')")
    hosted = start_background_process(request)
    hosted.process.wait(timeout=5)
    current = ProcessSessionStore(request.store_root).load(hosted.record["session_id"]).record
    assert current["status"] == "exited"
    assert current["exit_code"] == 0
    assert request.log_path.read_text().strip() == "hi-bg"


def test_host_keeps_enforcing_cap_after_handoff(tmp_path):
    request = managed_request(
        tmp_path,
        "import time; time.sleep(1.5)\nwhile True: print('x' * 1000, flush=True)",
        max_log_bytes=20_000,
    )
    hosted = start_background_process(request)
    record = process_registry.attach(hosted.record, hosted.process, hosted.store_root)
    try:
        assert record.to_record()["handoff_confirmed"] is True
        hosted.process.wait(timeout=5)
        current = ProcessSessionStore(request.store_root).load(record.session_id).record
        assert current["status"] == "killed"
        assert current["reason"] == "log_limit_exceeded"
        assert current["finished_at"] <= time.time()
    finally:
        process_registry.kill(record.session_id, record.access_scope, hosted.store_root)
