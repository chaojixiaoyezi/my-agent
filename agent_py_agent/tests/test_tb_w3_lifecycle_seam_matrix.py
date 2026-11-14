"""TB-W3: real TUI -> HTTP producer -> dispatcher -> store, with event-only seams.

Only the provider is scripted. Timing observers always delegate exactly once.
No prepared payloads, gateway files, thread records or execution targets are made
by this test. Each assertion uses the client wire and the real consumer's files.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
from agent_py_agent.agent.gateway_parts import request_context, request_http_client
from agent_py_agent.agent.gateway_parts import request_worker as worker
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
from agent_py_agent.cli.chat_parts import tui_actions, tui_worker, tui_worker_paths
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.tests.test_gateway_active_input_transport import (
    AdmissionLimits,
    OwnerIdentity,
    c_http,
    claim_request,
    coordinator_execution_scope,
    freeze_coordinator_claim,
    inputs,
    read_json_file,
    session_scope_for_request,
)

PHASES = ("P1_queued", "P2_prep", "P3_between_tools", "P4_final_stream",
          "P5_server_done_client_stale", "P6_both_done", "P7_ended")
ACTIONS = ("normal_message", "steer_input", "stop", "new", "compact", "model")


# LLM: Keep the exact production Enter precedence; no transport or state shortcuts.
# 函数用途: 按真实按键的控制、插话、普通队列顺序提交文字。
def tui_enter(params, text):
    if tui_actions._tui_submit_control_operation(params, text):
        return
    if tui_actions._tui_submit_active_turn_input(params, text, display_text=text):
        return
    tui_actions._tui_enqueue_job(params, text)


# LLM: Count actual claims, not provider rounds; a consumed input must not also execute.
# 函数用途: 检查每条已接收文字恰好消费一次，停止族只准准确绑定的输入过期。
def assert_i1_conserved(ledger):
    errors = []
    if ledger["action"] == "normal_message" and not any(
            row["text"] == "看下现在几点了" for row in ledger["accepted"]):
        errors.append("normal-message action was not accepted")
    for incoming in ledger["accepted"]:
        rid, text = incoming["body"]["request_id"], incoming["text"]
        if rid == ledger.get("first"):
            continue
        receipt = ledger["receipts"].get(rid)
        terminal = ledger["terminals"].get(rid, {})
        code = terminal.get("terminal_response", {}).get("error_code")
        bound = incoming["sent"].get("metadata", {}).get("expected_turn_id")
        stopped = ledger["terminals"].get(ledger.get("first"), {}).get("status") in {"cancelled", "stopped", "interrupted"}
        allowed_expiry = stopped and bound == ledger.get("first")
        executions = sum(row["id"] == rid for row in ledger["claims"])
        own_calls = [call for call in ledger["calls"] if call["request_id"] == rid]
        if code == "EXPIRED_TARGET_TURN" and allowed_expiry:
            message = gateway_client_error_message(code)
            if "重新发送" not in message or "原状态、版本或目标" in message:
                errors.append(f"{rid}: wrong expiry message {message!r}")
            if executions or own_calls:
                errors.append(f"{rid}: expired input nevertheless executed")
            continue
        consumed = any(text in call["messages"] for call in ledger["calls"]
                       if call["request_id"] == ledger.get("first") and call["after_boundary"])
        consumed = consumed or text in ledger.get("committed_first", "")
        own = terminal.get("status") == "done" and any(text in call["messages"] for call in own_calls)
        if (code == "EXPIRED_TARGET_TURN" or int(consumed) + int(own) != 1 or executions > 1
                or consumed and (executions or own_calls) or own and executions != 1):
            errors.append(f"{rid}: consumed={consumed}, own={own}, claims={executions}, code={code}")
        if receipt is None or receipt.state in {"pending", "queued", "active_pending"}:
            errors.append(f"{rid}: unsettled receipt {getattr(receipt, 'state', None)}")
    assert not errors, "I1: " + "; ".join(errors)


# LLM: The disk receipt is authoritative; do not infer freshness from TUI idle text.
# 函数用途: 核对完成后或新会话后的下一次 Enter 不再绑定旧回合。
def assert_i2_fresh_unbound(ledger):
    selected = [row for row in ledger["accepted"] if row["text"] == "probe-下一轮"]
    assert len(selected) == 1, "I2: probe was not accepted exactly once"
    if ledger["action"] == "normal_message" and ledger["phase"] in {"P6_both_done", "P7_ended"}:
        selected += [row for row in ledger["accepted"] if row["text"] == "看下现在几点了"]
    for row in selected:
        receipt = ledger["receipts"][row["body"]["request_id"]]
        assert "expected_turn_id" not in receipt.prepared_request["metadata"], f"I2: stale {receipt.request_id}"


# LLM: Compare absent fields as absent, preserving the digest-locked original producer bytes.
# 函数用途: 检查客户端、真实回执与认领文件之间的身份和绑定没有被重写。
def assert_i3_unmodified_pipe(ledger):
    errors = []
    claims = {row["id"]: row for row in ledger["claims"]}
    for row in ledger["accepted"]:
        rid = row["body"]["request_id"]
        receipt = ledger["receipts"].get(rid)
        if receipt is None:
            errors.append(f"{rid}: no receipt")
            continue
        sent = row["sent"].get("metadata", {})
        actual = receipt.prepared_request["metadata"]
        for key in ("message_id", "expected_turn_id"):
            if (key in sent, sent.get(key)) != (key in actual, actual.get(key)):
                errors.append(f"{rid}: producer rewrote {key}")
            if rid in claims and claims[rid]["metadata"].get(key) != actual.get(key):
                errors.append(f"{rid}: claimed a different {key}")
        if receipt.state == "queued" and rid not in claims:
            errors.append(f"{rid}: queued producer output never reached a consumer")
    assert not errors, "I3: " + "; ".join(errors)


# LLM: Every call records the real resolved backend model; settings never mutate a frozen response.
# 函数用途: 检查实际模型调用、显式换模和成功新会话的继承规则。
def assert_i4_live_model(ledger):
    assert ledger["calls"], "I4: no provider execution"
    assert all(call["model"] in {"tb-model-a", "tb-model-b"} for call in [*ledger["calls"], *ledger["probes"]]), ledger["calls"]
    if ledger["action"] != "model":
        assert all(call["model"] == "tb-model-a" for call in ledger["calls"]), "I4: unselected B was used"
    if ledger["action"] == "model":
        first = [call for call in ledger["calls"] if call["request_id"] == ledger["first"]]
        assert all(call["model"] == ("tb-model-b" if call["after_boundary"] else "tb-model-a") for call in first), "I4: frozen first model changed"
        probe = next(row["body"]["request_id"] for row in ledger["accepted"] if row["text"] == "probe-下一轮")
        assert all(call["model"] == "tb-model-b" for call in ledger["calls"] if call["request_id"] == probe), "I4: probe ignored B"
    for result in ledger["controls"]:
        if result.ok and result.kind == "new":
            successor = ledger["threads"][result.details["thread_id"]]
            assert successor.model_profile_id == ledger["profile_a"], "I4: successor model changed"
            probes = [row for row in ledger["accepted"] if row["text"] == "probe-下一轮"]
            assert len(probes) == 1, "I4: missing successor probe"
            terminal = ledger["terminals"][probes[0]["body"]["request_id"]]
            claim = terminal.get("conversation_claim")
            assert isinstance(claim, dict), "I4: probe never acquired a successor conversation claim"
            assert claim["thread_id"] == successor.thread_id, "I4: probe missed successor"
    if ledger["action"] == "new" and ledger["phase"] in {"P5_server_done_client_stale", "P6_both_done", "P7_ended"}:
        assert any(result.kind == "new" and result.ok for result in ledger["controls"]), "I4: idle /new did not succeed"


# LLM: Aggregate all invariant failures so anchor red cells produce a cell-by-invariant table.
# 函数用途: 一次报告四种不变量和最小账本，不让首个断言遮住其它问题。
def _assert_all(ledger):
    failures = []
    for name, check in (
            ("assert_i1_conserved", lambda: assert_i1_conserved(ledger)),
            ("assert_i2_fresh_unbound", lambda: assert_i2_fresh_unbound(ledger)),
            ("assert_i3_unmodified_pipe", lambda: assert_i3_unmodified_pipe(ledger)),
            ("assert_i4_live_model", lambda: assert_i4_live_model(ledger))):
        try:
            check()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
    evidence = {"cell": ledger["cell"], "violations": failures,
                "inputs": [{"id": row["body"]["request_id"], "state": row["body"].get("input_state"),
                            "disposition": row["body"].get("disposition"),
                            "receipt": getattr(ledger["receipts"].get(row["body"]["request_id"]), "state", None)}
                           for row in ledger["accepted"]],
                "terminals": {rid: {"status": row.get("status"), "code": row.get("terminal_response", {}).get("error_code")}
                              for rid, row in ledger["terminals"].items()}}
    print("TB_MATRIX " + json.dumps(evidence, ensure_ascii=False))
    assert not failures, json.dumps(evidence, ensure_ascii=False)


# LLM: Assemble real objects once per cell, sharing every worker ref with the Enter holder.
# 类用途: 用真实对象推进并观察一个生命周期格子，只用事件安排交错。
class _Cell:
    # LLM: All paths are fixture-owned; server.agent is the same real owner agent, not a fake service.
    # 函数用途: 组装隔离客户端、运行时、真实工作线程以及独立账本。
    def __init__(self, tmp_path, monkeypatch, pytest_request, real, phase, action, ending):
        self.agent, _backend, self.gate, _control, _drain, self.server, _close = real
        self.server.agent = self.agent
        self.mp, self.phase, self.action, self.ending = monkeypatch, phase, action, ending
        self.deadline = time.monotonic() + float(pytest_request.config.getoption("timeout") or 60.0)
        self.changed, self.reached, self.release = (threading.Event() for _ in range(3))
        self.first_submitted, self.first_cleaned = threading.Event(), threading.Event()
        self.executing_id, self.first_id, self.last_target = "", "", None
        self.boundary = False
        self.tool_calls, self.errors, self.threads = [], [], []
        self.ledger = {"cell": f"{action}-{phase}" + (f"-{ending}" if ending else ""),
                       "phase": phase, "action": action, "accepted": [], "http": [],
                       "claims": [], "calls": [], "probes": [], "attempts": [], "summaries": [], "controls": [],
                       "receipts": {}, "terminals": {}, "threads": {}}
        config = replace(self.agent.config, gateway_port=self.server.server.server_address[1])
        self.client = GatewayChatClientAgent(SimpleNamespace(), config, self.agent.root,
                                            [self.agent.root], self.agent.home_paths)
        assert self.client.owner_identity == OwnerIdentity.local_main()
        assert self.client.home_paths.owner_home_dir == self.agent.home_paths.owner_home_dir
        self.runtime = TuiRuntime("tb-matrix")
        self.jobs, self.stop_event = queue.Queue(), threading.Event()
        self.params = SimpleNamespace(agent=self.client, paths=self.gate.paths, use_gateway=True,
            args=SimpleNamespace(no_save=False, resume_context=None, gateway_timeout=None),
            state_lock=threading.RLock(), history_lock=threading.RLock(), is_running_ref=[False],
            pending_jobs_ref=[0], running_prompt_ref=[""], running_request_id_ref=[""],
            running_started_at_ref=[0.0], local_run_ref=[None], shutting_down_ref=[False],
            assistant_outputs=[], runtime_inject=[], prompt_files=[], current_session_id="tb-matrix",
            stop_event=self.stop_event, tui_runtime=self.runtime, jobs=self.jobs)
        self.params.pending_jobs_ref_for_enqueue = self.params.pending_jobs_ref
        self.cfg = tui_worker.TuiWorkerConfig(**{
            key: getattr(self.params, key) for key in (
                "jobs", "state_lock", "is_running_ref", "pending_jobs_ref", "running_prompt_ref",
                "running_request_id_ref", "running_started_at_ref", "agent", "args", "paths",
                "use_gateway", "assistant_outputs", "stop_event", "current_session_id", "tui_runtime")},
            conversation_history=[], history_lock=self.params.history_lock,
            build_history_context=lambda: "", last_token_estimate_ref=[0])
        self.note = self.agent.home_paths.owner_home_dir / "tb-note.txt"
        self.note.write_text("harmless registered-tool input", encoding="utf-8")
        self._install_observers()
        self.active = tui_actions._ensure_active_input_reconciler(self.params)
        self.control = tui_actions._ensure_control_operation_reconciler(self.params)
        self._observe_control_results()
        self.worker_thread = self._start(lambda: tui_worker._tui_worker_body(self.cfg))

    # LLM: The test budget is inherited from pytest --timeout, never a new poll interval.
    # 函数用途: 返回本格剩余等待时间。
    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    # LLM: Double-check around Event.clear prevents losing a producer notification.
    # 函数用途: 只在真实事件通知后复查条件，不休眠猜时间。
    def until(self, predicate, description):
        while not predicate():
            assert not self.errors, self.errors
            self.changed.clear()
            if predicate():
                return
            print("TB_WAIT", description, "accepted", [row["body"].get("request_id") for row in self.ledger["accepted"]],
                  "inbox", [path.name for path in self.gate.paths.inbox.glob("*.json")],
                  "processing", [path.name for path in self.gate.paths.processing.glob("*.json")],
                  "jobs", self.jobs.unfinished_tasks, flush=True)
            assert self.changed.wait(self.remaining()), f"waiting for {description}: {self.ledger!r}"

    # LLM: Catch thread exceptions for the test, retaining the original traceback.
    # 函数用途: 启动本格可回收的真实工作线程。
    def _start(self, operation):
        # LLM: Invoke the supplied real worker/runner exactly once.
        # 函数用途: 保存工作线程错误并唤醒测试主线程。
        def run():
            try:
                operation()
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.changed.set()
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    # LLM: Observer copies only real wire results; it does not alter transport args or returns.
    # 函数用途: 记录客户端收发、真实提交完成和工作线程收尾。
    def _install_observers(self):
        original_post, original_get = self.client.post_gateway_json, self.client.get_gateway_json

        # LLM: Keep the original HTTP transport and response value unchanged.
        # 函数用途: 记录真实 POST 的输入和响应，保留接收顺序。
        def post(path, payload, **options):
            result = original_post(path, payload, **options)
            row = {"path": path, "sent": json.loads(json.dumps(payload)), "text": payload.get("goal", payload.get("prompt", "")),
                   "status": result[0], "body": result[1]}
            self.ledger["http"].append(row)
            if path == "/ask" and result[0] in {200, 202} and result[1].get("request_id"):
                self.ledger["accepted"].append(row)
            self.changed.set()
            return result

        # LLM: Read-only reconciliation responses remain the server's original objects.
        # 函数用途: 记录原客户端的 GET 回执。
        def get(path, **options):
            result = original_get(path, **options)
            self.ledger["http"].append({"path": path, "status": result[0], "body": result[1]})
            self.changed.set()
            return result

        self.mp.setattr(self.client, "post_gateway_json", post)
        self.mp.setattr(self.client, "get_gateway_json", get)
        original_ask = request_http_client.post_gateway_ask

        # LLM: Idle Enter uses its own real HTTP transport; observe it without routing around it.
        # 函数用途: 收集普通排队提交的原始 HTTP 正文和接收响应。
        def ask(*args, **kwargs):
            result = original_ask(*args, **kwargs)
            payload = args[1]
            row = {"path": "/ask", "sent": json.loads(json.dumps(payload)), "text": payload["prompt"],
                   "status": result[0], "body": result[1]}
            self.ledger["http"].append(row)
            if result[0] in {200, 202} and result[1].get("request_id"):
                self.ledger["accepted"].append(row)
            self.changed.set()
            return result

        self.mp.setattr(request_http_client, "post_gateway_ask", ask)
        submitted = tui_worker_paths._submit_new_gateway_job

        # LLM: Let the real producer bind the actual job before publishing P1.
        # 函数用途: 在首条真实请求写入后通知主线程，尚不驱动认领。
        def submit(*args, **kwargs):
            result = submitted(*args, **kwargs)
            if not self.first_id:
                self.first_id = result[0]
                self.ledger["first"] = result[0]
                self.first_submitted.set()
            self.changed.set()
            return result

        self.mp.setattr(tui_worker_paths, "_submit_new_gateway_job", submit)
        update = tui_worker._tui_update_running_state

        # LLM: Freeze P1 before running-state publication; the accepted request remains unclaimed.
        # 函数用途: 固定首条已入队但尚未开始观察的客户端状态，消除排队控制的竞争。
        def update_running(*args, **kwargs):
            if args[1].gateway_request_id == self.first_id:
                self.pause("P1_queued")
            return update(*args, **kwargs)

        self.mp.setattr(tui_worker, "_tui_update_running_state", update_running)
        complete = self.runtime.complete_turn

        # LLM: Completion is observed after the original returns, never simulated.
        # 函数用途: 收集真实传入 complete_turn 的摘要。
        def completed(*args, **kwargs):
            result = complete(*args, **kwargs)
            self.ledger["summaries"].append((args[0], args[1]))
            self.changed.set()
            return result

        self.mp.setattr(self.runtime, "complete_turn", completed)
        cleanup = tui_worker._tui_cleanup_after_job

        # LLM: Do not block while the runtime lock is held; publish P6 only after real cleanup.
        # 函数用途: 等真实收尾清掉运行引用并确认队列，再标记完成。
        def cleaned(*args, **kwargs):
            result = cleanup(*args, **kwargs)
            if args[1].gateway_request_id == self.first_id:
                self.first_cleaned.set()
            self.changed.set()
            return result

        self.mp.setattr(tui_worker, "_tui_cleanup_after_job", cleaned)
        outcome = tui_worker_paths._gateway_outcome

        # LLM: P5 blocks before terminal-to-summary conversion, preserving the stale client view.
        # 函数用途: 将服务端已完成但客户端未完成的窗口固定下来。
        def observe_outcome(*args, **kwargs):
            if args[0].job.gateway_request_id == self.first_id:
                self.pause("P5_server_done_client_stale")
            return outcome(*args, **kwargs)

        self.mp.setattr(tui_worker_paths, "_gateway_outcome", observe_outcome)
        prep = request_context.gateway_conversation_context

        # LLM: This real prep entry runs inside the frozen selected-model scope.
        # 函数用途: 在第一次模型调用之前、真实会话准备开始时暂停。
        def prepare(*args, **kwargs):
            if self.executing_id == self.first_id:
                self.pause("P2_prep")
            return prep(*args, **kwargs)

        self.mp.setattr(request_context, "gateway_conversation_context", prepare)
        read = ReadFileTool.execute

        # LLM: Registered read_file executes unchanged once; only its entry is event-gated.
        # 函数用途: 用无害真实文件读取制造工具安全点窗口。
        def read_file(*args, **kwargs):
            self.tool_calls.append(self.executing_id)
            if self.executing_id == self.first_id:
                self.remember_target()
                self.pause("P3_between_tools")
            return read(*args, **kwargs)

        self.mp.setattr(ReadFileTool, "execute", read_file)
        # LLM: The same provider script is the sole replaced behaviour in both backend classes.
        # 函数用途: 保留真实后端实例，给两种后端返回同一组脚本化模型响应。
        def generate(backend, prompt, **kwargs):
            return self.generate(backend, prompt, **kwargs)
        self.mp.setattr(EchoBackend, "generate", generate)
        self.mp.setattr(AnthropicCompatibleBackend, "generate", generate)

    # LLM: Finish wrappers notify only after original callback/outbox transaction has committed.
    # 函数用途: 观察真实对账器的最终确认，避免跳过接回队列的回调。
    def _observe_control_results(self):
        for reconciler in (self.active, self.control):
            original = reconciler._finish

            # LLM: Capture the bound original in the closure; do not change call arguments.
            # 函数用途: 给真实回执收尾加一个事件通知。
            def finish(*args, _original=original, **kwargs):
                result = _original(*args, **kwargs)
                self.changed.set()
                return result
            self.mp.setattr(reconciler, "_finish", finish)
        callback = self.control.on_complete

        # LLM: Callback recording retains the actual typed control result.
        # 函数用途: 收集真实控制结果，同时照常更新 TUI。
        def control_done(*args, **kwargs):
            result = callback(*args, **kwargs)
            self.ledger["controls"].append(args[1])
            self.changed.set()
            return result
        self.mp.setattr(self.control, "on_complete", control_done)

    # LLM: Profile operations go through the real catalog transaction for the exact /ask owner.
    # 函数用途: 新增测试模型档案，可选择将其设为新会话默认值。
    def profile(self, name, *, select=False):
        profile_id = str(uuid4())
        profile = {"model_backend": "anthropic_compatible", "model_name": name,
                   "api_base": "https://fixture.example.invalid", "api_key": "fake",
                   "model_context_window_tokens": 65536}
        result = execute_model_profile_operation(self.agent, "add", {"profile_id": profile_id, "profile": profile})
        assert result["ok"], result
        if select:
            result = execute_model_profile_operation(self.agent, "set_default", {"profile_id": profile_id})
            assert result["ok"], result
        return profile_id

    # LLM: Obtain the target only from the real server projection, before client completion.
    # 函数用途: 保存用户最后看见的真实执行绑定，不在完成后刷新它。
    def remember_target(self):
        result = self.client.request_background_notices(self.params.current_session_id, after=0)
        target = result.get("execution_target")
        if target:
            assert target["turn_id"] == self.first_id, target
            self.last_target = target
            self.runtime.publish_execution_target(target)

    # LLM: Event gates add no state and do not change the wrapped function's original return.
    # 函数用途: 只在当前格子要求的首轮边界暂停真实调用。
    def pause(self, phase):
        desired = "P3_between_tools" if self.phase == "P7_ended" and self.ending == "cancelled" else self.phase
        if phase == desired and not self.reached.is_set():
            self.reached.set()
            self.changed.set()
            assert self.release.wait(self.remaining()), f"unreleased {phase}"

    # LLM: Provider outputs are scripted but backend/model selection and tool execution remain real.
    # 函数用途: 每轮先请求一次真实只读工具，再给最终文字，失败格在第二次调用抛真实提供商错误。
    def generate(self, backend, prompt, **options):
        rid = self.executing_id
        if getattr(options.get("request_options"), "is_probe", False):
            self.ledger["probes"].append({"request_id": rid, "model": backend.model_name, "prompt": prompt})
            nonce = prompt.split("with nonce ", 1)[1].split(".", 1)[0]
            return ModelResponse(text="", backend=backend.name, tool_use_blocks=[{
                "id": f"capability-{nonce}", "name": "my_agent_capability_probe", "input": {"nonce": nonce}}])
        call_number = sum(row["request_id"] == rid for row in self.ledger["calls"]) + 1
        model = str(getattr(backend, "model_name", ""))
        messages = json.dumps(options.get("messages") or prompt, ensure_ascii=False, default=str)
        self.ledger["calls"].append({"request_id": rid, "model": model, "messages": messages,
                                     "after_boundary": self.boundary, "number": call_number})
        self.changed.set()
        if rid == self.first_id and call_number == 2:
            self.pause("P4_final_stream")
            if self.phase == "P7_ended" and self.ending == "failed":
                raise ProviderRequestRejectedError("synthetic matrix provider failure", status_code=400)
        if rid and call_number == 1:
            return ModelResponse(text="先读取测试资料。", backend=backend.name, tool_use_blocks=[
                {"id": f"read-{rid}", "name": "read_file", "input": {"path": str(self.note)}}])
        return ModelResponse(text=f"本轮已完成-{rid}-{call_number}", backend=backend.name)

    # LLM: The ledger stores the on-disk claim before the real canonical runner consumes it.
    # 函数用途: 使用真实 claim、协调作用域及生产执行函数运行一次请求。
    def run_claim(self, claim):
        record = read_json_file(claim.path)
        self.ledger["claims"].append(record)
        self.executing_id = claim.request_id
        try:
            with coordinator_execution_scope(self.gate, claim, self.agent):
                assert worker._process_claimed_gateway_request_path(
                    self.agent, self.gate.paths, claim.path, claim.lease_owner, claim=claim) is True
        finally:
            self.executing_id = ""
            self.changed.set()

    # LLM: Admission and claim use the real accepted inbox request and frozen coordinator decision.
    # 函数用途: 只认领真实首条已接收请求，已被真实停止的请求不再执行。
    def start_first(self):
        path = self.gate.paths.inbox / f"{self.first_id}.json"
        if not path.exists():
            assert (self.gate.paths.terminal / path.name).exists()
            return
        receipt = inputs.read_gateway_input_receipt(self.gate.paths, self.first_id)
        decision = self.gate.admit(path, (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)),
                                   AdmissionLimits(8, 8))
        assert decision["status"] == "admitted", decision
        claim = claim_request(self.gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(self.gate, decision))
        assert claim is not None
        self.runner = self._start(lambda: self.run_claim(claim))

    # LLM: Wait for canonical reconciler completion; local busy refusals create no outbox row.
    # 函数用途: 在实际边界从真实用户入口注入动作。
    def act(self, action):
        before = len(self.ledger["accepted"])
        self.boundary = True
        self.ledger["attempts"].append(action)
        if action == "steer_input":
            result = self.client.request_active_turn_input(self.params.current_session_id,
                message="明确绑定的补充消息", message_id=f"bound-{uuid4()}", expected_turn_id=self.first_id,
                expected_execution=self.last_target)
            assert result.request_id, result
            assert len(self.ledger["accepted"]) == before + 1, ("bound input was not accepted", result)
            if result.delivery.value == "queued":
                tui_actions._tui_attach_gateway_job(self.params, "明确绑定的补充消息", display_text="明确绑定的补充消息",
                    gateway_request_id=result.request_id, client_message_id=self.ledger["accepted"][-1]["sent"]["metadata"]["message_id"])
            return
        if action == "normal_message":
            tui_enter(self.params, "看下现在几点了")
            if self.phase != "P1_queued":
                self.until(lambda: len(self.ledger["accepted"]) > before, "action HTTP acceptance")
            return
        text = f"/model {self.profile_b}" if action == "model" else f"/{action}"
        locally_refused = action == "compact" and self.params.is_running_ref[0]
        locally_refused = locally_refused or (action == "stop" and self.params.is_running_ref[0] and not self.runtime.execution_target())
        count = len(self.ledger["controls"])
        tui_enter(self.params, text)
        if not locally_refused:
            self.until(lambda: len(self.ledger["controls"]) > count, f"{action} control completion")

    # LLM: Dispatch only after held work finishes; repeat ticks with the ledger-derived bound.
    # 函数用途: 让真实调度器消费所有生产输出，等观察任务及对账器一起收口。
    def settle(self):
        self.release.set()
        runner = getattr(self, "runner", None)
        if runner is not None:
            runner.join(self.remaining())
            assert not runner.is_alive(), "server runner did not finish"
        assert not self.errors, self.errors
        for _tick in range(len(self.ledger["accepted"]) + 2):
            submitted = []
            # Wait for the real HTTP acknowledgement, not merely the producer's
            # file: otherwise the test can claim it during acceptance confirmation.
            self.until(lambda: self.quiet() or self.accepted_inbox_ready(),
                       "accepted producer request or settled TUI")
            self.gate.release_settled()
            worker.dispatch_pending_requests(self.gate.paths, AdmissionLimits(8, 8),
                lambda claim, *_scope: submitted.append(claim), admission_gate=self.gate)
            print("TB_DISPATCH", [claim.request_id for claim in submitted], flush=True)
            for claim in submitted:
                self.run_claim(claim)
            inputs.reconcile_gateway_input_receipts(self.gate.paths, self.agent)
            self.until(lambda: self.quiet() or self.accepted_inbox_ready(),
                       "next producer request or settled TUI")
            if not submitted and self.quiet():
                return
        else:
            pytest.fail("dispatcher did not settle within accepted inputs + 2 ticks")

    # LLM: Only already acknowledged producer files drive test-owned dispatch; no payload is written.
    # 函数用途: 等真实客户端确认接收后，再让测试调度器消费该请求。
    def accepted_inbox_ready(self):
        return any((self.gate.paths.inbox / f"{row['body']['request_id']}.json").exists()
                   for row in self.ledger["accepted"])

    # LLM: An empty dispatch tick is not quiescence while async producers still own work.
    # 函数用途: 同时核对真实任务队列、持久 outbox 和控制保存队列是否全部清空。
    def quiet(self):
        return (self.jobs.unfinished_tasks == 0 and not self.active._read_entries()
                and not self.control._read_entries()
                and self.control._setting_enqueuer.queue.unfinished_tasks == 0)

    # LLM: Read-only snapshots come from real consumer files and the real conversation store.
    # 函数用途: 在所有请求收口后读回账本及首轮提交的模型消息。
    def snapshot(self):
        for row in self.ledger["accepted"]:
            rid = row["body"]["request_id"]
            self.ledger["receipts"][rid] = inputs.read_gateway_input_receipt(self.gate.paths, rid)
            path = self.gate.paths.terminal / f"{rid}.json"
            if path.exists():
                self.ledger["terminals"][rid] = read_json_file(path)
        threads = self.agent.conversation_store.threads.list(limit=0)
        self.ledger["threads"] = {thread.thread_id: thread for thread in threads}
        rows = [row for thread in threads for row in self.agent.conversation_store.messages.recent(thread.thread_id, limit=0)
                if row.metadata.get("request_id") == self.first_id]
        self.ledger["committed_first"] = json.dumps([row.to_dict() for row in rows], ensure_ascii=False, default=str)
        return self.ledger

    # LLM: Always release hooks and join every owned worker; do not stop the user's Gateway.
    # 函数用途: 回收本格创建的线程，不遗留等待或后台重试。
    def close(self):
        self.release.set()
        self.stop_event.set()
        for reconciler in (self.active, self.control):
            reconciler._wake.set()
            for name in ("_settings_wake",):
                if event := getattr(reconciler, name, None):
                    event.set()
        owned = list(self.threads)
        for reconciler in (self.active, self.control):
            owned.extend(getattr(reconciler, name, None) for name in ("_worker", "_settings_worker"))
        owned.append(self.control._setting_enqueuer.worker)
        for thread in owned:
            if thread is not None:
                thread.join(self.remaining())
        assert not any(thread is not None and thread.is_alive() for thread in owned), "owned thread leaked"
        assert not self.errors, self.errors


# LLM: Reuse c_http unchanged; HOME and MY_AGENT_HOME are fixture temp paths for every Python run.
# 函数用途: 建立和回收一个完整隔离的真实生命周期测试环境。
@contextmanager
def _cell(tmp_path, monkeypatch, request, phase="P6_both_done", action="new", ending=""):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    with c_http(tmp_path, monkeypatch) as real:
        case = _Cell(tmp_path, monkeypatch, request, real, phase, action, ending)
        try:
            yield case
        finally:
            case.close()


# LLM: P7 expands only its two real endings; the other six phases are not duplicated.
# 函数用途: 将六个动作与八种边界组合为四十八格。
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("phase,ending", [(phase, "") for phase in PHASES[:-1]] +
                         [(PHASES[-1], ending) for ending in ("cancelled", "failed")],
                         ids=list(PHASES[:-1]) + ["P7_ended-cancelled", "P7_ended-failed"])
def test_lifecycle_seam_matrix(tmp_path, monkeypatch, request, phase, action, ending):
    with _cell(tmp_path, monkeypatch, request, phase, action, ending) as case:
        case.ledger["profile_a"] = case.profile("tb-model-a", select=True)
        case.profile_b = case.profile("tb-model-b")
        tui_enter(case.params, "nihao")
        case.until(case.first_submitted.is_set, "first /ask accepted")
        if phase == "P1_queued":
            case.until(case.reached.is_set, "queued phase before client running-state publication")
            assert (case.gate.paths.inbox / f"{case.first_id}.json").exists()
            case.act(action)
            case.start_first()
        else:
            case.start_first()
            if phase == "P6_both_done" or phase == "P7_ended" and ending == "failed":
                case.until(case.first_cleaned.is_set, "first real complete_turn and cleanup")
            else:
                case.until(case.reached.is_set, "requested lifecycle boundary")
            if phase == "P7_ended" and ending == "cancelled":
                case.act("stop")
                case.release.set()
                case.until(case.first_cleaned.is_set, "cancelled first complete_turn")
            if phase in PHASES[:5]:
                case.remember_target()
            case.act(action)
        case.settle()
        if phase == "P7_ended":
            terminal = read_json_file(case.gate.paths.terminal / f"{case.first_id}.json")
            assert terminal["status"] in ({"cancelled", "stopped", "interrupted"} if ending == "cancelled" else {ending}), terminal
        tui_enter(case.params, "probe-下一轮")
        case.until(lambda: any(row["text"] == "probe-下一轮" for row in case.ledger["accepted"]), "probe HTTP acceptance")
        case.settle()
        _assert_all(case.snapshot())


# LLM: Never execute the old placeholder; a real /new successor must resolve the live owner default.
# 函数用途: 覆盖先有占位会话、后选默认模型、再新会话并真实运行的链路。
def test_i4_placeholder_predecessor_new_session_runs_on_live_default(tmp_path, monkeypatch, request):
    with _cell(tmp_path, monkeypatch, request) as case:
        home = case.agent.home_paths
        old = case.agent.conversation_store.threads.get_or_create({
            "canonical_user_id": "local-agent", "owner_id": home.owner_id,
            "owner_home": str(home.owner_home_dir), "channel": "chat", "channel_user_id": "local-agent",
            "channel_conversation_id": case.params.current_session_id})
        assert old.model_profile_id == "default"
        before = (old.model_profile_id, old.model_selection_revision, old.model_selection_source,
                  old.model_selection_last_explicit_revision)
        case.ledger["profile_a"] = case.profile("tb-model-a", select=True)
        case.act("new")
        tui_enter(case.params, "probe-下一轮")
        case.until(case.first_submitted.is_set, "successor probe accepted")
        case.start_first()
        case.settle()
        ledger = case.snapshot()
        archived = case.agent.conversation_store.threads.require(old.thread_id)
        assert before == (archived.model_profile_id, archived.model_selection_revision,
                          archived.model_selection_source, archived.model_selection_last_explicit_revision)
        assert_i4_live_model(ledger)
        assert all(call["model"] == "tb-model-a" for call in ledger["calls"])
