"""IM 独立执行的原线程/调度/投递合同，不调用模型或实际网络。"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.background_routing import (
    BackgroundRouteDependencies,
    resolve_background_route,
)
from agent_py_agent.agent.conversation.execution_threads import ensure_execution_thread
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.scheduler.repository import SchedulerRepository
from agent_py_agent.agent.scheduler.service import SchedulerService
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


# LLM: 使用真实 home/ThreadStore/Repository，模型和网络不参与；调用者只改变可信测试入口通道。
# 函数用途: 准备普通用户的原聊天和隔离调度测试对象，不依赖本机线上配置。
def fixture_agent(tmp_path, channel="feishu", *, identity=None):
    identity = identity or OwnerIdentity("feishu", "user", "ou_a")
    owner = resolve_owner_home(tmp_path, identity)
    home = home_paths_with_owner(home_paths(tmp_path), owner)
    store = ConversationStore(home.owner_home_dir / "conversation")
    source = store.threads.get_or_create({
        "canonical_user_id": "ou_a", "channel": channel, "channel_conversation_id": "oc_source",
        "channel_user_id": "ou_a", "owner_id": home.owner_id, "owner_home": str(home.owner_home_dir),
    })
    repository = SchedulerRepository(home.owner_home_dir / "scheduler", owner_provider=identity.provider, owner_kind=identity.owner_kind, owner_id=home.owner_id)
    return SimpleNamespace(
        conversation_store=store, home_paths=home, root=home.owner_home_dir, scheduler_repository=repository,
        config=SimpleNamespace(timezone="Asia/Shanghai"), current_skill_snapshot=lambda: None,
        _current_run_params=RunParams(request_id="r1", task_attributes={"conversation_thread_id": source.thread_id}),
    ), source


def create_plan(agent, call_id):
    result = ScheduleTool(agent).execute({
        "action": "create", "name": "读取资料", "prompt": "整理资料后发给我", "schedule_kind": "every",
        "every_seconds": 600, "__tool_call_id": call_id,
    })
    assert result.ok, result.output
    return json.loads(result.output)["job"]


def test_im_plans_isolate_each_other_and_do_not_replace_chat_binding(tmp_path):
    agent, source = fixture_agent(tmp_path)
    first, second = create_plan(agent, "a"), create_plan(agent, "b")
    jobs, _ = agent.scheduler_repository.list_jobs()
    assert {job["source_thread_id"] for job in jobs} == {source.thread_id}
    assert len({job["thread_id"] for job in jobs}) == 2
    assert all(job["thread_id"] != source.thread_id for job in jobs)
    assert first["execution_mode"] == second["execution_mode"] == "background"
    assert create_plan(agent, "a")["job_id"] == first["job_id"]
    assert agent.conversation_store.threads.resolve(channel="feishu", channel_conversation_id="oc_source", channel_user_id="ou_a").thread_id == source.thread_id
    for job in jobs:
        thread = agent.conversation_store.threads.require(job["thread_id"])
        assert not thread.channel_bindings
        assert thread.cwd == str(agent.home_paths.owner_home_dir)
        dependencies = BackgroundRouteDependencies(agent.conversation_store.threads.load, lambda: (), lambda: ("", ""))
        assert resolve_background_route(dependencies, thread.thread_id) == ("feishu", "ou_a")


def test_tui_plan_preserves_original_thread(tmp_path):
    agent, source = fixture_agent(tmp_path, channel="tui")
    assert create_plan(agent, "a")["execution_mode"] == "current"
    assert agent.scheduler_repository.list_jobs()[0][0]["thread_id"] == source.thread_id


def test_scheduler_claim_uses_execution_thread_and_preserves_origin_snapshot(tmp_path):
    agent, source = fixture_agent(tmp_path)
    visible = create_plan(agent, "a")
    run = agent.scheduler_repository.reserve_manual_run(visible["job_id"])
    service = SchedulerService(agent.scheduler_repository, conversation_store=agent.conversation_store)
    service.enqueue_ready_runs(now=run["dispatch_after"] + 1)
    signal = agent.conversation_store.wakes.pending()[0]
    claim = service.claim_wake(signal, now=run["dispatch_after"] + 2)
    assert claim.status == "claimed"
    assert claim.claim.run["source_thread_id"] == source.thread_id
    assert signal.thread_id != source.thread_id
    assert agent.conversation_store.tasks.load(run["run_id"]).thread_id == signal.thread_id
    assert not agent.conversation_store.tasks.list(source.thread_id)


def test_source_owner_change_is_rejected_before_notification(tmp_path):
    agent, source = fixture_agent(tmp_path)
    thread = ensure_execution_thread(agent, source.thread_id, "op", kind="scheduled")
    agent.conversation_store.threads.update_atomic(source.thread_id, lambda current: replace(current, owner_id="foreign"))
    dependencies = BackgroundRouteDependencies(agent.conversation_store.threads.load, lambda: (), lambda: ("feishu", "ou_fallback"))
    with pytest.raises(DataCorruptionError):
        resolve_background_route(dependencies, thread.thread_id)


def test_group_next_sender_cannot_steal_background_notification_address(tmp_path):
    agent, source = fixture_agent(tmp_path, identity=OwnerIdentity("feishu", "group", "oc_source"))
    thread = ensure_execution_thread(agent, source.thread_id, "group-task", kind="scheduled")
    other = replace(source.channel_bindings[0], channel_user_id="ou_b", last_active_at=source.channel_bindings[0].last_active_at + 1)
    agent.conversation_store.threads.update_atomic(source.thread_id, lambda current: replace(current, channel_bindings=(other,)))
    dependencies = BackgroundRouteDependencies(agent.conversation_store.threads.load, lambda: (), lambda: ("feishu", "ou_b"))
    assert resolve_background_route(dependencies, thread.thread_id) == ("feishu", "oc_source")
    revoked = replace(other, channel_conversation_id="oc_other")
    agent.conversation_store.threads.update_atomic(source.thread_id, lambda current: replace(current, channel_bindings=(revoked,)))
    with pytest.raises(DataCorruptionError):
        resolve_background_route(dependencies, thread.thread_id)
