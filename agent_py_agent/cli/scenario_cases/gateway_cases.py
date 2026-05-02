from __future__ import annotations

"""LLM: implements gateway scenario tests for restart recovery, stale lease, and delayed response handling.

给人看的解释：
这个文件包含 gateway 重启恢复、过期租约恢复、延迟响应处理三个极端场景。
每个函数模拟一种 gateway 异常情况，验证系统能正确恢复。
"""

import json
import os
import time

from ...agent.agent_core.models import AgentRunResult
from ...agent.gateway import (
    _process_gateway_requests,
    gateway_paths,
    gateway_response_path,
    gateway_stale_processing,
    new_gateway_request_id,
    read_json_file,
    recover_gateway_processing_requests,
    requeue_gateway_processing_requests,
    write_gateway_request,
    write_json_file,
)
from ..scenario_utils import (
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)


def run_scenario_gateway_restart_case(args) -> int:
    """LLM: verify that gateway startup recovers leftover processing requests by requeuing them.

    新手说明:
    模拟 gateway 崩溃时请求卡在 processing 目录。
    重启后系统应该把遗留请求挪回 pending 队列重新处理。
    """

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-restart")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    gpaths = gateway_paths(agent)
    for path in (gpaths.inbox, gpaths.processing, gpaths.done, gpaths.failed, gpaths.responses):
        path.mkdir(parents=True, exist_ok=True)

    request_id = new_gateway_request_id()
    processing_path = gpaths.processing / f"{request_id}.json"
    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": "这个请求模拟 gateway 崩溃时卡在 processing。",
        "inject": [],
        "prompt_files": [],
        "save": False,
        "include_prompt": False,
        "created_at": time.time(),
        "client_pid": os.getpid(),
    }
    write_json_file(processing_path, payload)

    print_scenario_step(1, "模拟旧 gateway 崩溃遗留 processing 请求")
    print(f"processing_before={processing_path.exists()} path={processing_path}")
    requeued = requeue_gateway_processing_requests(gpaths)
    pending_path = gpaths.inbox / processing_path.name
    print_scenario_step(2, "执行 gateway 启动恢复步骤")
    print(f"requeued={requeued}")
    print(f"processing_after={processing_path.exists()}")
    print(f"pending_after={pending_path.exists()} path={pending_path}")

    final_ok = requeued == 1 and not processing_path.exists() and pending_path.exists()
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway restart requeue passed" if final_ok else "gateway restart requeue failed",
        extra={
            "case": "gateway-restart",
            "request_id": request_id,
            "pending_path": str(pending_path),
            "processing_path": str(processing_path),
            "requeued": requeued,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_stale_lease_case(args) -> int:
    """LLM: simulate an interrupted worker lease, then recover and finish the gateway request.

    新手说明:
    模拟 worker 租约过期后请求卡在 processing 目录的情况。
    验证系统能检测过期租约、恢复请求、重新处理并成功完成。
    """

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-stale-lease")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    gpaths = gateway_paths(agent)
    for path in (gpaths.inbox, gpaths.processing, gpaths.done, gpaths.failed, gpaths.responses):
        path.mkdir(parents=True, exist_ok=True)

    request_id = new_gateway_request_id()
    processing_path = gpaths.processing / f"{request_id}.json"
    stale_at = time.time() - 60
    write_json_file(
        processing_path,
        {
            "id": request_id,
            "kind": "ask",
            "prompt": "gateway stale lease scenario: please recover this interrupted request.",
            "inject": [],
            "prompt_files": [],
            "save": False,
            "include_prompt": False,
            "created_at": stale_at,
            "status": "processing",
            "attempts": 1,
            "lease_owner": "scenario-dead-worker",
            "lease_started_at": stale_at,
            "lease_heartbeat_at": stale_at,
            "updated_at": stale_at,
        },
    )

    print_scenario_step(1, "Create an old processing lease")
    stale_before = gateway_stale_processing(gpaths, timeout_seconds=1)
    print("stale_before=" + json.dumps(stale_before, ensure_ascii=False, sort_keys=True))

    print_scenario_step(2, "Recover stale processing back to pending")
    recovered = recover_gateway_processing_requests(
        gpaths,
        startup=False,
        max_attempts=2,
        timeout_seconds=1,
        agent=agent,
    )
    pending_path = gpaths.inbox / processing_path.name
    print("recovered=" + json.dumps(recovered, ensure_ascii=False, sort_keys=True))
    print(f"pending_after_recovery={pending_path.exists()} processing_after_recovery={processing_path.exists()}")

    print_scenario_step(3, "Let a live worker claim and complete the requeued request")
    processed = _process_gateway_requests(agent, gpaths, worker_id="scenario-recovery-worker")
    response_path = gateway_response_path(gpaths, request_id)
    done_path = gpaths.done / processing_path.name
    response = read_json_file(response_path)
    done_payload = read_json_file(done_path)
    print(f"processed={processed}")
    print(f"done_path={done_path} exists={done_path.exists()}")
    print(f"response_path={response_path} exists={response_path.exists()} ok={response.get('ok')}")

    final_ok = (
        stale_before
        and stale_before[0].get("request_id") == request_id
        and recovered["checked"] == 1
        and recovered["requeued"] == 1
        and pending_path.exists() is False
        and processing_path.exists() is False
        and processed == 1
        and done_path.exists()
        and response.get("ok") is True
        and response.get("status") == "done"
        and response.get("attempts") == 2
        and done_payload.get("lease_owner") == "scenario-recovery-worker"
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway stale lease recovery passed" if final_ok else "gateway stale lease recovery failed",
        extra={
            "case": "gateway-stale-lease",
            "request_id": request_id,
            "stale_before": stale_before,
            "recovered": recovered,
            "processed": processed,
            "done_path": str(done_path),
            "response_path": str(response_path),
            "response": response,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_delayed_response_case(args) -> int:
    """LLM: simulate a response arriving before a duplicate pending request is claimed.

    新手说明:
    当 gateway 响应已经存在时，worker 不应该再次调用模型。
    验证系统能检测已有响应、直接归档请求、不重复执行。
    """

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-delayed-response")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    gpaths = gateway_paths(agent)
    for path in (gpaths.inbox, gpaths.processing, gpaths.done, gpaths.failed, gpaths.responses):
        path.mkdir(parents=True, exist_ok=True)

    request_id = new_gateway_request_id()
    request_path = write_gateway_request(
        gpaths,
        {
            "id": request_id,
            "kind": "ask",
            "prompt": "gateway delayed response scenario: this should not run twice.",
            "inject": [],
            "prompt_files": [],
            "save": False,
            "include_prompt": False,
            "created_at": time.time(),
            "status": "pending",
            "attempts": 1,
        },
    )
    response_path = gateway_response_path(gpaths, request_id)
    write_json_file(
        response_path,
        {
            "id": request_id,
            "kind": "ask",
            "ok": True,
            "status": "done",
            "response": "late response already arrived",
            "backend": "scenario-delayed-response",
            "tool_rounds": 0,
            "attempts": 1,
        },
    )

    print_scenario_step(1, "Create a pending request with an already-arrived response")
    print(f"request_path={request_path} exists={request_path.exists()}")
    print(f"response_path={response_path} exists={response_path.exists()}")

    run_called = {"value": False}

    def fail_if_called(user_prompt: str, **kwargs) -> AgentRunResult:
        run_called["value"] = True
        raise AssertionError("agent.run should not be called when response already exists")

    agent.run = fail_if_called  # type: ignore[method-assign]

    print_scenario_step(2, "Let worker claim the duplicate request")
    processed = _process_gateway_requests(agent, gpaths, worker_id="scenario-delayed-response-worker")
    done_path = gpaths.done / request_path.name
    done_payload = read_json_file(done_path)
    response = read_json_file(response_path)
    pending_left = sorted(path.name for path in gpaths.inbox.glob("*.json"))
    processing_left = sorted(path.name for path in gpaths.processing.glob("*.json"))
    failed_left = sorted(path.name for path in gpaths.failed.glob("*.json"))
    print(f"processed={processed} run_called={run_called['value']}")
    print(f"done_path={done_path} exists={done_path.exists()}")
    print(
        "queue_left="
        + json.dumps(
            {"pending": pending_left, "processing": processing_left, "failed": failed_left},
            ensure_ascii=False,
            sort_keys=True,
        )
    )

    final_ok = (
        processed == 1
        and run_called["value"] is False
        and done_path.exists()
        and response.get("response") == "late response already arrived"
        and response.get("backend") == "scenario-delayed-response"
        and done_payload.get("id") == request_id
        and not pending_left
        and not processing_left
        and not failed_left
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway delayed response handling passed" if final_ok else "gateway delayed response handling failed",
        extra={
            "case": "gateway-delayed-response",
            "request_id": request_id,
            "processed": processed,
            "run_called": run_called["value"],
            "done_path": str(done_path),
            "response_path": str(response_path),
            "response": response,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_processing_stop_case(args) -> int:
    """LLM: verify gateway handles stop/restart correctly when a worker is mid-request (has claimed and is calling the model).

    新手说明:
    模拟 gateway 正在处理请求时（worker 已领任务、正在调模型）收到主动 stop/restart。
    验证：不卡死、不丢请求、不留半截 JSON、重启后能正确恢复。
    """

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

    print_scenario_step(1, "Submit request to gateway inbox")
    print(f"pending_before={pending_path.exists()} request_id={request_id}")

    print_scenario_step(2, "Simulate: worker claims request and starts agent.run (lease active)")
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

    print_scenario_step(3, "Simulate gateway stop/restart mid-processing (requeue stale leases)")
    requeued = requeue_gateway_processing_requests(gpaths)
    after_requeue_pending = gpaths.inbox / processing_path.name
    after_requeue_processing = gpaths.processing / processing_path.name
    print(f"requeued={requeued}")
    print(f"after_requeue_pending={after_requeue_pending.exists()}")
    print(f"after_requeue_processing={after_requeue_processing.exists()}")

    print_scenario_step(4, "Let a new worker pick up the requeued request and complete it")
    processed = _process_gateway_requests(agent, gpaths, worker_id="scenario-recovery-worker-after-stop")
    done_path = gpaths.done / processing_path.name
    done_payload = read_json_file(done_path)
    final_response = read_json_file(response_path)
    print(f"processed={processed}")
    print(f"done_path={done_path} exists={done_path.exists()}")
    print(f"response_path={response_path} exists={response_path.exists()} ok={final_response.get('ok')}")

    print_scenario_step(5, "Verify no half-written JSON, no lost requests")
    response_json_valid = False
    try:
        json.loads(final_response.get("response", "{}") or "{}")
        response_json_valid = True
    except json.JSONDecodeError:
        response_json_valid = False

    final_ok = (
        requeued == 1
        and not after_requeue_processing.exists()
        and after_requeue_pending.exists()
        and processed == 1
        and done_path.exists()
        and final_response.get("ok") is True
        and final_response.get("status") == "done"
        and done_payload.get("lease_owner") == "scenario-recovery-worker-after-stop"
        and done_payload.get("attempts") == 2
        and response_json_valid
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway processing stop passed" if final_ok else "gateway processing stop failed",
        extra={
            "case": "gateway-processing-stop",
            "request_id": request_id,
            "requeued": requeued,
            "processed": processed,
            "done_path": str(done_path),
            "response_path": str(response_path),
            "response": final_response,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2
