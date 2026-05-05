from __future__ import annotations

"""LLM: implements the gateway cross-day resume scenario that verifies memory-resume recovers gateway JSON facts.

给人看的解释：
这个文件验证 gateway 请求跨天后能被 memory-resume 命令找回。
先真正发一个 gateway 请求，再模拟隔天恢复场景。
"""

import json
import os
from pathlib import Path

from ...agent.gateway import (
    gateway_paths,
    gateway_response_path,
    new_gateway_request_id,
)
from ...agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    append_snapshot,
)
from ..scenario_utils import (
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_step,
    run_scenario_gateway_ask,
    run_scenario_subprocess,
    scenario_command,
    write_scenario_summary,
)


def _cross_day_setup(paths, args):
    """Run a real gateway ask and resolve request/response paths."""
    prompt = "gateway cross-day resume drill: please return a short recoverable gateway response."
    print_scenario_step(1, "Run a real background gateway ask")
    gateway_payload = run_scenario_gateway_ask(paths, prompt, timeout=args.timeout, save=True)
    if not gateway_payload.get("ok"):
        write_scenario_summary(
            paths, ok=False, reason="gateway ask failed",
            extra={"case": "gateway-cross-day-resume", "gateway": gateway_payload},
        )
        return None
    request_id = str(gateway_payload.get("id") or "")
    agent = load_scenario_agent(paths.config)
    gpaths = gateway_paths(agent)
    request_path = gpaths.done / f"{request_id}.json"
    if not request_path.exists():
        request_path = gpaths.failed / f"{request_id}.json"
    response_path = gateway_response_path(gpaths, request_id)
    print(f"request_id={request_id}")
    print(f"request_path={request_path} exists={request_path.exists()}")
    print(f"response_path={response_path} exists={response_path.exists()}")
    return agent, request_id, request_path, response_path, gateway_payload


def _run_cross_day_resume(paths, request_id):
    """Run memory-resume subprocess and return parsed payload."""
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    resume = run_scenario_subprocess(
        scenario_command(
            paths, "memory-resume", "gateway cross-day resume",
            "--request-id", request_id,
            "--since", "2026-04-29", "--until", "2026-04-30", "--json",
        ),
        env=env, timeout=30,
    )
    try:
        return json.loads(resume.stdout), resume.returncode
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"memory-resume JSON parse failed: {exc}", "stdout": resume.stdout}, resume.returncode


def _verify_cross_day_resume(resume_payload, returncode, request_id, request_path, response_path):
    """Verify cross-day resume found the gateway fact sources."""
    gateway_sources = resume_payload.get("gateway_fact_sources", []) if isinstance(resume_payload, dict) else []
    recommended_reads = resume_payload.get("resume", {}).get("recommended_read_paths", []) if isinstance(resume_payload, dict) else []
    context_block = resume_payload.get("brief", {}).get("context_block", "") if isinstance(resume_payload, dict) else ""
    return (
        returncode == 0
        and bool(request_id)
        and request_path.exists()
        and response_path.exists()
        and any(item.get("request_id") == request_id for item in gateway_sources if isinstance(item, dict))
        and str(request_path) in recommended_reads
        and str(response_path) in recommended_reads
        and str(response_path) in context_block
    )


def run_scenario_gateway_cross_day_resume_case(args) -> int:
    """LLM: run a real gateway ask, then verify cross-day memory-resume can recover its JSON facts.

    新手说明:
    先真正发一个 gateway 请求，拿到响应后模拟跨天恢复场景。
    验证 memory-resume 命令能找回这个请求的 JSON 事实源。
    """

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-cross-day-resume")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    setup = _cross_day_setup(paths, args)
    if setup is None:
        return 2
    agent, request_id, request_path, response_path, gateway_payload = setup

    print_scenario_step(2, "Simulate a previous-day clue and next-day recovery snapshot")
    _append_gateway_cross_day_resume_clues(
        agent.root, request_id=request_id, request_path=request_path, response_path=response_path,
    )

    print_scenario_step(3, "Run memory-resume against the real gateway request id")
    resume_payload, returncode = _run_cross_day_resume(paths, request_id)

    final_ok = _verify_cross_day_resume(resume_payload, returncode, request_id, request_path, response_path)
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway cross-day resume passed" if final_ok else "gateway cross-day resume failed",
        extra={
            "case": "gateway-cross-day-resume",
            "request_id": request_id,
            "request_path": str(request_path),
            "response_path": str(response_path),
            "gateway": gateway_payload,
            "resume": resume_payload,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def _append_gateway_cross_day_resume_clues(
    root: Path,
    *,
    request_id: str,
    request_path: Path,
    response_path: Path,
) -> None:
    """LLM: write deterministic cross-day archive clues for one real gateway request.

    新手说明:
    向归档系统写入模拟的跨天线索，让 memory-resume 命令能找到这个 gateway 请求的上下文。
    """

    append_raw_event(
        root,
        RawMemoryEvent(
            event_id=f"raw-scenario-gateway-cross-day-{request_id}",
            session_id="session-scenario-gateway-cross-day",
            request_id=request_id,
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            content_preview="gateway cross-day resume drill from the previous day",
            source="gateway",
            created_at="2026-04-29T23:58:00+00:00",
        ),
    )
    append_snapshot(
        root,
        CompressionSnapshot(
            snapshot_id=f"snapshot-scenario-gateway-cross-day-{request_id}",
            session_id="session-scenario-gateway-cross-day",
            compression_id=f"compression-scenario-gateway-cross-day-{request_id}",
            turn_range={
                "kind": "recovery_snapshot",
                "source": "gateway",
                "request_id": request_id,
            },
            user_intents=["continue the previous gateway request after a day boundary"],
            assistant_actions=["gateway response is available; read request and response JSON before continuing"],
            dispatch_events=[
                {
                    "source": "gateway",
                    "request_id": request_id,
                    "status": "done",
                }
            ],
            content_paths=[str(request_path), str(response_path)],
            next_actions=["Read the gateway request JSON and response JSON before taking action."],
            created_at="2026-04-30T00:05:00+00:00",
        ),
    )
