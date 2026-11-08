"""Replay declarative actions through the real Gateway HTTP and durable queue surfaces."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from agent_py_agent.tests.slo.fake_provider import FakeProvider
from agent_py_agent.tests.slo.load import GatewayProcess, durable_receipt, json_request, wait_until

from .normalize import normalize
from .recorder import Recorder


@contextmanager
def _gateway_calendar(calendar_day):
    """只固定回放的目录日历分区；事件时间、租约、进程身份和超时继续使用真实时钟。"""
    from unittest.mock import patch

    from agent_py_agent.agent.memory_archive import storage
    from agent_py_agent.agent.user_space import context_bundle, run_workspace

    day = date.fromisoformat(calendar_day)

    class CalendarDate(date):
        @classmethod
        def today(cls):
            return day

    with (patch.object(context_bundle, "date", CalendarDate),
          patch.object(storage, "_date_key", lambda _created_at: day.isoformat()),
          patch.object(run_workspace, "_date_key", lambda _created_at: day.isoformat())):
        yield


def _gateway_main():
    """在真实 Gateway 入口前安装测试日历，重启也从同一启动参数恢复。"""
    import runpy

    calendar_day = sys.argv.pop(1)
    with _gateway_calendar(calendar_day):
        runpy.run_module("agent_py_agent", run_name="__main__", alter_sys=True)


class ReplayGatewayProcess(GatewayProcess):
    def _command(self):
        command = super()._command()
        calendar_day = self.parameters["calendar_day"]
        bootstrap = "from agent_py_agent.tests.golden.replayer import _gateway_main; _gateway_main()"
        return [command[0], "-c", bootstrap, calendar_day, *command[3:]]


class Replayer:
    # LLM: Scenario roots are exclusive test directories; no installed or live user Gateway is used.
    # 函数用途: Bind the scenario, real isolated process, fake provider and evidence recorder.
    def __init__(self, root, scenario, fixture_parameters):
        self.root, self.scenario = Path(root), scenario
        # 并行 CI 的进程就绪观察覆盖一次完整启动及清理余量；轮询频率仍沿用原配置。
        self.bounds = {**fixture_parameters, "condition_seconds": max(fixture_parameters["condition_seconds"],
            fixture_parameters["startup_seconds"] + fixture_parameters["teardown_seconds"])}
        self.recorder = Recorder(self.root / "evidence")
        self.requests, self.aliases, self.last_stopped, self.delivered = {}, {}, None, set()
        # 持有的 provider 阶段必须晚于观察超时，不能在被测状态尚未观察到时自行结束。
        script = [{**entry, "deadline_seconds": max(float(entry.get("deadline_seconds", 0)),
            self.bounds["condition_seconds"] + self.bounds["teardown_seconds"])}
                  if entry.get("wait_for_release") else entry for entry in scenario["fake_model_script"]]
        self.provider = FakeProvider(self.root / "provider", script=script,
                                     teardown_seconds=self.bounds["teardown_seconds"])
        self.gateway = None

    # LLM: HTTP is the only ingress; complete wire bodies are retained separately from semantic trace fields.
    # 函数用途: Send one authenticated client action and record its actual response and receipt.
    def call(self, path, body, *, session="main"):
        self.recorder.raw("client_request", {"path": path, "body": body})
        self.recorder.event("client", "request_sent", method="POST", path=path, session_id=session,
            client_message_id=body.get("metadata", {}).get("message_id"),
            text=body.get("prompt", body.get("command", "")))
        status, result = json_request(self.gateway.base_url, path, body, headers=self.gateway.headers(),
                                      timeout=self.bounds["condition_seconds"])
        self.recorder.raw("client_response", {"status": status, "body": result})
        self.recorder.event("client", "response", request_id=result.get("request_id") or None,
            receipt_id=result.get("receipt_id"), session_id=session, status_code=status, ok=result.get("ok"),
            delivery_status=result.get("delivery_status"), input_state=result.get("input_state"),
            disposition=result.get("disposition"), control_state=result.get("control_state"),
            error_code=result.get("error_code"), control_kind=result.get("kind"))
        assert status in {200, 202}, result
        return result

    # LLM: IDs and epochs come from an actually read queue record, not from scenario expectations.
    # 函数用途: Load the current durable record for an exact request.
    def payload(self, request_id, *, terminal=False):
        paths = self.gateway.paths()
        candidates = [paths.terminal] if terminal else [paths.processing, paths.terminal, paths.inbox]
        for directory in candidates:
            path = directory / f"{request_id}.json"
            if path.is_file():
                try:
                    return json.loads(path.read_text())
                except FileNotFoundError:
                    # 队列记录可能刚移到下一目录，留给下一候选或下一次轮询读取。
                    continue
        return None

    # LLM: The production conversation claim binds the session and execution independently of request id reuse.
    # 函数用途: Extract the actual immutable execution fence for recorder events.
    def fence(self, row):
        return {"request_id": row["id"], "thread_id": row.get("conversation_claim", {}).get("thread_id"),
                "execution_attempt_id": row.get("execution_attempt_id"), "lease_epoch": row.get("lease_epoch", 0)}

    # LLM: A durable receipt is required even for requests that complete before the next poll.
    # 函数用途: Submit one queued question and bind its symbolic alias to the actual request identity.
    def ask(self, step):
        session, alias = step.get("session", "main"), step["ref"]
        before_calls = sum(not row["is_probe"] for row in self.provider.records)
        result = self.call("/ask", {"prompt": step.get("text", "fixture-question"),
            "conversation_id": session, "metadata": {"message_id": alias}, "delivery_intent": "queue",
            "client_capabilities": {"tool_approval": self.scenario.get("interactive_approvals", False)}}, session=session)
        assert durable_receipt(result, self.gateway.paths()), result
        request_id = result["request_id"]
        self.aliases[alias] = request_id
        self.requests[request_id] = {"session": session, "started": False, "finished": False}
        row = wait_until(lambda: self.payload(request_id), self.bounds["condition_seconds"], self.bounds["poll_seconds"])
        self.recorder.raw("request_file", row)
        if step.get("wait") == "completed":
            self.complete(alias)
        elif step.get("wait") == "provider_request":
            wait_until(lambda: sum(not row["is_probe"] for row in self.provider.records) > before_calls,
                       self.bounds["condition_seconds"], self.bounds["poll_seconds"])
            with self.provider.condition:
                record = [row for row in self.provider.records if not row["is_probe"]][before_calls]
                self.requests[request_id]["provider_turn"] = record["turn"]
            self.observe_start(request_id)

    # LLM: The start observation records a claimed execution only after its execution id is durable.
    # 函数用途: Capture the real processing fence once per execution.
    def observe_start(self, request_id):
        row = wait_until(lambda: self.payload(request_id) if ((self.payload(request_id) or {}).get("execution_attempt_id")
                         and (self.payload(request_id) or {}).get("conversation_claim", {}).get("thread_id")) else None,
                         self.bounds["condition_seconds"], self.bounds["poll_seconds"])
        state = self.requests[request_id]
        if not state["started"]:
            previous = self.last_stopped
            fields = {} if previous is None else {"previous_request_id": previous["request_id"],
                "previous_execution_attempt_id": previous["execution_attempt_id"], "happens_after": [previous["edge"]]}
            self.recorder.raw("processing_file", row)
            self.recorder.event("queue", "attempt_start", **self.fence(row), state="processing", **fields)
            state["started"] = True
        return row

    # LLM: Acknowledgement is not exit; canonical terminal capability closeout is verified separately.
    # 函数用途: Read completion, record the terminal fence, and deliver a deduplicated fake adapter receipt.
    def complete(self, alias, *, cancelled=False):
        request_id = self.aliases[alias]
        self.observe_start(request_id)
        row = wait_until(lambda: self.payload(request_id, terminal=True), self.bounds["condition_seconds"], self.bounds["poll_seconds"])
        state = self.requests[request_id]
        if state["finished"]:
            return row
        self.recorder.provider(self.provider, **self.fence(row))
        closeout = row.get("capability_closeout")
        if cancelled:
            assert closeout and closeout.get("acknowledged") is True, row
            if "shell_pid" in state:
                self.prove_process_exit({"target": alias})
            edge = self.recorder.event("queue", "exit_proof", **self.fence(row), confirmed=True,
                proof_kind="canonical_capability_and_exact_pid_absent" if "shell_pid" in state else "canonical_capability_closeout")
            self.last_stopped = {**self.fence(row), "edge": edge}
        else:
            assert row["status"] == "done" and row.get("ok") is True, row
        self.recorder.raw("terminal_file", row)
        for input_id in state.get("steer_inputs", []):
            # LLM: Pending ingress is not acceptance; a non-cancelled turn must actually consume its steering input.
            # 函数用途: Verify the original input's durable final disposition without resending it.
            def disposition():
                status, receipt = json_request(self.gateway.base_url, f"/input-status/{input_id}",
                    headers=self.gateway.headers(), timeout=self.bounds["probe_seconds"])
                if status != 200:
                    return None
                if receipt.get("input_state") == "consumed" or cancelled and receipt.get("input_state") == "terminal_unknown":
                    return receipt
                return None
            receipt = wait_until(disposition, self.bounds["condition_seconds"], self.bounds["poll_seconds"])
            assert receipt["target_turn_id"] == request_id, receipt
            self.recorder.raw("steer_input_receipt", receipt)
            self.recorder.event("client", "input_disposition", **self.fence(row), input_request_id=input_id,
                input_state=receipt["input_state"], delivery_status=receipt["delivery_status"])
        self.recorder.session_and_tools(self.gateway.home, self.gateway.paths(), self.fence(row))
        response = row.get("terminal_response", {})
        self.recorder.event("queue", "attempt_terminal", **self.fence(row), state=row["status"],
            disposition="committed", response=response.get("response"), ok=row.get("ok"),
            tool_calls=response.get("tool_calls", 0), tool_rounds=response.get("tool_rounds", 0),
            attempts=row.get("attempts", 0), closeout_acknowledged=bool(closeout and closeout.get("acknowledged")))
        if row.get("ok") and str(response.get("response") or "").strip() and request_id not in self.delivered:
            self.delivered.add(request_id)
            receipt_id = hashlib.sha256((request_id + ":fixture-im").encode()).hexdigest()
            outbox = {"request_id": request_id, "receipt_id": receipt_id, "content": response.get("response"), "effect_kind": "im_send"}
            (self.root / "evidence" / f"outbox-{len(self.delivered)}.json").write_text(json.dumps(outbox))
            self.recorder.raw("fake_adapter_outbox", outbox)
            self.recorder.event("adapter", "effect", **self.fence(row), receipt_id=receipt_id, effect_kind="im_send", count=1)
        state["finished"] = True
        return row

    # LLM: Stop and new enter the same persistent control service used by clients.
    # 函数用途: Execute a typed control and retain acknowledgement separately from exit proof.
    def control(self, step):
        session = step.get("session", "main")
        result = self.call("/control", {"command": "/" + step["action"], "conversation_id": session,
            "metadata": {"message_id": step["ref"]}}, session=session)
        assert result.get("ok") and result.get("delivery_status") == "accepted", result
        if step["action"] == "stop":
            request_id = self.aliases[step["target"]]
            row = self.observe_start(request_id)
            self.recorder.event("client", "stop_ack", **self.fence(row), receipt_id=result.get("receipt_id"))
        if step["action"] == "new":
            self.recorder.event("client", "new_session", thread_id=result.get("details", {}).get("thread_id"),
                predecessor_thread_id=result.get("details", {}).get("predecessor_thread_id"), receipt_id=result.get("receipt_id"))

    # LLM: Approval input reuses the real request binding and public file bridge; no policy gate is replaced.
    # 函数用途: Approve exactly the tool call actually published by this isolated Gateway.
    def approve(self, step):
        from agent_py_agent.agent.contracts.tool_approval import (
            ToolApprovalDecision,
            ToolApprovalRequest,
        )
        from agent_py_agent.agent.gateway_parts.permission_bridge import (
            write_gateway_permission_decision,
        )
        request_id = self.aliases[step["target"]]
        paths = self.gateway.paths()
        chunk = paths.processing / f"{request_id}.chunks.jsonl"

        # LLM: Only a complete permission_requested frame from this exact request can authorize the decision.
        # 函数用途: Read the real pending permission from its live chunk stream.
        def pending():
            if (self.gateway.paths().terminal / f"{request_id}.json").exists():
                terminal = self.payload(request_id, terminal=True)
                raise AssertionError(f"tool completed without publishing approval: {terminal.get('terminal_response', {}).get('channel_delivery')}")
            if not chunk.exists():
                return None
            for line in chunk.read_text().splitlines():
                row = json.loads(line)
                if row.get("kind") == "permission_requested":
                    return ToolApprovalRequest.from_mapping(row["permission"])
            return None
        permission = wait_until(pending, self.bounds["condition_seconds"], self.bounds["poll_seconds"])
        row = self.observe_start(request_id)
        self.recorder.raw("approval_requested", permission.to_dict())
        self.recorder.event("approval", "approval_requested", **self.fence(row),
            receipt_id=permission.permission_id, tool_name=permission.tool_name,
            operation_id=permission.binding["operation_id"], round=permission.round, call_index=permission.call_index)
        decision = ToolApprovalDecision(permission.permission_id, step["decision"])
        decision_path = write_gateway_permission_decision(chunk, permission, decision)
        self.recorder.raw("approval_decision", json.loads(decision_path.read_text()))
        self.recorder.event("approval", "effect", **self.fence(row), receipt_id=permission.permission_id,
            operation_id=permission.binding["operation_id"], decision=decision.decision,
            effect_kind="approval_decision", count=1)

    # LLM: Child status comes from canonical task-local storage, not from a tool's accepted response.
    # 函数用途: Wait for the actual spawned child to close and record its durable terminal result.
    def wait_children(self, step):
        from agent_py_agent.agent.subagents.services.agent_run_state import read_agent_state_payload
        request_id = self.aliases[step["target"]]

        # LLM: The bounded fixture home contains only this scenario's tasks; canonical locators are resolved by production code.
        # 函数用途: Find the real child task and wait for its terminal state.
        def terminal_children():
            rows = {}
            for path in self.gateway.home.rglob("task.json"):
                data = read_agent_state_payload(path)
                if data.get("goal") == "Return fixture-answer.":
                    rows[data["id"]] = data
            return list(rows.values()) if rows and all(str(row.get("status", "")).upper() in {"DONE", "FAILED", "CANCELLED"} for row in rows.values()) else None
        children = wait_until(terminal_children, self.bounds["condition_seconds"], self.bounds["poll_seconds"])
        assert len(children) == 1, children
        row = self.payload(request_id, terminal=True)
        for child in children:
            assert str(child["status"]).upper() == "DONE", child
            self.recorder.raw("subagent_terminal", child)
            self.recorder.event("subagent", "effect", **self.fence(row), child_id=child["id"],
                state=str(child["status"]).lower(), effect_kind="subagent_closeout", count=1)

    # LLM: The marker is written by the actual shell tool process before its blocking wait.
    # 函数用途: Bind the shell readiness marker to its real PID, without inferring readiness from launch.
    def wait_process_ready(self, step):
        marker = self.gateway.home / "owners" / "local" / "main" / "process-ready"
        # LLM: File creation can precede its first complete write; an empty marker is not readiness.
        # 函数用途: Read only a complete PID produced by the actual tool process.
        def marker_pid():
            value = marker.read_text().strip() if marker.exists() else ""
            return int(value) if value.isdecimal() else None
        pid = wait_until(marker_pid, self.bounds["condition_seconds"], self.bounds["poll_seconds"])
        os.kill(pid, 0)
        self.requests[self.aliases[step["target"]]]["shell_pid"] = pid
        self.observe_start(self.aliases[step["target"]])

    # LLM: A missing exact PID is process exit evidence; permission errors remain failures.
    # 函数用途: Confirm that the real shell process exited after stop rather than treating its ACK as exit.
    def prove_process_exit(self, step):
        request_id = self.aliases[step["target"]]
        if self.requests[request_id].get("shell_exit_proved"):
            return
        pid = self.requests[request_id]["shell_pid"]

        # LLM: ESRCH is the only successful absence observation; a live or inaccessible process is not exit proof.
        # 函数用途: Test the exact process identity recorded by the shell readiness marker.
        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False
        wait_until(gone, self.bounds["condition_seconds"], self.bounds["poll_seconds"])
        self.recorder.event("process", "shell_exit_proof", **self.fence(self.payload(request_id, terminal=True)),
                            confirmed=True, proof_kind="exact_pid_absent", pid=pid)
        self.requests[request_id]["shell_exit_proved"] = True

    # LLM: Restart proves the old foreground subprocess exited before a replacement can execute.
    # 函数用途: Restart the same isolated Gateway home and record the real process-exit boundary.
    def restart(self, step):
        request_id = self.aliases[step["target"]]
        row = self.observe_start(request_id)
        turn = self.requests[request_id]["provider_turn"]
        # LLM: Holding the provider's publication lock prevents it completing between readiness proof and process death.
        # 函数用途: Observe this exact call blocked on its unreleased phase before killing its real executor.
        def blocked():
            with self.provider.condition:
                return self.provider.records[turn].get("waiting") is True
        wait_until(blocked, self.bounds["condition_seconds"], self.bounds["poll_seconds"])
        with self.provider.condition:
            record = self.provider.records[turn]
            assert record.get("waiting") and not self.provider.phase_gate(record["gate_name"]).is_set()
            os.killpg(self.gateway.proc.pid, signal.SIGKILL)
            self.gateway.proc.wait(timeout=self.bounds["teardown_seconds"])
        assert self.gateway.proc.poll() is not None
        edge = self.recorder.event("process", "exit_proof", **self.fence(row), confirmed=True, proof_kind="subprocess_wait")
        self.last_stopped = {**self.fence(row), "edge": edge}
        self.provider.release_phase(record["gate_name"])
        self.gateway.start()
        self.requests[request_id]["started"] = False
        # LLM: A stale processing row from the dead process cannot be recorded as a replacement execution.
        # 函数用途: Wait for both a different execution ID and a strictly newer durable lease epoch.
        def replacement_claim():
            candidate = self.payload(request_id)
            return candidate and candidate.get("execution_attempt_id") not in {None, "", row["execution_attempt_id"]} and candidate.get("lease_epoch", 0) > row["lease_epoch"]
        wait_until(replacement_claim, self.bounds["condition_seconds"], self.bounds["poll_seconds"])
        self.observe_start(request_id)

    # LLM: Conditions are observable HTTP/files/provider facts; no scenario step is a fixed sleep.
    # 函数用途: Execute the scenario and return its normalized, invariant-ready trace.
    def run(self):
        self.provider.start()
        try:
            self.gateway = ReplayGatewayProcess(self.root / "gateway", self.provider, self.bounds, self.scenario.get("config", {}))
            self.recorder.gateway_origin = self.gateway.base_url
            self.provider.replacements = {"OWNER_HOME": str(self.gateway.home / "owners" / "local" / "main"),
                                          "PYTHON": sys.executable}
            from agent_py_agent.agent.user_space.approval_mode import (
                execute_approval_mode_operation,
            )
            from agent_py_agent.agent.user_space.home_layout import home_paths
            from agent_py_agent.agent.user_space.owner_resolver import (
                OwnerIdentity,
                home_paths_with_owner,
                resolve_owner_home,
            )
            home = home_paths_with_owner(home_paths(self.gateway.home), resolve_owner_home(self.gateway.home, OwnerIdentity.local_main()))
            execute_approval_mode_operation(home, "set", "ask" if self.scenario.get("interactive_approvals") else "auto")
            try:
                self.gateway.start()
                for step in self.scenario["steps"]:
                    action = step["action"]
                    if action == "ask":
                        self.ask(step)
                    elif action in {"stop", "new"}:
                        self.control(step)
                    elif action == "steer":
                        result = self.call("/ask", {"prompt": step["text"], "conversation_id": step.get("session", "main"),
                            "metadata": {"message_id": step["ref"]}})
                        assert result.get("disposition") == "active_turn_input", result
                        target = result["target_turn_id"]
                        assert target in self.requests and not self.requests[target]["finished"], result
                        self.requests[target].setdefault("steer_inputs", []).append(result["request_id"])
                    elif action == "release_provider":
                        self.provider.release_phase(step.get("phase", "default"))
                    elif action == "wait_terminal":
                        self.complete(step["target"], cancelled=step.get("cancelled", False))
                    elif action == "approve":
                        self.approve(step)
                    elif action == "wait_children":
                        self.wait_children(step)
                    elif action == "wait_process_ready":
                        self.wait_process_ready(step)
                    elif action == "prove_process_exit":
                        self.prove_process_exit(step)
                    elif action == "restart":
                        self.restart(step)
                    else:
                        raise AssertionError(f"unsupported scenario action: {action}")
                assert all(state["finished"] for state in self.requests.values())
                trace = self.recorder.finish()
                result = normalize(trace)
                (self.root / "evidence" / "trace.normalized.json").write_text(json.dumps(result, indent=2) + "\n")
                return result
            finally:
                self.provider.release.set()
                self.gateway.close()
        finally:
            self.provider.close()


# LLM: This public fixture entry point never changes expected baselines.
# 函数用途: Run one scenario against a fresh real Gateway process.
def replay(root, scenario, fixture_parameters):
    return Replayer(root, scenario, fixture_parameters).run()
