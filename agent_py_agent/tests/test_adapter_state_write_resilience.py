"""通道适配器常驻进程的状态写入韧性与 /status 存活事实合同。

2026-09-28 生产事故：飞书适配器每 5 秒写一次 adapter_state.json，磁盘写满时写入抛 OSError 没人接，进程整个退出，
状态文件却还写着 running，飞书从 15:13 起一直不通。三条硬合同：
1. 周期状态写入失败不杀进程：记账后下一轮重试，恢复后把失败次数与最近错误写进状态文件；
2. 进程因异常退出时先停适配器、删 pid 文件，再把状态写成 failed；状态写不进去也留 ERROR 日志，pid 文件一定没了；
3. /status 里的适配器存活按 adapter.pid 对应进程是否真活着判定，状态文件只作补充，且只读不清理。
撤修复即 FAIL，见各用例注释。全部确定性构造（注入 ENOSPC、假管理器、已退出的子进程 pid）。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.channel_health import _utc_now_iso, adapter_process_facts
from agent_py_agent.agent.gateway_parts.daemon_control import write_pid_record
from agent_py_agent.cli import adapter


@pytest.fixture(autouse=True)
def _fresh_state_write_health():
    adapter._ADAPTER_STATE_WRITE_HEALTH["failures"] = 0
    adapter._ADAPTER_STATE_WRITE_HEALTH["last_error"] = {}
    yield
    adapter._ADAPTER_STATE_WRITE_HEALTH["failures"] = 0
    adapter._ADAPTER_STATE_WRITE_HEALTH["last_error"] = {}


# 函数用途: 等待循环在非主线程里跑，真实 signal.signal 会拒绝；这里只记录，不改进程的信号处理。
@pytest.fixture(autouse=True)
def _no_signal_install(monkeypatch):
    installed: list[int] = []
    monkeypatch.setattr(adapter.signal, "signal", lambda signum, handler: installed.append(signum))
    return installed


def _gpaths(tmp_path):
    root = tmp_path / "gw"
    root.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(root=root, adapter_pid=root / "adapter.pid")


# 函数用途: 轮询结构化条件，超时给出明确断言信息；上限只是防挂起。
def _wait_until(predicate, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"等待{what}超时"
        time.sleep(0.01)


# 函数用途: 让状态文件的原子写前 N 次抛 ENOSPC（磁盘写满的真实形态），之后正常写。
def _fail_state_writes(monkeypatch, failures: int) -> None:
    real_write = adapter.write_json_file_atomic
    left = {"n": failures}

    def write(path, payload):
        if left["n"] > 0:
            left["n"] -= 1
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(path, payload)

    monkeypatch.setattr(adapter, "write_json_file_atomic", write)


# 类用途: 假通道管理器：可让 start_all 抛错，记录 stop_all 次数。
class _FakeManager:
    def __init__(self, *, start_error: BaseException | None = None) -> None:
        self.start_error = start_error
        self.stopped = 0

    def start_all(self) -> None:
        if self.start_error is not None:
            raise self.start_error

    def stop_all(self) -> None:
        self.stopped += 1

    def runtime_channel_statuses(self) -> list[dict]:
        return [{"name": "feishu", "health": {"state": "healthy"}}]

    def list_adapters(self) -> list[str]:
        return ["feishu"]


def _read_state(gpaths) -> dict:
    return json.loads((gpaths.root / "adapter_state.json").read_text(encoding="utf-8"))


def test_periodic_state_write_failure_does_not_kill_loop_and_recovers(tmp_path, monkeypatch, caplog) -> None:
    gpaths = _gpaths(tmp_path)
    monkeypatch.setattr(adapter, "ADAPTER_STATE_WRITE_INTERVAL_SECONDS", 0.01)
    _fail_state_writes(monkeypatch, 1)
    stop_event = threading.Event()
    state_path = gpaths.root / "adapter_state.json"
    thread = threading.Thread(
        target=adapter._wait_for_adapter_shutdown, args=(_FakeManager(), gpaths, stop_event), daemon=True
    )
    with caplog.at_level(logging.WARNING, logger="agent_py_agent.adapter"):
        thread.start()  # 撤修复：第一次写的 ENOSPC 从线程里炸出，循环结束，状态文件永远不出现
        _wait_until(
            lambda: state_path.exists() and _read_state(gpaths).get("state_write_failures") == 1,
            "写失败后下一轮恢复写入",
        )
        stop_event.set()
        thread.join(timeout=5.0)
    assert not thread.is_alive()
    payload = _read_state(gpaths)
    assert payload["state"] == "running" and payload["state_write_failures"] == 1
    assert payload["last_state_write_error"]["type"] == "OSError"
    assert payload["last_state_write_error"]["state"] == "running"
    assert any("adapter_state_write_failed state=running" in record.getMessage() for record in caplog.records)


def _run_foreground_with(monkeypatch, gpaths, manager: _FakeManager) -> None:
    monkeypatch.setattr(adapter, "ChannelManager", lambda **kwargs: manager)
    monkeypatch.setattr(adapter, "_register_requested_adapters", lambda manager, channel, agent: None)
    agent = SimpleNamespace(config=SimpleNamespace(gateway_port=1))
    adapter._run_adapter_foreground(agent, SimpleNamespace(channel="feishu"), gpaths)


def test_unhandled_error_finishes_with_failed_state_and_removed_pid(tmp_path, monkeypatch) -> None:
    gpaths = _gpaths(tmp_path)
    manager = _FakeManager(start_error=RuntimeError("feishu ws connect failed"))

    with pytest.raises(RuntimeError, match="feishu ws connect failed"):
        _run_foreground_with(monkeypatch, gpaths, manager)

    assert not gpaths.adapter_pid.exists()  # 撤修复：pid 文件还在、状态文件还是 starting
    payload = _read_state(gpaths)
    assert payload["state"] == "failed" and payload["reason"] == "unhandled_error"
    assert payload["error"] == {"type": "RuntimeError", "message": "feishu ws connect failed"}
    assert manager.stopped == 1


def test_exit_state_write_failure_still_removes_pid_and_logs(tmp_path, monkeypatch, caplog) -> None:
    gpaths = _gpaths(tmp_path)
    manager = _FakeManager(start_error=RuntimeError("boom"))
    _fail_state_writes(monkeypatch, 99)  # 磁盘一直满：starting 与 failed 都写不进去

    with caplog.at_level(logging.WARNING, logger="agent_py_agent.adapter"), pytest.raises(RuntimeError, match="boom"):
        _run_foreground_with(monkeypatch, gpaths, manager)

    assert not gpaths.adapter_pid.exists()  # 删 pid 文件不需要磁盘空间，是最后的结构化痕迹
    assert not (gpaths.root / "adapter_state.json").exists()
    messages = [record.getMessage() for record in caplog.records]
    assert any("adapter_state_write_failed_at_exit state=failed reason=unhandled_error" in m for m in messages)
    assert adapter._ADAPTER_STATE_WRITE_HEALTH["failures"] == 2
    assert manager.stopped == 1


def _exited_child_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return child.pid


def test_status_adapter_facts_follow_process_liveness_not_state_file(tmp_path) -> None:
    gpaths = _gpaths(tmp_path)
    facts = adapter_process_facts(gpaths)
    assert facts["adapter_alive"] is False and facts["adapter_pid"] is None and facts["adapter_state"] == ""

    # 事故形态：状态文件写着新鲜的 running，pid 记录指向已退出的进程 → 不算活
    state_path = gpaths.root / "adapter_state.json"
    state_path.write_text(json.dumps({"state": "running", "updated_at": _utc_now_iso()}), encoding="utf-8")
    gpaths.adapter_pid.write_text(json.dumps({"pid": _exited_child_pid(), "start_time": None}), encoding="utf-8")
    facts = adapter_process_facts(gpaths)  # 撤修复（只信状态文件）：这里会报活
    assert facts["adapter_alive"] is False and facts["adapter_state"] == "running"
    assert facts["adapter_state_stale"] is False
    assert gpaths.adapter_pid.exists()  # /status 只读，不清理陈旧 pid 文件

    write_pid_record(gpaths.adapter_pid)  # 本进程的记录 → 活
    facts = adapter_process_facts(gpaths)
    assert facts["adapter_alive"] is True and facts["adapter_pid"] == os.getpid()

    state_path.write_text("{not json", encoding="utf-8")  # 状态文件坏了：存活判断不受影响，错误单列
    facts = adapter_process_facts(gpaths)
    assert facts["adapter_alive"] is True and facts["adapter_state"] == ""
    assert set(facts["adapter_state_error"]) == {"category", "context"}  # 结构化子集，不带路径与正文
    assert facts["adapter_state_error"]["context"] == "gateway.status.adapter_state.read"
    assert str(tmp_path) not in json.dumps(facts)

    gpaths.adapter_pid.write_text("{bad pid record", encoding="utf-8")  # pid 记录坏了：不算活，错误同样是子集
    facts = adapter_process_facts(gpaths)
    assert facts["adapter_alive"] is False and set(facts["adapter_pid_error"]) == {"category", "context"}


def test_adapter_error_record_is_redacted() -> None:
    record = adapter._error_record(RuntimeError("feishu api_key=sk-live-123456 rejected"))
    assert record["type"] == "RuntimeError"
    assert "sk-live-123456" not in record["message"] and "<redacted>" in record["message"]
