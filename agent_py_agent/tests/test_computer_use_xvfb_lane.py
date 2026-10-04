"""J16 片 B 的 Linux 车道 Xvfb 冒烟：真适配器子进程（底层 Server 单一运行路径）、真 X11 后端、真 OCR、真点击。
只在车道容器里跑：需要 MY_AGENT_XVFB_LANE=1、Xvfb、openbox、python3-tk 与 computer-control-mcp；Mac 上自动跳过。
流程：观察 → 点击候选 → 再观察确认状态变了；移动窗口后点旧候选 → OBSERVATION_STALE 且没点；未知别名 → window_not_found。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
from agent_py_agent.agent.plugin_observation import OBSERVATION_KEY, OBSERVATION_STALE
from agent_py_agent.agent.tooling.computer_use_profile import (
    ComputerUseTier,
    computer_use_mcp_servers,
    with_computer_use_observation,
)
from agent_py_agent.agent.tooling.mcp_registration import register_mcp_servers
from agent_py_agent.tests.test_plugin_observation import _Repo

pytestmark = pytest.mark.skipif(os.environ.get("MY_AGENT_XVFB_LANE") != "1", reason="只在 Linux 车道容器（MY_AGENT_XVFB_LANE=1）里跑")

DISPLAY = ":99"
RUN_SCOPE = {"owner_id": "local/main", "owner_home": "/owner", "run_id": "run-1", "task_id": "task-1", "attempt_id": "attempt-1", "turn_id": "turn-1"}

# 测试窗口：两个大字号按钮、一个带闪动光标的输入框、一行状态；点 Submit 改状态并计数；每 100ms 读命令文件
# （move 移动 / relabel 改按钮文字 / reopen 关掉再开一个同样的窗口 / quit）。
TK_APP = textwrap.dedent(
    """
    import sys, tkinter as tk
    from pathlib import Path
    clicks, commands = Path(sys.argv[1]), Path(sys.argv[2])
    count, state = {"n": 0}, {"reopen": False}
    def build():
        root = tk.Tk(); root.title("J16 Smoke"); root.geometry("420x360+100+80")
        status = tk.StringVar(value="status idle")
        def submit():
            count["n"] += 1; status.set("status submitted"); clicks.write_text(str(count["n"]))
        button = tk.Button(root, text="Submit", font=("DejaVu Sans", 22), width=10, command=submit); button.pack(pady=10)
        tk.Button(root, text="Cancel", font=("DejaVu Sans", 22), width=10).pack(pady=10)
        entry = tk.Entry(root, font=("DejaVu Sans", 20), width=12); entry.insert(0, "name"); entry.pack(pady=10)
        tk.Label(root, textvariable=status, font=("DejaVu Sans", 18)).pack(pady=10)
        def poll():
            if commands.exists():
                command = commands.read_text().strip(); commands.unlink()
                if command == "move":
                    root.geometry("+360+300")
                elif command == "relabel":
                    button.config(text="Submit!")
                elif command == "reopen":
                    state["reopen"] = True; root.destroy(); return
                elif command == "quit":
                    root.destroy(); return
            root.after(100, poll)
        root.after(100, poll); root.mainloop()
    while True:
        state["reopen"] = False; build()
        if not state["reopen"]:
            break
    """
)


# 类用途: 最小注册表替身：工具映射、构造参数里的 owner 权威库、客户端列表。
class _Registry:
    def __init__(self, repo):
        self.tools = {}
        self._construction_params = SimpleNamespace(runtime_repo=repo)
        self._mcp_clients = []


# 函数用途: 启动一个子进程并记下 PID（只杀自己起的进程）。
def _spawn(argv, env, log):
    return subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT)


# 函数用途: 等条件成立，超时抛错。
def _wait(predicate, seconds, what):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"等待超时：{what}")


# 函数用途: 经代理调一次工具，把归档记进假库（和片 A 用例同一条持久化入口），返回结果；lane 带 registry 与 repo。
def _call(lane, name, arguments, operation_id):
    outcome = lane.registry.tools[name].execute({**arguments, "__run_scope": RUN_SCOPE, "__operation_id": operation_id})
    archive = {"run_id": "run-1", "task_id": "task-1", "operation_id": operation_id, "attempt_id": "attempt-1", "tool": outcome.tool,
               "ok": outcome.ok, "error_code": outcome.error_code, "idempotency_key": "", "tool_result_envelope": outcome.result_envelope}
    persist_tool_runtime_ledger(SimpleNamespace(subagents=SimpleNamespace(runtime_db=lane.repo), local_store=SimpleNamespace()), archive)
    return outcome


# 函数用途: 按倒序结束自己起的进程，等不到就杀。
def _stop(procs):
    for proc in reversed(procs):
        proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def desktop(tmp_path):
    for binary in ("Xvfb", "openbox", "xdpyinfo"):
        assert shutil.which(binary), f"车道镜像缺 {binary}"
    env = {**os.environ, "DISPLAY": DISPLAY}
    log = (tmp_path / "desktop.log").open("w")
    procs = [_spawn(["Xvfb", DISPLAY, "-screen", "0", "1024x768x24", "-nolisten", "tcp"], env, log)]
    _wait(lambda: subprocess.run(["xdpyinfo", "-display", DISPLAY], capture_output=True).returncode == 0, 15, "Xvfb 就绪")
    procs.append(_spawn(["openbox"], env, log))
    time.sleep(1.0)
    clicks, commands = tmp_path / "clicks.txt", tmp_path / "command.txt"
    procs.append(_spawn([sys.executable, "-c", TK_APP, str(clicks), str(commands)], env, log))
    time.sleep(2.0)
    try:
        yield SimpleNamespace(env=env, clicks=clicks, commands=commands, log=tmp_path / "desktop.log")
    finally:
        _stop(procs)
        log.close()
        evidence_dir = os.environ.get("MY_AGENT_XVFB_EVIDENCE_DIR")
        if evidence_dir:
            shutil.copy(tmp_path / "desktop.log", Path(evidence_dir) / "desktop.log")


def _candidate(outcome, needle):
    candidates = json.loads(outcome.output)["structuredContent"][OBSERVATION_KEY]["candidates"]
    match = next((c for c in candidates if needle.lower() in c["label"].lower()), None)
    assert match is not None, f"OCR 候选里没有 {needle!r}：{[c['label'] for c in candidates]}"
    return match


@pytest.fixture
def lane(desktop, tmp_path):
    repo = _Repo()
    registry = _Registry(repo)
    servers = with_computer_use_observation(
        computer_use_mcp_servers({}, ComputerUseTier(enabled=True, is_local_admin=True, access_mode="full-access", environ=desktop.env, python_executable=sys.executable)),
        enabled=True)
    clients = register_mcp_servers(registry, servers)
    registry._mcp_clients = clients
    state = SimpleNamespace(repo=repo, registry=registry, clients=clients, desktop=desktop, evidence={})
    try:
        publication = clients[0].publication if clients else None
        assert publication is not None and publication.status == "published", f"{publication} launch={getattr(clients[0], '_launch_failure', None) if clients else None}"
        yield state
    finally:
        for client in clients:
            client.stop()
        out = Path(os.environ.get("MY_AGENT_XVFB_EVIDENCE_DIR") or tmp_path) / "j16-xvfb-smoke.json"
        existing = json.loads(out.read_text()) if out.exists() else {}
        out.write_text(json.dumps({**existing, **state.evidence}, ensure_ascii=False, indent=2))


def test_observe_click_and_reobserve_on_a_real_xvfb_desktop(lane):
    assert {"mcp__computer_use__observe_window", "mcp__computer_use__click_candidate", "mcp__computer_use__get_screen_size"} <= set(lane.registry.tools)
    size = _call(lane, "mcp__computer_use__get_screen_size", {}, "op-0")
    assert size.ok, size.output
    lane.evidence["upstream_tool_via_delegation"] = json.loads(size.output).get("structuredContent") or json.loads(size.output).get("result")
    first = _call(lane, "mcp__computer_use__observe_window", {}, "op-1")
    assert first.ok, first.output
    payload = json.loads(first.output)["structuredContent"]
    assert payload["frame"]["space"] == "screen_points" and "my_agent_observation" in payload, payload
    assert first.result_envelope["observation"]["provider_id"] == "mcp:computer_use"
    submit = _candidate(first, "Submit")
    lane.evidence["first_observation"] = {"window": payload["window"], "generation": payload["generation"], "candidate_count": payload["candidate_count"],
                                          "labels": [c["label"] for c in payload[OBSERVATION_KEY]["candidates"]]}
    clicked = _call(lane, "mcp__computer_use__click_candidate", {"candidate_id": submit["candidate_id"]}, "op-2")
    assert clicked.ok, clicked.output
    _wait(lambda: lane.desktop.clicks.exists() and lane.desktop.clicks.read_text() == "1", 10, "Tk 收到一次点击")
    lane.evidence["click"] = json.loads(clicked.output)["structuredContent"]
    second = _call(lane, "mcp__computer_use__observe_window", {"window": payload["window"]}, "op-3")
    assert second.ok, second.output
    changed = _candidate(second, "submitted")
    lane.evidence["second_observation"] = {"generation": json.loads(second.output)["structuredContent"]["generation"], "status_label": changed["label"]}


def test_switch_off_publishes_only_the_upstream_tools_on_the_real_adapter(desktop):
    registry = _Registry(_Repo())
    servers = computer_use_mcp_servers({}, ComputerUseTier(enabled=True, is_local_admin=True, access_mode="full-access", environ=desktop.env, python_executable=sys.executable))
    clients = register_mcp_servers(registry, with_computer_use_observation(servers, enabled=False))
    try:
        assert clients[0].publication.status == "published", str(clients[0].publication)
        assert not {name for name in registry.tools if name.endswith("observe_window") or name.endswith("click_candidate")}, "开关关着适配器不交出观察工具"
        assert "mcp__computer_use__list_windows" in registry.tools and "mcp__computer_use__type_text" in registry.tools
    finally:
        for client in clients:
            client.stop()


def test_moved_window_makes_the_candidate_stale_and_unknown_alias_is_structured(lane):
    first = _call(lane, "mcp__computer_use__observe_window", {}, "op-1")
    assert first.ok, first.output
    submit = _candidate(first, "Submit")
    lane.desktop.commands.write_text("move")
    time.sleep(1.0)
    stale = _call(lane, "mcp__computer_use__click_candidate", {"candidate_id": submit["candidate_id"]}, "op-2")
    assert (stale.ok, stale.reported_error_code, stale.effect_outcome) == (False, OBSERVATION_STALE, "not_started"), stale.output
    assert not lane.desktop.clicks.exists(), "窗口移动后旧候选没有被点"
    lane.evidence["stale_after_move"] = {"reported_error_code": stale.reported_error_code, "effect_outcome": stale.effect_outcome,
                                         "clicked": lane.desktop.clicks.exists()}
    unknown = _call(lane, "mcp__computer_use__observe_window", {"window": "win:nope:1"}, "op-3")
    assert not unknown.ok
    error = json.loads(unknown.output)["structuredContent"]["my_agent_observation_error"]
    # J16 片 F 起 window_not_found 会带可见窗口清单（windows）和是否截断（truncated）两个结构化补充，这里只钉住码和补充键的范围。
    assert error["code"] == "window_not_found" and set(error) <= {"code", "windows", "truncated"}, error
    assert isinstance(error.get("windows", []), list) and isinstance(error.get("truncated", False), bool), error
    lane.evidence["unknown_alias"] = json.loads(unknown.output)["structuredContent"]
