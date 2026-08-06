"""后台进程注册表 + list/status/kill 工具测试。

用真实子进程(python -c sleep/print)起后台进程,验证:登记、list/status/kill 各工作、
进程退出后状态惰性更新、kill 杀整个进程组(连带子进程)、不存在 session_id 优雅报错。

CI 友好:用短 sleep + 轮询等待,kill 兜底确保不留孤儿;每个用例 teardown 清注册表。
所有命令用 sys.executable 跑,跨平台(POSIX/Windows)一致,不依赖系统 sleep。
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling.process_registry import (
    BackgroundProcess,
    ProcessRegistry,
    _read_log_tail,
    process_registry,
)
from agent_py_agent.agent.tooling.process_tools import (
    KillProcessTool,
    ListProcessesTool,
    ProcessStatusTool,
)
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="进程组语义为 POSIX 专属")


def _py_command(body: str) -> str:
    """构造一条跨平台的 python 内联命令字符串(交给 shell/powershell 执行)。"""
    inline = f"{shlex.quote(sys.executable)} -c {shlex.quote(body)}"
    if os.name == "nt":
        return f"& {inline}"
    return inline


def _start_background(tool: ShellTool, command: str) -> dict:
    result = tool.execute({"command": command, "run_in_background": True})
    assert result.ok is True, result.output
    return json.loads(result.output)


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def shell(tmp_path: Path) -> ShellTool:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ShellTool(workspace, options=ShellToolOptions(default_timeout=30, access_mode="full-access"))


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例前后清空全局注册表,避免用例间串状态;teardown 兜底杀掉残留进程。"""
    process_registry.clear()
    yield
    for entry in process_registry.list():
        if entry["status"] == "running":
            process_registry.kill(entry["session_id"])
    process_registry.clear()


class TestRegistration:
    def test_run_command_background_registers_session(self, shell: ShellTool):
        """run_command 后台启动会登记进注册表并返回 session_id。"""
        payload = _start_background(shell, _py_command("import time; print('hi'); time.sleep(5)"))
        assert payload["status"] == "started"
        assert payload["session_id"]
        assert payload["pid"] > 0
        record = process_registry.get(payload["session_id"])
        assert record is not None
        assert record.pid == payload["pid"]
        assert record.status == "running"

    def test_session_ids_are_unique(self, shell: ShellTool):
        """连起两个后台进程拿到不同 session_id。"""
        a = _start_background(shell, _py_command("import time; time.sleep(5)"))
        b = _start_background(shell, _py_command("import time; time.sleep(5)"))
        assert a["session_id"] != b["session_id"]
        assert process_registry.list()[0]["status"] == "running"


class TestListProcesses:
    def test_list_empty(self):
        """没有后台进程时 list 返回空。"""
        out = json.loads(ListProcessesTool().execute({}).output)
        assert out["count"] == 0
        assert out["processes"] == []

    def test_list_shows_running(self, shell: ShellTool):
        """list 列出运行中的后台进程及状态。"""
        payload = _start_background(shell, _py_command("import time; time.sleep(5)"))
        out = json.loads(ListProcessesTool().execute({}).output)
        assert out["count"] == 1
        entry = out["processes"][0]
        assert entry["session_id"] == payload["session_id"]
        assert entry["status"] == "running"
        assert "uptime_seconds" in entry


class TestProcessStatus:
    def test_status_running_with_output(self, shell: ShellTool):
        """status 返回运行状态并带最近输出(日志尾部)。"""
        payload = _start_background(
            shell, _py_command("import time,sys; print('MARKER', flush=True); time.sleep(5)")
        )
        sid = payload["session_id"]
        assert _wait_until(lambda: "MARKER" in (process_registry.status(sid) or {}).get("output_tail", ""))
        out = json.loads(ProcessStatusTool().execute({"session_id": sid}).output)
        assert out["status"] == "running"
        assert "MARKER" in out["output_tail"]

    def test_status_updates_after_exit(self, shell: ShellTool):
        """进程退出后,status 惰性 poll 把状态更新为 exited 并带退出码。"""
        payload = _start_background(shell, _py_command("print('quick')"))
        sid = payload["session_id"]
        assert _wait_until(lambda: (process_registry.status(sid) or {}).get("status") == "exited")
        out = json.loads(ProcessStatusTool().execute({"session_id": sid}).output)
        assert out["status"] == "exited"
        assert out["exit_code"] == 0

    def test_status_nonzero_exit_code(self, shell: ShellTool):
        """非零退出码被如实收割。"""
        payload = _start_background(shell, _py_command("import sys; sys.exit(3)"))
        sid = payload["session_id"]
        assert _wait_until(lambda: (process_registry.status(sid) or {}).get("status") == "exited")
        out = json.loads(ProcessStatusTool().execute({"session_id": sid}).output)
        assert out["exit_code"] == 3

    def test_status_not_found(self):
        """不存在的 session_id 优雅报错(PROCESS_NOT_FOUND, retryable)。"""
        result = ProcessStatusTool().execute({"session_id": "does-not-exist"})
        assert result.ok is False
        assert result.error_code == "PROCESS_NOT_FOUND"
        assert result.retryable is True
        body = json.loads(result.output)
        assert body["error"] == "process_not_found"

    def test_status_missing_session_id(self):
        """缺 session_id 参数报 TOOL_INVALID_ARGUMENTS。"""
        result = ProcessStatusTool().execute({})
        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"

    def test_status_coerces_non_string_session_id(self, shell: ShellTool):
        """模型把 session_id 发成非字符串时被强制转字符串(不存在则报 NOT_FOUND 而非崩溃)。"""
        result = ProcessStatusTool().execute({"session_id": 12345})
        assert result.ok is False
        assert result.error_code == "PROCESS_NOT_FOUND"


