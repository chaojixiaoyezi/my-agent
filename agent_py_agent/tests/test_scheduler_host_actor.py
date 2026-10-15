"""宿主身份合同：真实原账、工具与子代理持久化；无业务网络、模型或生产数据。"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.execution_threads import (
    EXECUTION_ORIGIN_KEY,
    IM_TASK_EXECUTION_KEY,
)
from agent_py_agent.agent.conversation.host_actor import (
    HostActorError,
    actor_from_scope,
    background_host_actor,
    current_actor_for_child,
    resolve_host_actor,
    task_host_actor,
)
from agent_py_agent.agent.conversation.im_tool_approval import approval_recipient
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_im_execution_threads import fixture_agent
from agent_py_agent.tests.test_schedule_control import _agent


# LLM: 只使用真实 owner/ThreadStore/Repository，角色与 full_access 不伪造通道身份。
# 函数用途: 建立同一群的准确宿主执行参数及监控工具。
def group_case(tmp_path, *, actor_id="ou_a", background=True):
    agent, source = fixture_agent(tmp_path, identity=OwnerIdentity("feishu", "group", "oc_source"))
    actor = actor_from_scope(agent, SimpleNamespace(channel="feishu", channel_user_id=actor_id,
        channel_conversation_id="oc_source"), source.thread_id, "gwreq-source")
    agent._current_run_params = RunParams(request_id="gwreq-source", task_attributes={
        "conversation_thread_id": source.thread_id}, host_channel_actor=actor)
    agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(
        im_scheduled_tasks_background=background))
    from agent_py_agent.tests.test_group_authorization import (
        grant_synthetic_group_monitor_capability,
    )

    grant_synthetic_group_monitor_capability(agent, actor, members={actor_id, "ou_a", "ou_b"})
    return agent, source, actor, ScheduleTool(agent)


def create(tool, *, key="one"):
    result = tool.execute({"action": "create", "name": "合成监控", "prompt": "合成内部任务",
        "schedule_kind": "every", "every_seconds": 600, "__tool_call_id": key})
    assert result.ok, (result.error_code, result.output)
    return json.loads(result.output)["job"]


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("access", ["normal", "full_access"])
def test_group_creator_can_use_all_schedule_operations_after_create(tmp_path, background, access):
    agent, _source, _actor, tool = group_case(tmp_path, background=background)
    agent.config.command_access_mode = access
    job = create(tool)
    assert agent.scheduler_repository.get_job(job["job_id"])["creator"] == {"channel": "feishu", "actor_id": "ou_a"}
    assert json.loads(tool.execute({"action": "list"}).output)["total"] == 1
    assert json.loads(tool.execute({"action": "status"}).output)["scheduler"]["active_jobs"] == 1
    for action in ("get", "history", "update", "pause", "resume", "run_now", "delete"):
        current = agent.scheduler_repository.get_job(job["job_id"])
        args = {"action": action, "job_id": job["job_id"], "expected_version": current["version"]}
        if action == "update":
            args["prompt"] = "更新合成任务"
        result = tool.execute(args)
        assert result.ok, (action, result.error_code, result.output)


@pytest.mark.parametrize("action", ["get", "history", "update", "pause", "resume", "run_now", "delete"])
def test_other_group_member_is_distinct_from_absent_record(tmp_path, action):
    agent, source, actor, tool = group_case(tmp_path)
    job = create(tool)
    agent._current_run_params.host_channel_actor = replace(actor, actor_id="ou_b")
    assert json.loads(tool.execute({"action": "list"}).output)["total"] == 1
    before = agent.scheduler_repository.store_path.read_bytes()
    result = tool.execute({"action": action, "job_id": job["job_id"], "expected_version": job["version"], "prompt": "不会写入"})
    if action in {'get', 'history'}:
        assert result.ok
    else:
        assert result.error_code == ('SCHEDULER_MEMBERSHIP_UNAVAILABLE' if action in {'pause', 'delete'} else 'CHANNEL_ACTOR_SCOPE_MISMATCH')
    assert agent.scheduler_repository.store_path.read_bytes() == before
    assert tool.execute({"action": "get", "job_id": "job_absent"}).error_code == "SCHEDULER_NOT_FOUND"
    assert resolve_host_actor(agent, source.thread_id).actor_id == "ou_b"


@pytest.mark.parametrize("action", ["create", "list", "status"])
def test_old_model_attributes_never_upgrade_to_host_identity(tmp_path, action):
    agent, _source, _actor, tool = group_case(tmp_path)
    agent._current_run_params.host_channel_actor = None
    agent._current_run_params.task_attributes.update(channel="feishu", channel_user_id="ou_a", user_id="ou_a",
        host_channel_actor={"channel": "feishu", "conversation_id": "oc_source", "actor_id": "ou_a"})
    before = list(agent.conversation_store.storage.threads_dir.iterdir())
    result = tool.execute({"action": action, "name": "不应创建", "prompt": "合成", "schedule_kind": "every", "every_seconds": 600})
    assert result.error_code == "CHANNEL_ACTOR_IDENTITY_REQUIRED"
    assert not agent.scheduler_repository.store_path.exists()
    assert list(agent.conversation_store.storage.threads_dir.iterdir()) == before


def test_frozen_background_origin_restores_actor_and_rejects_conflicting_entry(tmp_path):
    agent, _source, actor, tool = group_case(tmp_path)
    job = create(tool)
    stored = agent.scheduler_repository.get_job(job["job_id"])
    agent.conversation_store.tasks.bind({"thread_id": stored["thread_id"], "task_id": "srun-synthetic", "goal": "合成后台"})
    agent._current_run_params = RunParams(task_id="srun-synthetic", task_attributes={"conversation_thread_id": stored["thread_id"],
        "host_channel_actor": {"channel": "feishu", "conversation_id": "oc_source", "actor_id": "ou_forged"}})
    assert tool.execute({"action": "get", "job_id": job["job_id"]}).ok
    agent._current_run_params.host_channel_actor = replace(actor, actor_id="ou_b")
    for action in ("list", "get", "update", "delete"):
        assert tool.execute({"action": action, "job_id": job["job_id"]}).error_code == "CHANNEL_ACTOR_IDENTITY_CONFLICT"


def test_bad_origin_cross_owner_and_missing_current_thread_fail_closed(tmp_path):
    agent, source, actor, tool = group_case(tmp_path)
    agent._current_run_params.host_channel_actor = replace(actor, owner_id="another-owner")
    assert tool.execute({"action": "list"}).error_code == "CHANNEL_ACTOR_SCOPE_MISMATCH"
    agent._current_run_params.host_channel_actor = actor
    agent._current_run_params.task_attributes = {}
    with pytest.raises(HostActorError, match="可信发起人"):
        current_actor_for_child(agent)
    agent.conversation_store.threads.write(replace(source, metadata={EXECUTION_ORIGIN_KEY: {"binding": "invalid"}}))
    with pytest.raises(HostActorError) as caught:
        resolve_host_actor(agent, source.thread_id)
    assert caught.value.error_code == "CHANNEL_ACTOR_IDENTITY_INVALID"


@pytest.mark.parametrize("identity", [OwnerIdentity("feishu", "user", "ou_a"), OwnerIdentity.local_main()])
@pytest.mark.parametrize("access", ["normal", "full_access"])
def test_private_admin_and_user_identity_do_not_borrow_another_actor(tmp_path, identity, access):
    agent, source = fixture_agent(tmp_path, identity=identity)
    agent.config.command_access_mode = access
    actor = agent._current_run_params.host_channel_actor
    assert ScheduleTool(agent).execute({"action": "list"}).ok
    agent._current_run_params.host_channel_actor = replace(actor, actor_id="ou_other")
    assert ScheduleTool(agent).execute({"action": "create", "name": "不可创建", "prompt": "合成",
        "schedule_kind": "every", "every_seconds": 600}).error_code == "CHANNEL_ACTOR_SCOPE_MISMATCH"
    assert source.owner_id == actor.owner_id


def test_missing_background_actor_cannot_make_group_approval_or_monitor(tmp_path):
    from agent_py_agent.agent.conversation.host_actor import background_host_actor

    agent, source, actor, tool = group_case(tmp_path)
    agent.conversation_store.tasks.bind({"thread_id": source.thread_id, "task_id": "legacy-root", "goal": "旧根"})
    assert background_host_actor(agent, source.thread_id, "legacy-root") is None
    agent._current_run_params = RunParams(task_id="legacy-root", task_attributes={"conversation_thread_id": source.thread_id})
    assert tool.execute({"action": "list"}).error_code == "CHANNEL_ACTOR_IDENTITY_REQUIRED"
    with pytest.raises(HostActorError) as caught:
        approval_recipient(agent, SimpleNamespace(root_task_id="legacy-root", thread_id=source.thread_id))
    assert caught.value.error_code == "CHANNEL_ACTOR_IDENTITY_REQUIRED"
    assert actor.actor_id == "ou_a"


# LLM: 使用真正 SimpleAgent、创建服务、ThreadTaskLink 和序列化；只给一次合成 root 绑定，不替 runner 做任务。
# 函数用途: 验证主根不是 SubAgentTask 时仍能创建/恢复孩子，且孙级沿原专有字段继承。
def managed_child(tmp_path, *, conversation_id='oc_group'):
    agent = _agent(tmp_path, group="oc_group")
    source = agent.conversation_store.threads.get_or_create({"channel": "feishu", "channel_conversation_id": conversation_id,
        "channel_user_id": "ou_one", "canonical_user_id": "ou_one", "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir)})
    actor = actor_from_scope(agent, SimpleNamespace(channel="feishu", channel_user_id="ou_one",
        channel_conversation_id=conversation_id), source.thread_id, "gwreq-one")
    agent._current_run_params = RunParams(request_id="gwreq-one", task_id="gwreq-one",
        task_attributes={"conversation_thread_id": source.thread_id}, host_channel_actor=actor)
    from agent_py_agent.tests.test_group_authorization import (
        grant_synthetic_group_monitor_capability,
    )

    grant_synthetic_group_monitor_capability(agent, actor, members={"ou_one", "ou_other"})
    params = create_run_params(agent, {"goal": "合成孩子", "host_channel_actor": {"actor_id": "ou_forged"},
        "attributes": {"host_channel_actor": {"actor_id": "ou_forged"}, "channel_user_id": "ou_forged"}},
        "合成孩子", ["schedule", "create_subagents"])
    child = agent.subagents.create_run(params=params)
    return agent, source, actor, child


@pytest.mark.parametrize('conversation_id', ['oc_group', 'oc_group:thread:om_topic'])
def test_model_top_level_and_attributes_cannot_override_persisted_child_actor(tmp_path, conversation_id):
    agent, source, actor, child = managed_child(tmp_path, conversation_id=conversation_id)
    restored = agent.subagents.load(child.id)
    assert restored.host_channel_actor == actor.to_record()
    assert "host_channel_actor" not in restored.attributes and "channel_user_id" not in restored.attributes
    assert task_host_actor(agent, restored) == actor
    assert agent.conversation_store.tasks.load(child.root_id).thread_id == source.thread_id
    assert agent.conversation_store.tasks.load(child.root_id).host_channel_actor == actor.to_record()
    result = agent.subagents.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(parent_run_id=child.id,
        child_specs=[HierarchyChildSpec(goal="合成孙", role="worker", attributes={"host_channel_actor": {"actor_id": "ou_forged"}})],
        apply=True, requested_by=child.id))
    grandchild = agent.subagents.load(result.created_run_ids[0])
    assert grandchild.host_channel_actor == actor.to_record()
    assert "host_channel_actor" not in grandchild.attributes
    assert task_host_actor(agent, grandchild) == actor


@pytest.mark.parametrize('conversation_id', ['oc_group', 'oc_group:thread:om_topic'])
def test_child_actor_controls_own_monitor_and_same_recipient_after_restore(tmp_path, conversation_id):
    agent, source, actor, child = managed_child(tmp_path, conversation_id=conversation_id)
    agent._current_run_params = RunParams(task_id=child.root_id, run_id=child.id,
        task_attributes=child.attributes, host_channel_actor=task_host_actor(agent, child))
    tool = ScheduleTool(agent)
    job = create(tool)
    assert tool.execute({"action": "get", "job_id": job["job_id"]}).ok
    assert approval_recipient(agent, SimpleNamespace(root_task_id=child.root_id, thread_id=source.thread_id)) == ("feishu", actor.actor_id)
    restored = agent.subagents.load(child.id)
    restored.host_channel_actor = None
    restored.attributes["host_channel_actor"] = actor.binding()
    assert task_host_actor(agent, restored) == actor
    agent._current_run_params.host_channel_actor = task_host_actor(agent, restored)
    assert tool.execute({"action": "get", "job_id": job["job_id"]}).ok


def test_same_group_shared_thread_cannot_substitute_other_actor_root(tmp_path):
    agent, source, actor, child = managed_child(tmp_path)
    foreign = replace(actor, actor_id="ou_other", entry_request_id="gwreq-other")
    agent.conversation_store.tasks.bind({"thread_id": source.thread_id, "task_id": "gwreq-other", "goal": "合成他人"},
        host_channel_actor=foreign)
    child.host_channel_actor = foreign.to_record()
    with pytest.raises(HostActorError) as caught:
        task_host_actor(agent, child)
    assert caught.value.error_code == "CHANNEL_ACTOR_IDENTITY_CONFLICT"
    before = agent.conversation_store.storage.task_path(child.root_id).read_bytes()
    with pytest.raises(HostActorError):
        agent.conversation_store.tasks.bind({"thread_id": source.thread_id, "task_id": child.root_id, "goal": "不会改写"},
            host_channel_actor=foreign)
    assert agent.conversation_store.storage.task_path(child.root_id).read_bytes() == before


def test_approval_rejects_current_actor_and_exact_root_scope_conflicts(tmp_path):
    agent, source, actor, child = managed_child(tmp_path)
    foreign = replace(actor, actor_id="ou_other", entry_request_id="gwreq-other")
    agent.conversation_store.tasks.bind({"thread_id": source.thread_id, "task_id": "gwreq-other", "goal": "另一个根"},
        host_channel_actor=foreign)
    with pytest.raises(HostActorError) as caught:
        approval_recipient(agent, SimpleNamespace(root_task_id="gwreq-other", thread_id=source.thread_id))
    assert caught.value.error_code == "CHANNEL_ACTOR_IDENTITY_CONFLICT"
    agent._current_run_params.host_channel_actor = foreign
    with pytest.raises(HostActorError) as caught:
        approval_recipient(agent, SimpleNamespace(root_task_id=child.root_id, thread_id=source.thread_id))
    assert caught.value.error_code == "CHANNEL_ACTOR_IDENTITY_CONFLICT"


def test_monitor_actor_and_existing_main_root_conflicts_share_the_same_refusal(tmp_path):
    agent, source, actor, child = managed_child(tmp_path)
    before = agent.scheduler_repository.store_path.read_bytes() if agent.scheduler_repository.store_path.exists() else None
    agent._current_run_params.host_channel_actor = replace(actor, actor_id="ou_other", entry_request_id="new-user-input")
    assert ScheduleTool(agent).execute({"action": "list"}).error_code == "CHANNEL_ACTOR_IDENTITY_CONFLICT"
    assert (agent.scheduler_repository.store_path.read_bytes() if agent.scheduler_repository.store_path.exists() else None) == before
    agent.conversation_store.tasks.bind({"thread_id": source.thread_id, "task_id": "unknown-root", "goal": "未知旧任务"})
    agent._current_run_params = RunParams(task_id="unknown-root", host_channel_actor=actor,
        task_attributes={"conversation_thread_id": source.thread_id})
    assert ScheduleTool(agent).execute({"action": "list"}).error_code == "CHANNEL_ACTOR_IDENTITY_REQUIRED"
    assert agent.conversation_store.tasks.load(child.root_id).host_channel_actor == actor.to_record()


def test_same_actor_new_entry_continues_exact_root_without_rewriting_actor(tmp_path):
    agent, source, actor, child = managed_child(tmp_path)
    path = agent.conversation_store.storage.task_path(child.root_id)
    before = path.read_bytes()
    agent._current_run_params.host_channel_actor = replace(actor, entry_request_id="gwreq-new-entry")
    assert approval_recipient(agent, SimpleNamespace(root_task_id=child.root_id, thread_id=source.thread_id)) == ("feishu", actor.actor_id)
    params = create_run_params(agent, {"goal": "本人续跑孩子"}, "本人续跑孩子", ["schedule"])
    assert params.host_channel_actor == actor
    assert agent.subagents.create_run(params=params).host_channel_actor == actor.to_record()
    assert path.read_bytes() == before


def test_legacy_child_cannot_borrow_foreign_frozen_work_thread(tmp_path):
    agent, source, actor, child = managed_child(tmp_path)
    agent._current_run_params = RunParams(request_id="foreign-new", host_channel_actor=replace(actor, actor_id="ou_other"),
        task_attributes={"conversation_thread_id": source.thread_id})
    tool = ScheduleTool(agent)
    foreign = agent.scheduler_repository.get_job(create(tool, key="other")["job_id"])
    child.host_channel_actor = None
    child.attributes["conversation_thread_id"] = foreign["thread_id"]
    child.attributes["host_channel_actor"] = {"channel": "feishu", "actor_id": "ou_other", "conversation_id": "oc_group"}
    with pytest.raises(HostActorError) as caught:
        task_host_actor(agent, child)
    assert caught.value.error_code == "CHANNEL_ACTOR_IDENTITY_CONFLICT"
    # 旧主群根没有冻结证据时不能补认当前发言人。
    agent.conversation_store.tasks.bind({"thread_id": source.thread_id, "task_id": "legacy-root", "goal": "旧合成根"})
    child.root_id = child.parent_id = "legacy-root"
    with pytest.raises(HostActorError) as caught:
        task_host_actor(agent, child)
    assert caught.value.error_code == "CHANNEL_ACTOR_IDENTITY_REQUIRED"


def test_legacy_main_root_cannot_borrow_foreign_work_for_monitors_or_approval(tmp_path):
    agent, source, actor, _child = managed_child(tmp_path)
    agent._current_run_params = RunParams(request_id="foreign-new", host_channel_actor=replace(actor, actor_id="ou_other"),
        task_attributes={"conversation_thread_id": source.thread_id})
    tool = ScheduleTool(agent)
    other = agent.scheduler_repository.get_job(create(tool, key="foreign-main")["job_id"])
    agent.conversation_store.tasks.bind({"thread_id": source.thread_id, "task_id": "legacy-main", "goal": "未知旧根"})
    agent._current_run_params = RunParams(task_id="legacy-main", task_attributes={"conversation_thread_id": other["thread_id"],
        "host_channel_actor": {"channel": "feishu", "actor_id": "ou_other", "conversation_id": "oc_group"}})
    before = agent.scheduler_repository.store_path.read_bytes()
    for action in ("list", "get", "pause", "update", "delete"):
        result = tool.execute({"action": action, "job_id": other["job_id"], "expected_version": other["version"], "prompt": "不会执行"})
        assert result.error_code == "CHANNEL_ACTOR_SCOPE_MISMATCH"
    with pytest.raises(HostActorError) as caught:
        approval_recipient(agent, SimpleNamespace(root_task_id="legacy-main", thread_id=source.thread_id))
    assert caught.value.error_code == "CHANNEL_ACTOR_SCOPE_MISMATCH"
    assert agent.scheduler_repository.store_path.read_bytes() == before


@pytest.mark.parametrize('conversation_id', ['oc_group', 'oc_group:thread:future_topic'])
def test_child_owner_root_or_parent_actor_conflict_is_rejected(tmp_path, conversation_id):
    agent, _source, actor, child = managed_child(tmp_path, conversation_id=conversation_id)
    child.owner = "other-owner"
    with pytest.raises(HostActorError) as caught:
        task_host_actor(agent, child)
    assert caught.value.error_code == "CHANNEL_ACTOR_SCOPE_MISMATCH"
    child.owner = actor.owner_id
    child.root_id = "missing-root"
    with pytest.raises(HostActorError) as caught:
        task_host_actor(agent, child)
    assert caught.value.error_code == "CHANNEL_ACTOR_IDENTITY_INVALID"
    with pytest.raises(HostActorError):
        agent.subagents.create_run(params=CreateRunParams(goal="不应创建", thought="", plan=[], owner=actor.owner_id,
            parent_id=child.id, root_id=child.id, host_channel_actor=replace(actor, actor_id="ou_forged")))


def test_first_delegated_session_task_restores_actor_before_main_root_promotion(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
    from agent_py_agent.tests.test_im_delegation_runtime import runtime_fixture

    agent, params, task, context, source = runtime_fixture(tmp_path, monkeypatch)
    assert agent.conversation_store.tasks.load(task.task_id) is None
    actor = background_host_actor(agent, task.target_thread_id, task.task_id)
    assert actor == agent._current_run_params.host_channel_actor
    assert actor.source_thread_id == source.thread_id and actor.entry_request_id == context.request_id
    prepare_delegated_execution(agent, params)
    assert agent.conversation_store.tasks.load(task.task_id).host_channel_actor == actor.to_record()
    children = agent.conversation_store.threads.require(task.target_thread_id).metadata[IM_TASK_EXECUTION_KEY]["child_run_ids"]
    assert task_host_actor(agent, agent.subagents.load(children[0])) == actor


@pytest.mark.parametrize("invalid", ["wrong_target", "wrong_sender", "wrong_policy", "scheduled", "owner", "bad_root"])
@pytest.mark.parametrize('conversation_id', ['oc_a', 'oc_a:thread:future_topic'])
def test_delegated_restore_requires_original_task_target_source_policy_and_owner(tmp_path, monkeypatch, invalid, conversation_id):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.tests.test_im_delegation_runtime import runtime_fixture

    agent, _, task, _, source = runtime_fixture(tmp_path, monkeypatch, conversation_id=conversation_id)
    thread_id = task.target_thread_id
    if invalid == "wrong_target":
        thread_id = source.thread_id
    elif invalid == "wrong_sender":
        write_json_file_atomic(agent.conversation_store.session_tasks._path(task.task_id),
                               replace(task, sender_thread_id="foreign-source").to_dict())
    elif invalid == "bad_root":
        write_json_file_atomic(agent.conversation_store.storage.task_path(task.task_id), {"task_id": "wrong-id"})
    else:
        # 只破坏合成原记录的机器字段，不改变普通正文；读取必须拒绝，而不能补认当前发言人。
        def corrupt(current):
            metadata = dict(current.metadata)
            if invalid == "wrong_policy":
                metadata[IM_TASK_EXECUTION_KEY] = {**metadata[IM_TASK_EXECUTION_KEY], "session_task_id": "other-task"}
            elif invalid == "scheduled":
                metadata[EXECUTION_ORIGIN_KEY] = {**metadata[EXECUTION_ORIGIN_KEY], "kind": "scheduled"}
            return replace(current, owner_id="foreign-owner" if invalid == "owner" else current.owner_id, metadata=metadata)

        agent.conversation_store.threads.update_atomic(task.target_thread_id, corrupt)
    with pytest.raises(HostActorError):
        background_host_actor(agent, thread_id, task.task_id)


def test_generic_session_task_has_no_actor_and_cannot_borrow_delegated_work(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    from agent_py_agent.tests.test_im_delegation_runtime import runtime_fixture

    agent, _, task, _, source = runtime_fixture(tmp_path, monkeypatch)
    generic = agent.conversation_store.session_tasks.create(SessionTaskDraft(
        sender_thread_id=source.thread_id, target_thread_id=source.thread_id, goal="普通会话派活"))
    assert background_host_actor(agent, source.thread_id, generic.task_id) is None
    foreign = agent.conversation_store.session_tasks.create(SessionTaskDraft(
        sender_thread_id=source.thread_id, target_thread_id=task.target_thread_id, goal="试图借用别的委派身份"))
    with pytest.raises(HostActorError) as caught:
        background_host_actor(agent, task.target_thread_id, foreign.task_id)
    assert caught.value.error_code == "CHANNEL_ACTOR_SCOPE_MISMATCH"


def test_typed_root_bind_rejects_malformed_origin_before_creating_record(tmp_path):
    agent, source, actor, _ = group_case(tmp_path)
    agent.conversation_store.threads.update_atomic(source.thread_id, lambda current: replace(current,
        metadata={**current.metadata, EXECUTION_ORIGIN_KEY: "malformed"}))
    with pytest.raises(HostActorError) as caught:
        agent.conversation_store.tasks.bind({"thread_id": source.thread_id, "task_id": "never-written", "goal": "合成"},
                                           host_channel_actor=actor)
    assert caught.value.error_code == "CHANNEL_ACTOR_IDENTITY_INVALID"
    assert not agent.conversation_store.storage.task_path("never-written").exists()


@pytest.mark.parametrize("mode", ["followup", "isolated"])
@pytest.mark.parametrize("background", [False, True])
def test_real_scheduled_claim_and_background_actor_keep_original_group_creator(tmp_path, mode, background):
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
    from agent_py_agent.tests.test_scheduler_im_l_integration import im_case

    case = im_case(tmp_path, execution={"mode": mode}, group=True, background=background)
    # 群后来真实收到 B 的消息：最新绑定变成 B，创建人的 canonical run 快照仍必须是 A。
    case.store.threads.update_atomic(case.thread.thread_id, lambda thread: replace(thread,
        channel_bindings=tuple(replace(row, channel_user_id="ou_b") for row in thread.channel_bindings)))
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    result = case.agent.scheduler_service.claim_wake(signal, now=case.job["next_run_at"])
    assert result.status == "claimed"
    claim = result.claim
    frozen = case.agent.scheduler_repository.get_active_run(claim.run_id)
    assert frozen["creator"] == {"channel": "feishu", "actor_id": "ou_a"}
    assert frozen["execution_context"]["origin_bindings"][0]["channel_user_id"] == "ou_a"
    current = _run_params(signal.thread_id, BackgroundRunRequest(thread_id=signal.thread_id,
        task_id=claim.run_id, reason=signal.reason), agent=case.agent,
        thread=case.store.threads.require(signal.thread_id))
    case.agent._current_run_params = current
    actor = current.host_channel_actor
    assert actor.actor_id == "ou_a" and actor.source_thread_id == case.thread.thread_id
    assert case.store.tasks.load(claim.run_id).host_channel_actor == actor.to_record()
    assert ScheduleTool(case.agent).execute({"action": "get", "job_id": case.job["job_id"]}).ok
    assert approval_recipient(case.agent, SimpleNamespace(root_task_id=claim.run_id,
        thread_id=signal.thread_id)) == ("feishu", "ou_a")
    child_params = create_run_params(case.agent, {"goal": "合成调度孩子"}, "合成调度孩子", ["schedule"])
    child = case.agent.subagents.create_run(params=child_params)
    assert task_host_actor(case.agent, child) == actor
    case.agent._current_run_params.host_channel_actor = replace(actor, actor_id="ou_b")
    with pytest.raises(HostActorError):
        approval_recipient(case.agent, SimpleNamespace(root_task_id=claim.run_id, thread_id=signal.thread_id))


def test_known_legacy_queued_isolated_origin_migration_preserves_thread_fields(tmp_path):
    from agent_py_agent.agent.scheduler.execution import prepare_execution
    from agent_py_agent.tests.test_scheduler_im_l_integration import im_case

    case = im_case(tmp_path, execution={"mode": "isolated"}, group=True)
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    run = case.agent.scheduler_repository.get_active_run(signal.root_task_id)
    current = case.store.threads.require(signal.thread_id)
    frozen = current.metadata[EXECUTION_ORIGIN_KEY]
    old = replace(current, model_profile_id="existing-user-selection", model_selection_revision=current.model_selection_revision + 1,
        model_selection_source="explicit", model_selection_last_explicit_revision=current.model_selection_revision + 1,
        cwd=str(tmp_path / "existing-cwd"),
        metadata={key: value for key, value in current.metadata.items() if key != EXECUTION_ORIGIN_KEY})
    case.store.threads.update_atomic(signal.thread_id, lambda _: old)
    prepare_execution(case.agent.scheduler_service, run, now=case.job["next_run_at"])
    restored = case.store.threads.require(signal.thread_id)
    assert restored.metadata[EXECUTION_ORIGIN_KEY] == frozen
    assert restored.model_profile_id == old.model_profile_id and restored.cwd == old.cwd
    assert {key: value for key, value in restored.metadata.items() if key != EXECUTION_ORIGIN_KEY} == old.metadata
    result = case.agent.scheduler_service.claim_wake(signal, now=case.job["next_run_at"])
    assert result.status == "claimed"
    assert background_host_actor(case.agent, signal.thread_id, run["run_id"]).actor_id == "ou_a"


@pytest.mark.parametrize("invalid", ["claimed", "started", "wrong_run", "wrong_owner"])
def test_legacy_isolated_migration_does_not_upgrade_claimed_or_mismatched_thread(tmp_path, invalid):
    from agent_py_agent.agent.scheduler.execution import prepare_execution
    from agent_py_agent.tests.test_scheduler_im_l_integration import im_case

    case = im_case(tmp_path, execution={"mode": "isolated"}, group=True)
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    run = case.agent.scheduler_repository.get_active_run(signal.root_task_id)
    if invalid in {"claimed", "started"}:
        claimed = case.agent.scheduler_service.claim_wake(signal, now=case.job["next_run_at"])
        assert claimed.status == "claimed"
        if invalid == "started":
            case.agent.scheduler_repository.mark_run_running(claimed.claim.run_id, claimed.claim.claim_id,
                now=case.job["next_run_at"])
    current = case.store.threads.require(signal.thread_id)
    metadata = {key: value for key, value in current.metadata.items() if key != EXECUTION_ORIGIN_KEY}
    if invalid == "wrong_run":
        metadata["scheduler_run_id"] = "foreign-run"
    old = replace(current, metadata=metadata, owner_id="foreign-owner" if invalid == "wrong_owner" else current.owner_id)
    case.store.threads.update_atomic(signal.thread_id, lambda _: old)
    path = case.store.storage.thread_path(signal.thread_id)
    before = path.read_bytes()
    with pytest.raises(HostActorError):
        prepare_execution(case.agent.scheduler_service, run, now=case.job["next_run_at"])
    assert path.read_bytes() == before


def test_queued_origin_write_and_real_concurrent_claim_use_one_repository_transition(tmp_path, monkeypatch):
    from threading import Event, Thread

    from agent_py_agent.agent.scheduler.execution import prepare_execution
    from agent_py_agent.tests.test_scheduler_im_l_integration import im_case

    case = im_case(tmp_path, execution={"mode": "isolated"}, group=True)
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    run = case.agent.scheduler_repository.get_active_run(signal.root_task_id)
    current = case.store.threads.require(signal.thread_id)
    case.store.threads.update_atomic(signal.thread_id, lambda _: replace(current,
        metadata={key: value for key, value in current.metadata.items() if key != EXECUTION_ORIGIN_KEY}))
    original = case.store.threads.update_atomic
    attempted, finished = Event(), Event()
    workers, outcomes, ordering = [], [], []

    def claim():
        attempted.set()
        outcomes.append(case.agent.scheduler_repository.claim_run(run["run_id"], lease_seconds=60,
            now=case.job["next_run_at"]))
        ordering.append("claimed")
        finished.set()

    def write_while_claim_waits(thread_id, updater):
        worker = Thread(target=claim)
        workers.append(worker)
        worker.start()
        assert attempted.wait(1)
        assert not finished.wait(0.05)
        assert case.agent.scheduler_repository.get_active_run(run["run_id"])["status"] == "queued"
        result = original(thread_id, updater)
        assert case.agent.scheduler_repository.get_active_run(run["run_id"])["status"] == "queued"
        ordering.append("migrated")
        return result

    monkeypatch.setattr(case.store.threads, "update_atomic", write_while_claim_waits)
    prepare_execution(case.agent.scheduler_service, run, now=case.job["next_run_at"])
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert finished.is_set() and outcomes[0]["status"] == "claimed"
    assert ordering == ["migrated", "claimed"]
    assert case.store.threads.require(signal.thread_id).metadata[EXECUTION_ORIGIN_KEY]["binding"]["actor_id"] == "ou_a"
    assert not case.agent.scheduler_repository.get_active_run(run["run_id"])["started_at"]


def test_local_new_request_can_create_monitor_before_task_root_promotion(tmp_path):
    from agent_py_agent.tests.test_scheduler_tool import _agent as local_agent

    agent = local_agent(tmp_path)
    assert agent.conversation_store.tasks.load("task-1") is None
    assert resolve_host_actor(agent, "thread-1") is None
    result = ScheduleTool(agent).execute({"action": "create", "name": "本机监控", "prompt": "合成任务",
        "schedule_kind": "every", "every_seconds": 600})
    assert result.ok, (result.error_code, result.output)
    job = agent.scheduler_repository.get_job(json.loads(result.output)["job"]["job_id"])
    assert job["creator"] == {"local": True} and job["thread_id"] == "thread-1"
