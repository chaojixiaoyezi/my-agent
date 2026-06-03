from __future__ import annotations

import subprocess
import time

from ....runtime_errors import runtime_error_report


def process_startup_returncode(process: subprocess.Popen) -> int | None:
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return None
    for index in range(6):
        returncode = poll()
        if returncode is not None:
            return int(returncode)
        if index < 5:
            time.sleep(0.05)
    return None


def mark_background_channel_failure(request, *, error: str) -> list[dict[str, object]]:
    manager = getattr(getattr(request, "agent", None), "subagents", None)
    mark_errors: list[dict[str, object]] = []
    for run_id in request.run_ids:
        try:
            task = manager.load(run_id)
            task.status = "CHANNEL_ERROR"
            task.channel_status = "BROKEN"
            task.failure_type = "background_dispatch_startup"
            task.result = error
            task.updated_at = time.time()
            manager.save(task)
        except Exception as exc:
            mark_errors.append(
                {"run_id": str(run_id), **runtime_error_report(exc, context="background_dispatch.channel_failure.save")}
            )
    return mark_errors
