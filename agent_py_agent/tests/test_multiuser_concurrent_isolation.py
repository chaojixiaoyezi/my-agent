"""多用户并发隔离(飞书真实负载场景):N 用户并发经 owner 池各自读写,数据严格隔离、池自洽、不崩。

飞书接入=多个用户并发打到同一网关。test_owner_pool_concurrency 验了并发 get 缓存一致;这里更进一层:
每用户**并发真写记忆 + 读回**,断言 ① 各读到自己的、读不到别人的(并发下隔离不破)② 池恰好 N 个 agent
③ 全程不崩不串户。是"可以接入飞书"的核心地基验证。
"""

from __future__ import annotations

import threading

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
from agent_py_agent.agent.settings.config import AgentConfig


def _base(tmp_path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), gateway_per_user_owner_scoping=True),
        tmp_path,
    )


def _req(user_id: str) -> dict:
    return {"user_id": user_id, "metadata": {"user_id": user_id, "channel": "feishu"}}


def test_many_users_concurrent_write_read_strict_isolation(tmp_path) -> None:
    base = _base(tmp_path)
    n_users = 12
    errors: list[Exception] = []
    leaked: list[str] = []
    lock = threading.Lock()

    def work(uid: str) -> None:
        try:
            agent = _resolve_request_agent(base, _req(uid))
            secret = f"机密-{uid}-合同金额"
            agent.memory.add("user", secret)
            # 读回自己的
            own = agent.memory.search(secret, top_k=5)
            if not any(uid in r.content for r in own):
                with lock:
                    leaked.append(f"{uid} 读不到自己的记忆")
            # 读别人的 secret → 必须读不到(隔离)
            other = "机密-user-7-合同金额" if uid != "user-7" else "机密-user-0-合同金额"
            cross = agent.memory.search(other, top_k=5)
            if any(("user-7" in r.content and uid != "user-7") or ("user-0" in r.content and uid != "user-0")
                   for r in cross):
                with lock:
                    leaked.append(f"{uid} 串到了别人的记忆")
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=work, args=(f"user-{i}",)) for i in range(n_users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发多用户读写不应出错:{errors[:3]}"
    assert not leaked, f"⭐ 并发下隔离被破坏:{leaked[:5]}"
    assert base._owner_pool.active_count() == n_users  # 恰好 N 个作用域 agent,无重复无泄漏


def test_concurrent_same_user_shares_one_agent(tmp_path) -> None:
    """同一用户并发多请求 → 共享同一个作用域 agent(池缓存一致),不会每请求建一个。"""
    base = _base(tmp_path)
    seen: set[int] = set()
    lock = threading.Lock()

    def work() -> None:
        agent = _resolve_request_agent(base, _req("alice"))
        with lock:
            seen.add(id(agent))

    threads = [threading.Thread(target=work) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 1  # 同用户 30 并发请求 → 同一个 agent 实例(双检去重)
    assert base._owner_pool.active_count() == 1
