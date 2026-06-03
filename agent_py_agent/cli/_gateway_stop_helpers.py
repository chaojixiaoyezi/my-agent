
from __future__ import annotations

"""small helpers for gateway stop/kill command behavior."""

import time

from ..agent.gateway import (
    GatewayPaths,
    log_gateway_event,
    terminate_pid,
    wait_for_pid_exit,
    write_json_file,
)


def _force_kill_gateway(agent, paths: GatewayPaths, pid: int) -> bool:
    terminate_pid(pid)
    if not wait_for_pid_exit(pid, 5):
        return False
    write_json_file(paths.state, {"status": "killed", "pid": pid, "stopped_at": time.time()})
    log_gateway_event(
        agent,
        "gateway_killed",
        {"status": "killed", "pid": pid, "updated_at": time.time()},
    )
    try:
        paths.pid.unlink()
    except OSError:
        pass
    print(f"gateway killed pid={pid}")
    return True
