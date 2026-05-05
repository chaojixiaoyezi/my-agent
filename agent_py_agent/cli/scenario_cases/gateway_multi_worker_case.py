from __future__ import annotations

"""LLM: implements the gateway multi-worker scenario that proves concurrent workers process every request once.

给人看的解释：
这个文件验证两个 worker 同时处理一个队列时，每个请求只会被处理一次。
不会出现重复处理或遗漏。
"""

import json
import threading
import time
from dataclasses import dataclass

from ...agent.agent_core.models import AgentRunResult
from ...agent.gateway import (
    _process_gateway_requests,
    gateway_paths,
    gateway_response_path,
    new_gateway_request_id,
    read_json_file,
    write_gateway_request,
)
from ..scenario_utils import (
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)


@dataclass
class WorkerRunResults:
    """Bundle of worker run results for multi-worker scenario."""
    processed_by_worker: dict[str, int]
    run_prompts_by_worker: dict[str, list[str]]
    errors: list[str]
    alive_threads: list[str]


@dataclass
class MultiWorkerScenarioSetup:
    """Bundle of gateway setup data for multi-worker scenario."""
    gpaths: object
    request_ids: list[str]
    request_count: int


def _multi_worker_setup(args):
    """Setup for multi-worker scenario: create workspace, agent, gateway paths, and enqueue requests."""
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-multi-worker")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    gpaths = gateway_paths(agent)
    for path in (gpaths.inbox, gpaths.processing, gpaths.done, gpaths.failed, gpaths.responses):
        path.mkdir(parents=True, exist_ok=True)

    request_count = max(4, int(getattr(args, "count", 0) or 0))
    request_ids: list[str] = []
    print_scenario_step(1, f"Enqueue {request_count} pending gateway requests")
    for index in range(request_count):
        request_id = f"{new_gateway_request_id()}-{index}"
        request_ids.append(request_id)
        write_gateway_request(
            gpaths,
            {
                "id": request_id,
                "kind": "ask",
                "prompt": f"gateway multi-worker scenario request {index}",
                "inject": [],
                "prompt_files": [],
                "save": False,
                "include_prompt": False,
                "created_at": time.time(),
                "status": "pending",
                "attempts": 0,
            },
        )
    print("request_ids=" + json.dumps(request_ids, ensure_ascii=False))
    return paths, gpaths, request_ids, request_count


def _multi_worker_verify(setup: MultiWorkerScenarioSetup, results: WorkerRunResults):
    """Verify multi-worker results: check responses, done archives, and queue state."""
    responses = {request_id: read_json_file(gateway_response_path(setup.gpaths, request_id)) for request_id in setup.request_ids}
    done_payloads = {request_id: read_json_file(setup.gpaths.done / f"{request_id}.json") for request_id in setup.request_ids}
    response_backends = sorted(str(payload.get("backend") or "") for payload in responses.values())
    done_owners = sorted(str(payload.get("lease_owner") or "") for payload in done_payloads.values())
    pending_left = sorted(path.name for path in setup.gpaths.inbox.glob("*.json"))
    processing_left = sorted(path.name for path in setup.gpaths.processing.glob("*.json"))
    failed_left = sorted(path.name for path in setup.gpaths.failed.glob("*.json"))
    print("response_backends=" + json.dumps(response_backends, ensure_ascii=False))
    print("done_owners=" + json.dumps(done_owners, ensure_ascii=False))
    print(
        "queue_left="
        + json.dumps(
            {"pending": pending_left, "processing": processing_left, "failed": failed_left},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return (
        not results.errors
        and not results.alive_threads
        and sum(results.processed_by_worker.values()) == setup.request_count
        and len([count for count in results.processed_by_worker.values() if count > 0]) == 2
        and all(payload.get("ok") is True for payload in responses.values())
        and all(payload.get("status") == "done" for payload in responses.values())
        and all(payload.get("attempts") == 1 for payload in responses.values())
        and set(response_backends) == {"scenario-worker-0", "scenario-worker-1"}
        and set(done_owners) == {"scenario-worker-0", "scenario-worker-1"}
        and not pending_left
        and not processing_left
        and not failed_left
    ), responses, done_payloads


def _multi_worker_finish(paths, setup: MultiWorkerScenarioSetup, results: WorkerRunResults):
    """Complete multi-worker run: print diagnostics and verify results."""
    print("processed_by_worker=" + json.dumps(results.processed_by_worker, ensure_ascii=False, sort_keys=True))
    print("run_prompts_by_worker=" + json.dumps(results.run_prompts_by_worker, ensure_ascii=False, sort_keys=True))
    if results.errors:
        print("worker_errors=" + json.dumps(results.errors, ensure_ascii=False))
    if results.alive_threads:
        print("worker_threads_still_alive=" + json.dumps(results.alive_threads, ensure_ascii=False))

    print_scenario_step(3, "Verify every request has one response and one done archive")
    final_ok, responses, done_payloads = _multi_worker_verify(setup, results)
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway multi-worker processing passed" if final_ok else "gateway multi-worker processing failed",
        extra={
            "case": "gateway-multi-worker",
            "request_ids": setup.request_ids,
            "processed_by_worker": results.processed_by_worker,
            "run_prompts_by_worker": results.run_prompts_by_worker,
            "errors": results.errors,
            "responses": responses,
            "done_owners": [str(payload.get("lease_owner") or "") for payload in done_payloads.values()],
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_multi_worker_case(args) -> int:
    """LLM: run two gateway workers against one pending queue and prove every request completes once.

    新手说明:
    启动两个 worker 同时处理同一队列中的请求。
    验证每个请求只被处理一次，不会出现重复处理或遗漏。
    """

    paths, gpaths, request_ids, request_count = _multi_worker_setup(args)

    print_scenario_step(2, "Run two live workers concurrently")
    lock = threading.Lock()
    start_barrier = threading.Barrier(2)
    processed_by_worker: dict[str, int] = {}
    run_prompts_by_worker: dict[str, list[str]] = {}
    errors: list[str] = []

    def worker_run(worker_id: str):
        def slow_run(user_prompt: str, **kwargs) -> AgentRunResult:
            time.sleep(0.05)
            with lock:
                run_prompts_by_worker.setdefault(worker_id, []).append(user_prompt)
            return AgentRunResult(
                prompt=f"{worker_id} prompt: {user_prompt}",
                response=f"{worker_id} response: {user_prompt}",
                backend=worker_id,
                used_memories=0,
            )

        worker_agent = load_scenario_agent(paths.config)
        worker_agent.run = slow_run  # type: ignore[method-assign]
        try:
            start_barrier.wait(timeout=5)
            processed = _process_gateway_requests(worker_agent, gpaths, worker_id=worker_id)
        except Exception as exc:
            with lock:
                errors.append(f"{worker_id}: {type(exc).__name__}: {exc}")
        else:
            with lock:
                processed_by_worker[worker_id] = processed

    threads = [
        threading.Thread(target=worker_run, args=(f"scenario-worker-{index}",), daemon=True)
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    alive_threads = [thread.name for thread in threads if thread.is_alive()]

    return _multi_worker_finish(
        paths,
        MultiWorkerScenarioSetup(gpaths=gpaths, request_ids=request_ids, request_count=request_count),
        WorkerRunResults(processed_by_worker=processed_by_worker, run_prompts_by_worker=run_prompts_by_worker, errors=errors, alive_threads=alive_threads),
    )
