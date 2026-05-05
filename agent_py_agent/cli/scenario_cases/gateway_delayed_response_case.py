from __future__ import annotations

"""LLM: gateway delayed response scenario test.

给人看的解释：
模拟 gateway 响应已经存在时，worker 不应该再次调用模型。
验证系统能检测已有响应、直接归档请求、不重复执行。
从 gateway_cases.py 拆出来，让那个文件不超 400 行。
"""

import json
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
    write_json_file,
)
from ..scenario_utils import (
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)


def _delayed_response_setup(args):
    """Setup for delayed response scenario: create workspace, agent, gateway paths, request and response."""
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
    return paths, agent, gpaths, request_id, request_path, response_path


@dataclass
class _DelayedResponseVerifyContext:
    """Bundle for _delayed_response_verify to reduce parameter count."""
    paths: Any
    request_id: str
    processed: int
    run_called: dict
    done_path: Any
    response: dict
    pending_left: list
    processing_left: list
    failed_left: list
    done_payload: dict = None  # type: ignore[assignment]


def _delayed_response_verify(ctx: _DelayedResponseVerifyContext) -> int:
    """Verify delayed response handling results."""
    final_ok = (
        ctx.processed == 1
        and ctx.run_called["value"] is False
        and ctx.done_path.exists()
        and ctx.response.get("response") == "late response already arrived"
        and ctx.response.get("backend") == "scenario-delayed-response"
        and ctx.done_payload.get("id") == ctx.request_id
        and not ctx.pending_left
        and not ctx.processing_left
        and not ctx.failed_left
    )
    write_scenario_summary(
        ctx.paths,
        ok=final_ok,
        reason="gateway delayed response handling passed" if final_ok else "gateway delayed response handling failed",
        extra={
            "case": "gateway-delayed-response",
            "request_id": ctx.request_id,
            "processed": ctx.processed,
            "run_called": ctx.run_called["value"],
            "done_path": str(ctx.done_path),
            "response_path": str(ctx.response_path) if hasattr(ctx, 'response_path') else "",
            "response": ctx.response,
        },
    )
    print(f"\nsummary_json={ctx.paths.summary_json}")
    print(f"summary_md={ctx.paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_delayed_response_case(args) -> int:
    """LLM: simulate a response arriving before a duplicate pending request is claimed.

    新手说明:
    当 gateway 响应已经存在时，worker 不应该再次调用模型。
    验证系统能检测已有响应、直接归档请求、不重复执行。
    """

    paths, agent, gpaths, request_id, request_path, response_path = _delayed_response_setup(args)

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

    return _delayed_response_verify(
        _DelayedResponseVerifyContext(
            paths=paths,
            request_id=request_id,
            processed=processed,
            run_called=run_called,
            done_path=done_path,
            response=response,
            pending_left=pending_left,
            processing_left=processing_left,
            failed_left=failed_left,
            done_payload=done_payload,
        )
    )
