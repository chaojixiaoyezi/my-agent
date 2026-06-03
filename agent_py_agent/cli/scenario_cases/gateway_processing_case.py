
from __future__ import annotations

"""gateway processing-stop scenario case.

给人看的解释：
这个模块专门模拟 gateway 正在处理请求时被 stop/restart 打断，验证请求能恢复并完成。
"""

import json
import os
import time
from dataclasses import dataclass

from ...agent.gateway import (
    _process_gateway_requests,
    gateway_paths,
    gateway_response_path,
    new_gateway_request_id,
    read_json_file,
    requeue_gateway_processing_requests,
    write_json_file,
)
from ..scenario_utils import (
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)


@dataclass
class ProcessingVerifyResults:
    done_path: object
    response_path: object
    final_response: dict
    response_json_valid: bool


@dataclass
class ProcessingStopVerifyRequest:
    paths: object
    request_id: str
    requeued: int
    processed: int
    after_requeue_processing: object
    after_requeue_pending: object
    done_payload: dict
    verify: ProcessingVerifyResults


@dataclass
class ProcessingRequeueResult:
    requeued: int
    pending_path: object
    processing_path: object


@dataclass
class ProcessingCompletionResult:
    processed: int
    done_payload: dict
    verify: ProcessingVerifyResults


def _processing_stop_setup(args):
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-processing-stop")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    gpaths = gateway_paths(agent)
    for path in (gpaths.inbox, gpaths.processing, gpaths.done, gpaths.failed, gpaths.responses):
        path.mkdir(parents=True, exist_ok=True)

    request_id = new_gateway_request_id()
    pending_path = gpaths.inbox / f"{request_id}.json"
    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": "gateway processing stop mid-request test.",
        "inject": [],
        "prompt_files": [],
        "save": False,
        "include_prompt": False,
        "created_at": time.time(),
        "status": "pending",
        "attempts": 0,
        "client_pid": os.getpid(),
    }
    write_json_file(pending_path, payload)
    return paths, agent, gpaths, request_id, pending_path, payload


def _processing_stop_simulate_lease(gpaths, request_id, pending_path, payload):
    processing_path = gpaths.processing / pending_path.name
    try:
        pending_path.rename(processing_path)
    except OSError as exc:
        raise AssertionError(f"failed to move pending to processing: {exc}") from exc

    now = time.time()
    processing_payload = dict(payload)
    processing_payload.update(
        {
            "status": "processing",
            "attempts": 1,
            "lease_owner": "scenario-mid-request-worker",
            "lease_started_at": now,
            "lease_heartbeat_at": now,
            "updated_at": now,
        }
    )
    write_json_file(processing_path, processing_payload)
    response_path = gateway_response_path(gpaths, request_id)
    print(f"processing_path={processing_path} exists={processing_path.exists()}")
    print(f"processing_before_stop={read_json_file(processing_path).get('status')}")
    return processing_path, response_path


def _processing_stop_verify_results(request: ProcessingStopVerifyRequest):
    verify = request.verify
    final_ok = (
        request.requeued == 1
        and not request.after_requeue_processing.exists()
        and request.after_requeue_pending.exists()
        and request.processed == 1
        and verify.done_path.exists()
        and verify.final_response.get("ok") is True
        and verify.final_response.get("status") == "done"
        and request.done_payload.get("lease_owner") == "scenario-recovery-worker-after-stop"
        and request.done_payload.get("attempts") == 2
        and verify.response_json_valid
    )
    write_scenario_summary(
        request.paths,
        ok=final_ok,
        reason="gateway processing stop passed" if final_ok else "gateway processing stop failed",
        extra={
            "case": "gateway-processing-stop",
            "request_id": request.request_id,
            "requeued": request.requeued,
            "processed": request.processed,
            "done_path": str(verify.done_path),
            "response_path": str(verify.response_path),
            "response": verify.final_response,
        },
    )
    print(f"\nsummary_json={request.paths.summary_json}")
    print(f"summary_md={request.paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def _processing_stop_requeue(gpaths, processing_path) -> ProcessingRequeueResult:
    print_scenario_step(3, "Simulate gateway stop/restart mid-processing (requeue stale leases)")
    requeued = requeue_gateway_processing_requests(gpaths)
    after_requeue_pending = gpaths.inbox / processing_path.name
    after_requeue_processing = gpaths.processing / processing_path.name
    print(f"requeued={requeued}")
    print(f"after_requeue_pending={after_requeue_pending.exists()}")
    print(f"after_requeue_processing={after_requeue_processing.exists()}")
    return ProcessingRequeueResult(requeued, after_requeue_pending, after_requeue_processing)


def _processing_stop_complete(agent, gpaths, processing_path, response_path) -> ProcessingCompletionResult:
    print_scenario_step(4, "Let a new worker pick up the requeued request and complete it")
    processed = _process_gateway_requests(agent, gpaths, worker_id="scenario-recovery-worker-after-stop")
    done_path = gpaths.done / processing_path.name
    done_payload = read_json_file(done_path)
    final_response = read_json_file(response_path)
    print(f"processed={processed}")
    print(f"done_path={done_path} exists={done_path.exists()}")
    print(f"response_path={response_path} exists={response_path.exists()} ok={final_response.get('ok')}")
    return ProcessingCompletionResult(
        processed=processed,
        done_payload=done_payload,
        verify=ProcessingVerifyResults(
            done_path=done_path,
            response_path=response_path,
            final_response=final_response,
            response_json_valid=_response_json_valid(final_response),
        ),
    )


def _response_json_valid(final_response: dict) -> bool:
    print_scenario_step(5, "Verify no half-written JSON, no lost requests")
    try:
        json.loads(final_response.get("response", "{}") or "{}")
        return True
    except json.JSONDecodeError:
        return False


def run_scenario_gateway_processing_stop_case(args) -> int:
    paths, agent, gpaths, request_id, pending_path, payload = _processing_stop_setup(args)
    print_scenario_step(1, "Submit request to gateway inbox")
    print(f"pending_before={pending_path.exists()} request_id={request_id}")
    print_scenario_step(2, "Simulate: worker claims request and starts agent.run (lease active)")
    processing_path, response_path = _processing_stop_simulate_lease(gpaths, request_id, pending_path, payload)
    requeue = _processing_stop_requeue(gpaths, processing_path)
    completion = _processing_stop_complete(agent, gpaths, processing_path, response_path)
    return _processing_stop_verify_results(
        ProcessingStopVerifyRequest(
            paths=paths,
            request_id=request_id,
            requeued=requeue.requeued,
            processed=completion.processed,
            after_requeue_processing=requeue.processing_path,
            after_requeue_pending=requeue.pending_path,
            done_payload=completion.done_payload,
            verify=completion.verify,
        )
    )
