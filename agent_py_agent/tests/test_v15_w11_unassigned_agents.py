"""W11：真实null归属及身份/当前工作两个独立观察层。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
from agent_py_agent.agent.conversation.business_contracts import BackgroundAgentIdentity
from agent_py_agent.agent.conversation.session_messaging import queue_owned_background_message
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.tests.test_business_v12_f3_agents import business_host as business_host
from agent_py_agent.tests.test_mega_business_api_actions import native_http as native_http


# LLM: pytest仅构造/恢复身份DTO并通过公开动作schema验证显式null参数；不认证、不创建线程或授予权限，覆盖create/list/assign/submit同一可空合同。
# 函数用途: 检查无归属身份和公开业务参数都能保留真正的None。
def test_null_identity_and_public_schema_roundtrip():
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
    from agent_py_agent.agent.plugin_host.schemas import validate_object
    identity = BackgroundAgentIdentity("agent", None, None, None, "available", "reuse", 1)
    assert BackgroundAgentIdentity.from_dict(identity.to_dict()).space_id is None
    for action, params in (("background_agents.create", {"name": "worker", "space_id": None}),
            ("background_agents.list", {"space_id": None}),
            ("tasks.assign", {"agent_id": "agent", "space_id": None, "instruction": "work"}),
            ("tasks.submit", {"space_id": None, "instruction": "work"})):
        assert validate_object(params, business_action_spec(action).params_schema)["space_id"] is None


# LLM: pytest经工具入口及领域服务写入临时有空间/无归属身份，读取并区分省略、null和具体空间筛选，同时验证跨Owner及不可用空间拒绝；不运行模型。
# 函数用途: 检查无归属助手可创建和查询，且不会与有空间助手混淆。
def test_null_create_get_list_and_explicit_null_filter(business_host):
    host, actor = business_host
    service = BackgroundAgentService(host)
    root = service.create({"space_id": None, "name": "root worker"}, actor=actor, idempotency_key="null")["agent"]
    scoped = service.create({"space_id": "space-A", "name": "scoped"}, actor=actor, idempotency_key="scoped")["agent"]
    assert root["space_id"] is None
    assert service.get(root["agent_id"], actor=actor)["agent"]["space_id"] is None
    assert {row["agent_id"] for row in service.list({}, actor=actor)["items"]} == {root["agent_id"], scoped["agent_id"]}
    assert [row["agent_id"] for row in service.list({"space_id": None}, actor=actor)["items"]] == [root["agent_id"]]
    assert [row["agent_id"] for row in service.list({"space_id": "space-A"}, actor=actor)["items"]] == [scoped["agent_id"]]
    with pytest.raises(PermissionError):
        service.get(root["agent_id"], actor=replace(actor, owner_id="other"))
    with pytest.raises(PermissionError):
        service.create({"space_id": "denied", "name": "denied"}, actor=actor, idempotency_key="denied")


# LLM: pytest在临时无归属身份上实际派活并重放幂等键，核canonical上下文的null空间和准确task_id，再拒绝错空间；真实Store记录任务与唤醒，fixture禁止模型调用。
# 函数用途: 验证无归属派活保持原任务归属，重放不另建任务或借用默认空间。
def test_unassigned_task_binding_never_selects_a_default_space(business_host):
    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": None, "name": "worker"}, actor=actor, idempotency_key="a")["agent"]
    request = {"agent_id": agent["agent_id"], "space_id": None, "instruction": "work"}
    result = service.assign(request, actor=actor, idempotency_key="task")
    task = host.conversation_store.session_tasks.require(result["task_id"])
    assert task.metadata["business_context"]["space_id"] is None
    assert task.metadata["business_context"]["task_id"] == task.task_id
    assert service.assign(request, actor=actor, idempotency_key="task")["task_id"] == task.task_id
    with pytest.raises(ValueError, match="SPACE_MISMATCH"):
        service.assign({**request, "space_id": "space-A"}, actor=actor, idempotency_key="wrong")


# LLM: pytest在临时Store依次观察空闲身份、真实排队任务及注入的claim读错；要求工作态分别idle/waiting/unknown，查询不得推进队列或把available当running。
# 函数用途: 检查助手能接单与是否正在工作是两件独立的事。
def test_work_state_does_not_treat_available_as_running(business_host, monkeypatch):
    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="a")["agent"]
    assert agent["identity_state"] == "available" and agent["work_state"] == "idle"
    task = host.conversation_store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, agent["agent_id"], "queued"))
    observed = service.get(agent["agent_id"], actor=actor)["agent"]
    assert observed["work_state"] == "waiting"
    assert host.conversation_store.session_tasks.require(task.task_id).status == "queued"
    monkeypatch.setattr(service.store.claims, "load_report", lambda identifier: ({}, {"error": "unreadable"}))
    observed = service.list({}, actor=actor)["items"][0]
    assert observed["identity_state"] == "available" and observed["work_state"] == "unknown"


# LLM: pytest使用临时真实空间服务和可撤销授权回调；对照显式unassigned与旧默认选择，并核异Owner及撤权后复核拒绝，不建执行任务或调用模型。
# 函数用途: 确认即使只有一个空间，明确的无归属选择也不会自动挂到该空间。
def test_real_null_context_does_not_inherit_the_only_space(tmp_path):
    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
    from agent_py_agent.agent.conversation.business_context import (
        resolve_business_context,
        revalidate_business_context,
    )
    from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService

    service = BusinessSpaceService(tmp_path / "owner", owner_id="owner", enabled=True)
    allowed = [True]
    actor = BusinessActor("owner", "user", service, lambda action, facts: allowed[0])
    sole = service.create({"name": "only space"}, actor=actor, idempotency_key="space")
    options = dict(actor=actor, task_id="stask-null", requested_space_id=None, project_ref=None, binding_id=None)
    unassigned = resolve_business_context(**options, unassigned=True)
    assert unassigned.space_id is None and unassigned.space_revision is None
    assert resolve_business_context(**options).space_id == sole["space_id"], "旧默认选择合同不改变"
    revalidate_business_context(actor=actor, context=unassigned)
    with pytest.raises(BusinessError, match="SPACE_SCOPE_MISMATCH"):
        revalidate_business_context(actor=actor, context=replace(unassigned, owner_id="other"))
    allowed[0] = False
    with pytest.raises(BusinessError, match="BUSINESS_ACCESS_DENIED"):
        revalidate_business_context(actor=actor, context=unassigned)


# LLM: pytest在临时Store/运行库建立准确任务、claim、run和attempt绑定；原入口激活同一pending attempt，核created父run的合法执行及终态拒绝，只写测试存储。
# LLM: 原任务用accepted表示受理，running仅属于claim与运行库；不扩展原SessionTask状态机。
# 函数用途: 检查运行中展示需要完整且仍有效的任务与执行证据。
def test_work_state_requires_exact_live_claim_task_and_runtime(business_host, tmp_path):
    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
        publish_claim_runtime_authority,
    )
    from agent_py_agent.agent.conversation.session_tasks import (
        SESSION_TASK_ACCEPTED,
        SessionTaskUpdate,
    )
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": None, "name": "worker"}, actor=actor, idempotency_key="a")["agent"]
    store = host.conversation_store
    task = store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, agent["agent_id"], "work"),
        business_context_intent=BusinessContextIntent(actor.owner_id, None, None, None),
        business_context_factory=lambda identifier: BusinessExecutionContext(actor.owner_id, None, None, identifier,
            None, None, (), (), "business-context.v1"))
    context = BusinessExecutionContext.from_dict(task.metadata["business_context"])
    repo = RuntimeRepository(tmp_path / "runtime.db")
    run = repo.record_run_creation(owner_id=actor.owner_id, conversation_task_id="runtime-root", run_id="exact-run",
        thread_id=agent["agent_id"], attempt_status="pending",
        task_run_metadata={"business_context": context.to_dict(), "session_task_ref": task.task_id})
    store.session_tasks.bind_execution(task.task_id, task_run_id=run["task_run_id"], target_thread_id=agent["agent_id"])
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status=SESSION_TASK_ACCEPTED, conversation_request_id="invocation-exact"))
    assert store.session_tasks.require(task.task_id).status == SESSION_TASK_ACCEPTED
    host.subagents = SimpleNamespace(runtime_db=repo)
    claim = store.claims.acquire({"thread_id": agent["agent_id"], "task_id": "runtime-root", "lease_seconds": 300})
    with bind_claim_execution(ClaimExecutionBinding(store.claims, agent["agent_id"], claim["claim_id"], agent["agent_id"])):
        publish_claim_runtime_authority({"invocation_run_id": "invocation-exact", "task_id": "runtime-root",
            "run_id": "exact-run", "agent_run_id": run["agent_run_id"], "attempt_id": run["attempt_id"]})
    assert repo.get_agent_run(run["agent_run_id"])["status"] == "created"
    assert repo.get_attempt(run["attempt_id"])["status"] == "pending"
    observed = service.get(agent["agent_id"], actor=actor)
    assert observed["agent"]["work_state"] == "unknown", "running claim不能把未启动的pending attempt当作执行存活"
    assert observed["current_execution"] is None
    # 沿真实runner入口激活准确current attempt；父run仍可为created，不靠手改父run制造执行证据。
    attempt = repo.create_attempt(run["agent_run_id"], reuse_pending=True, reject_running=True,
        expected_pending_attempt_id=run["attempt_id"])
    assert attempt["attempt_id"] == run["attempt_id"] and attempt["status"] == "running"
    assert repo.get_agent_run(run["agent_run_id"])["status"] == "created"
    observed = service.get(agent["agent_id"], actor=actor)
    assert observed["agent"]["work_state"] == "running"
    assert observed["current_execution"]["task_id"] == task.task_id
    assert observed["current_execution"]["run_id"] == "exact-run"
    assert observed["current_execution"]["attempt_id"] == run["attempt_id"]
    # 父run随后更新为running，精确执行关联仍保持不变。
    with repo.transaction() as connection:
        connection.execute("UPDATE agent_runs SET status='running' WHERE agent_run_id=?", (run["agent_run_id"],))
    observed = service.get(agent["agent_id"], actor=actor)
    assert observed["agent"]["work_state"] == "running"
    assert observed["current_execution"]["task_id"] == task.task_id
    with repo.transaction() as connection:
        connection.execute("UPDATE agent_runs SET status='cancelled' WHERE agent_run_id=?", (run["agent_run_id"],))
        connection.execute("UPDATE agent_attempts SET status='cancelled' WHERE attempt_id=?", (run["attempt_id"],))
    observed = service.get(agent["agent_id"], actor=actor)
    assert observed["agent"]["work_state"] == "unknown", "终态执行不能因旧running claim而继续显示运行"
    assert observed["current_execution"] is None


# LLM: pytest复用本地HTTP Gateway fixture，经真实业务客户端准备/发送请求；临时配置下核null身份、列表配额及超额409/not_started，不外推外网可达或模型执行。
# 函数用途: 检查无归属和额度合同能完整穿过客户端与Gateway运输层。
def test_null_dto_and_quota_roundtrip_through_real_client_and_gateway(native_http, tmp_path):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
    from agent_py_agent.cli.space_commands import business_client_for_agent

    server, base = native_http
    base.config.model_backend = "echo"
    base.config.max_background_agents = 1
    base.conversation_store = ConversationStore(base.home_paths.owner_home_dir / "conversations")
    client_agent = GatewayChatClientAgent(SimpleNamespace(), SimpleNamespace(
        gateway_port=server.server.server_address[1], gateway_auth_token=""), tmp_path, [tmp_path],
        SimpleNamespace(root=base.home_paths.root), owner_identity=OwnerIdentity.local_main())
    client = business_client_for_agent(client_agent)
    created = client.send(client.prepare("background_agents.create", {"space_id": None, "name": "wire worker"}, idempotency_key="wire-null-create"))
    assert created["ok"] is True, created
    identifier = created["data"]["agent"]["agent_id"]
    assert created["data"]["agent"]["space_id"] is None
    listed = client.call("background_agents.list", {"space_id": None})
    assert listed["ok"] is True, listed
    assert [row["agent_id"] for row in listed["data"]["items"]] == [identifier]
    assert listed["data"]["quota"]["used"] == listed["data"]["quota"]["limit"] == 1
    refused = client.send(client.prepare("background_agents.create", {"space_id": None, "name": "over limit"}, idempotency_key="wire-null-refuse"))
    assert refused["http_status"] == 409 and refused["error"]["code"] == "BACKGROUND_AGENT_LIMIT_REACHED", refused
    assert refused["effect_outcome"] == "not_started"


# LLM: 通过正式消息入口追加真实guidance；get/list均须看到已受理待办，不能依赖不存在的SessionTask。
# 函数用途: 验证消息尚未消费时显示等待，而不是空闲。
@pytest.mark.parametrize("surface", ["get", "list"])
def test_work_state_waits_for_accepted_unconsumed_message(business_host, surface):
    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": "space-A", "name": "worker"}, actor=actor,
        idempotency_key="message-worker")["agent"]
    reply = queue_owned_background_message(
        host,
        actor=actor,
        target=service._thread(agent["agent_id"], actor),
        text="请处理此项工作",
        input_kind="message",
        operation_key=service._operation_key(actor, "pending-work"),
    )
    assert reply["receipt_state"] == "accepted"
    page = service.store.session_tasks.list_page(target_thread_id=agent["thread_id"], limit=100)
    assert page["coverage"] == "complete" and page["items"] == []
    pending, errors = service.store.guidance.pending_report("thread", agent["thread_id"], limit=100)
    assert not errors and len(pending) == 1
    observed = service.get(agent["agent_id"], actor=actor)["agent"] if surface == "get" else service.list({}, actor=actor)["items"][0]
    assert observed["identity_state"] == "available"
    assert observed["work_state"] == "waiting"


# LLM: 仅注入guidance读取的坏账/未覆盖报告；没有SessionTask不等于该来源已确认无待办。
# 函数用途: 验证消息观察失败或不完整时保持未知，不把它显示为空闲。
@pytest.mark.parametrize("surface", ["get", "list"])
@pytest.mark.parametrize("code", ["GUIDANCE_QUEUE_INVALID", "GUIDANCE_INDEX_INCOMPLETE"])
def test_work_state_keeps_unreadable_message_queue_unknown(business_host, monkeypatch, surface, code):
    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": "space-A", "name": "worker"}, actor=actor,
        idempotency_key="unreadable-worker")["agent"]
    observations = []

    # LLM: 保留精确目标和正数读取上限，并替换为真实报告形状；只用于模拟底层读取失败。
    # 函数用途: 记录有界消息查询，返回坏账而非伪造空队列成功。
    def unreadable(target_type, target_id, *, limit):
        observations.append((target_type, target_id, limit))
        return [], [{"code": code}]

    monkeypatch.setattr(service.store.guidance, "pending_report", unreadable)
    observed = service.get(agent["agent_id"], actor=actor)["agent"] if surface == "get" else service.list({}, actor=actor)["items"][0]
    assert observed["identity_state"] == "available"
    assert observed["work_state"] == "unknown"
    assert len(observations) == 1
    assert observations[0][:2] == ("thread", agent["thread_id"])
    assert 0 < observations[0][2] <= 100


# LLM: pytest经真实canonical创建口对照显式无归属、旧默认选择及Owner/任务/空间/项目/绑定拒绝；仅写临时Store，成功重放不得再调用factory。
# 函数用途: 验证允许null归属不会放松既有任务身份与冻结选择校验。
@pytest.mark.parametrize(
    "requested,selected,foreign_owner,wrong_task,project_ref,binding_id,rejection",
    [
        (None, None, False, False, None, None, None),
        (None, "space-A", False, False, None, None, None),
        ("space-A", "space-A", False, False, None, None, None),
        ("space-A", None, False, False, None, None, "owner/space mismatch"),
        ("space-A", "space-B", False, False, None, None, "selection conflicts"),
        (None, None, True, False, None, None, "owner/space mismatch"),
        (None, None, False, True, None, None, "task_id does not match"),
        ("space-A", "space-A", False, False, "foreign-project", None, "selection conflicts"),
        ("space-A", "space-A", False, False, None, "foreign-binding", "selection conflicts"),
    ],
)
def test_canonical_null_context_preserves_scope_and_idempotency_guards(
        business_host, requested, selected, foreign_owner, wrong_task, project_ref, binding_id, rejection):
    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )

    host, actor = business_host
    target = BackgroundAgentService(host).create({"space_id": None, "name": "canonical-worker"},
        actor=actor, idempotency_key="canonical-worker")["agent"]
    store = host.conversation_store.session_tasks
    draft = SessionTaskDraft(actor.source_thread_id, target["thread_id"], "canonical-work",
        dedupe_key="canonical-work")
    intent = BusinessContextIntent(actor.owner_id, requested, None, None)
    calls = []

    # LLM: 只由真实创建器传入预留task_id；按参数构造正常或明确冲突的DTO并记录调用，不能替代canonical校验或创建任务。
    # 函数用途: 为边界用例提供可审计的冻结上下文。
    def factory(task_id):
        calls.append(task_id)
        return BusinessExecutionContext("foreign-owner" if foreign_owner else actor.owner_id,
            selected, project_ref, "foreign-task" if wrong_task else task_id,
            None if selected is None else 1, binding_id, (), (), "business-context.v1")

    try:
        task = store.create(draft, business_context_intent=intent, business_context_factory=factory)
    except ValueError as exc:
        assert rejection is not None, f"valid canonical context was rejected: {exc}"
        assert rejection in str(exc)
        assert store.list_page(target_thread_id=target["thread_id"], limit=100)["items"] == []
    else:
        assert rejection is None, "conflicting context was committed"
        assert task.metadata["business_context"]["space_id"] == selected
        assert task.metadata["business_context"]["task_id"] == task.task_id
        assert store.require(task.task_id).metadata == task.metadata
        replay = store.create(draft, business_context_intent=intent, business_context_factory=factory)
        assert replay.task_id == task.task_id and replay.metadata == task.metadata
        assert calls == [task.task_id]
