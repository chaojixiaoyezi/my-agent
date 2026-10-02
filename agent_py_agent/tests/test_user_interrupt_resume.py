"""第 7 条（用户 10-02 选 A）：Esc / /interrupt 中断后，隔几分钟再发消息仍接着原任务。

来源：TUI 的 Esc 与 /interrupt 同一入口，只中断本轮、任务关联保持 active。约 2–4 分钟后发现层账本自愈
（owner_wake_discovery._project_task_ledger_terminal，operator wake-discovery-ledger-heal）看到"主执行轮已
cancelled、关联还 active"，把任务收成 cancelled、关掉 TaskRun；之后同会话的新消息开新任务目录（38 在 C12
观察里报告，DESIGN_LEDGER C12a/C12b 的待定项）。用户的习惯是 Esc 后过一会儿发"继续"。

修法只认结构化事实：中断到一轮时给被中断的主执行代次记一条 runtime_events（runtime_db.user_interrupt），
账本自愈在续接窗口内跳过它；过了窗口照原规则收口（原因 user_interrupt_expired）。不看用户发的文字。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \
        agent_py_agent/tests/test_user_interrupt_resume.py
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.backends import gateway_helpers, http
from agent_py_agent.agent.concurrency.interrupt import register_interruptible
from agent_py_agent.agent.conversation.control_commands import (
    conversation_request_interrupt_name,
    parse_conversation_control,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_service, request_execution
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.recovery import terminalize_gateway_request_file
from agent_py_agent.agent.owner_wake_discovery import unfinished_task_ids
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.user_interrupt import (
    USER_INTERRUPT_EVENT,
    USER_INTERRUPT_RESUME_SECONDS,
    record_user_interrupt,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_parts.control_runtime import (
    ChatControlExecution,
    ChatControlState,
    execute_chat_control,
)
from agent_py_agent.tests.test_task_resource_stop import (
    main_task as main_task,  # noqa: F401 本地入口夹具
)

_HOLD_SECONDS = 30.0


# ---------------------------------------------------------------- 合同：账本自愈与续接窗口


@pytest.fixture
def interrupted_owner(tmp_path):
    """主执行轮被用户中断后 cancelled、关联与任务账本仍 active/RUNNING 的 owner home（同 R1-03 自愈夹具的形状）。"""
    home = tmp_path / "home"
    repo = RuntimeRepository(home / "runtime.db")
    task_id = "gwreq-esc-1"
    run = repo.record_run_creation(
        owner_id="local/main", goal="整理目录", conversation_task_id=task_id, run_id=task_id, role="main",
    )
    task_root = home / "tasks" / "2026-10-02" / "tidy"
    (task_root / "work").mkdir(parents=True)
    (task_root / "work" / "state.json").write_text(
        json.dumps({"status": "RUNNING", "task_id": task_id, "primary_run_id": task_id}), encoding="utf-8",
    )
    (task_root / "work" / "run_workspace.json").write_text(
        json.dumps({"request_id": task_id, "run_id": task_id, "task_id": task_id}), encoding="utf-8",
    )
    link_dir = home / "workspace" / "runtime" / "workspaces" / "main-x" / "conversations" / "tasks"
    link_dir.mkdir(parents=True)
    link = {"task_id": task_id, "status": "active", "task_path": "tasks/2026-10-02/tidy",
            "thread_id": "thread-1", "goal": "整理目录"}
    (link_dir / f"{task_id}.json").write_text(json.dumps(link), encoding="utf-8")
    return {"home": home, "repo": repo, "task_id": task_id, "run": run, "task_root": task_root,
            "link_path": link_dir / f"{task_id}.json"}


def _interrupt_and_cancel(owner: dict, *, minutes_ago: float) -> None:
    """真实顺序：中断时先记事件，执行循环随后把这一代收成 cancelled；事件时刻按"几分钟前"回拨。"""
    repo, run = owner["repo"], owner["run"]
    assert record_user_interrupt(repo, owner["task_id"], source="gateway_interrupt") is True
    repo.settle_agent_run(
        agent_run_id=run["agent_run_id"], status="cancelled", attempt_id=run["attempt_id"],
        payload={"status": "cancelled", "runtime_status": "cancelled", "runtime_reason": "user_stop"},
    )
    with repo.transaction() as conn:
        conn.execute("UPDATE runtime_events SET created_at = ? WHERE event_type = ?",
                     (time.time() - minutes_ago * 60, USER_INTERRUPT_EVENT))


def _task_run(repo: RuntimeRepository, task_id: str):
    return repo._runtime_connect().execute(
        "SELECT status, closed_at, task_run_id FROM task_runs WHERE task_id = ?", (task_id,),
    ).fetchone()


def _events(repo: RuntimeRepository, event_type: str) -> list[dict]:
    rows = repo._runtime_connect().execute(
        "SELECT payload_json FROM runtime_events WHERE event_type = ? ORDER BY seq", (event_type,),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_user_interrupted_task_stays_resumable_five_minutes_later(interrupted_owner):
    owner = interrupted_owner
    _interrupt_and_cancel(owner, minutes_ago=5)
    deadlines: list[float] = []

    remaining = unfinished_task_ids(owner["home"], deadline_out=deadlines)

    assert owner["task_id"] not in remaining  # 被中断的执行不会被自动驱动
    assert _read(owner["link_path"])["status"] == "active"
    assert _read(owner["task_root"] / "work" / "state.json")["status"] == "RUNNING"
    assert _task_run(owner["repo"], owner["task_id"])["closed_at"] == 0
    assert _events(owner["repo"], "status_conflict") == []
    # 判定缓存到窗口截止时刻必须重算，过期才能照原规则收口。
    assert deadlines and abs(min(deadlines) - (time.time() - 300 + USER_INTERRUPT_RESUME_SECONDS)) < 5


def test_user_interrupt_past_the_window_is_closed_with_its_own_reason(interrupted_owner):
    owner = interrupted_owner
    _interrupt_and_cancel(owner, minutes_ago=USER_INTERRUPT_RESUME_SECONDS / 60 + 1)

    unfinished_task_ids(owner["home"])

    assert _read(owner["link_path"])["status"] == "cancelled"
    assert _read(owner["task_root"] / "work" / "state.json")["status"] == "CANCELLED"
    row = _task_run(owner["repo"], owner["task_id"])
    assert row["status"] == "cancelled" and row["closed_at"] > 0
    [closed] = _events(owner["repo"], "task_run.closed")
    assert closed["operator"] == "wake-discovery-ledger-heal"
    assert closed["reason"] == "user_interrupt_expired"


def test_interrupt_on_an_older_attempt_does_not_protect_the_current_run(interrupted_owner):
    """事件绑定精确代次：用户中断过上一代、这一代是被别的原因取消的，自愈照旧收口。"""
    owner = interrupted_owner
    repo, run = owner["repo"], owner["run"]
    record_user_interrupt(repo, owner["task_id"], source="gateway_interrupt")
    newer = repo.create_attempt(agent_run_id=run["agent_run_id"])
    repo.settle_agent_run(agent_run_id=run["agent_run_id"], status="cancelled", attempt_id=newer["attempt_id"],
                          payload={"status": "cancelled", "runtime_status": "cancelled"})

    unfinished_task_ids(owner["home"])

    assert _read(owner["link_path"])["status"] == "cancelled"
    [closed] = _events(repo, "task_run.closed")
    assert closed["reason"] == "terminal_run_ledger_stale"


# ---------------------------------------------------------------- 合同：Gateway 控制入口


def _gateway_agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)


def _feishu_task(agent: SimpleAgent, task_id: str) -> tuple[object, dict]:
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "u-1", "channel": "feishu", "channel_conversation_id": "c-1",
        "channel_user_id": "u-1", "now": time.time() - 40,
    })
    agent.conversation_store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": "整理目录",
                                         "status": "active", "now": time.time() - 30})
    run = agent.subagents.runtime_db.record_run_creation(
        owner_id="local/main", goal="整理目录", conversation_task_id=task_id, run_id=task_id, role="main",
    )
    return thread, run


def _control(agent: SimpleAgent, text: str):
    command = parse_conversation_control(text)
    assert command is not None
    return execute_gateway_conversation_control(
        agent, gateway_paths(agent), command, GatewayControlScope("u-1", "feishu", "c-1"),
    )


def test_gateway_interrupt_records_the_current_attempt(tmp_path):
    agent = _gateway_agent(tmp_path)
    _thread, run = _feishu_task(agent, "gwreq-esc-2")

    with register_interruptible(conversation_request_interrupt_name("gwreq-esc-2")):
        result = _control(agent, "/interrupt")

    assert result.ok, result.message
    repo = agent.subagents.runtime_db
    [event] = repo.events_for_agent_run(run["agent_run_id"], event_type=USER_INTERRUPT_EVENT)
    assert event["attempt_id"] == run["attempt_id"]
    assert event["payload"]["source"] == "gateway_interrupt"
    assert agent.conversation_store.tasks.load("gwreq-esc-2").status == "active"


def test_idle_interrupt_records_nothing(tmp_path):
    agent = _gateway_agent(tmp_path)
    _thread, run = _feishu_task(agent, "gwreq-esc-3")

    result = _control(agent, "/interrupt")  # 没有运行中的回合

    assert result.ok is False
    assert agent.subagents.runtime_db.events_for_agent_run(run["agent_run_id"], event_type=USER_INTERRUPT_EVENT) == []


def test_stop_within_the_window_still_stops_the_task_and_its_processes(tmp_path, monkeypatch):
    """C12 那组别退回去：中断后窗口内关联仍 active，/stop 走正常任务停止，冻结并交出本任务的资源。"""
    agent = _gateway_agent(tmp_path)
    task_id = "gwreq-esc-4"
    _thread, run = _feishu_task(agent, task_id)
    with register_interruptible(conversation_request_interrupt_name(task_id)):
        assert _control(agent, "/interrupt").ok
    repo = agent.subagents.runtime_db
    repo.settle_agent_run(agent_run_id=run["agent_run_id"], status="cancelled", attempt_id=run["attempt_id"],
                          payload={"status": "cancelled", "runtime_status": "cancelled"})
    unfinished_task_ids(Path(agent.home_paths.owner_home_dir))  # 自愈在窗口内跳过
    assert agent.conversation_store.tasks.load(task_id).status == "active"
    cancelled: list[tuple[str, object]] = []
    monkeypatch.setattr(control_service, "_cancel_request_execution",
                        lambda request_id, *, resources: cancelled.append((request_id, resources)))

    result = _control(agent, "/stop")

    assert result.ok, result.message
    assert "当前任务正在停止" in result.message
    assert agent.conversation_store.tasks.load(task_id).status == "interrupted"
    assert [request_id for request_id, _resources in cancelled] == [task_id]
    assert cancelled[0][1] is not None and cancelled[0][1].main is not None


def test_local_interrupt_records_the_published_attempt(main_task):  # noqa: F811 夹具
    """本地 chat 入口与 Gateway 同一事实源，用本次调用已发布的精确身份。"""
    from agent_py_agent.agent.conversation.local_run_control import LocalRunControl

    agent, _link, record, _scope, _store = main_task
    control = LocalRunControl("message")
    control.bind_runtime_authority({
        "invocation_run_id": "message", "task_id": "task", "run_id": "main",
        "agent_run_id": record["agent_run_id"], "attempt_id": record["attempt_id"],
    })

    result = execute_chat_control(
        ChatControlExecution(agent, False, ChatControlState(True, 0, "工作", 0, "session", "message", control)),
        parse_conversation_control("/interrupt"),
    )

    assert result.ok
    [event] = agent.subagents.runtime_db.events_for_agent_run(record["agent_run_id"], event_type=USER_INTERRUPT_EVENT)
    assert event["attempt_id"] == record["attempt_id"]
    assert event["payload"] == {"task_id": "task", "source": "local_interrupt",
                                "resume_window_seconds": USER_INTERRUPT_RESUME_SECONDS}


# ---------------------------------------------------------------- 真实链路：Gateway ask + 脚本化假模型


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


# 类用途: 进程内的供应商假线路：IR-WORK 先 list_files（晋升会话任务）再挂起等中断；IR-CONTINUE 先 list_files 再收尾。
class _Wire:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.holding = threading.Event()
        self.interrupted = threading.Event()

    def __call__(self, request) -> dict:
        payload = json.loads(json.dumps(request.payload))
        gateway_helpers._emit_provider_attempt({"attempt_id": f"ir-{next(self._ids)}", "status": "started"})
        names = [str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or []]
        if names == ["my_agent_capability_probe"]:
            import re

            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload, ensure_ascii=False))
            return self._reply(payload, None, ("my_agent_capability_probe", {"nonce": nonce.group(1) if nonce else ""}))
        messages = [m for m in payload.get("messages") or [] if isinstance(m, dict)]
        starts = [i for i, m in enumerate(messages)
                  if m.get("role") == "user" and _text_of(m.get("content")).lstrip().startswith("# User Task")]
        start = starts[-1] if starts else 0
        heading = _text_of(messages[start].get("content")) if messages else ""
        listed = any(m.get("role") == "tool" for m in messages[start + 1:])
        if not listed:
            return self._reply(payload, None, ("list_files", {"path": "."}))
        if "IR-WORK" in heading:
            self._hold()
        return self._reply(payload, "IR-DONE 好的，接着做完了。", None)

    # 函数用途: 与真实传输一样登记停止回调，被 /interrupt 打断时像断开的连接一样抛出。
    def _hold(self) -> None:
        woken = threading.Event()
        self.holding.set()
        deadline = time.time() + _HOLD_SECONDS
        with gateway_helpers._provider_interrupt_callback(woken.set):
            while not gateway_helpers._provider_is_interrupted() and time.time() < deadline:
                woken.wait(0.05)
            stopped = gateway_helpers._provider_is_interrupted()
        if not stopped:
            raise TimeoutError("测试挂起点超时，没有收到中断")
        self.interrupted.set()
        raise InterruptedError("模型接口请求已被用户停止")

    def _reply(self, payload: dict, text: str | None, call: tuple[str, dict] | None) -> dict:
        message: dict = {"role": "assistant", "content": text}
        finish = "stop"
        if call is not None:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_ir_{next(self._ids)}", "type": "function",
                "function": {"name": call[0], "arguments": json.dumps(call[1], ensure_ascii=False)},
            }]}
            finish = "tool_calls"
        return {"id": f"chatcmpl-ir-{next(self._ids)}", "object": "chat.completion", "created": int(time.time()),
                "model": str(payload.get("model") or ""),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020}}


def _real_agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(
        model_backend="openai_compatible", api_base="http://127.0.0.1:9/v1", model_name="ir-scripted",
        api_key="ir-fake-key-not-a-credential", stream_enabled=False, model_context_window_tokens=200_000,
        enable_tools=True, memory_path="memory.jsonl", my_agent_home=str(tmp_path / "home"),
        gateway_workspace="gateway", orphan_supervision_interval_seconds=0, memory_curator_enabled=False,
        enable_self_learning=False,
    ), tmp_path / "root")


# 函数用途: 以 TUI 会话身份经真实 worker 入口（request_execution._handle_gateway_request）跑一轮 Gateway ask，
#   登记可中断名、按结果收尾，返回它落下的响应；回合被中断时与生产一样由 worker 接住，不往外抛。
def _ask(agent: SimpleAgent, paths, request_id: str, prompt: str) -> dict:
    owner = agent.home_paths.owner_id
    request = {"id": request_id, "kind": "ask", "prompt": prompt, "status": "processing", "turn_phase": "open",
               "execution_attempt_id": f"ir-attempt-{request_id}",
               "conversation": {"canonical_user_id": owner, "channel": "chat",
                                "channel_conversation_id": "ir-session", "channel_user_id": owner}}
    path = paths.processing / f"{request_id}.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    response: dict = {}
    try:
        with register_interruptible(conversation_request_interrupt_name(request_id)):
            response = request_execution._handle_gateway_request(agent, path)
        return response
    finally:
        status = "done" if response.get("ok") else "failed"
        terminalize_gateway_request_file(
            paths, path, paths.done if status == "done" else paths.failed, request_id,
            conversation_store=agent.conversation_store,
            terminal_response={"id": request_id, "status": status, "ok": status == "done"},
        )


# 函数用途: 搭真实环境：真实 SimpleAgent 与 Gateway 请求目录，只替换供应商传输。
def _real_setup(tmp_path: Path, monkeypatch) -> tuple[SimpleAgent, object, _Wire]:
    agent = _real_agent(tmp_path)
    wire = _Wire()
    monkeypatch.setattr(http, "post_json", wire)
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    return agent, paths, wire


# 函数用途: 第一轮在模型调用上挂起时，以同一会话身份发 /interrupt（与 TUI 的 Esc 同一入口），返回第一轮的响应。
def _interrupted_first_turn(agent: SimpleAgent, paths, wire: _Wire) -> dict:
    outcome: list[object] = []
    worker = threading.Thread(
        target=lambda: outcome.append(_ask(agent, paths, "ir-work-1", "IR-WORK 整理一下当前目录")), daemon=True,
    )
    worker.start()
    assert wire.holding.wait(_HOLD_SECONDS), "第一轮没有走到挂起点"
    scope = GatewayControlScope(agent.home_paths.owner_id, "chat", "ir-session")
    interrupted = execute_gateway_conversation_control(agent, paths, parse_conversation_control("/interrupt"), scope)
    worker.join(_HOLD_SECONDS)
    assert interrupted.ok, interrupted.message
    assert wire.interrupted.is_set() and not worker.is_alive() and len(outcome) == 1
    return outcome[0]


# 函数用途: 取测试会话的线程（与请求里的渠道身份一致）。
def _session_thread(agent: SimpleAgent):
    owner = agent.home_paths.owner_id
    return agent.conversation_store.threads.get_or_create({
        "canonical_user_id": owner, "channel": "chat", "channel_conversation_id": "ir-session", "channel_user_id": owner,
    })


def test_esc_then_continue_five_minutes_later_resumes_the_same_task(tmp_path, monkeypatch):
    agent, paths, wire = _real_setup(tmp_path, monkeypatch)
    assert _interrupted_first_turn(agent, paths, wire).get("status") == "interrupted"
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    thread = _session_thread(agent)
    [link] = store.tasks.list(thread.thread_id)
    main = repo.main_agent_run_for_task(link.task_id)
    assert link.status == "active" and main["status"] == "cancelled"

    # 过了 5 分钟：发现层账本自愈照常跑一遍。
    with repo.transaction() as conn:
        conn.execute("UPDATE runtime_events SET created_at = created_at - 300 WHERE event_type = ?",
                     (USER_INTERRUPT_EVENT,))
    unfinished_task_ids(Path(agent.home_paths.owner_home_dir))
    assert store.tasks.load(link.task_id).status == "active"

    continued = _ask(agent, paths, "ir-continue-2", "IR-CONTINUE 继续")
    assert continued.get("ok") is True, continued

    links = store.tasks.list(thread.thread_id)
    assert [item.task_id for item in links] == [link.task_id], "继续应接着原任务，不能另开新任务"
    assert links[0].task_path == link.task_path
    resumed = repo.main_agent_run_for_task(link.task_id)
    assert resumed["agent_run_id"] == main["agent_run_id"]
    assert int(resumed["current_attempt_generation"]) > int(main["current_attempt_generation"])