class TestKillProcess:
    def test_kill_running(self, shell: ShellTool):
        """kill 终止运行中的进程并更新状态为 killed。"""
        payload = _start_background(shell, _py_command("import time; time.sleep(30)"))
        sid = payload["session_id"]
        out = json.loads(KillProcessTool().execute({"session_id": sid}).output)
        assert out["status"] == "killed"
        assert _wait_until(lambda: (process_registry.status(sid) or {}).get("status") == "killed")

    @_POSIX_ONLY
    def test_kill_terminates_process_group(self, shell: ShellTool):
        """kill 杀整个进程组:shell 父进程下的子进程(sleep)也一并被杀,不留孤儿。"""
        # shell -c 起一个会 fork 子进程的命令:外层 shell 是组长,内层 python sleep 是子进程。
        payload = _start_background(
            shell, f"{_py_command('import time; time.sleep(30)')} & wait"
        )
        sid = payload["session_id"]
        pid = payload["pid"]
        pgid = os.getpgid(pid)

        # 等进程组里出现子进程(组里 >1 个成员)。
        def _group_members() -> list[str]:
            import subprocess
            out = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True)
            return out.stdout.split()

        assert _wait_until(lambda: len(_group_members()) >= 2), "子进程未能在进程组内起来"

        KillProcessTool().execute({"session_id": sid})
        # 杀完后进程组应清空(父子都死)。
        assert _wait_until(lambda: len(_group_members()) == 0), f"进程组仍有残留: {_group_members()}"

    def test_kill_already_exited(self, shell: ShellTool):
        """对已结束的进程 kill 返回 already_exited 而非报错。"""
        payload = _start_background(shell, _py_command("print('done')"))
        sid = payload["session_id"]
        assert _wait_until(lambda: (process_registry.status(sid) or {}).get("status") == "exited")
        result = KillProcessTool().execute({"session_id": sid})
        assert result.ok is True
        out = json.loads(result.output)
        assert out["status"] == "already_exited"

    def test_kill_not_found(self):
        """kill 不存在的 session_id 优雅报错。"""
        result = KillProcessTool().execute({"session_id": "ghost"})
        assert result.ok is False
        assert result.error_code == "PROCESS_NOT_FOUND"

    def test_kill_missing_session_id(self):
        """kill 缺 session_id 报 TOOL_INVALID_ARGUMENTS。"""
        result = KillProcessTool().execute({})
        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"


class TestRegistryUnit:
    def test_prune_finished_over_limit(self):
        """已结束记录超上限时淘汰最老的,运行中的不淘汰。"""
        from agent_py_agent.agent.tooling import process_registry as mod

        reg = ProcessRegistry()
        # 灌入超过上限的"已结束"记录。
        total = mod._MAX_FINISHED + 5
        for i in range(total):
            rec = reg.register(command=f"c{i}", pid=1000 + i, output_file="")
            rec.status = "exited"
            rec.exit_code = 0
            rec.finished_at = time.time() + i
        reg._prune_finished_locked()
        assert len(reg._processes) == mod._MAX_FINISHED

    def test_read_log_tail_truncates(self, tmp_path: Path):
        """日志尾部读取在超限时只保留尾部并标注截断。"""
        log = tmp_path / "big.log"
        log.write_text("\n".join(f"line{i}" for i in range(1000)), encoding="utf-8")
        tail = _read_log_tail(str(log), 200)
        assert "截断" in tail
        assert "line999" in tail
        assert len(tail) < 600

    def test_read_log_tail_missing_file(self):
        """日志文件不存在返回空串而不抛异常。"""
        assert _read_log_tail("/no/such/file.log", 100) == ""

    def test_to_summary_running_no_exit_code(self):
        """运行中记录的 summary 不带 exit_code。"""
        rec = BackgroundProcess(session_id="x", command="c", pid=1, started_at=time.time())
        summary = rec.to_summary()
        assert "exit_code" not in summary
        assert summary["status"] == "running"


