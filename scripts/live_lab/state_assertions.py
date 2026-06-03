
from __future__ import annotations

import json
from types import SimpleNamespace


def assert_no_subagent_state_blockers(stdout: str) -> None:
    text = _gateway_response_text(stdout)
    lower = text.lower()
    markers = [
        "subagent state notice",
        "blocking_run_ids",
        "done_verified: 0",
        "尚未完整通过",
        "status=blocked",
    ]
    if any(marker in lower for marker in markers):
        raise RuntimeError("子代理链路仍阻塞，不能把自然语言 E2E 记为通过。")


def assert_persisted_subagent_state_clean(fixture_root) -> None:
    tasks = _persisted_subagent_tasks(fixture_root)
    if not tasks:
        raise RuntimeError("自然语言 E2E 没有创建任何子代理。")
    blockers = _blocking_task_ids(tasks)
    if blockers:
        raise RuntimeError(
            "持久化子代理状态仍未完成: "
            f"done_verified={_done_verified_count(tasks)}/{len(tasks)} blockers={', '.join(blockers)}"
        )


def _persisted_subagent_tasks(fixture_root) -> list[SimpleNamespace]:
    root = fixture_root / ".my_agent" / "subagents"
    tasks: list[SimpleNamespace] = []
    for path in sorted(root.glob("subagent-*/task.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict):
            tasks.append(SimpleNamespace(**payload))
    return tasks


def _blocking_task_ids(tasks: list[SimpleNamespace]) -> list[str]:
    blockers: list[str] = []
    for task in tasks:
        status = str(getattr(task, "status", "") or "").upper()
        verification = str(getattr(task, "verification_status", "") or "").upper()
        if status in {"BLOCKED", "FAILED", "TIMEOUT", "CANCELLED"} or verification in {"FAILED", "REJECTED"}:
            blockers.append(str(getattr(task, "id", "") or "unknown"))
    return blockers


def _done_verified_count(tasks: list[SimpleNamespace]) -> int:
    count = 0
    for task in tasks:
        status = str(getattr(task, "status", "") or "").upper()
        verification = str(getattr(task, "verification_status", "") or "").upper()
        if status in {"DONE", "VERIFIED", "SUCCEEDED"} and verification == "VERIFIED":
            count += 1
    return count


def _gateway_response_text(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return str(stdout or "")
    if not isinstance(payload, dict):
        return str(stdout or "")
    if payload.get("ok") is False:
        return f"gateway_not_ok {payload.get('error') or ''} {payload.get('response') or ''}"
    return str(payload.get("response") or stdout or "")
