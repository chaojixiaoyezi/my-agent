"""会话互通派活链：真实 Gateway 链路集成测试（脚本化假模型，进程内，不走网络）。

为什么要有这组测试：会话互通连续两次交付都是"单测全绿、真实链路坏掉"——单测要么替换了线程/任务关联/
唤醒，要么手工拼后台回合参数，绕过了真实的 唤醒 → 认领 → run_claimed → 回合装配。这里一律走产品入口：
- 前台回合：真实 Gateway ask（request_execution._run_gateway_ask），请求文件与收尾同真实 worker；
- 后台回合：Gateway 自己构造的后台调度器（gateway_loops._build_background_scheduler）逐轮 tick，
  真实唤醒队列、后台认领、run_claimed、回合装配与工具循环；
- 模型：只替换供应商传输（backends.http.post_json），请求组装、响应解析、工具协议都走真实
  openai_compatible 后端；假线路按结构化标记（回合开头、测试者写的标记、任务正文标记、工具结果）出招，
  挂起时与真实传输一样登记停止回调，被停止控制打断就抛 InterruptedError。

判定只看结构化事实：会话任务记录、guidance 一次性回执、唤醒队列、工具返回的结构化字段、工具操作账本、
模型调用计数。已知未修的缺陷用 strict xfail 标出（只接受 AssertionError）；链路本身断掉时抛
RealChainBroken，不会被 xfail 吞掉。修好之后 strict xfail 会以 XPASS 失败，提醒把标记去掉转正。
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import PENDING_TURN_INPUT_INVALIDATION_LIMIT
from agent_py_agent.agent.backends import gateway_helpers, http
from agent_py_agent.agent.conversation import FakeDeliveryService
from agent_py_agent.agent.conversation.store_guidance import GuidanceStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_context, request_execution
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.recovery import terminalize_gateway_request_file
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.gateway_loops import _build_background_scheduler

_SESSION_TASK_LINE = "另一个会话派来一个任务"
_HOLD_SECONDS = 30.0
_JOIN_SECONDS = 60.0
_HELD_FINAL = "RC-HOLD-FINAL 挂起的回合被放行后交付的答复。"
# 空转保护：同一回合的模型调用超过这个数就按"用户停止"打断，让回归以计数失败而不是把测试挂死。
_SPIN_GUARD_CALLS = 40


# 类用途: 链路本身断掉（没开出回合、后台 tick 抛错、前置条件不成立）；与已知缺陷的 AssertionError 区分开。
class RealChainBroken(Exception):
    pass


# 函数用途: 前置条件检查；不成立时抛 RealChainBroken，strict xfail 不会把它当成"预期中的失败"。
def _require(condition: object, message: str) -> None:
    if not condition:
        raise RealChainBroken(message)


# 函数用途: 把 OpenAI 消息内容（字符串或分段列表）拼成纯文本，供假线路读结构化标记。
def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


# 函数用途: 解析工具结果或宿主事实里的 JSON；解析不了时保留前 200 字，便于失败时定位。
def _parse_json(text: str) -> dict:
    start = text.find("{")
    try:
        value = json.loads(text[start:] if start >= 0 else text)
    except ValueError:
        return {"raw": text[:200]}
    return value if isinstance(value, dict) else {"raw": text[:200]}


# 类用途: 一次模型请求在假线路眼里的结构化形状。
@dataclass
class _Turn:
    trigger: str
    heading: str
    turn_text: str
    results: dict[str, dict]
    tools: tuple[str, ...]
    facts: dict = field(default_factory=dict)


# 函数用途: 按回合开头（# User Task / # Host Event）切出当前回合，并收集本回合的工具结果。
def _turn_shape(payload: dict) -> _Turn:
    messages = [message for message in payload.get("messages") or [] if isinstance(message, dict)]
    tools = tuple(str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or [])
    headings = [index for index, message in enumerate(messages) if message.get("role") == "user"
                and _text_of(message.get("content")).lstrip().startswith(("# User Task", "# Host Event"))]
    if not headings:
        return _Turn("", "", "", {}, tools)
    last = headings[-1]
    text = _text_of(messages[last].get("content")).lstrip()
    trigger = "user" if text.startswith("# User Task") else "host"
    prior = max((index for index in range(last) if messages[index].get("role") == "assistant"), default=-1)
    turn_text = "\n".join(_text_of(message.get("content")) for message in messages[prior + 1:]
                          if message.get("role") != "assistant")
    names = {}
    for message in messages[last + 1:]:
        for call in message.get("tool_calls") or []:
            names[str(call.get("id") or "")] = str((call.get("function") or {}).get("name") or "")
    results = {}
    for message in messages[last + 1:]:
        if message.get("role") == "tool":
            results[names.get(str(message.get("tool_call_id") or ""), "")] = _parse_json(_text_of(message.get("content")))
    lines = text.split("\n", 2)
    facts = _parse_json(text) if trigger == "host" and "{" in text else {}
    return _Turn(trigger, lines[1] if len(lines) > 1 else "", turn_text, results, tools, facts)


# 类用途: 进程内的供应商假线路：按结构化标记出招、记录每次调用，并能像真实传输一样被停止控制打断。
class ScriptedWire:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.calls: list[dict] = []
        self.holding: dict[str, threading.Event] = {}
        self.releases: dict[str, threading.Event] = {}
        self.interrupted: list[str] = []
        self._per_turn: dict[str, int] = {}

    # 函数用途: 替换 backends.http.post_json；探测请求照实回答，其余交给脚本并记账。
    def __call__(self, request) -> dict:
        payload = json.loads(json.dumps(request.payload))
        gateway_helpers._emit_provider_attempt({"attempt_id": f"rc-{next(self._ids)}", "status": "started"})
        names = [str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or []]
        if names == ["my_agent_capability_probe"]:
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload, ensure_ascii=False))
            return self._reply(payload, None, ("my_agent_capability_probe", {"nonce": nonce.group(1) if nonce else ""}))
        turn = _turn_shape(payload)
        # 挂起中被停止控制打断时 _script 会抛出，这次调用的 kind 保持 interrupted。
        entry = {"kind": "interrupted", "trigger": turn.trigger, "task": str(turn.facts.get("session_task_id") or ""),
                 "heading": turn.heading[:60], "tools": turn.tools, "results": turn.results,
                 "saw_note": "RC-NOTE-" in turn.turn_text}
        turn_key = entry["task"] or entry["heading"]
        with self._lock:
            self.calls.append(entry)
            self._per_turn[turn_key] = self._per_turn.get(turn_key, 0) + 1
            spinning = self._per_turn[turn_key] > _SPIN_GUARD_CALLS
        if spinning:
            entry["kind"] = "spin-guard"
            raise InterruptedError(f"RC 假线路：同一回合模型调用超过 {_SPIN_GUARD_CALLS} 次，判定为空转")
        kind, text, call = self._script(turn)
        entry["kind"] = kind
        return self._reply(payload, text, call)

    # 函数用途: 测试者控制的挂起点：先登记与真实传输相同的停止回调，被打断时像断开的连接一样抛出。
    def hold(self, name: str) -> None:
        release = self.releases.setdefault(name, threading.Event())
        self.holding.setdefault(name, threading.Event()).set()
        woken = threading.Event()
        deadline = time.time() + _HOLD_SECONDS
        with gateway_helpers._provider_interrupt_callback(woken.set):
            while not release.is_set():
                if gateway_helpers._provider_is_interrupted():
                    self.interrupted.append(name)
                    raise InterruptedError("模型接口请求已被用户停止")
                if time.time() > deadline:
                    raise TimeoutError(f"测试挂起点 {name} 超过 {_HOLD_SECONDS} 秒没有放行")
                woken.wait(0.05)

    # 函数用途: 测试者一侧：等某个挂起点真的挂上（说明模型调用正在进行）。
    def wait_holding(self, name: str) -> bool:
        return self.holding.setdefault(name, threading.Event()).wait(_HOLD_SECONDS)

    # 函数用途: 测试者一侧：放行某个挂起点。
    def release(self, name: str) -> None:
        self.releases.setdefault(name, threading.Event()).set()

    # 函数用途: 按会话任务号统计派活回合里的模型调用次数。
    def calls_for_task(self, task_id: str) -> list[dict]:
        with self._lock:
            return [call for call in self.calls if call["task"] == task_id]

    # 函数用途: 把脚本动作包装成 OpenAI chat.completion 响应。
    def _reply(self, payload: dict, text: str | None, call: tuple[str, dict] | None) -> dict:
        message: dict = {"role": "assistant", "content": text}
        finish = "stop"
        if call is not None:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_rc_{next(self._ids)}", "type": "function",
                "function": {"name": call[0], "arguments": json.dumps(call[1], ensure_ascii=False)},
            }]}
            finish = "tool_calls"
        return {"id": f"chatcmpl-rc-{next(self._ids)}", "object": "chat.completion", "created": int(time.time()),
                "model": str(payload.get("model") or ""),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020}}

    # 函数用途: 脚本总入口：派活回合、测试者输入的前台回合、其余（唤醒回合等）三类。
    def _script(self, turn: _Turn) -> tuple[str, str | None, tuple[str, dict] | None]:
        if turn.trigger == "host" and _SESSION_TASK_LINE in turn.heading:
            return self._task_turn(turn)
        if turn.trigger == "user" and turn.heading.startswith("RC-"):
            return self._user_turn(turn)
        return ("wake-saw-note" if "RC-NOTE-" in turn.turn_text else "other"), "RC-ACK 收到。", None

    # 函数用途: 测试者输入的前台回合：派活、发普通消息、取消、在忙时挂起。
    def _user_turn(self, turn: _Turn) -> tuple[str, str | None, tuple[str, dict] | None]:
        verb, *words = turn.heading.split()
        tool, final = {"RC-DISPATCH": ("create_session_task", "RC-DISPATCH-DONE"),
                       "RC-MESSAGE": ("send_session_message", "RC-MESSAGE-DONE"),
                       "RC-CANCEL": ("cancel_session_task", "RC-CANCEL-DONE")}.get(verb, ("", ""))
        if tool and tool in turn.results:
            return f"user-{verb.lower()}-final", final, None
        if verb == "RC-DISPATCH":
            return "user-dispatch", None, (tool, {"target_thread_id": words[0], "goal": " ".join(words[1:])})
        if verb == "RC-MESSAGE":
            return "user-message", None, (tool, {"target_thread_id": words[0], "message": " ".join(words[1:])})
        if verb == "RC-CANCEL":
            return "user-cancel", None, (tool, {"task_id": words[0]})
        if verb == "RC-BUSY":
            if "list_files" not in turn.results:
                self.hold("busy")
                return "user-busy-list", None, ("list_files", {"path": "."})
            return "user-busy-final", "RC-BUSY-DONE 忙完了。", None
        if verb == "RC-LIST-THEN":
            return self._list_then_send(words[0], turn)
        return "user-other", "RC-ACK 好的。", None

    # 函数用途: 先列本 owner 的会话，再只凭清单挑目标（非当前、最近活动、允许该类型），派活或发消息。
    def _list_then_send(self, kind: str, turn: _Turn) -> tuple[str, str | None, tuple[str, dict] | None]:
        tool = "create_session_task" if kind == "task" else "send_session_message"
        if tool in turn.results:
            return "user-list-then-final", "RC-LIST-THEN-DONE", None
        if "list_owner_sessions" not in turn.results:
            return "user-list", None, ("list_owner_sessions", {})
        rows = turn.results["list_owner_sessions"].get("sessions") or []
        candidates = [row for row in rows if not row.get("is_current") and kind in (row.get("allowed_kinds") or [])]
        if not candidates:
            return "user-list-no-target", "RC-LIST-THEN-NO-TARGET", None
        target = candidates[0]["thread_id"]
        if kind == "task":
            return "user-list-dispatch", None, (tool, {"target_thread_id": target, "goal": "RC-GOAL-DONE 由清单选出的目标执行。"})
        return "user-list-message", None, (tool, {"target_thread_id": target, "message": "RC-NOTE-LISTED 由清单选出的目标。"})

    # 函数用途: 派活回合：按任务正文标记直接完成、沿链继续派、或在模型调用中挂起等取消。
    def _task_turn(self, turn: _Turn) -> tuple[str, str | None, tuple[str, dict] | None]:
        goal = re.search(r"RC-(?:GOAL|CHAIN)[^\n]*", turn.turn_text)
        marker = goal.group(0) if goal else ""
        if marker.startswith("RC-CHAIN"):
            return self._chain_step(marker, turn)
        if marker.startswith("RC-GOAL-HOLD-FIRST"):
            self.hold("task")
            return "task-hold-released", _HELD_FINAL, None
        if marker.startswith("RC-GOAL-HOLD-AFTER-TOOL"):
            if "list_files" not in turn.results:
                return "task-hold-list", None, ("list_files", {"path": "."})
            self.hold("task")
            return "task-hold-released", _HELD_FINAL, None
        if marker.startswith("RC-GOAL-DONE"):
            return "task-done", "RC-TASK-DONE 任务完成。", None
        return "task-unknown", "RC-TASK-UNKNOWN", None

    # 函数用途: 链式派活：正文里的 next= 列表就是后续各跳的目标，每一跳派给第一个、把余下的交给它。
    def _chain_step(self, marker: str, turn: _Turn) -> tuple[str, str | None, tuple[str, dict] | None]:
        hops = [hop for hop in (re.search(r"next=(\S*)", marker).group(1).split(",")) if hop]
        if not hops or "create_session_task" in turn.results:
            return "task-chain-final", "RC-CHAIN-DONE 本跳完成。", None
        return "task-chain-create", None, ("create_session_task", {
            "target_thread_id": hops[0], "goal": f"RC-CHAIN next={','.join(hops[1:])} 链式派活。"})


# 类用途: 一次测试的真实环境：同一个 owner 下的三个本地会话、真实 Gateway 路径与后台调度器。
@dataclass
class RealChain:
    agent: SimpleAgent
    paths: object
    wire: ScriptedWire
    threads: dict[str, str]
    scheduler: object
    asks: itertools.count = field(default_factory=lambda: itertools.count(1))

    # 函数用途: 以某个会话的身份跑一轮真实 Gateway 前台 ask（与真实 worker 同样落 processing 文件并收尾）。
    def ask(self, label: str, prompt: str):
        request_id = f"rc-{label.lower()}-{next(self.asks)}"
        request = {"id": request_id, "kind": "ask", "prompt": prompt, "status": "processing", "turn_phase": "open",
                   "execution_attempt_id": f"rc-attempt-{request_id}", "conversation": self._conversation(label)}
        path = self.paths.processing / f"{request_id}.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        context = request_context.GatewayAskRunContext(
            self.agent, request, path, self.paths.responses / f"{request_id}.json", request_id, None,
        )
        status = "done"
        try:
            return request_execution._run_gateway_ask(context)
        except BaseException:
            status = "failed"
            raise
        finally:
            terminalize_gateway_request_file(
                self.paths, path, self.paths.done if status == "done" else self.paths.failed, request_id,
                conversation_store=self.agent.conversation_store,
                terminal_response={"id": request_id, "status": status, "ok": status == "done"},
            )

    # 函数用途: 跑后台调度器直到没有待处理唤醒（或达到上限）；后台回合在真实链路上抛错时报 RealChainBroken。
    def drain(self, *, max_ticks: int = 10) -> None:
        for tick in range(1, max_ticks + 1):
            try:
                self.scheduler.tick(now=time.time())
            except Exception as exc:
                raise RealChainBroken(f"后台第 {tick} 轮 tick 在真实链路上抛错（唤醒没能开出回合）："
                                      f"{type(exc).__name__}: {exc}") from exc
            if not self.agent.conversation_store.wakes.pending(limit=0):
                return

    # 函数用途: 在后台线程里 drain，供"目标回合正在执行时由前台取消/发消息"的场景使用。
    def drain_in_background(self) -> tuple[threading.Thread, list[BaseException]]:
        errors: list[BaseException] = []

        def run() -> None:
            try:
                self.drain()
            except BaseException as exc:  # noqa: BLE001 - 交回测试线程统一判定
                errors.append(exc)

        worker = threading.Thread(target=run, name="rc-background-drain", daemon=True)
        worker.start()
        return worker, errors

    # 函数用途: 渠道会话身份：三个会话都是本机管理员 owner 下的 chat 会话（会话互通白名单内）。
    def _conversation(self, label: str) -> dict:
        owner = self.agent.home_paths.owner_id
        return {"canonical_user_id": owner, "channel": "chat", "channel_conversation_id": f"rc-session-{label}",
                "channel_user_id": owner}

    # 函数用途: 按任务正文开头的标记找会话任务记录（结构化记录，不读消息正文）。
    def task(self, goal_prefix: str):
        rows, errors = self.agent.conversation_store.session_tasks.list_report(limit=0)
        _require(not errors, f"会话任务账本读取出错：{errors}")
        found = [row for row in rows if str(row.goal).startswith(goal_prefix)]
        _require(len(found) == 1, f"应当恰好有一条以 {goal_prefix} 开头的会话任务，实际 {len(found)} 条")
        return found[0]

    # 函数用途: 某个会话线程里一次性回执仍是 pending 的投递（按回执状态判断，不看 delivered_at）。
    def pending_deliveries(self, label: str) -> list:
        guidance = self.agent.conversation_store.guidance
        entries = guidance.recent("thread", self.threads[label], limit=200, include_delivered=True)
        pending = []
        for entry in entries:
            key = str((entry.metadata or {}).get("dedupe_key") or "")
            receipt = guidance.receipt(key) if key else None
            if receipt is not None and receipt.status == "pending":
                pending.append(entry)
        return pending

    # 函数用途: 从工具操作账本（runtime.db）读某个工具的结构化返回；这是取消等控制动作的持久记录。
    def tool_outputs(self, tool: str) -> list[dict]:
        database = Path(self.agent.home_paths.owner_home_dir) / "runtime.db"
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            rows = conn.execute("select payload_json from runtime_events where event_type='tool_completed' "
                                "order by seq").fetchall()
            outputs = []
            for (payload,) in rows:
                event = json.loads(payload)
                if event.get("tool") != tool:
                    continue
                found = conn.execute("select outcome_json from tool_operations where operation_id=?",
                                     (event.get("operation_id"),)).fetchone()
                output = (json.loads(found[0]).get("result") or {}).get("output") if found else None
                # 拒绝类结果的错误码记在工具事件上，正文里只有 details，所以两边合并。
                parsed = _parse_json(output) if isinstance(output, str) else dict(output or {})
                outputs.append({"error_code": str(event.get("error_code") or ""), **parsed})
        return outputs

    # 函数用途: 某个会话线程里助手消息的正文（仅用于确认脚本标记有没有被交付，不做别的判断）。
    def assistant_texts(self, label: str) -> list[str]:
        messages = self.agent.conversation_store.messages.recent(self.threads[label], limit=200)
        return [str(message.content or "") for message in messages if message.role == "assistant"]

    # 函数用途: 经真实 Gateway 会话预检建出（或取回）一个渠道会话，记下它的 thread_id。
    def open_session(self, label: str) -> str:
        request = {"id": f"rc-preflight-{label}", "kind": "ask", "prompt": "RC-INIT",
                   "conversation": self._conversation(label)}
        preflight = request_context.preflight_gateway_conversation(request_context.GatewayConversationLoadRequest(
            self.agent, request, request["id"], request["prompt"],
        ))
        self.threads[label] = preflight.thread_id
        return preflight.thread_id


# 函数用途: 真实环境的 AgentConfig：只有供应商传输是假的；owner 身份可按用例指定（默认本机管理员）。
def _agent_config(tmp_path: Path, **owner: str) -> AgentConfig:
    return AgentConfig(
        model_backend="openai_compatible", api_base="http://127.0.0.1:9/v1", model_name="rc-scripted",
        api_key="rc-fake-key-not-a-credential", stream_enabled=False, model_context_window_tokens=200_000,
        enable_tools=True, memory_path="memory.jsonl", my_agent_home=str(tmp_path / "home"),
        gateway_workspace="gateway", orphan_supervision_interval_seconds=0, memory_curator_enabled=False,
        enable_self_learning=False, **owner,
    )


# 函数用途: 建好某个 agent 的真实 Gateway 请求目录并返回路径。
def _ready_gateway_paths(agent: SimpleAgent) -> object:
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    return paths


# 函数用途: 搭一个真实环境：真实 SimpleAgent、真实 Gateway 路径、三个 chat 会话、Gateway 同款后台调度器。
def _real_chain(tmp_path: Path, monkeypatch) -> RealChain:
    agent = SimpleAgent(_agent_config(tmp_path), tmp_path / "root")
    wire = ScriptedWire()
    monkeypatch.setattr(http, "post_json", wire)
    chain = RealChain(agent, _ready_gateway_paths(agent), wire, {},
                      _build_background_scheduler(agent, FakeDeliveryService()))
    for label in ("A", "B", "C"):
        chain.open_session(label)
    return chain


# 函数用途: 同一个 home 下另一个普通用户 owner（Gateway 多 owner 的真实形态），用来对照"跨 owner 不列出"。
def _other_owner_chain(chain: RealChain, tmp_path: Path) -> RealChain:
    agent = SimpleAgent(_agent_config(tmp_path, my_agent_owner_provider="feishu", my_agent_owner_kind="user",
                                      my_agent_owner_id="rc-other"), tmp_path / "root-other")
    return RealChain(agent, _ready_gateway_paths(agent), chain.wire, {}, scheduler=None)


def test_session_task_wake_opens_a_turn_and_completes(tmp_path, monkeypatch) -> None:
    """正向对照：A 派活给 B，B 的派活唤醒必须经真实链路开出回合并完成（7b83c8730 就挂在这里）。"""
    chain = _real_chain(tmp_path, monkeypatch)
    chain.ask("A", f"RC-DISPATCH {chain.threads['B']} RC-GOAL-DONE 整理三条要点。")
    task = chain.task("RC-GOAL-DONE")
    assert task.status == "queued", task

    chain.drain()

    turns = chain.wire.calls_for_task(task.task_id)
    assert [call.get("kind") for call in turns] == ["task-done"], (
        f"B 的派活唤醒没有开出回合：全部模型调用 {[call.get('kind') for call in chain.wire.calls]}")
    assert "create_session_task" in turns[0]["tools"], "派活回合的工具集里缺少会话工具"
    task = chain.agent.conversation_store.session_tasks.load(task.task_id)
    assert (task.status, task.conversation_request_id) == ("done", task.task_id), task
    assert task.summary, "done 回报应当带上目标回合的最终答复摘要"
    reports = [entry for entry in chain.pending_deliveries("A")
               if (entry.metadata or {}).get("session_task_status") == "done"]
    assert [entry.metadata.get("session_task_id") for entry in reports] == [task.task_id], "派活方没有收到 done 回报"


def test_message_wakes_an_idle_session(tmp_path, monkeypatch) -> None:
    """正向对照：给空闲会话发普通消息，消息唤醒必须开出回合、让模型看到这条消息，并结案唤醒。"""
    chain = _real_chain(tmp_path, monkeypatch)
    chain.ask("A", f"RC-MESSAGE {chain.threads['C']} RC-NOTE-IDLE 你好 C。")
    _require(chain.pending_deliveries("C"), "消息没有进 C 的邮箱")

    chain.drain()

    saw = [call for call in chain.wire.calls if call.get("kind") == "wake-saw-note"]
    assert len(saw) == 1, f"C 的消息唤醒没有开出回合：全部模型调用 {[call.get('kind') for call in chain.wire.calls]}"
    assert not chain.agent.conversation_store.wakes.pending(limit=0), "消息唤醒没有结案"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="新缺陷：空闲目标的消息唤醒回合只在后台上下文里看到消息，没有认领/确认，回执仍 pending，"
                          "下一回合会再收到一遍（修复后转正）")
def test_idle_message_is_acknowledged_once_and_not_redelivered(tmp_path, monkeypatch) -> None:
    """消息唤醒回合看到的消息必须被确认消费；目标的下一回合不能再收到同一条消息。"""
    chain = _real_chain(tmp_path, monkeypatch)
    chain.ask("A", f"RC-MESSAGE {chain.threads['C']} RC-NOTE-IDLE 你好 C。")
    chain.drain()
    _require([call for call in chain.wire.calls if call.get("kind") == "wake-saw-note"], "C 的消息唤醒没有开出回合")
    pending_after_wake = chain.pending_deliveries("C")
    before = len(chain.wire.calls)
    chain.ask("C", "RC-PING 你好。")

    redelivered = [call for call in chain.wire.calls[before:] if call["saw_note"]]
    assert pending_after_wake == [], "C 的唤醒回合结束后消息回执仍是 pending"
    assert redelivered == [], f"同一条消息在 C 的下一回合又被送了一遍：{[call.get('kind') for call in redelivered]}"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="观察项②未修：目标已在自己回合里消费了消息，唤醒仍再起一轮空回合（第 8 片重做后转正）")
def test_busy_target_consumes_message_without_an_extra_empty_turn(tmp_path, monkeypatch) -> None:
    """B 正在执行前台请求时收到消息：请求不失败、消息在本回合被消费；之后唤醒不应再起空回合。"""
    chain = _real_chain(tmp_path, monkeypatch)
    results: list[object] = []
    busy = threading.Thread(target=lambda: results.append(chain.ask("B", "RC-BUSY 先看看工作目录。")), daemon=True)
    busy.start()
    _require(chain.wire.wait_holding("busy"), "B 的前台回合没有进入模型调用")
    chain.ask("A", f"RC-MESSAGE {chain.threads['B']} RC-NOTE-BUSY 你好 B。")
    chain.wire.release("busy")
    busy.join(_JOIN_SECONDS)
    _require(results and not busy.is_alive(), "B 的前台请求没有正常结束")
    finals = [call for call in chain.wire.calls if call.get("kind") == "user-busy-final"]
    _require(finals and finals[-1]["saw_note"], "B 的前台回合没有在安全点收到这条消息")
    _require(chain.pending_deliveries("B") == [], "B 的前台回合结束后消息回执仍是 pending")

    before = len(chain.wire.calls)
    chain.drain()

    extra = [call.get("kind") for call in chain.wire.calls[before:]]
    assert extra == [], f"内容已被消费，唤醒仍多跑了 {len(extra)} 次模型调用：{extra}"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="场景 5 未修：取消找不到后台派活回合，stop_confirmed=false、目标照常交付（取消修复通过后转正）")
@pytest.mark.parametrize("window", ["HOLD-FIRST", "HOLD-AFTER-TOOL"])
def test_cancel_stops_the_bound_task_turn_during_a_model_call(tmp_path, monkeypatch, window) -> None:
    """绑定后、模型调用进行中取消：停止确认、目标回合停下且不再交付输出、控制记录可查、任务保持 cancelled。"""
    chain = _real_chain(tmp_path, monkeypatch)
    chain.ask("A", f"RC-DISPATCH {chain.threads['B']} RC-GOAL-{window} 耗时较长的任务。")
    task = chain.task(f"RC-GOAL-{window}")
    worker, errors = chain.drain_in_background()
    _require(chain.wire.wait_holding("task"), "B 的派活回合没有进入模型调用")
    bound = chain.agent.conversation_store.session_tasks.load(task.task_id)
    _require((bound.status, bound.conversation_request_id) == ("accepted", task.task_id),
             f"取消前任务应已绑定到派活回合：{bound.status} / {bound.conversation_request_id}")

    chain.ask("A", f"RC-CANCEL {task.task_id}")
    chain.wire.release("task")
    worker.join(_JOIN_SECONDS)
    _require(not worker.is_alive(), "后台 drain 没有在时限内结束")
    if errors:
        raise RealChainBroken(f"后台回合在真实链路上抛错：{errors[0]!r}") from errors[0]
    # 取消工具会给发送方 A 发取消通知并唤醒 A；A 的前台取消回合结束前，这条唤醒在忙通道上只能空等，
    # 后台 drain 的 10 轮 tick 可能先用完（542139f95 上实测余量 1–4 轮，门禁因此时过时不过）。
    # B 的派活唤醒必须在这一片结束时就结案，先单独断言；A 的通知唤醒等前台回合结束后在主线程再排空。
    b_wakes = [wake for wake in chain.agent.conversation_store.wakes.pending(limit=0)
               if wake.thread_id == chain.threads["B"]]
    assert not b_wakes, f"取消后 B 的派活唤醒没有结案：{b_wakes}"
    chain.drain()

    cancels = [row for row in chain.tool_outputs("cancel_session_task") if row.get("task_id") == task.task_id]
    _require(cancels, "工具操作账本里没有这次取消的记录")
    final = chain.agent.conversation_store.session_tasks.load(task.task_id)
    delivered = [text for text in chain.assistant_texts("B") if "RC-HOLD-FINAL" in text]
    # 回合是否真停下只看"放行后的答复有没有被交付"：打断在途调用、或调用返回后丢弃答复，两种停法都成立。
    assert cancels[-1].get("stop_confirmed") is True, f"停止控制没有确认：{cancels[-1]}"
    assert delivered == [], "目标回合被取消后仍交付了放行后的答复"
    assert final.status == "cancelled" and not final.summary, final
    assert not chain.agent.conversation_store.wakes.pending(limit=0), "取消后目标的唤醒没有结案"


def test_task_turn_does_not_spin_on_another_tasks_pending_report(tmp_path, monkeypatch) -> None:
    """空转回归：A 的邮箱挂着别的任务的回报时，A 的派活回合只调一次模型就收尾。"""
    chain = _real_chain(tmp_path, monkeypatch)
    chain.ask("A", f"RC-DISPATCH {chain.threads['B']} RC-GOAL-DONE 快速任务一。")
    chain.drain()
    first = chain.task("RC-GOAL-DONE 快速任务一")
    _require([entry.metadata.get("session_task_id") for entry in chain.pending_deliveries("A")] == [first.task_id],
             "前置条件：A 的邮箱里应当挂着任务一的 done 回报")

    chain.ask("B", f"RC-DISPATCH {chain.threads['A']} RC-GOAL-DONE 往返任务二。")
    chain.drain()

    second = chain.task("RC-GOAL-DONE 往返任务二")
    kinds = [call.get("kind") for call in chain.wire.calls_for_task(second.task_id)]
    assert kinds == ["task-done"], f"A 的派活回合出现空转：{len(kinds)} 次模型调用 {kinds[:12]}"
    assert chain.agent.conversation_store.session_tasks.load(second.task_id).status == "done"


def test_spin_backstop_bounds_an_inconsistent_pending_input_check(tmp_path, monkeypatch) -> None:
    """空转兜底：人为让"有待处理输入"与"能否认领"判据不一致（复现原空转根因），回合也必须在上限内收尾。"""
    chain = _real_chain(tmp_path, monkeypatch)
    chain.ask("A", f"RC-DISPATCH {chain.threads['B']} RC-GOAL-DONE 快速任务一。")
    chain.drain()
    original = GuidanceStore.available_for_turn

    def ignore_ownership(self, entry, *, expected_turn_id, owning_task_id=""):
        return original(self, entry, expected_turn_id=expected_turn_id)

    monkeypatch.setattr(GuidanceStore, "available_for_turn", ignore_ownership)
    chain.ask("B", f"RC-DISPATCH {chain.threads['A']} RC-GOAL-DONE 往返任务二。")
    chain.drain()

    second = chain.task("RC-GOAL-DONE 往返任务二")
    calls = chain.wire.calls_for_task(second.task_id)
    _require(len(calls) > 1, "故障注入没有生效：判据不一致时回合应当被反复作废重来")
    assert len(calls) <= PENDING_TURN_INPUT_INVALIDATION_LIMIT + 1, f"兜底没有生效：{len(calls)} 次模型调用"
    assert not chain.agent.conversation_store.wakes.pending(limit=0), "兜底收尾后唤醒没有结案"


def test_default_config_stops_the_chain_at_depth_four(tmp_path, monkeypatch) -> None:
    """默认配置（owner home 下没有 capability 配置文件）：A→B→C→A→B 放行，第 5 次派活按深度 4 被拒。"""
    chain = _real_chain(tmp_path, monkeypatch)
    _require(not Path(chain.agent.capability_config_path).exists(), "前置条件：不应存在 capability 配置文件")
    a, b, c = (chain.threads[label] for label in ("A", "B", "C"))
    chain.ask("A", f"RC-DISPATCH {b} RC-CHAIN next={c},{a},{b},{c} 链式派活。")

    chain.drain(max_ticks=12)

    rows, errors = chain.agent.conversation_store.session_tasks.list_report(limit=0)
    _require(not errors, f"会话任务账本读取出错：{errors}")
    chain_tasks = sorted((row for row in rows if str(row.goal).startswith("RC-CHAIN")), key=lambda row: row.created_at)
    assert [row.target_thread_id for row in chain_tasks] == [b, c, a, b], [row.goal for row in chain_tasks]
    assert [row.status for row in chain_tasks] == ["done"] * 4
    assert [row.origin_task_id for row in chain_tasks] == ["", *(row.task_id for row in chain_tasks[:3])]
    rejected = [row for row in chain.tool_outputs("create_session_task") if row.get("error_code")]
    assert [(row.get("error_code"), row.get("details")) for row in rejected] == [
        ("SESSION_TASK_CHAIN_LIMIT", {"depth": 4, "limit": 4})]


_LISTED_ROW_KEYS = {"thread_id", "status", "last_activity_at", "channel", "is_current", "allowed_kinds"}


# TODO(list_owner_sessions): 目前只对管理员（local/main）开放。将来对普通用户开放时，要补权限用例：
#   普通用户能否看到工具、只能列出自己 owner 的会话、allowed_kinds 随普通用户开关变化。
@pytest.mark.parametrize("kind", ["task", "message"])
def test_admin_lists_owner_sessions_and_reaches_the_listed_target(tmp_path, monkeypatch, kind) -> None:
    """正向对照：A 调 list_owner_sessions，只凭清单挑出 B 再派活/发消息，B 收到并执行；清单不含别的 owner、不含正文。"""
    chain = _real_chain(tmp_path, monkeypatch)
    other = _other_owner_chain(chain, tmp_path)
    other_thread = other.open_session("X")
    other.ask("X", "RC-PING RC-SECRET-OTHER 其他 owner 会话的正文。")
    chain.ask("B", "RC-PING RC-SECRET-B B 会话的正文。")  # 让 B 成为最近活动的会话；A 的提示里不出现 B 的 id

    chain.ask("A", f"RC-LIST-THEN {kind} 先列出会话，再找最近活动的那个。")

    listings = [call["results"]["list_owner_sessions"] for call in chain.wire.calls
                if "list_owner_sessions" in call["results"]]
    _require(listings, f"A 的回合没有拿到 list_owner_sessions 的结果：{[call.get('kind') for call in chain.wire.calls]}")
    listing = listings[-1]
    rows = {row.get("thread_id"): row for row in listing.get("sessions") or []}
    a, b, c = (chain.threads[label] for label in ("A", "B", "C"))
    assert set(rows) == {a, b, c} and other_thread not in rows, f"清单应当恰好是本 owner 的三个会话：{sorted(rows)}"
    assert listing.get("current_thread_id") == a and listing.get("unreadable_records") == 0, listing
    assert (rows[a]["is_current"], rows[a]["allowed_kinds"]) == (True, []), rows[a]
    assert not rows[b]["is_current"] and kind in rows[b]["allowed_kinds"], rows[b]
    assert all(set(row) == _LISTED_ROW_KEYS for row in rows.values()), "清单行只能有结构化字段"
    serialized = json.dumps(listing, ensure_ascii=False)
    assert "RC-SECRET" not in serialized and "RC-PING" not in serialized, "清单里出现了会话正文"

    chain.drain()

    if kind == "task":
        task = chain.task("RC-GOAL-DONE 由清单选出")
        assert (task.target_thread_id, task.status) == (b, "done"), task
    else:
        saw = [call for call in chain.wire.calls if call.get("kind") == "wake-saw-note"]
        assert len(saw) == 1, f"B 没有收到按清单发出的消息：{[call.get('kind') for call in chain.wire.calls]}"
        targets = {entry.target_id for entry in chain.agent.conversation_store.guidance.recent(
            "thread", b, limit=50, include_delivered=True) if "RC-NOTE-LISTED" in str(entry.message)}
        assert targets == {b}, "按清单发出的消息没有投给 B"
