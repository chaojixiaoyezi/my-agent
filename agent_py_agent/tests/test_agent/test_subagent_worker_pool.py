from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


def _accepted_result(summary: str) -> ModelResponse:
    return ModelResponse(
        text=(
            "[SUBAGENT_RESULT]\n"
            "{\n"
            '  "status": "AWAITING_ACCEPTANCE",\n'
            f'  "summary": "{summary}",\n'
            '  "used_tools": [],\n'
            '  "used_skills": [],\n'
            '  "evidence": [{"kind": "note", "summary": "worker pool 证据", "ok": true}],\n'
            '  "evidence_packets": [{"id": "evpkt-worker-pool", "claim": "worker pool 任务已完成", "checked_scope": "worker pool runner", "evidence_refs": ["runner_result.json"], "artifact_refs": ["output.json"], "confidence": 0.9}],\n'
            '  "capability_requests": [],\n'
            '  "artifacts": [],\n'
            '  "tests": [{"name": "worker-pool", "command": "", "ok": true, "summary": "通过"}],\n'
            '  "patches": [],\n'
            '  "lessons": [],\n'
            '  "next_actions": [],\n'
            '  "blocked_reason": "",\n'
            '  "failure_type": ""\n'
            "}\n"
            "[/SUBAGENT_RESULT]"
        ),
        backend="worker-pool-test",
    )


class CountingAcceptedBackend(BaseBackend):
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        assert "[SUBAGENT_RESULT]" in prompt
        with self.lock:
            self.calls += 1
            call_no = self.calls
        time.sleep(0.03)
        return _accepted_result(f"worker call {call_no}")


class OneSlowOneFastBackend(BaseBackend):
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        assert "[SUBAGENT_RESULT]" in prompt
        with self.lock:
            self.calls += 1
            call_no = self.calls
        if call_no == 1:
            time.sleep(6.0)
            return _accepted_result("slow worker eventually finished")
        return _accepted_result("fast worker finished")


def test_dispatch_parallel_runner_pool_respects_start_rate(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = CountingAcceptedBackend()
        monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
        cfg = AgentConfig(
            model_backend="worker-pool-test",
            subagent_workspace="subs",
            runner_concurrency="3",
            runner_start_rate="2",
        )
        agent = SimpleAgent(cfg, root)
        tasks = [
            agent.subagents.create_run(goal=f"并发任务 {index}", thought="等待 worker pool。", plan=["执行", "验收"])
            for index in range(3)
        ]

        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            execute_runners=True,
            max_runners=3,
            probe=False,
            reviewer="worker-pool-test",
        )

        loaded = [agent.subagents.load(task.id) for task in tasks]
        done = [item for item in loaded if item.status == "DONE"]
        planning = [item for item in loaded if item.status == "PLANNING"]

        assert backend.calls == 2
        assert len([item for item in report.records if item.step == "runner"]) == 2
        assert len(done) == 2
        assert len(planning) == 1


def test_dispatch_parallel_runner_pool_timeout_does_not_block_other_workers(monkeypatch, tmp_path):
    root = tmp_path
    backend = OneSlowOneFastBackend()
    monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
    cfg = AgentConfig(
        model_backend="worker-pool-test",
        subagent_workspace="subs",
        runner_concurrency="2",
        runner_start_rate="2",
        runner_timeout_seconds="3.0",
    )
    agent = SimpleAgent(cfg, root)
    tasks = [
        agent.subagents.create_run(goal=f"超时隔离任务 {index}", thought="等待 worker pool。", plan=["执行", "验收"])
        for index in range(2)
    ]

    router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
    report = agent.dispatch_subagents(
        router,
        CapabilityConfig(),
        apply=True,
        execute_runners=True,
        max_runners=2,
        probe=False,
        reviewer="worker-pool-timeout-test",
    )

    loaded = [agent.subagents.load(task.id) for task in tasks]
    statuses = sorted(item.status for item in loaded)
    runner_records = [item for item in report.records if item.step == "runner"]

    assert len(runner_records) == 2
    assert any(item.status == "TIMEOUT" for item in loaded)
    assert any(item.status == "DONE" for item in loaded)
    assert statuses == ["DONE", "TIMEOUT"]
    assert any(not item.ok and "timed out" in item.message for item in runner_records)
    assert any(item.ok for item in runner_records)
