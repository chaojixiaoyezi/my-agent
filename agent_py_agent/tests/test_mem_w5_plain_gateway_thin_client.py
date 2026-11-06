from __future__ import annotations

# LLM: MEM-W5 让 plain 终端（显式 --plain，或缺少 prompt_toolkit 自动降级）连 Gateway 时与 TUI 一样只构造瘦客户端；
#   direct 聊天（不连 Gateway）仍构造完整智能体，由既有 test_fresh_direct_chat_keeps_original_mcp_construction_policy 覆盖。
# 模块用途: 回归保护 plain + Gateway 聊天进程不再常驻完整 SimpleAgent/ToolRegistry（实测每进程约 44 MB）。
import json
import queue
import subprocess
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.tests.support.ci_authenticated_gateway import seed_gateway_credential

_SCRIPT = r'''
import sys
from argparse import Namespace
from agent_py_agent.cli import chat
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
chat._has_prompt_toolkit = lambda: sys.argv[3] == "toolkit"
args = Namespace(config=sys.argv[1], workspace_root="", gateway=True, plain=sys.argv[2] == "plain", session_id="")
route = chat._select_chat_client_mode(args)
client = chat.make_agent(args)
assert route == (True, False), route
assert args._gateway_client_mode is True
assert isinstance(client, GatewayChatClientAgent), type(client).__name__
assert "agent_py_agent.agent.core" not in sys.modules
print("THIN-OK")
'''


# LLM: 新解释器观察真实构造与导入集合；同一 pytest 进程里 agent.core 早已被别的用例导入，观察会失真。
# 函数用途: 确认 --plain 与“缺少 prompt_toolkit”两种 plain + Gateway 入口都只构造瘦客户端、不加载完整智能体。
@pytest.mark.parametrize("plain,toolkit", [("plain", "toolkit"), ("auto", "missing")])
def test_plain_gateway_chat_constructs_only_thin_client(tmp_path, plain, toolkit):
    home = tmp_path / "home"
    seed_gateway_credential(home)
    config = tmp_path / "agent.yaml"
    config.write_text("model_backend: echo\naccess_mode: full-access\n", encoding="utf-8")
    env = {"HOME": str(tmp_path), "MY_AGENT_HOME": str(home), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, "-c", _SCRIPT, str(config), plain, toolkit],
                            capture_output=True, text=True, env=env, timeout=120, check=False)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "THIN-OK" in result.stdout


@pytest.mark.parametrize("allocated,expected", [(" canonical ", "canonical"), ("", "local"), (None, "local")])
def test_plain_worker_publishes_allocated_target_without_changing_message(monkeypatch, allocated, expected):
    from agent_py_agent.cli.chat_parts import plain_worker
    from agent_py_agent.cli.chat_parts.plain_state import ChatJob

    job = ChatJob(user="消息", show_prompt=False, inject=[], prompt_files=[], request_id="local",
                  client_message_id="message")
    lock = threading.Lock()
    acquiring = threading.Event()

    class PublicationLock:
        def __enter__(self):
            acquiring.set()
            lock.acquire()

        def __exit__(self, *_exc):
            lock.release()

    cfg = SimpleNamespace(agent=object(), args=object(), paths=object(), assistant_outputs=[],
                          build_history_context=lambda: "", current_session_id="session",
                          local_run_ref=[None], state_lock=PublicationLock(), running_request_id_ref=[job.request_id],
                          use_gateway=True)

    def handle(ctx):
        assert cfg.running_request_id_ref == ["local"]
        # 从另一个线程持锁，证明回调不能在锁外提前发布；释放后必须同步完成。
        finished = threading.Event()
        failures = []

        def allocate():
            try:
                ctx.on_request_allocated(allocated)
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=allocate)
        try:
            with lock:
                thread.start()
                assert acquiring.wait(5)
                assert not finished.is_set()
                assert cfg.running_request_id_ref == ["local"]
        finally:
            thread.join(5)
        assert not thread.is_alive() and not failures
        assert finished.is_set() and cfg.running_request_id_ref == [expected]
        return "结果", False

    monkeypatch.setattr(plain_worker, "_plain_gateway_handle", handle)
    assert plain_worker._plain_process_job(cfg, job) == ("结果", False)
    assert job.request_id == "local" and job.client_message_id == "message"


