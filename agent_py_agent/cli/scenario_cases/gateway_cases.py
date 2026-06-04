
from __future__ import annotations

"""implements gateway scenario tests for restart recovery, stale lease, and delayed response handling.

给人看的解释：
这个文件包含 gateway 重启恢复、过期租约恢复、延迟响应处理三个极端场景。
每个函数模拟一种 gateway 异常情况，验证系统能正确恢复。
"""

import json
import os
import time
from dataclasses import dataclass

from ...agent.backends import ModelResponse
from ...agent.gateway_parts import (
    _process_gateway_requests,
    gateway_paths,
    gateway_response_path,
    gateway_stale_processing,
    new_gateway_request_id,
    read_json_file,
    recover_gateway_processing_requests,
    requeue_gateway_processing_requests,
    write_json_file,
)
from ..scenario_utils import (
    create_scenario_workspace,
    install_scenario_backend,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)
from .gateway_delayed_response_case import run_scenario_gateway_delayed_response_case


class ScenarioGatewayRecoveryBackend:

    name = "scenario_gateway_recovery_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        return ModelResponse(
            text='{"scenario": "gateway-stale-lease", "ok": true}',
            backend=self.name,
        )


def _restart_case_write_processing_payload(gpaths, request_id):
    import os
    import time

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
    return processing_path, payload


def _restart_case_verify(paths, requeued, processing_path, pending_path):
    final_ok = requeued == 1 and not processing_path.exists() and pending_path.exists()
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway restart requeue passed" if final_ok else "gateway restart requeue failed",
        extra={
            "case": "gateway-restart",
            "pending_path": str(pending_path),
            "processing_path": str(processing_path),
            "requeued": requeued,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_restart_case(args) -> int:

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
    processing_path, _ = _restart_case_write_processing_payload(gpaths, request_id)

    print_scenario_step(1, "模拟旧 gateway 崩溃遗留 processing 请求")
    print(f"processing_before={processing_path.exists()} path={processing_path}")
    requeued = requeue_gateway_processing_requests(gpaths)
    pending_path = gpaths.inbox / processing_path.name
    print_scenario_step(2, "执行 gateway 启动恢复步骤")
    print(f"requeued={requeued}")
    print(f"processing_after={processing_path.exists()}")
    print(f"pending_after={pending_path.exists()} path={pending_path}")

    return _restart_case_verify(paths, requeued, processing_path, pending_path)


def _stale_lease_setup(args):
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-stale-lease")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    install_scenario_backend(agent, ScenarioGatewayRecoveryBackend())
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
    return paths, agent, gpaths, request_id, processing_path


@dataclass
class _StaleLeaseVerifyContext:
    paths: Any
    request_id: str
    processing_path: Any
    pending_path: Any
    stale_before: list
    recovered: dict
    processed: int
    done_path: Any
    response_path: Any
    response: dict
    done_payload: dict


def _stale_lease_verify(ctx: _StaleLeaseVerifyContext) -> int:
    final_ok = (
        ctx.stale_before
        and ctx.stale_before[0].get("request_id") == ctx.request_id
        and ctx.recovered["checked"] == 1
        and ctx.recovered["requeued"] == 1
        and ctx.pending_path.exists() is False
        and ctx.processing_path.exists() is False
        and ctx.processed == 1
        and ctx.done_path.exists()
        and ctx.response.get("ok") is True
        and ctx.response.get("status") == "done"
        and ctx.response.get("attempts") == 2
        and ctx.done_payload.get("lease_owner") == "scenario-recovery-worker"
    )
    write_scenario_summary(
        ctx.paths,
        ok=final_ok,
        reason="gateway stale lease recovery passed" if final_ok else "gateway stale lease recovery failed",
        extra={
            "case": "gateway-stale-lease",
            "request_id": ctx.request_id,
            "stale_before": ctx.stale_before,
            "recovered": ctx.recovered,
            "processed": ctx.processed,
            "done_path": str(ctx.done_path),
            "response_path": str(ctx.response_path),
            "response": ctx.response,
        },
    )
    print(f"\nsummary_json={ctx.paths.summary_json}")
    print(f"summary_md={ctx.paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_stale_lease_case(args) -> int:

    paths, agent, gpaths, request_id, processing_path = _stale_lease_setup(args)

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

    return _stale_lease_verify(
        _StaleLeaseVerifyContext(
            paths=paths,
            request_id=request_id,
            processing_path=processing_path,
            pending_path=pending_path,
            stale_before=stale_before,
            recovered=recovered,
            processed=processed,
            done_path=done_path,
            response_path=response_path,
            response=response,
            done_payload=done_payload,
        )
    )