class TestKillSafety:
    """杀进程的安全性(blast radius):升级硬杀、不误伤无关进程、不对 pid<=0 发信号。"""

    @_POSIX_ONLY
    def test_escalates_to_sigkill_when_sigterm_ignored(self, monkeypatch, tmp_path: Path):
        """进程忽略 SIGTERM(杀不干净)→ 宽限后升级 SIGKILL 硬杀,确实杀死,不留命。"""
        import subprocess

        from agent_py_agent.agent.tooling import process_registry as mod

        monkeypatch.setattr(mod, "_KILL_GRACE_SECONDS", 0.3)  # 缩短宽限加速
        ready = tmp_path / "ready"
        # 装好 SIG_IGN 后再落 ready 文件 —— 避免 SIGTERM 在 handler 装好前到达的竞态
        body = (
            "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).write_text('1'); time.sleep(60)"
        )
        proc = subprocess.Popen([sys.executable, "-c", body], start_new_session=True)
        try:
            assert _wait_until(lambda: ready.exists(), timeout=5), "子进程未装好 SIGTERM 忽略"
            result = mod._terminate_process_tree(proc.pid, proc)
            assert result == "SIGTERM->SIGKILL"  # ⭐ SIGTERM 被忽略 → 升级 SIGKILL
            proc.wait(timeout=3)
            assert proc.poll() is not None  # 进程确实死了(SIGKILL 无法被忽略)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_kill_does_not_touch_unrelated_process(self, shell: ShellTool):
        """杀 A 不影响无关的 B —— 只动注册 pid 的进程组,不误伤(杀错进程的核心防线)。"""
        a = _start_background(shell, _py_command("import time; time.sleep(30)"))
        b = _start_background(shell, _py_command("import time; time.sleep(30)"))
        KillProcessTool().execute({"session_id": a["session_id"]})
        assert _wait_until(lambda: (process_registry.status(a["session_id"]) or {}).get("status") == "killed")
        assert (process_registry.status(b["session_id"]) or {}).get("status") == "running"  # ⭐ B 毫发无伤

    def test_terminate_nonpositive_pid_is_noop(self):
        """pid<=0 → noop,绝不 os.killpg(0)(那会杀掉调用方自己的整个进程组,灾难性)。"""
        from agent_py_agent.agent.tooling.process_registry import _terminate_process_tree

        assert _terminate_process_tree(0, None) == "noop"
        assert _terminate_process_tree(-5, None) == "noop"

    def test_pid_alive_rejects_nonpositive(self):
        from agent_py_agent.agent.tooling.process_registry import _pid_alive

        assert _pid_alive(0) is False and _pid_alive(-1) is False


class TestRegistryConcurrency:
    def test_concurrent_register_list_status_kill_threadsafe(self):
        """run_command 登记与查/杀工具并发(类文档承诺线程安全):锁下不崩、不损坏。

        全用 pid=0(不存活探测、kill 时 refresh 先标 exited 故不发任何真实信号)——纯压注册表的锁。
        """
        import threading

        reg = ProcessRegistry()
        errors: list[Exception] = []
        sids: list[str] = []
        guard = threading.Lock()

        def register_worker(w: int) -> None:
            try:
                for j in range(20):
                    rec = reg.register(command=f"c{w}-{j}", pid=0, output_file="")
                    with guard:
                        sids.append(rec.session_id)
            except Exception as exc:
                errors.append(exc)

        def query_worker() -> None:
            try:
                for _ in range(40):
                    reg.list()
                    with guard:
                        recent = sids[-5:]
                    for sid in recent:
                        reg.status(sid)
                        reg.kill(sid)  # pid=0 → already_exited,不发信号
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=register_worker, args=(w,)) for w in range(10)]
        threads += [threading.Thread(target=query_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发操作不应出错:{errors[:3]}"
        for entry in reg.list():  # 注册表自洽:无半损坏记录
            assert entry["session_id"] and "status" in entry


class TestToolSpecs:
    def test_specs_have_precise_schema(self):
        """三个工具的 schema 对齐原生 tool_use 规范:必填参数声明 + 精确类型。"""
        list_tool = ListProcessesTool()
        status_tool = ProcessStatusTool()
        kill_tool = KillProcessTool()
        list_spec = list_tool.model_spec
        status_spec = status_tool.model_spec
        kill_spec = kill_tool.model_spec

        assert list_spec.name == "list_processes"
        assert list_spec.input_schema.get("required", []) == []

        assert status_spec.input_schema["required"] == ["session_id"]
        assert status_spec.input_schema["properties"]["session_id"]["type"] == "string"
        assert status_tool.runtime_policy.effect_resolver.default_effect == "read_only"

        assert kill_spec.input_schema["required"] == ["session_id"]
        assert kill_spec.input_schema["properties"]["session_id"]["type"] == "string"
        assert kill_tool.runtime_policy.effect_resolver.default_effect == "mutating"

    def test_tools_registered_in_catalog(self, tmp_path: Path):
        """三个工具真的注册进了 ToolRegistry,模型可见。"""
        from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

        params = ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=50,
            max_matches=50,
            web_max_chars=1000,
            http_timeout=10,
            catalog_limit=200,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
        registry = ToolRegistry(params)
        names = {spec.name for spec in registry.specs()}
        assert {"list_processes", "process_status", "kill_process"} <= names
