# LLM: 原设置服务→决策服务→真实本地 HTTP→原账本；后台 Curator 点与前台 skill_tool（插件工具短名单所用的点）共用同一 owner、
#   同一决策连接。本地服务按请求 state 里的 lane 选择性阻塞，不访问真实供应商；每个测试先复位进程关闭标记。
# 模块用途: P4-F 组合证据：Curator 后台决策与插件相关前台决策并发时互不拖住、设置撤销只命中对应点、共享连接冷却按设计回原方案、
#   宿主关闭时两者一起取消。
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.conversation import decision_policy, decision_service
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.tests.test_decision_model_profiles import decision as add_decision
from agent_py_agent.tests.test_decision_protocol import questions, response
from agent_py_agent.tests.test_decision_settings import host_at, patch

_LANES = ("curator", "foreground")


# LLM: 只在本机随机端口提供自有协议样本；按 lane 分别记录到达与阻塞，全部线程和等待在 finally 回收。
# 函数用途: 让两个接入点经真实传输栈并发请求同一连接，并由测试控制各自何时返回。
@pytest.fixture
def lanes():
    state = SimpleNamespace(blocked=set(), requests=[], entered={lane: threading.Event() for lane in _LANES},
                            release={lane: threading.Event() for lane in _LANES})

    # LLM: 测试服务器只接收有限自有请求并返回固定协议，无命令执行或持久副作用。
    # 类用途: 按请求材料里的 lane 标记分流阻塞，其余沿原成功响应。
    class Handler(BaseHTTPRequestHandler):
        # LLM: 只读请求材料里的 lane 标记做分流，不解析其余正文；阻塞等待有上限，不会挂住测试收尾。
        # 函数用途: 记录请求所属 lane，被阻塞时有界等待放行，再返回固定成功协议。
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            lane = body["state"].get("lane", "")
            state.requests.append(lane)
            state.entered[lane].set()
            if lane in state.blocked:
                state.release[lane].wait(3)
            encoded = json.dumps(response()).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # LLM: 不把测试请求路径或身份头写进日志。
        # 函数用途: 关闭标准 HTTP 请求日志。
        def log_message(self, *_args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{http.server_port}"
    worker = threading.Thread(target=lambda: http.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield state
    finally:
        for event in state.release.values():
            event.set()
        http.shutdown()
        http.server_close()
        worker.join(2)


# 函数用途: 每个测试从“宿主未关闭”开始，收尾由 monkeypatch 还原。
@pytest.fixture(autouse=True)
def fresh_host(monkeypatch):
    monkeypatch.setattr(decision_policy, "_HOST_SHUTDOWN", False)


# LLM: 只经原配置入口创建临时 owner/thread/模型；owner 级同时打开后台 curator 与前台 skill_tool，期限可调。
# 函数用途: 返回宿主、前台线程及前台/后台两个独立阶段与参数。
def configured(tmp_path, lanes, *, timeout=2.0, background_timeout=2.0):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    key, _ = add_decision(host, api_base=lanes.url)
    patch(host, {"enabled": True, "profile_id": key, "timeout_seconds": timeout, "stage_timeout_seconds": 4.0,
                 "background_timeout_seconds": background_timeout,
                 "points.curator.mode": "apply", "points.skill_tool.mode": "apply"})
    foreground = SimpleNamespace(request_id="req-fg", run_id="run-fg", task_id="task-fg",
                                 task_attributes={"conversation_thread_id": thread.thread_id}, live_archive_state={})
    foreground.tui_runtime = TuiRuntime("decision-concurrency")
    foreground.effective_on_chunk = foreground.tui_runtime.begin_turn(foreground.request_id)
    background = SimpleNamespace(request_id="", run_id="curator-run-1", task_id="", task_attributes={},
                                 live_archive_state={})
    return SimpleNamespace(
        host=host, thread=thread, foreground=foreground, background=background,
        fg_stage=decision_service.begin_decision_stage(host, foreground, operation_id="turn-1"),
        bg_stage=decision_service.begin_decision_stage(host, background, operation_id="curator-lease-1",
                                                       scope="owner_background"))


# 函数用途: 以前台 skill_tool 点发一次决策（插件工具短名单所用的点），不执行返回的建议。
def decide_foreground(env):
    return decision_service.decide(env.host, env.foreground, env.fg_stage, point="skill_tool", state={"lane": "foreground"},
                                   questions=questions(), candidates_revision="tools-1", source_refs=("snapshot:1",))


# 函数用途: 以后台 curator 点发一次决策，不执行返回的建议。
def decide_background(env):
    return decision_service.decide(env.host, env.background, env.bg_stage, point="curator", state={"lane": "curator"},
                                   questions=questions(), candidates_revision="batch-1", source_refs=("batch:1",))


def test_slow_background_curator_does_not_hold_up_the_foreground_plugin_point(tmp_path, lanes):
    env = configured(tmp_path, lanes)
    lanes.blocked = {"curator"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        background = pool.submit(decide_background, env)
        assert lanes.entered["curator"].wait(1)
        started = time.monotonic()
        foreground = decide_foreground(env)
        assert foreground.status == "success" and foreground.may_apply and time.monotonic() - started < 1.0
        assert not background.done(), "后台仍在等，前台已经独立完成"
        lanes.release["curator"].set()
        assert background.result(timeout=2).status == "success"
    for run_id in ("run-fg", "curator-run-1"):
        summary = model_call_summary(env.host, run_id=run_id)
        assert summary["purpose_breakdown"]["decision"]["physical_model_attempt_count"] == 1


def test_thread_setting_change_cancels_only_the_foreground_decision(tmp_path, lanes):
    env = configured(tmp_path, lanes)
    lanes.blocked = {"curator", "foreground"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        background, foreground = pool.submit(decide_background, env), pool.submit(decide_foreground, env)
        assert lanes.entered["curator"].wait(1) and lanes.entered["foreground"].wait(1)
        patch(env.host, {"points.skill_tool.mode": "observe"}, thread_id=env.thread.thread_id, scope="thread")
        cancelled = foreground.result(timeout=0.8)
        assert (cancelled.status, cancelled.reason) == ("stale", "settings_changed") and not cancelled.may_apply
        assert not background.done()
        lanes.release["curator"].set()
        lanes.release["foreground"].set()
        assert background.result(timeout=2).status == "success"


# LLM: 通知层按点精确（只提前取消路由真正变化的后台点）；响应后的复核按整份策略版本保守判定，所以同 owner 的前台建议
#   也会在返回后作废为 policy_changed。两者都回原方案、都不挂起；这是 e131b1f6e 起的既定保守取舍，已登记台账。
def test_owner_curator_change_cancels_background_at_once_and_foreground_discards_after_response(tmp_path, lanes):
    env = configured(tmp_path, lanes)
    lanes.blocked = {"curator", "foreground"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        background, foreground = pool.submit(decide_background, env), pool.submit(decide_foreground, env)
        assert lanes.entered["curator"].wait(1) and lanes.entered["foreground"].wait(1)
        patch(env.host, {"points.curator.mode": "observe"})
        cancelled = background.result(timeout=0.8)
        assert (cancelled.status, cancelled.reason) == ("stale", "settings_changed")
        assert not foreground.done(), "前台点的路由没变，不被提前取消"
        lanes.release["foreground"].set()
        lanes.release["curator"].set()
        late = foreground.result(timeout=2)
    assert (late.status, late.reason) == ("stale", "policy_changed") and not late.may_apply


def test_background_timeout_cools_the_shared_connection_and_foreground_keeps_its_plan(tmp_path, lanes):
    env = configured(tmp_path, lanes, background_timeout=0.2)
    lanes.blocked = {"curator"}
    timed_out = decide_background(env)
    assert timed_out.status == "deadline" and not timed_out.may_apply
    foreground = decide_foreground(env)
    assert (foreground.status, foreground.reason) == ("cooldown", "connection_backoff") and not foreground.may_apply
    assert foreground.retry_after_seconds > 0 and lanes.requests == ["curator"], "冷却期内前台不发请求，直接保留原方案"


def test_host_shutdown_cancels_both_concurrent_decisions(tmp_path, lanes):
    env = configured(tmp_path, lanes)
    lanes.blocked = {"curator", "foreground"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        background, foreground = pool.submit(decide_background, env), pool.submit(decide_foreground, env)
        assert lanes.entered["curator"].wait(1) and lanes.entered["foreground"].wait(1)
        assert decision_policy.cancel_active_decisions_for_shutdown() == 2
        results = [future.result(timeout=0.8) for future in (background, foreground)]
    assert [(item.status, item.reason) for item in results] == [("stale", "host_shutdown")] * 2
