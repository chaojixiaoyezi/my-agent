# LLM: 本模块只释放进程内缓存；持久任务/子代理/wake/审批等状态仍由原合同负责，不写终态。
# 模块用途: 在没有在途执行且没有持久工作时回收空闲 IM 用户的重量级实例。
from __future__ import annotations

import time
from pathlib import Path


# LLM: 按候选版本复核在途及磁盘硬事实，异常保留实例；登记与池均重检时刻，不能擦除并发新消息。
# 函数用途: 回收一批已空闲的用户缓存，保留会话、身份、记忆、工具进程和任务文件。
def release_idle_owner_agents(supervisor) -> int:
    from ..owner_wake_discovery import _owner_has_hard_facts

    pool = getattr(supervisor, "_owner_pool", None)
    config = getattr(supervisor._base_agent, "config", None)
    seconds = float(getattr(config, "owner_agent_idle_seconds", 60.0))
    if pool is None or seconds <= 0:
        return 0
    released = 0
    for key, agent, touched in pool.idle_candidates(seconds):
        observed_at = time.monotonic()
        if supervisor._active_threads_for_owner(id(agent)):
            continue
        future = supervisor._curator_inflight.get(id(agent))
        if future is not None and not future.done():
            continue
        home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
        if not home:
            continue
        try:
            if _owner_has_hard_facts(Path(home)):
                continue
        except Exception:  # noqa: BLE001 无法确认事实时保留，不得误当空闲
            continue
        if (supervisor._registry.discard_idle(key, observed_at)
                and pool.evict_idle(key, agent, touched)):
            supervisor._owner_schedulers.pop(id(agent), None)
            released += 1
    return released
