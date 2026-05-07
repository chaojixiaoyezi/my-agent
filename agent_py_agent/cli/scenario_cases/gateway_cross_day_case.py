from __future__ import annotations

"""LLM: implements the gateway cross-day resume scenario that verifies memory-resume recovers gateway JSON facts.

给人看的解释：
这个文件验证 gateway 请求跨天后能被 memory-resume 命令找回。
先真正发一个 gateway 请求，再模拟隔天恢复场景。
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ...agent.gateway import (
    gateway_paths,
    gateway_response_path,
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
    prompt = "gateway cross-day resume drill: reply with CROSS_DAY_RESUME_OK only."
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
    request_path = _gateway_request_fact_path(gpaths, request_id, gateway_payload)
    response_path = gateway_response_path(gpaths, request_id)
    print(f"request_id={request_id}")
    print(f"request_path={request_path} exists={request_path.exists()}")
    print(f"response_path={response_path} exists={response_path.exists()}")
    return agent, request_id, request_path, response_path, gateway_payload


def _gateway_request_fact_path(gpaths, request_id: str, gateway_payload: dict) -> Path:
    candidates = [
        gpaths.done / f"{request_id}.json",
        gpaths.failed / f"{request_id}.json",
        gpaths.processing / f"{request_id}.json",
        gpaths.inbox / f"{request_id}.json",
    ]
    payload_path = str(gateway_payload.get("request_file") or "")
    if payload_path:
        candidates.append(Path(payload_path))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _run_cross_day_resume(paths, request_id):
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


@dataclass(frozen=True)
class CrossDayResumeVerifyRequest:
    resume_payload: dict
    returncode: int
    request_id: str
    request_path: Path
    response_path: Path


def _verify_cross_day_resume(request: CrossDayResumeVerifyRequest):
    resume_payload = request.resume_payload
    gateway_sources = resume_payload.get("gateway_fact_sources", []) if isinstance(resume_payload, dict) else []
    recommended_reads = resume_payload.get("resume", {}).get("recommended_read_paths", []) if isinstance(resume_payload, dict) else []
    context_block = resume_payload.get("brief", {}).get("context_block", "") if isinstance(resume_payload, dict) else ""
    return (
        request.returncode == 0
        and bool(request.request_id)
        and request.request_path.exists()
        and request.response_path.exists()
        and any(item.get("request_id") == request.request_id for item in gateway_sources if isinstance(item, dict))
        and str(request.request_path) in recommended_reads
        and str(request.response_path) in recommended_reads
        and str(request.response_path) in context_block
    )


def run_scenario_gateway_cross_day_resume_case(args) -> int:

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

    final_ok = _verify_cross_day_resume(
        CrossDayResumeVerifyRequest(resume_payload, returncode, request_id, request_path, response_path)
    )
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
    append_raw_event(root, _gateway_cross_day_raw_event(request_id))
    append_snapshot(root, _gateway_cross_day_snapshot(request_id, request_path, response_path))


def _gateway_cross_day_raw_event(request_id: str) -> RawMemoryEvent:
    return RawMemoryEvent(
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
    )


def _gateway_cross_day_snapshot(request_id: str, request_path: Path, response_path: Path) -> CompressionSnapshot:
    return CompressionSnapshot(
        snapshot_id=f"snapshot-scenario-gateway-cross-day-{request_id}",
        session_id="session-scenario-gateway-cross-day",
        compression_id=f"compression-scenario-gateway-cross-day-{request_id}",
        turn_range={"kind": "recovery_snapshot", "source": "gateway", "request_id": request_id},
        user_intents=["continue the previous gateway request after a day boundary"],
        assistant_actions=["gateway response is available; read request and response JSON before continuing"],
        dispatch_events=[{"source": "gateway", "request_id": request_id, "status": "done"}],
        content_paths=[str(request_path), str(response_path)],
        next_actions=["Read the gateway request JSON and response JSON before taking action."],
        created_at="2026-04-30T00:05:00+00:00",
    )
