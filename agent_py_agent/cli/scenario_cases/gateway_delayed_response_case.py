
from __future__ import annotations

"""gateway orphan response projection scenario test.

给人看的解释：
模拟只有 response 投影、没有 canonical terminal 的情况。
验证 worker 不会把孤立投影当成完成事实，而会执行请求并用权威结果修复投影。
从 gateway_cases.py 拆出来，让那个文件不超 400 行。
"""

import json
import time
from dataclasses import dataclass
from typing import Any

from ...agent.agent_core.models import AgentRunResult
from ...agent.gateway_parts import (
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
            "prompt": "gateway orphan response projection scenario: execute canonical request.",
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
    paths: Any
    request_id: str
    processed: int
    run_called: dict
    done_path: Any
    response_path: Any
    response: dict
    pending_left: list
    processing_left: list
    failed_left: list
    done_payload: dict = None  # type: ignore[assignment]


@dataclass(frozen=True)
class _DelayedResponseRunRequest:
    paths: Any
    agent: Any
    gpaths: Any
    request_id: str
    request_path: Any
    response_path: Any
    run_called: dict


def _delayed_response_verify(ctx: _DelayedResponseVerifyContext) -> int:
    final_ok = (
        ctx.processed == 1
        and ctx.run_called["value"] is True
        and ctx.done_path.exists()
        and ctx.response.get("response") == "canonical response replaced orphan projection"
        and ctx.response.get("backend") == "scenario-fresh-response"
        and ctx.done_payload.get("id") == ctx.request_id
        and not ctx.pending_left
        and not ctx.processing_left
        and not ctx.failed_left
    )
    write_scenario_summary(
        ctx.paths,
        ok=final_ok,
        reason="gateway orphan projection handling passed" if final_ok else "gateway orphan projection handling failed",
        extra={
            "case": "gateway-delayed-response",
            "request_id": ctx.request_id,
            "processed": ctx.processed,
            "run_called": ctx.run_called["value"],
            "done_path": str(ctx.done_path),
            "response_path": str(ctx.response_path),
            "response": ctx.response,
        },
    )
    print(f"\nsummary_json={ctx.paths.summary_json}")
    print(f"summary_md={ctx.paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def _collect_delayed_response_result(request: _DelayedResponseRunRequest) -> _DelayedResponseVerifyContext:
    processed = _process_gateway_requests(request.agent, request.gpaths, worker_id="scenario-delayed-response-worker")
    done_path = request.gpaths.done / request.request_path.name
    return _DelayedResponseVerifyContext(
        paths=request.paths,
        request_id=request.request_id,
        processed=processed,
        run_called=request.run_called,
        done_path=done_path,
        response_path=request.response_path,
        response=read_json_file(request.response_path),
        pending_left=sorted(path.name for path in request.gpaths.inbox.glob("*.json")),
        processing_left=sorted(path.name for path in request.gpaths.processing.glob("*.json")),
        failed_left=sorted(path.name for path in request.gpaths.failed.glob("*.json")),
        done_payload=read_json_file(done_path),
    )


def run_scenario_gateway_delayed_response_case(args) -> int:
    paths, agent, gpaths, request_id, request_path, response_path = _delayed_response_setup(args)
    print_scenario_step(1, "Create a pending request with an orphan response projection")
    print(f"request_path={request_path} exists={request_path.exists()}")
    print(f"response_path={response_path} exists={response_path.exists()}")

    run_called = {"value": False}

    def run_canonical_request(user_prompt: str, *, params=None) -> AgentRunResult:
        run_called["value"] = True
        return AgentRunResult(
            prompt=f"canonical prompt: {user_prompt}",
            response="canonical response replaced orphan projection",
            backend="scenario-fresh-response",
            used_memories=0,
        )

    agent.run = run_canonical_request  # type: ignore[method-assign]

    print_scenario_step(2, "Let worker execute and repair the orphan projection")
    verify_ctx = _collect_delayed_response_result(
        _DelayedResponseRunRequest(paths, agent, gpaths, request_id, request_path, response_path, run_called)
    )
    print(f"processed={verify_ctx.processed} run_called={run_called['value']}")
    print(f"done_path={verify_ctx.done_path} exists={verify_ctx.done_path.exists()}")
    print(
        "queue_left="
        + json.dumps(
            {
                "pending": verify_ctx.pending_left,
                "processing": verify_ctx.processing_left,
                "failed": verify_ctx.failed_left,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )

    return _delayed_response_verify(verify_ctx)