def test_plain_context_retains_positional_local_run_and_optional_callback():
    from agent_py_agent.cli.chat_parts.plain_handlers import PlainJobContext

    job, agent, args, paths, local_run = (object() for _ in range(5))
    outputs, history = [], lambda: "history"
    ctx = PlainJobContext(job, agent, args, paths, outputs, history, "session", local_run)
    assert ctx.local_run is local_run and ctx.on_request_allocated is None
    assert ctx.job is job and ctx.assistant_outputs is outputs and ctx.build_history_context is history


class _PlainWorkerDone(Exception):
    """只结束测试自己的阻塞队列消费者，不增加产品退出协议。"""


class _PlainJobs(queue.Queue):
    def __init__(self):
        super().__init__()
        self.accepted = []
        self.drained = threading.Event()

    def put(self, item, *args, **kwargs):
        self.drained.clear()
        if item is not None:
            self.accepted.append(item)
        return super().put(item, *args, **kwargs)

    def get(self, *args, **kwargs):
        item = super().get(*args, **kwargs)
        if item is None:
            self.task_done()
            raise _PlainWorkerDone
        return item

    def task_done(self):
        super().task_done()
        if not self.unfinished_tasks:
            self.drained.set()


def test_plain_gateway_stream_steer_stop_new_and_next_turn(tmp_path, monkeypatch):
    """真实输入、worker、认证HTTP和规范终态；仅供应商用Event控制，不替换运输或服务端裁决。"""
    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.agent.concurrency.interrupt import (
        is_interrupted,
        register_interrupt_callback,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.daemon_control import write_pid_record
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_requests
    from agent_py_agent.agent.gateway_parts.response_renderer import (
        read_gateway_terminal_response_file,
    )
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
    from agent_py_agent.cli.chat_parts import plain, plain_handlers, plain_worker
    from agent_py_agent.cli.chat_parts.plain_state import RunPlainConfig

    class Provider(EchoBackend):
        def __init__(self):
            self.streaming = threading.Event()
            self.resume = threading.Event()
            self.steer_received = threading.Event()
            self.release = threading.Event()
            self.stopped = threading.Event()
            self.prompts = []
            self.lock = threading.Lock()
            self.active = self.peak = 0

        def generate(self, prompt, on_chunk=None, **kwargs):
            with self.lock:
                index = len(self.prompts)
                self.prompts.append(prompt + json.dumps(kwargs.get("messages", []), ensure_ascii=False))
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                assert on_chunk is not None
                if index == 0:
                    on_chunk("首轮正在生成")
                    self.streaming.set()
                    assert self.resume.wait(15), "没有推进插话安全点"
                    return ModelResponse(text="首轮阶段结果", backend=self.name)
                if index == 1:
                    # 返回合法的本地只读工具调用，使插话完成模型ACK后仍保持当前回合活跃。
                    return ModelResponse(text="核对附件后继续", backend=self.name, tool_use_blocks=[{
                        "id": "read-turn-note", "name": "read_file", "input": {"path": str(prompt_file)},
                    }])
                if index == 2:
                    with register_interrupt_callback(self.release.set):
                        on_chunk("接收补充后继续生成")
                        self.steer_received.set()
                        assert self.release.wait(15), "没有收到精确停止"
                        # finally的释放只负责清理，不能充当stop已生效的证据。
                        assert is_interrupted(), "测试释放不能代替业务停止"
                        self.stopped.set()
                        raise InterruptedError("首轮已停止")
                text = "下一轮已接收附件"
                on_chunk(text)
                return ModelResponse(text=text, backend=self.name)
            finally:
                with self.lock:
                    self.active -= 1

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                   gateway_per_user_owner_scoping=True, gateway_native_stream_max=0),
                        tmp_path / "workspace")
    provider = Provider()
    agent.backend = provider
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    jobs, refs = _PlainJobs(), plain._make_plain_input_refs()
    gateway_stop = threading.Event()
    settled = [threading.Event(), threading.Event()]
    failures, controls = [], []
    plain_thread = gateway_thread = listener_thread = None
    http_threads = ()

    def gateway_work():
        try:
            count = 0
            while not gateway_stop.wait(0.01):
                processed = _process_gateway_requests(agent, paths, worker_id="plain-contract")
                if processed:
                    count += processed
                    assert count <= len(settled), "控制不应生成额外模型回合"
                    settled[count - 1].set()
        except BaseException as exc:
            failures.append(exc)

    try:
        server.start()
        listener_thread = server._thread
        # 使用原PID producer记录真实当前进程，不替换客户端存活检查。
        write_pid_record(paths.pid)
        assert plain_handlers.check_gateway_alive(paths)
        client = GatewayChatClientAgent(SimpleNamespace(),
                                       replace(agent.config, gateway_port=server.server.server_address[1]),
                                       agent.root, [agent.root], agent.home_paths)
        original_post = client.post_gateway_json

        def post(path, payload, **kwargs):
            status, body = original_post(path, payload, **kwargs)
            controls.append((path, payload, status, body))
            return status, body

        monkeypatch.setattr(client, "post_gateway_json", post)
        prompt_file = agent.root / "turn-note.txt"
        marker = "PLAIN_PROMPT_FILE_DELIVERY"
        prompt_file.write_text(marker, encoding="utf-8")
        cfg = RunPlainConfig(agent=client, args=SimpleNamespace(no_save=False, memory_limit=10, gateway_timeout=10),
                             use_gateway=True, paths=paths, runtime_inject=[], prompt_files=[str(prompt_file)],
                             conversation_history=[], history_lock=threading.Lock(), jobs=jobs,
                             state_lock=threading.Lock(), build_history_context=lambda: "",
                             session_manager=None, current_session_id="plain-headless")
        worker_cfg = plain_worker._make_plain_worker_cfg(cfg, refs)

        def plain_work():
            try:
                plain_worker._plain_worker(worker_cfg)
            except _PlainWorkerDone:
                pass
            except BaseException as exc:
                failures.append(exc)

        plain_thread = threading.Thread(target=plain_work, name="plain-contract-client", daemon=True)
        gateway_thread = threading.Thread(target=gateway_work, name="plain-contract-gateway", daemon=True)
        plain_thread.start()
        gateway_thread.start()
        assert plain._handle_plain_message(cfg, "首轮消息", refs) is False
        assert provider.streaming.wait(15), repr(failures)
        first_job, = jobs.accepted
        processing, = paths.processing.glob("*.json")
        first_id = read_json_file(processing)["id"]
        assert first_id != first_job.request_id
        with cfg.state_lock:
            assert refs.is_running_ref == [True]
            assert refs.running_request_id_ref == [first_id]
            assert refs.pending_jobs_ref == [0]
        original_message_id = first_job.request_id
        assert first_job.client_message_id == ""
        old_thread = agent.conversation_store.threads.resolve(channel="chat", channel_user_id="local-agent",
                                                             channel_conversation_id=cfg.current_session_id)
        assert old_thread is not None

        assert plain._handle_plain_message(cfg, "/btw 保留这条补充", refs) is False
        path, payload, status, body = controls[-1]
        # 原协议明确区分“正在等待安全点”的202与真正消费后的200/accepted。
        assert path == "/control" and status == 202 and body["ok"] is True
        assert body["kind"] == "steer" and body["request_id"] == first_id
        assert payload["metadata"]["expected_turn_id"] == first_id
        assert body["delivery_status"] == "unknown"
        assert [row.message for row in agent.conversation_store.guidance.pending("request", first_id)] == ["保留这条补充"]
        operation_id = body["operation_id"]
        provider.resume.set()
        assert provider.steer_received.wait(15), repr(failures)
        status, delivered = client.get_gateway_json(f"/control-status/{operation_id}", timeout=5,
                                                    conversation_id=cfg.current_session_id)
        assert status == 200 and delivered["ok"] is True
        assert delivered["kind"] == "steer" and delivered["request_id"] == first_id
        assert delivered["delivery_status"] == "accepted" and delivered["control_state"] == "completed"
        assert agent.conversation_store.guidance.pending("request", first_id) == []
        assert "保留这条补充" in provider.prompts[1]

        assert plain._handle_plain_message(cfg, "/stop", refs) is False
        path, payload, status, body = controls[-1]
        assert path == "/control" and status == 200 and body["ok"] is True
        assert body["kind"] == "stop" and body["request_id"] == first_id
        assert payload["metadata"]["expected_turn_id"] == first_id
        assert provider.stopped.wait(15), repr(failures)
        assert settled[0].wait(15) and jobs.drained.wait(15), repr(failures)
        first_terminal = paths.terminal / f"{first_id}.json"
        stopped_bytes = first_terminal.read_bytes()
        stopped = read_gateway_terminal_response_file(first_terminal, request_id=first_id)
        assert stopped["status"] == "interrupted" and stopped["error_code"] == "INTERRUPTED"
        assert first_job.completion_status == "interrupted"
        assert agent.conversation_store.guidance.pending("request", first_id) == []

        assert plain._handle_plain_message(cfg, "/new", refs) is False
        path, payload, status, body = controls[-1]
        assert path == "/control" and status == 200 and body["ok"] is True and body["kind"] == "new"
        new_thread = agent.conversation_store.threads.resolve(channel="chat", channel_user_id="local-agent",
                                                             channel_conversation_id=cfg.current_session_id)
        assert new_thread is not None and new_thread.thread_id != old_thread.thread_id
        assert plain._handle_plain_message(cfg, "下一轮消息", refs) is False
        assert settled[1].wait(15) and jobs.drained.wait(15), repr(failures)
        assert not failures
        assert len(jobs.accepted) == 2 and len(controls) == 3
        second_job = jobs.accepted[1]
        terminals = list(paths.terminal.glob("*.json"))
        assert len(terminals) == 2
        second_terminal, = (path for path in terminals if path != first_terminal)
        next_response = read_gateway_terminal_response_file(second_terminal, request_id=second_terminal.stem)
        assert next_response["ok"] is True and next_response["response"] == "下一轮已接收附件"
        assert second_job.completion_status == "completed"
        assert first_job.request_id == original_message_id and first_job.client_message_id == ""
        assert second_terminal.stem != second_job.request_id and second_job.client_message_id == ""
        assert len(provider.prompts) == 4 and all(marker in prompt for prompt in provider.prompts)
        assert provider.peak == 1 and provider.active == 0
        assert first_terminal.read_bytes() == stopped_bytes
        assert refs.assistant_outputs == ["下一轮已接收附件"]
        assert jobs.unfinished_tasks == 0 and jobs.empty()
        with cfg.state_lock:
            assert refs.is_running_ref == [False] and refs.pending_jobs_ref == [0]
            assert refs.running_request_id_ref == [""] and refs.running_prompt_ref == [""]
            assert refs.running_started_at_ref == [0.0] and refs.local_run_ref == [None]
        assert not list(paths.inbox.glob("*.json")) and not list(paths.processing.glob("*.json"))
        # 下一轮确实沿/new发表的新绑定落库，而不是只改变了控制回执里的显示字段。
        messages = agent.conversation_store.messages.recent(new_thread.thread_id, limit=10)
        assert any(row.role == "user" and row.content == "下一轮消息" for row in messages)
        assert any(row.role == "assistant" and row.content == "下一轮已接收附件" for row in messages)
    finally:
        provider.resume.set()
        provider.release.set()
        gateway_stop.set()
        if plain_thread is not None:
            jobs.put(None)
        for thread in (gateway_thread, plain_thread):
            if thread is not None:
                thread.join(15)
        if server.server is not None:
            http_threads = tuple(server.server._request_executor._threads)
        server.stop()
        for thread in http_threads:
            thread.join(5)
        assert all(thread is None or not thread.is_alive()
                   for thread in (gateway_thread, plain_thread, listener_thread, *http_threads))
        assert jobs.unfinished_tasks == 0
