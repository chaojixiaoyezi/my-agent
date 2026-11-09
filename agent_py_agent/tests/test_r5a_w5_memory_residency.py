"""离线驻留回放：真实Agent请求/工具生命周期，分开记录分配、当前RSS与峰值。

可用 ``python -m agent_py_agent.tests.test_r5a_w5_memory_residency --replay
--root <隔离目录> --report <JSON>`` 在不同解释器上重复采样；通过不代表I-8关闭。
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import threading
import time
import tracemalloc
import weakref
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.concurrency.daemon_executor import DurableDaemonThreadPoolExecutor
from agent_py_agent.agent.gateway_parts.background_resource_report import (
    _owner_pool_memory_gauges,
    _rss_peak_bytes,
    collect_runtime_memory_diagnostics,
)
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool, build_owner_scoped_agent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaChange, OwnerQuotaEnforcer
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
)

_REQUEST_CONTEXT = ContextVar("r5a_w5_request", default=None)


class _Payload:
    def __init__(self, size=65536):
        self.data = bytearray(size)


class _ProbeTool(BaseTool):
    model_spec = make_test_model_spec("residency_probe")
    runtime_policy = make_test_runtime_policy()

    def __init__(self):
        self.calls = 0

    def execute(self, parameters):
        self.calls += 1
        return ToolHandlerOutcome(True, "probe-complete")


class _FakeLLM:
    name = "r5a_offline"

    def __init__(self, agent, references):
        self.calls = 0
        self.agent = weakref.ref(agent)
        self.references = references

    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint="local://offline", model="fixture",
                                      stream=False, native_supported=True, evidence="offline_replay")

    def generate(self, prompt, on_chunk=None, **kwargs):
        params = getattr(self.agent(), "_current_run_params", None)
        if params is not None:
            self.references["params"].append(weakref.ref(params))
            memo = getattr(params, "_context_estimate_memo", None)
            if memo is not None:
                self.references["memos"].append(weakref.ref(memo))
        self.calls += 1
        if self.calls % 2:
            return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
                "id": f"probe-{self.calls}", "name": "residency_probe", "input": {},
            }])
        return ModelResponse(text="offline-complete", backend=self.name)


def _current_rss():
    statm = Path("/proc/self/statm")
    if sys.platform.startswith("linux") and statm.is_file():
        return int(statm.read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE"), "proc_self_statm"
    if sys.platform == "darwin":
        # 只查询本回放进程，不读取线上PID，也不把ru_maxrss当成当前值。
        result = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
                                check=True, capture_output=True, text=True)
        return int(result.stdout.strip()) * 1024, "ps_self_rss_kib"
    return None, "unavailable"


def _fd_count():
    for name in ("/proc/self/fd", "/dev/fd"):
        if Path(name).is_dir():
            return len(os.listdir(name))
    return None


def _snapshot(phase, pool, references, *, cycle=0, requests=0, full_snapshot=False):
    assert tracemalloc.is_tracing()
    # 密集采样仍取实时计数；完整分配快照只在控制点与释放后的首个样本执行。
    diagnostics = (collect_runtime_memory_diagnostics(
        owner_pool=pool, config=SimpleNamespace(enable_gateway_memory_diagnostics=True), top_n=5)
        if full_snapshot else {
            "tracemalloc_scope": "existing_trace", "tracemalloc_top": [],
            "owner_pool": _owner_pool_memory_gauges(pool),
            "rss_peak_bytes": _rss_peak_bytes(), "rss_source": "resource.ru_maxrss (peak)",
        })
    assert diagnostics["tracemalloc_scope"] == "existing_trace"
    current, peak = tracemalloc.get_traced_memory()
    rss, source = _current_rss()
    # gc.get_objects产生的临时列表在计数后立即释放，不把它本身当作持有者。
    counts = Counter(type(value).__name__ for value in gc.get_objects()) if full_snapshot else None
    return {
        "phase": phase, "cycle": cycle, "requests": requests, "monotonic": time.monotonic(),
        "python_live_bytes": current, "python_peak_bytes": peak,
        "rss_current_bytes": rss, "rss_current_source": source,
        "rss_peak_bytes": diagnostics["rss_peak_bytes"], "rss_peak_source": diagnostics["rss_source"],
        "rss_minus_traced_bytes": None if rss is None else rss - current,
        "residual_classification": "allocator/native/unresolved",
        "gc_object_count": None if counts is None else sum(counts.values()),
        "selected_objects": {name: None if counts is None else counts[name] for name in (
            "SimpleAgent", "ContextEstimateMemo", "ModelCallLedger", "ModelCallRecord", "RunParams")},
        "tracked_alive": {name: sum(ref() is not None for ref in refs)
                          for name, refs in references.items()},
        "threads": len(threading.enumerate()), "fd_count": _fd_count(),
        "owner_pool": diagnostics["owner_pool"], "allocation_top": diagnostics["tracemalloc_top"],
        "full_snapshot": full_snapshot,
    }


def _idle(samples, pool, references, phase, *, cycle, requests):
    # 连续静息采样与释放后的GC分开，不能仅凭一次collect声称堆已归还。
    for index in range(3):
        time.sleep(0.01)
        samples.append(_snapshot(phase, pool, references, cycle=cycle, requests=requests,
                                 full_snapshot=phase == "released_after_gc" and index == 0))


def _context_control(executor_type):
    payload = _Payload()
    reference = weakref.ref(payload)
    token = _REQUEST_CONTEXT.set(payload)
    context = copy_context()
    executor = executor_type(max_workers=1, thread_name_prefix="r5a-w5-control")
    try:
        future = executor.submit(context.run, lambda: _REQUEST_CONTEXT.get() is not None)
        assert future.result(timeout=10)
    finally:
        _REQUEST_CONTEXT.reset(token)
    del payload, context, future, token
    executor.submit(lambda: None).result(timeout=10)
    gc.collect()
    retained_idle = reference() is not None
    executor.shutdown(wait=True)
    del executor
    gc.collect()
    return {"visible_during_job": True, "retained_idle": retained_idle,
            "retained_after_release": reference() is not None}


def _quota_controls(root):
    controls = []
    for quota_mb in (100, 1024, 100000):
        directory = root / f"quota-{quota_mb}"
        directory.mkdir()
        target = directory / "fixture.bin"
        enforcer = OwnerQuotaEnforcer(directory, max_bytes=quota_mb * 1024 * 1024)
        with enforcer.reserve([OwnerQuotaChange(target, 1024)]):
            target.write_bytes(bytes(1024))
        ledger = json.loads((directory / ".owner-quota-ledger.json").read_text())
        assert ledger["used_bytes"] == 1024 and ledger["reserved_bytes"] == 0
        controls.append({"quota_mb": quota_mb, "synthetic_bytes": 1024,
                         "used_bytes": ledger["used_bytes"], "reserved_bytes": ledger["reserved_bytes"]})
    return controls


def _run_replay(root):
    # 冷导入及共享不可变目录属于夹具准备；所有 Agent 实例和请求仍在完整追踪窗口内创建。
    from agent_py_agent.agent.core import SimpleAgent

    import_module("agent_py_agent.agent.gateway_parts.business_api").business_action_specs()
    import_module("agent_py_agent.agent.agent_core.self_map_bootstrap")
    root.mkdir(parents=True, exist_ok=True)
    config = AgentConfig(my_agent_home=str(root / "home"), enable_plugins=False,
                         enable_subagents=False, home_context_enabled=False, auto_save_memory=False,
                         enable_gateway_memory_diagnostics=True, owner_agent_pool_max_agents=2)
    pool = OwnerScopedAgentPool(config, root, max_agents=2)
    references = {name: [] for name in ("agents", "params", "memos", "results", "request_payloads")}
    tools, samples, finished = [], [], 0

    def build(base_config, workspace, owner, workspace_roots) -> SimpleAgent:
        agent = build_owner_scoped_agent(base_config, workspace, owner, workspace_roots)
        agent.backend = _FakeLLM(agent, references)
        tool = _ProbeTool()
        agent.tools.register(tool)
        tools.append(weakref.ref(tool))
        references["agents"].append(weakref.ref(agent))
        return agent

    pool._builder = build

    def request(index, owner):
        nonlocal finished
        with pool.acquire_pinned(owner) as agent:
            payload = _Payload()
            references["request_payloads"].append(weakref.ref(payload))
            token = _REQUEST_CONTEXT.set(payload)
            try:
                result = agent.run("offline residency probe", save=False, allowed_tools=["residency_probe"],
                                   request_id=f"replay-{index}", context_scope="isolated")
            finally:
                _REQUEST_CONTEXT.reset(token)
            assert result.response == "offline-complete" and result.tool_rounds == 1
            assert result.runtime_status == "ok"
            references["results"].append(weakref.ref(result))
            params = getattr(agent, "_current_run_params", None)
            if params is not None:
                references["params"].append(weakref.ref(params))
                memo = getattr(params, "_context_estimate_memo", None)
                if memo is not None:
                    references["memos"].append(weakref.ref(memo))
            ledger = agent._model_call_ledger
            assert all(record.status == "finished" for record in ledger.records())
            assert len(ledger.records()) <= ledger.options.max_records
            finished += 1

    before_threads = {thread.ident for thread in threading.enumerate()}
    assert not tracemalloc.is_tracing(), "回放必须独占自己的完整追踪窗口"
    tracemalloc.start(1)
    try:
        samples.append(_snapshot("trace_started", pool, references))
        # 独立正控制：明确由本测试列表持有的2MiB Python分配，释放后弱引用必须消失。
        positive = [_Payload(262144) for _ in range(8)]
        positive_refs = [weakref.ref(item) for item in positive]
        baseline_bytes = tracemalloc.get_traced_memory()[0]
        samples.append(_snapshot("positive_retained", pool, references, full_snapshot=True))
        assert sum(ref() is not None for ref in positive_refs) == 8
        del positive
        gc.collect()
        released_bytes = tracemalloc.get_traced_memory()[0]
        assert all(ref() is None for ref in positive_refs)
        assert baseline_bytes - released_bytes > 1048576
        controls = {"python_retention_live_delta": baseline_bytes - released_bytes,
                    "quota_admission": _quota_controls(root),
                    "standard_executor": _context_control(ThreadPoolExecutor),
                    "durable_executor": _context_control(DurableDaemonThreadPoolExecutor)}
        assert not controls["durable_executor"]["retained_idle"]
        assert not controls["durable_executor"]["retained_after_release"]
        assert not controls["standard_executor"]["retained_after_release"]
        assert controls["standard_executor"]["retained_idle"] == bool(
            getattr(sys.flags, "thread_inherit_context", 0))
        # 固定 owner 的第三次请求验证复用；第三个不同 owner 超过容量，验证淘汰。
        for cycle in range(1):
            for mode, total in (("fixed", 3), ("growing", 1)):
                for index in range(total):
                    owner = OwnerIdentity.provider_user("replay", str(index % 2 if mode == "fixed" else index + 2))
                    request(finished, owner)
                    if index in (0, 1, total - 1):
                        samples.append(_snapshot(mode, pool, references, cycle=cycle, requests=finished))
                        assert pool.active_count() <= 2
                _idle(samples, pool, references, "idle_before_release", cycle=cycle, requests=finished)
            # 只走原pool退休/CAS路径；不调用任何堆归还函数或修改生产阈值。
            for key, agent, touched in pool.idle_candidates(0):
                assert pool.evict_idle(key, agent, touched)
            del agent
            samples.append(_snapshot("released_before_gc", pool, references, cycle=cycle, requests=finished))
            gc.collect()
            _idle(samples, pool, references, "released_after_gc", cycle=cycle, requests=finished)
        assert pool.active_count() == 0
        assert not pool._pins and not pool._retired_agents
        return {"schema_version": 1, "python": sys.version, "gil_enabled": (
                    sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True),
                "thread_inherit_context": getattr(sys.flags, "thread_inherit_context", 0),
                "tracking_started_before_work": True, "request_count": finished,
                "pool_capacity": 2, "cycles": 1, "controls": controls, "samples": samples,
                "normal_exit": True, "i8_status": "unresolved",
                "post_release_alive": {name: sum(ref() is not None for ref in refs)
                                       for name, refs in references.items()},
                "tools_alive_after_release": sum(ref() is not None for ref in tools),
                "workers_after_release": sum(thread.ident not in before_threads
                                             for thread in threading.enumerate()),
                "limits": ["离线小样本，未运行在线Gateway或真实供应商", "未覆盖企业规模及多年运行",
                           "RSS与追踪差额不证明原生泄漏或分配器根因"]}
    finally:
        tracemalloc.stop()


def test_fixed_capacity_replay_and_normal_exit(tmp_path):
    report = tmp_path / "residency.json"
    home = tmp_path / "isolated-home"
    home.mkdir()
    completed = subprocess.run([
        sys.executable, "-m", "agent_py_agent.tests.test_r5a_w5_memory_residency",
        "--replay", "--root", str(home / "work"), "--report", str(report),
    ], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=240,
        env={**os.environ, "HOME": str(home), "MY_AGENT_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    assert completed.returncode == 0, completed.stderr
    data = json.loads(report.read_text())
    assert data["normal_exit"] and data["request_count"] == 4
    assert data["tracking_started_before_work"] and data["i8_status"] == "unresolved"
    assert data["tracking_stopped_after_work"]
    assert [row["quota_mb"] for row in data["controls"]["quota_admission"]] == [100, 1024, 100000]
    assert sum(row["phase"] == "released_after_gc" for row in data["samples"]) == 3
    assert len([row for row in data["samples"] if row["phase"] == "fixed"]) == 3
    assert [row["owner_pool"]["hard_count"] for row in data["samples"]
            if row["phase"] == "fixed"] == [1, 2, 2]
    assert [row["owner_pool"]["hard_count"] for row in data["samples"]
            if row["phase"] == "growing"] == [2]
    assert {row["phase"] for row in data["samples"] if row["full_snapshot"]} == {
        "positive_retained", "released_after_gc"}
    assert all(row["rss_current_source"] != row["rss_peak_source"] for row in data["samples"])
    assert all(set(row) == {"location", "size_bytes", "block_count"} for sample in data["samples"]
               for row in sample["allocation_top"])
    assert "offline residency probe" not in report.read_text()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", action="store_true", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    outcome = _run_replay(arguments.root)
    assert not tracemalloc.is_tracing()
    outcome["tracking_stopped_after_work"] = True
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
