"""问题8:陈旧账本清理——disabled policy / finished claim 超保留期归档。

真机 2026-08-09:一个线程堆出 182 个 disabled policy 文件(加锁文件 364 个),
disabled 只置 enabled=False 不清理,目录无限累积。归档=移动非删除,可回滚;
归档目录与扫描目录同级,glob 不递归,归档项从 list/due 扫描自然消失。
"""
from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent_with_thread(tmp_path: Path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
        ),
        tmp_path,
    )
    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayConversationLoadRequest,
        gateway_conversation_context,
    )

    conversation = gateway_conversation_context(
        GatewayConversationLoadRequest(
            agent,
            {
                "conversation": {
                    "channel": "chat",
                    "channel_conversation_id": "gc-thread",
                    "channel_user_id": "local-agent",
                }
            },
            "gw-gc",
            "账本清理测试",
        )
    )
    return agent, conversation.thread_id


def _policy_files(store, thread_id: str) -> list[Path]:
    return [
        p
        for p in sorted(store.storage.policies_dir.glob("*.json"))
        if _policy_thread(p) == thread_id
    ]


def _policy_thread(path: Path) -> str:
    import json

    return str(json.loads(path.read_text(encoding="utf-8")).get("thread_id") or "")


def test_disabled_policy_archived_after_retention(tmp_path):
    """disabled 超保留期的 policy 归档(含 .lock),enabled 与未超期不动。"""
    agent, thread_id = _agent_with_thread(tmp_path)
    store = agent.conversation_store
    now = 1_800_000_000.0
    old = store.progress.create(
        {
            "thread_id": thread_id,
            "task_id": "task-a",
            "interval_seconds": 120,
            "metadata": {"kind": "subagent_progress_watch", "watch_run_id": ""},
        }
    )
    fresh = store.progress.create(
        {
            "thread_id": thread_id,
            "task_id": "task-b",
            "interval_seconds": 120,
            "metadata": {"kind": "subagent_progress_watch", "watch_run_id": ""},
        }
    )
    store.progress.disable(old.policy_id, now=now - 8 * 24 * 3600)
    store.progress.disable(fresh.policy_id, now=now - 3600)
    # enabled 的不归档(即使超期)
    kept_enabled = store.progress.create(
        {
            "thread_id": thread_id,
            "task_id": "task-c",
            "interval_seconds": 120,
            "metadata": {"kind": "subagent_progress_watch", "watch_run_id": ""},
        }
    )
    lock = Path(f"{store.storage.policy_path(old.policy_id)}.lock")
    lock.write_text("{}", encoding="utf-8")

    summary = store.gc_stale_ledger_records(now=now)

    assert summary["archived_policies"] >= 1
    assert not store.storage.policy_path(old.policy_id).exists(), "超期 disabled 应归档"
    assert not lock.exists(), "归档应带走 .lock"
    assert store.storage.policy_path(fresh.policy_id).exists(), "未超期 disabled 应保留"
    assert store.storage.policy_path(kept_enabled.policy_id).exists(), "enabled 应保留"
    archive_dir = store.storage.policies_dir.parent / ".ledger_archive" / "policies"
    assert (archive_dir / f"{old.policy_id}.json").exists(), "归档目录应能回滚"
    # 归档后扫描不再读到
    policies, _ = store.progress.list_report()
    ids = {p.policy_id for p in policies}
    assert old.policy_id not in ids
    assert fresh.policy_id in ids and kept_enabled.policy_id in ids


def test_finished_claim_archived_after_retention(tmp_path):
    """finished claim 超保留期归档,running 与未超期 finished 不动。"""
    agent, thread_id = _agent_with_thread(tmp_path)
    store = agent.conversation_store
    now = 1_800_000_000.0
    # 老 finished claim
    from agent_py_agent.agent.conversation.store_claims import (
        BackgroundClaimPayload,
        _new_claim,
    )

    old = _new_claim(
        BackgroundClaimPayload(
            thread_id=thread_id,
            reason="old",
            current=now - 10 * 24 * 3600,
            lease=30,
        )
    )
    claim_path = store.storage.background_claim_path(thread_id)
    import json

    claim_path.write_text(json.dumps(old), encoding="utf-8")
    store.claims.finish(
        {
            "thread_id": thread_id,
            "claim_scope_id": thread_id,
            "claim_id": old["claim_id"],
            "status": "finished",
            "now": now - 10 * 24 * 3600 + 60,
        }
    )
    # 新 finished claim(另一个 scope)
    fresh_claim = _new_claim(
        BackgroundClaimPayload(
            thread_id=thread_id,
            reason="fresh",
            current=now - 3600,
            lease=30,
            claim_scope_id="scope-fresh",
        )
    )
    fresh_path = store.storage.background_claim_path("scope-fresh")
    fresh_path.write_text(json.dumps(fresh_claim), encoding="utf-8")
    store.claims.finish(
        {
            "thread_id": thread_id,
            "claim_scope_id": "scope-fresh",
            "claim_id": fresh_claim["claim_id"],
            "status": "finished",
            "now": now - 3600 + 60,
        }
    )
    # running claim(另一个 scope)
    running_claim = _new_claim(
        BackgroundClaimPayload(
            thread_id=thread_id,
            reason="running",
            current=now - 10 * 24 * 3600,
            lease=30,
            claim_scope_id="scope-running",
        )
    )
    store.storage.background_claim_path("scope-running").write_text(
        json.dumps(running_claim), encoding="utf-8"
    )

    summary = store.gc_stale_ledger_records(now=now)

    assert summary["archived_claims"] >= 1
    assert not claim_path.exists(), "超期 finished claim 应归档"
    assert fresh_path.exists(), "未超期 finished claim 应保留"
    assert store.storage.background_claim_path("scope-running").exists(), "running 应保留"
    archive_dir = store.storage.background_claims_dir.parent / ".ledger_archive" / "claims"
    assert (archive_dir / claim_path.name).exists(), "归档目录应能回滚"


def test_ledger_gc_wired_into_scheduler_tick(tmp_path):
    """调度器 tick 低频触发 gc(实例状态控频)。"""
    agent, thread_id = _agent_with_thread(tmp_path)
    store = agent.conversation_store
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        FakeDeliveryService,
    )

    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent, store=store, channels=FakeDeliveryService()
            ),
            "store": store,
            "claim_ttl_seconds": 30,
        }
    )
    now = 1_800_000_000.0
    policy = store.progress.create(
        {
            "thread_id": thread_id,
            "task_id": "task-x",
            "interval_seconds": 120,
            "metadata": {"kind": "subagent_progress_watch", "watch_run_id": ""},
        }
    )
    store.progress.disable(policy.policy_id, now=now - 8 * 24 * 3600)
    # 预置 gc 基线:首 tick 不触发(now - last < 6h),验证控频而非首次立即跑
    scheduler._ledger_last_gc_at = now

    scheduler.tick(now=now)
    assert store.storage.policy_path(policy.policy_id).exists(), "距上次 gc 未满 6h,不应归档"
    scheduler.tick(now=now + 7 * 3600)
    assert not store.storage.policy_path(policy.policy_id).exists(), "超过 6h 间隔应归档"
