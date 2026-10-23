# LLM: D1测试使用真实临时ConversationStore；A的授权/游标/回执接缝由显式测试宿主替代，不宣称HTTP端到端。
# 模块用途: 覆盖插件交互、任务和Goal适配器的安全合同及原持久状态。
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.plugin_host.contracts import (
    AuthorizedCall,
    PluginIngressSource,
    VerifiedPluginBinding,
    decode_call,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.goals import GoalAdapter
from agent_py_agent.agent.plugin_host.interactions import InteractionAdapter, ResponseTickets
from agent_py_agent.agent.plugin_host.schemas import canonical_json, thaw
from agent_py_agent.agent.plugin_host.tasks import TaskAdapter
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, _owner_id
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_feishu_oauth import ctx as oauth_context

_FIXTURES = (background, oauth_context)


# LLM: 仅为测试生成授权对象；生产必须由A的真实issuer核验，不能采用这个工厂。
# 类用途: 在真实临时存储前提供可撤权、精确身份的测试组合根。
class Host:
    # LLM: 所有业务文件限pytest临时目录，进度和取消替身显式记录副作用。
    # 函数用途: 准备一个普通owner与一条任务。
    def __init__(self, root):
        self.owner = OwnerIdentity("local", "user", "alice")
        self.store = ConversationStore(root)
        self.store.threads.write(ConversationThread("thread-one", "alice", owner_id=_owner_id(self.owner)))
        self.store.tasks.bind({"thread_id": "thread-one", "task_id": "task-one", "goal": "原任务"})
        self.issued, self.receipts, self.snapshots = {}, {}, {}
        self.lock = threading.RLock()
        self.enabled = True
        self.allowed_tasks = {"task-one"}
        self.allowed_threads = {"thread-one"}
        self.cancelled = []
        self.items = [{"id": "one", "title": "检查", "status": "pending"},
                      {"id": "two", "title": "实现", "status": "done"}]

    # LLM: 每次调用生成精确对象登记，不把可复制DTO当真实授权。
    # 函数用途: 用A0解码器构造测试调用。
    def auth(self, action, payload, key=None, *, owner=None, installation="install-one", caps=None):
        raw = {"schema": "plugin_host_call.v2", "action": action, "payload": payload}
        if key is not None:
            raw["idempotency_key"] = key
        call = decode_call(json.dumps(raw).encode())
        selected = owner or self.owner
        source = PluginIngressSource(self, object())
        capabilities = caps if caps is not None else {
            "tasks.read", "tasks.control", "goals.read", "goals.control", "chat",
            "interactions.read", "interactions.respond",
        }
        binding = VerifiedPluginBinding({"installation_ref": installation, "activation_id": "activation-one",
            "grant_revision": 1, "package_sha256": "a" * 64}, "human-not-implied", selected, source, capabilities)
        result = AuthorizedCall(call, binding, selected, {}, None)
        self.issued[id(result)] = result
        return result

    # LLM: 代表生产A/C门；伪造、撤权和停用在存储访问前失败。
    # 函数用途: 复核对象来源和用户当前状态。
    def revalidate(self, authorized):
        if self.issued.get(id(authorized)) is not authorized:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        if not self.enabled:
            raise HostAPIError("USER_DISABLED", 403)

    # LLM: 只返回固定owner的存储；不能跟随请求路径或替代owner。
    # 函数用途: 解析测试owner上下文。
    def resolve(self, authorized):
        if authorized.effective_owner != self.owner:
            raise HostAPIError("TARGET_DENIED", 403)
        return SimpleNamespace(store=self.store, owner=self.owner, allows=self.allows)

    # LLM: 线程授权不隐式包含任务，分别做集合核验。
    # 函数用途: 检查精确资源范围。
    def allows(self, authorized, kind, resource_id):
        return resource_id in (self.allowed_threads if kind == "thread" else self.allowed_tasks)

    # LLM: 此替身验证调用与冻结分页接缝；不替代A的MAC游标集成测试。
    # 函数用途: 给领域测试提供绑定动作/身份的快照分页。
    def page(self, authorized, rows):
        payload = authorized.call.payload
        scope = (authorized.binding.installation_ref, authorized.effective_owner, authorized.call.action)
        cursor = payload.get("cursor")
        if cursor:
            if cursor not in self.snapshots or self.snapshots[cursor][0] != scope:
                raise HostAPIError("CURSOR_EXPIRED", 410)
            _, rows = self.snapshots[cursor]
        limit = payload.get("limit", 50)
        remaining = list(rows)[limit:]
        token = f"cursor-{len(self.snapshots)}" if remaining else None
        if token:
            self.snapshots[token] = (scope, remaining)
        return {"items": list(rows)[:limit], "next_cursor": token, "snapshot_id": "snapshot-one",
                "complete": not remaining, "load_errors": []}

    # LLM: 仅测试串行幂等边界；真实部署须注入原持久control receipt服务。
    # 函数用途: 在相同语义重放时返回原结果，不重复执行副作用。
    def once(self, authorized, effect):
        scope = (authorized.binding.installation_ref, authorized.effective_owner,
                 authorized.call.action, authorized.call.idempotency_key)
        digest = canonical_json(authorized.call.payload)
        with self.lock:
            if scope in self.receipts:
                old, result = self.receipts[scope]
                if old != digest:
                    raise HostAPIError("IDEMPOTENCY_CONFLICT", 409)
                return result
            result = effect()
            self.receipts[scope] = digest, result
            return result

    # LLM: 进度替身仅提供原待办形状，不推断任务完成。
    # 函数用途: 返回测试清单。
    def progress(self, authorized, task):
        return self.items

    # LLM: 子树测试显式提供原后代身份，不让线程权限扩大成任务权限。
    # 函数用途: 返回需要取消的后代编号。
    def descendants(self, authorized, task):
        return []

    # LLM: 这里模拟原精确取消接缝，真实进程停止不属于此单元替身的验证范围。
    # 函数用途: 改真实任务状态但绝不删除Goal。
    def cancel(self, authorized, tasks, reason):
        for task in tasks:
            self.store.tasks.update_status({"task_id": task.task_id, "status": "interrupted"})
            self.cancelled.append(task.task_id)
        return {"cancel_requested": True, "resources_stopped": None}


# LLM: 每个用例独占真实临时store，禁止读取用户现有任务。
# 函数用途: 提供干净的测试宿主。
@pytest.fixture
def host(tmp_path):
    return Host(tmp_path / "conversations")


# LLM: 文件内容及mtime不变才能证明本测试观察点的只读行为。
# 函数用途: 保存业务目录文件快照。
def files_at(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


# LLM: 基于真实TaskLink读取，返回必须是闭合Task而不是遥测摘要。
# 函数用途: 验证任务读入口及零持久写入。
def test_tasks_list_get_progress_are_canonical_and_read_only(host):
    adapter = TaskAdapter(host, host)
    before = files_at(host.store.storage.root)
    listing = adapter.call(host.auth("tasks.list", {"thread_id": "thread-one"}))
    assert listing is not None
    task = thaw(adapter.call(host.auth("tasks.get", {"task_id": "task-one"})).data)
    assert task["task_id"] == "task-one" and task["revision"] >= 1
    assert task["status"] == "active" and listing.http_status == 200
    progress = adapter.call(host.auth("tasks.progress", {"task_id": "task-one", "limit": 1}))
    assert progress.data["has_more"] is True
    assert progress.data["items"][0]["status"] == "pending"
    assert files_at(host.store.storage.root) == before


# LLM: 旧revision拒绝与同key重放都必须在原任务状态上观察，取消不得暗清Goal。
# 函数用途: 验证取消CAS、幂等与领域独立。
def test_task_cancel_cas_and_replay_preserve_goal(host):
    adapter = TaskAdapter(host, host)
    goal = host.store.goals.create({"thread_id": "thread-one", "task_id": "task-one", "objective": "保持目标"})
    initial = adapter.call(host.auth("tasks.get", {"task_id": "task-one"}))
    assert initial is not None
    revision = initial.data["revision"]
    bad = host.auth("tasks.cancel", {"task_id": "task-one", "expected_revision": revision + 1}, "bad-key-00000001")
    with pytest.raises(HostAPIError) as caught:
        adapter.call(bad)
    assert caught.value.code == "REVISION_CONFLICT" and host.cancelled == []
    good = host.auth("tasks.cancel", {"task_id": "task-one", "expected_revision": revision}, "good-key-0000001")
    result = adapter.call(good)
    assert result.http_status == 202 and result.data["resources_stopped"] is None
    assert adapter.call(good) == result and host.cancelled == ["task-one"]
    assert host.store.goals.load("thread-one").goal_id == goal.goal_id


# LLM: 领域直调也须来源、capability和资源门，不信任伪造的AuthorizedCall字段。
# 函数用途: 验证伪授权、停用与范围拒绝。
def test_tasks_fail_closed_on_forgery_disabled_and_scope(host):
    adapter = TaskAdapter(host, host)
    authorized = host.auth("tasks.get", {"task_id": "task-one"})
    with pytest.raises(HostAPIError) as error:
        adapter.call(replace(authorized))
    assert error.value.status == 403
    host.enabled = False
    with pytest.raises(HostAPIError) as error:
        adapter.call(authorized)
    assert error.value.code == "USER_DISABLED"
    host.enabled = True
    host.allowed_tasks.clear()
    with pytest.raises(HostAPIError) as error:
        adapter.call(authorized)
    assert error.value.status == 404


# LLM: 私卡替身与业务临时库隔离，明确不替代IM认证、真实密码表单或OAuth集成测试。
# 类用途: 提供原卡元数据、人工认证事实和原CAS提交观察。
class Cards:
    # LLM: 秘密诱饵用于断言裁剪，不是真实凭据。
    # 函数用途: 准备四类原卡与一个原决定锁。
    def __init__(self):
        self.now = 1000.0
        self.human = object()
        self.lock = threading.RLock()
        self.decisions = []
        self.rows = [dict(interaction_id=f"card-{index}", request_id="request-one", thread_id="thread-one",
                         run_id="task-one", kind=kind, revision=1, expires_at=1100.0,
                         presentation_ref=f"presentation-{index}", secret="NEVER-EXPOSE", fields={"password": "SECRET"})
                     for index, kind in enumerate(("tool_approval", "task_credential", "model_configuration", "feishu_oauth"))]

    # LLM: 只有测试认证适配器持有这个对象，插件传字符串或dict永远不能冒充。
    # 函数用途: 检查人工操作证明。
    def verify_human(self, proof, authorized, interaction, decision_payload):
        if proof is not self.human:
            raise HostAPIError("HUMAN_TICKET_REQUIRED", 403)

    # LLM: 不调用consumer lease续租函数。
    # 函数用途: 返回原待办元数据。
    def pending(self, authorized, thread_id):
        return self.rows

    # LLM: 这里的锁模拟原审批首决锁；版本在锁内复核，不从插件payload取得决定。
    # 函数用途: 记录宿主票据中的原决定。
    def respond(self, authorized, ticket):
        with self.lock:
            row = next(row for row in self.rows if row["interaction_id"] == ticket.interaction_id)
            if row["revision"] != ticket.revision:
                raise HostAPIError("REVISION_CONFLICT", 409)
            self.decisions.append(thaw(ticket.decision_payload))
            row["revision"] += 1
            return "decided" if row["kind"] == "tool_approval" else "submitted"


# LLM: 票据签发者与API调用方分开，时钟可控便于复现边界。
# 函数用途: 准备交互测试组合根。
def interaction_setup(host):
    cards = Cards()
    tickets = ResponseTickets(host, cards.verify_human, clock=lambda: cards.now)
    return cards, tickets, InteractionAdapter(host, cards, tickets)


# LLM: 使用真实目录快照和秘密诱饵；只读不包含票据、字段、凭据引用或工具参数。
# 函数用途: 验证两类列表只返回相应kind且不产生业务写入。
def test_pending_lists_strip_secrets_without_writes(host):
    cards, tickets, adapter = interaction_setup(host)
    before = files_at(host.store.storage.root)
    approvals = adapter.call(host.auth("interactions.pending_approvals.list", {"thread_id": "thread-one"}))
    assert approvals is not None
    inputs = adapter.call(host.auth("interactions.pending_inputs.list", {"thread_id": "thread-one"}))
    assert [row["kind"] for row in approvals.data["items"]] == ["tool_approval"]
    assert {row["kind"] for row in inputs.data["items"]} == {
        "credential_request", "model_selection", "oauth_authorization"}
    assert "SECRET" not in canonical_json(inputs.data) and "NEVER-EXPOSE" not in canonical_json(inputs.data)
    assert files_at(host.store.storage.root) == before and cards.decisions == []


# LLM: 同一个真人票据只能完成一次原决定，同key重放即使票据过期仍返回原已提交结果。
# 函数用途: 同时覆盖审批和输入的正常提交与安全重放。
@pytest.mark.parametrize("index,action", [(0, "interactions.approvals.respond"), (1, "interactions.inputs.respond")])
def test_human_ticket_commit_and_idempotent_replay(host, index, action):
    cards, tickets, adapter = interaction_setup(host)
    reader = host.auth("interactions.pending_approvals.list", {"thread_id": "thread-one"})
    decision = {"decision": "approved"} if index == 0 else {"host_submission_ref": "submission-one"}
    token = tickets.issue(reader, cards.rows[index], cards.human, decision)
    assert isinstance(token, str) and len(token) >= 32
    decision["decision"] = "tampered"
    payload = {"interaction_id": f"card-{index}", "expected_revision": 1, "response_ticket": token}
    call = host.auth(action, payload, "response-key-0001")
    result = adapter.call(call)
    assert result.http_status == 200 and result.data["replayed"] is False
    cards.now += 100
    assert adapter.call(call).data["replayed"] is True and len(cards.decisions) == 1
    assert cards.decisions[0].get("decision") != "tampered"
    with pytest.raises(HostAPIError) as error:
        adapter.call(host.auth(action, payload, "response-key-0002"))
    assert error.value.status in {409, 410}


# LLM: 绑定错误不能触碰原决定；版本错误区分冲突，过期区分已失效。
# 函数用途: 检查伪造、过期和跨资源票据。
@pytest.mark.parametrize("change,status", [("forged", 403), ("expired", 410), ("owner", 403),
    ("installation", 403), ("revision", 409), ("action", 403), ("interaction", 403), ("disabled", 403)])
def test_ticket_rejections_leave_original_unchanged(host, change, status):
    cards, tickets, adapter = interaction_setup(host)
    reader = host.auth("interactions.pending_approvals.list", {"thread_id": "thread-one"})
    token = tickets.issue(reader, cards.rows[0], cards.human, {"decision": "approved"})
    assert isinstance(token, str)
    payload = {"interaction_id": "card-0", "expected_revision": 1, "response_ticket": token}
    options, action = {}, "interactions.approvals.respond"
    if change == "forged":
        payload["response_ticket"] = "forged-ticket"
    if change == "expired":
        cards.now += 60
    if change == "owner":
        options["owner"] = OwnerIdentity("local", "user", "bob")
    if change == "installation":
        options["installation"] = "another-installation"
    if change == "revision":
        payload["expected_revision"] = 2
    if change == "action":
        action = "interactions.inputs.respond"
    if change == "interaction":
        payload["interaction_id"] = "card-1"
    if change == "disabled":
        host.enabled = False
    with pytest.raises(HostAPIError) as error:
        adapter.call(host.auth(action, payload, "reject-ticket-001", **options))
    assert error.value.status == status and cards.decisions == []


# LLM: 插件自报同意不是人工证明，响应schema绝不接decision或fields。
# 函数用途: 阻断自签票据与原始秘密输入。
def test_plugin_cannot_issue_ticket_or_submit_raw_fields(host):
    cards, tickets, adapter = interaction_setup(host)
    reader = host.auth("interactions.pending_approvals.list", {"thread_id": "thread-one"})
    with pytest.raises(HostAPIError) as error:
        tickets.issue(reader, cards.rows[0], {"human": True}, {"decision": "approved"})
    assert error.value.status == 403
    for field in ("decision", "fields"):
        with pytest.raises(HostAPIError) as error:
            host.auth("interactions.inputs.respond", {"interaction_id": "card-1", "expected_revision": 1,
                      "response_ticket": "fake", field: "SECRET"}, "raw-fields-key01")
        assert error.value.code == "INVALID_SCHEMA"


# LLM: 调度回调是B/C接缝，测试只观察请求资格，不声称启动了真实模型。
# 类用途: 用真实任务绑定验证Goal生命周期和资源独立。
class GoalLifecycle:
    # LLM: 资源集合只用于证明pause/clear没有调用取消端口。
    # 函数用途: 记录受限调度请求。
    def __init__(self, host):
        self.host = host
        self.scheduled = []
        self.resources = {"running-process", "live-request"}

    # LLM: 代表原预算/权限准入门，必须在持久提交前调用。
    # 函数用途: 重新检查当前授权与reply-only模式。
    def check(self, authorized, current, operation):
        self.host.revalidate(authorized)

    # LLM: 记录唯一Goal任务身份，metadata有明确plugin来源才允许投递。
    # 函数用途: 绑定真实临时任务并记录调度请求。
    def created(self, authorized, goal):
        assert goal.metadata["tool_policy"] == "none"
        self.host.store.tasks.bind({"thread_id": goal.thread_id, "task_id": goal.task_id,
                                    "goal": goal.objective, "work_kind": "goal", "work_name": goal.name})
        self.host.allowed_tasks.add(goal.task_id)
        self.scheduled.append(goal.goal_id)

    # LLM: 更新描述不接管资源或重建task身份。
    # 函数用途: 同步原任务目标。
    def updated(self, authorized, goal):
        self.host.store.tasks.update_goal({"task_id": goal.task_id, "goal": goal.objective})

    # LLM: 仅记录后续调度，不能复活新attempt或扩大工具范围。
    # 函数用途: 记录恢复请求。
    def resumed(self, authorized, goal):
        self.scheduled.append(goal.goal_id)


# LLM: 原Goal文件与计时器不因列表或get而变化，名称/正文只来自授权Goal。
# 函数用途: 验证Goal创建、查询、冲突和同键重放。
def test_goal_create_list_get_and_conflicts(host):
    lifecycle = GoalLifecycle(host)
    adapter = GoalAdapter(host, lifecycle)
    create = host.auth("goals.create", {"thread_id": "thread-one", "objective": "新目标"}, "create-goal-key1")
    result = adapter.call(create)
    assert result is not None
    assert result.http_status == 201 and result.data["duration_seconds"] is None
    assert adapter.call(create) == result and len(lifecycle.scheduled) == 1
    before = files_at(host.store.storage.root)
    payload = {"thread_id": "thread-one", "goal_id": result.data["goal_id"]}
    assert adapter.call(host.auth("goals.get", payload)).data == result.data
    assert len(adapter.call(host.auth("goals.list", {"thread_id": "thread-one"})).data["items"]) == 1
    assert files_at(host.store.storage.root) == before
    with pytest.raises(HostAPIError) as error:
        adapter.call(host.auth("goals.create", {"thread_id": "thread-one", "objective": "冲突"}, "create-goal-key2"))
    assert error.value.status == 409


# LLM: 更新/暂停/恢复/清除以真实Goal revision为准；pause/clear不得停止任何当前资源。
# 函数用途: 验证控制序列、CAS、清除重放与身份保持。
def test_goal_control_sequence_preserves_task_resources_and_usage(host):
    goal = host.store.goals.create({"thread_id": "thread-one", "task_id": "task-one", "objective": "旧目标"})
    lifecycle = GoalLifecycle(host)
    adapter = GoalAdapter(host, lifecycle)
    payload = {"thread_id": goal.thread_id, "goal_id": goal.goal_id, "expected_revision": goal.revision}
    updated = adapter.call(host.auth("goals.update", {**payload, "objective": "新目标"}, "update-goal-key1"))
    assert updated is not None
    assert updated.data["task_id"] == "task-one" and updated.data["objective"] == "新目标"
    with pytest.raises(HostAPIError) as error:
        adapter.call(host.auth("goals.pause", payload, "pause-stale-key1"))
    assert error.value.code == "REVISION_CONFLICT"
    payload["expected_revision"] = updated.data["revision"]
    paused = adapter.call(host.auth("goals.pause", payload, "pause-good-key01"))
    assert paused.data["status"] == "paused" and lifecycle.resources == {"running-process", "live-request"}
    assert host.store.tasks.load("task-one").status == "active"
    payload["expected_revision"] = paused.data["revision"]
    resumed = adapter.call(host.auth("goals.resume", payload, "resume-goal-key1"))
    assert resumed.data["status"] == "active" and resumed.data["tokens_used"] == goal.tokens_used
    payload["expected_revision"] = resumed.data["revision"]
    clear = host.auth("goals.clear", payload, "clear-goal-key01")
    cleared = adapter.call(clear)
    assert thaw(cleared.data) == {"goal_id": goal.goal_id, "removed": True, "current_execution_cancelled": False}
    assert adapter.call(clear) == cleared
    assert host.store.goals.list_report(goal.thread_id)[0] == []
    assert host.store.tasks.load("task-one").status == "active" and host.cancelled == []


# LLM: 已进入budget_limited先在状态门拒绝；此例不作为后续预算复核的证据，缺chat和停用另核明确码。
# 函数用途: 区分预算状态冲突、缺少chat与用户停用的拒绝。
def test_goal_resume_rechecks_budget_grant_and_disabled_user(host):
    goal = host.store.goals.create({"thread_id": "thread-one", "task_id": "task-one", "objective": "受限", "token_budget": 1})
    host.store.goals.account_usage({"thread_id": "thread-one", "goal_id": goal.goal_id, "token_delta": 1})
    lifecycle = GoalLifecycle(host)
    adapter = GoalAdapter(host, lifecycle)
    payload = {"thread_id": "thread-one", "goal_id": goal.goal_id, "expected_revision": goal.revision}
    with pytest.raises(HostAPIError) as error:
        adapter.call(host.auth("goals.resume", payload, "resume-budget-001"))
    assert error.value.code == "GOAL_STATE_CONFLICT" and error.value.status == 409 and lifecycle.scheduled == []
    with pytest.raises(HostAPIError) as error:
        adapter.call(host.auth("goals.resume", payload, "resume-nochat-001", caps={"goals.control"}))
    assert error.value.code == "CAPABILITY_DENIED"
    host.enabled = False
    with pytest.raises(HostAPIError) as error:
        adapter.call(host.auth("goals.resume", payload, "resume-disabled1"))
    assert error.value.code == "USER_DISABLED"


# LLM: 先暂停再记真实用量可保留paused；因此此例必须进入预算分支，不能用更早状态冲突冒充。
# 函数用途: 分别验证允许恢复的目标在token或时间预算耗尽时不会调度、写入或清零用量。
@pytest.mark.parametrize("budget", ["tokens", "time"])
def test_goal_resume_rejects_exhausted_paused_budget(host, budget):
    limits = {"token_budget": 1} if budget == "tokens" else {"duration_seconds": 60}
    goal = host.store.goals.create({"thread_id": "thread-one", "task_id": "task-one", "objective": "暂停预算", **limits})
    paused = host.store.goals.update({"thread_id": goal.thread_id, "goal_id": goal.goal_id,
                                    "status": "paused", "expected_revision": goal.revision})
    usage = {"token_delta": 1} if budget == "tokens" else {"time_delta_seconds": 60}
    host.store.goals.account_usage({"thread_id": goal.thread_id, "goal_id": goal.goal_id,
                                  "mode": "active_or_stopped", **usage})
    current = host.store.goals.list_report(goal.thread_id)[0][0]
    assert current.status == "paused" and current.revision == paused.revision
    assert (current.tokens_used == 1) if budget == "tokens" else (current.time_used_seconds >= 60)
    lifecycle = GoalLifecycle(host)
    with host.store.goals.transition_guard(goal.thread_id):
        pass
    before = files_at(host.store.storage.root)
    with pytest.raises(HostAPIError) as error:
        GoalAdapter(host, lifecycle).call(host.auth("goals.resume", {"thread_id": goal.thread_id,
            "goal_id": goal.goal_id, "expected_revision": current.revision}, "resume-paused-01"))
    assert error.value.code == "GOAL_BUDGET_EXHAUSTED" and error.value.status == 409
    assert lifecycle.scheduled == [] and lifecycle.resources == {"running-process", "live-request"}
    assert files_at(host.store.storage.root) == before


# LLM: 目标状态/自身预算均允许恢复，必须调用原宿主准入；拒绝后不写Goal、不投递也不杀现有资源。
# 函数用途: 验证宿主实时准入拒绝不能被Goal恢复绕过。
def test_goal_resume_propagates_host_admission_denial(host):
    goal = host.store.goals.create({"thread_id": "thread-one", "task_id": "task-one", "objective": "宿主准入"})
    paused = host.store.goals.update({"thread_id": goal.thread_id, "goal_id": goal.goal_id,
                                    "status": "paused", "expected_revision": goal.revision})
    lifecycle = GoalLifecycle(host)
    seen = []

    # LLM: 只替代B/C准入接缝，不替代Goal状态或原存储；必须确实到达resume门才能记录。
    # 函数用途: 模拟宿主当前策略拒绝并保留到达证据。
    def deny(authorized, current, operation):
        seen.append((current.goal_id, current.status, operation))
        raise HostAPIError("CAPABILITY_DENIED", 403)

    lifecycle.check = deny
    with host.store.goals.transition_guard(goal.thread_id):
        pass
    before = files_at(host.store.storage.root)
    with pytest.raises(HostAPIError) as error:
        GoalAdapter(host, lifecycle).call(host.auth("goals.resume", {"thread_id": goal.thread_id,
            "goal_id": goal.goal_id, "expected_revision": paused.revision}, "resume-denied-01"))
    assert error.value.code == "CAPABILITY_DENIED" and seen == [(goal.goal_id, "paused", "resume")]
    assert lifecycle.scheduled == [] and lifecycle.resources == {"running-process", "live-request"}
    assert files_at(host.store.storage.root) == before


# LLM: 这是原任务的硬要求；当前原source白名单缺plugin_control时应真实失败，不改断言或跳过。
# 函数用途: 验证原Goal记录明确记为插件控制而非用户或unknown。
def test_goal_transition_records_plugin_control_in_canonical_store(host):
    goal = host.store.goals.create({"thread_id": "thread-one", "task_id": "task-one", "objective": "来源"})
    adapter = GoalAdapter(host, GoalLifecycle(host))
    result = adapter.call(host.auth("goals.pause", {"thread_id": goal.thread_id, "goal_id": goal.goal_id,
                               "expected_revision": goal.revision}, "source-check-key"))
    assert result is not None
    assert host.store.goals.list_report(goal.thread_id)[0][0].transition["source"] == "plugin_control"


# LLM: 用真实原审批发布器/RuntimeDB/原首决锁，不以模拟Cards的结果代替原服务。
# 函数用途: 验证插件审批投影不续租且票据提交保留原attempt校验。
@pytest.mark.parametrize("changed", [False, True])
def test_native_approval_projection_and_first_decision(tmp_path, host, changed):
    from agent_py_agent.agent.conversation.agent_tool_approval import (
        list_pending_agent_tool_approvals,
        publish_agent_tool_approval,
    )
    from agent_py_agent.agent.plugin_host.interactions import approval_interaction, respond_approval
    from agent_py_agent.tests.test_gateway_agent_control_service import (
        _bound_agent_tree,
        _child_approval_request,
    )

    agent, _, child = _bound_agent_tree(tmp_path / "native")
    native_thread = agent.conversation_store.threads.load(child.agent_thread_id)
    agent.conversation_store.threads.write(replace(native_thread, owner_id=_owner_id(OwnerIdentity.local_main())))
    request = _child_approval_request(child.id)
    handle = publish_agent_tool_approval(agent, run_id=child.id, thread_id=child.agent_thread_id, request_value=request)
    before = files_at(agent.conversation_store.storage.root)
    pending = list_pending_agent_tool_approvals(agent, root_task_id=child.root_id)[0]
    row = approval_interaction(agent, pending, "host-presentation-one")
    assert row is not None
    assert files_at(agent.conversation_store.storage.root) == before
    host.owner = OwnerIdentity.local_main()
    host.store = agent.conversation_store
    host.allowed_tasks = {child.id}
    host.allowed_threads = {child.agent_thread_id}
    human = Cards()
    tickets = ResponseTickets(host, human.verify_human)
    reader = host.auth("interactions.pending_approvals.list", {"thread_id": child.agent_thread_id})
    token = tickets.issue(reader, row, human.human, {"permission_id": request.permission_id, "decision": "approved"})
    if changed:
        repo = agent.subagents.runtime_db
        run = repo.agent_run_for_run_id(child.id)
        assert repo.settle_agent_attempt(agent_run_id=run["agent_run_id"], attempt_id=child.runner_active_attempt_id)["settled"]
        agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    call = host.auth("interactions.approvals.respond", {"interaction_id": row["interaction_id"],
                     "expected_revision": row["revision"], "response_ticket": token}, "native-approval-001")
    backend = SimpleNamespace(respond=lambda a, ticket: respond_approval(host, agent, a, ticket))
    adapter = InteractionAdapter(host, backend, tickets)
    if changed:
        with pytest.raises(HostAPIError) as error:
            adapter.call(call)
        assert error.value.status in {403, 409, 410}
        assert json.loads(handle.path.read_text())["status"] == "pending"
    else:
        assert adapter.call(call).data["status"] == "decided"
        assert json.loads(handle.path.read_text())["decision"]["decision"] == "approved"


# LLM: 冻结A0接受120字符名称，原store不支持时保留真实失败作为跨文件合同缺口。
# 函数用途: 验证长名称不会被偷偷截短或拒绝。
def test_goal_name_uses_frozen_contract_limit(host):
    result = GoalAdapter(host, GoalLifecycle(host)).call(host.auth("goals.create",
        {"thread_id": "thread-one", "objective": "名称边界", "name": "名" * 120}, "long-name-test-001"))
    assert result.http_status == 201 and result.data["name"] == "名" * 120


# LLM: 原锁前用屏障制造两次重叠取消；记录持锁顺序避免红灯测试真的永久死锁。
# 函数用途: 验证包含根在内的统一锁序和锁内CAS。
def test_overlapping_cancel_uses_one_lock_order(host, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    for task_id in ("P", "Z", "A"):
        host.store.tasks.bind({"thread_id": "thread-one", "task_id": task_id, "goal": task_id})
        host.allowed_tasks.add(task_id)
    host.descendants = lambda a, task: {"P": ["Z", "A"], "Z": ["A"]}[task.task_id]
    host.once = lambda a, effect: effect()  # 两个不同key，不用测试替身的全局receipt锁掩盖业务锁序。
    barrier, held = threading.Barrier(2), threading.local()
    original = host.store.tasks.transition_guard

    # LLM: 仍进入原文件锁；顺序违规立即报断言而不让两个测试线程互相卡住。
    # 函数用途: 观察每条并发取消的实际锁序。
    @contextmanager
    def ordered(task_id):
        if not getattr(held, "ids", []):
            barrier.wait(timeout=5)
            held.ids = []
        assert not held.ids or held.ids[-1] < task_id, (held.ids, task_id)
        with original(task_id):
            held.ids.append(task_id)
            try:
                yield
            finally:
                held.ids.pop()

    monkeypatch.setattr(host.store.tasks, "transition_guard", ordered)
    adapter = TaskAdapter(host, host)
    calls = [host.auth("tasks.cancel", {"task_id": item,
        "expected_revision": adapter.call(host.auth("tasks.get", {"task_id": item})).data["revision"],
        "include_children": True}, f"parallel-cancel-{item}") for item in ("P", "Z")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(adapter.call, call) for call in calls]
        for future in futures:
            try:
                assert future.result(timeout=10).http_status == 202
            except HostAPIError as error:
                assert error.code == "REVISION_CONFLICT"


# LLM: 提交前后的异常分类不得捏造未执行；未知结果不能自动重放原决定。
# 函数用途: 验证原交互服务异常只返回安全unknown且不重复副作用。
def test_ticket_unknown_outcome_is_structured_and_never_retried(host):
    cards, tickets, adapter = interaction_setup(host)
    reader = host.auth("interactions.pending_approvals.list", {"thread_id": "thread-one"})
    token = tickets.issue(reader, cards.rows[0], cards.human, {"decision": "approved"})
    invoked = []

    # LLM: 注入写后传输错误，模拟业务已触碰但无法确认结果的边界。
    # 函数用途: 检测是否发生不安全重复提交。
    def fail(a, record):
        invoked.append(record.interaction_id)
        raise OSError("DO-NOT-EXPOSE-RAW")

    cards.respond = fail
    call = host.auth("interactions.approvals.respond", {"interaction_id": "card-0", "expected_revision": 1,
                     "response_ticket": token}, "unknown-response-key")
    for _ in range(2):
        with pytest.raises(HostAPIError) as caught:
            adapter.call(call)
        assert caught.value.outcome == "unknown" and "DO-NOT-EXPOSE-RAW" not in str(caught.value)
    assert invoked == ["card-0"]


# LLM: 取消完成后撤权仍不得把已经发生的副作用标成not_started。
# 函数用途: 验证回包末核保留真实已提交事实。
def test_revocation_after_cancel_preserves_committed_outcome(host):
    adapter = TaskAdapter(host, host)
    revision = adapter.call(host.auth("tasks.get", {"task_id": "task-one"})).data["revision"]
    original = host.cancel

    # LLM: 在真实临时状态写入后撤权，以覆盖回包而非准入阶段。
    # 函数用途: 复现提交后停用的竞态。
    def cancel(a, tasks, reason):
        result = original(a, tasks, reason)
        host.enabled = False
        return result

    host.cancel = cancel
    with pytest.raises(HostAPIError) as caught:
        adapter.call(host.auth("tasks.cancel", {"task_id": "task-one", "expected_revision": revision}, "cancel-disable-key"))
    assert caught.value.code == "USER_DISABLED" and caught.value.outcome == "committed"
    assert host.cancelled == ["task-one"]


# LLM: Python的0/1等于False/True，但公共取消回执只允许真实bool或null；未知保持unknown。
# 函数用途: 防止宿主异常回执被投影为已停止资源。
@pytest.mark.parametrize("stopped", [0, 1])
def test_cancel_rejects_integer_resource_status(host, stopped):
    adapter = TaskAdapter(host, host)
    revision = adapter.call(host.auth("tasks.get", {"task_id": "task-one"})).data["revision"]
    original = host.cancel

    # LLM: 真实临时任务已取消后才返回错误类型，不能据异常抹掉已发生的副作用。
    # 函数用途: 注入边界回执错误。
    def cancel(a, tasks, reason):
        return {**original(a, tasks, reason), "resources_stopped": stopped}

    host.cancel = cancel
    with pytest.raises(HostAPIError) as error:
        adapter.call(host.auth("tasks.cancel", {"task_id": "task-one", "expected_revision": revision}, "typed-cancel-key1"))
    assert error.value.code == "OUTCOME_UNKNOWN" and error.value.outcome == "unknown"
    assert host.cancelled == ["task-one"]


# LLM: 子树越权或取锁前后成员变化都必须在调用原取消端口前拒绝，不悄悄缩小取消集合。
# 函数用途: 检查取消子树的全量授权与成员CAS。
@pytest.mark.parametrize("case", ["denied", "changed"])
def test_cancel_subtree_scope_and_membership_are_rechecked(host, case):
    host.store.tasks.bind({"thread_id": "thread-one", "task_id": "child-one", "goal": "child"})
    host.allowed_tasks.add("child-one")
    calls = []

    # LLM: 首次与锁内读取可改变结果，以模拟任务树在锁前增长；不修改底层任务锁。
    # 函数用途: 提供可控的原后代快照。
    def descendants(a, task):
        calls.append(task.task_id)
        return [] if case == "changed" and len(calls) == 1 else ["child-one"]

    host.descendants = descendants
    if case == "denied":
        host.allowed_tasks.remove("child-one")
    adapter = TaskAdapter(host, host)
    revision = adapter.call(host.auth("tasks.get", {"task_id": "task-one"})).data["revision"]
    with pytest.raises(HostAPIError) as error:
        adapter.call(host.auth("tasks.cancel", {"task_id": "task-one", "expected_revision": revision,
                               "include_children": True}, "subtree-check-key"))
    assert error.value.code == ("RESOURCE_NOT_FOUND" if case == "denied" else "REVISION_CONFLICT")
    assert host.cancelled == [] and host.store.tasks.load("task-one").status == "active"


# LLM: 票据不能活过原卡期限，激活或grant换代不能沿用旧真人授权。
# 函数用途: 覆盖早到期及安装授权换代。
@pytest.mark.parametrize("change", ["card_expiry", "activation", "grant", "package"])
def test_ticket_original_expiry_and_generation_binding(host, change):
    cards, tickets, adapter = interaction_setup(host)
    cards.rows[0]["expires_at"] = cards.now + 2
    reader = host.auth("interactions.pending_approvals.list", {"thread_id": "thread-one"})
    token = tickets.issue(reader, cards.rows[0], cards.human, {"decision": "approved"})
    call = host.auth("interactions.approvals.respond", {"interaction_id": "card-0", "expected_revision": 1,
                     "response_ticket": token}, "generation-key01")
    if change == "card_expiry":
        cards.now += 2
    else:
        values = dict(call.binding.grant)
        values.update({"activation": {"activation_id": "activation-two"}, "grant": {"grant_revision": 2},
                       "package": {"package_sha256": "b" * 64}}[change])
        binding = replace(call.binding, grant=values)
        call = replace(call, binding=binding)
        host.issued[id(call)] = call
    with pytest.raises(HostAPIError) as error:
        adapter.call(call)
    assert error.value.status == (410 if change == "card_expiry" else 403) and cards.decisions == []


# LLM: 原发卡、验签、输入依赖均真实运行；运输与OAuth配置沿现有隔离fixture替代，不访问真人账号。
# 函数用途: 为三类原私卡只读投影准备宿主资源和原载荷。
def native_input(kind, ctx, tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.conversation.input_dependencies import (
        InputDependencyAccess,
        input_dependency_access,
    )
    from agent_py_agent.agent.gateway_parts.model_profile_service import execute_model_text_control
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        validated_private_interaction,
    )
    from agent_py_agent.tests.test_feishu_oauth import _authorize
    from agent_py_agent.tests.test_im_model_cards import callback, setup
    from agent_py_agent.tests.test_task_credentials import _card

    if kind == "model_configuration":
        base, owner, scope, host, thread = setup(tmp_path / "model", monkeypatch)
        execute_model_text_control(base, parse_conversation_control("/model"), scope)
        body, actor = callback(base), "ou_actor"
        inputs = InputDependencyAccess(host.conversation_store, owner, None, None, None)
        home, root = host.home_paths.owner_home_dir, base.home_paths.root
    else:
        home, root = ctx.agent.home_paths.owner_home_dir, ctx.agent.home_paths.root
        inputs = input_dependency_access(ctx.agent)
        if kind == "feishu_oauth":
            return home, inputs, _authorize(ctx)[2]
        _, _, body, _ = _card(ctx)
        actor = "ou_synthetic"
    payload, locked = validated_private_interaction(root, body, channel="feishu", actor_id=actor,
        lock_error=lambda: "", admin_identity=lambda: object())
    assert locked is None
    return home, inputs, payload


# LLM: 直接读原卡的字节/mtime必须完全不变；原签名、凭据引用、OAuth状态和密文均不能进入公共DTO。
# 函数用途: 验证三类真实私卡读接缝，无消费或秘密回传。
@pytest.mark.parametrize("kind", ["task_credential", "model_configuration", "feishu_oauth"])
def test_native_pending_inputs_are_read_only_and_secret_free(oauth_context, tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        read_plugin_pending_input,
    )
    from agent_py_agent.agent.plugin_host.interactions import project_interaction

    home, inputs, payload = native_input(kind, oauth_context, tmp_path, monkeypatch)
    before = files_at(home)
    row = read_plugin_pending_input(home, inputs, payload, presentation_ref="native-presentation")
    assert row is not None
    public = project_interaction(row)
    assert public["respond_via"] == "host_ui" and row["kind"] == kind and row["revision"] > 0
    forbidden = {"reference", "state", "ciphertext", "fields", "credential_ref", "actor_id", "owner"}
    assert not forbidden.intersection(public) and not forbidden.intersection(row)
    assert files_at(home) == before
    wrong_owner = {**payload, "owner": {"provider": "local", "owner_kind": "user", "owner_id": "other"}}
    with pytest.raises(HostAPIError) as error:
        read_plugin_pending_input(home, inputs, wrong_owner, presentation_ref="native-presentation")
    assert error.value.status == 403 and files_at(home) == before


# LLM: 同尾账号ID不能跨provider或群/个人互认；线程使用原完整owner投影而非OwnerIdentity的末段。
# 函数用途: 用规范线程归属验证任务入口，并拒绝同名不同来源的记录。
def test_tasks_use_canonical_owner_namespace(host):
    adapter = TaskAdapter(host, host)
    call = host.auth("tasks.get", {"task_id": "task-one"})
    assert adapter.call(call).data["task_id"] == "task-one"
    thread = host.store.threads.load("thread-one")
    host.store.threads.write(replace(thread, owner_id=_owner_id(OwnerIdentity.provider_user("feishu", "alice"))))
    with pytest.raises(HostAPIError) as error:
        adapter.call(call)
    assert error.value.code == "RESOURCE_NOT_FOUND"


# LLM: 保留原签名/输入/owner/quota/模型配置服务，只替代飞书运输、插件授权组合根与真人SDK身份来源。
# 函数用途: 为原输入响应端到端适配测试准备三类真实卡。
def native_response_setup(host, ctx, tmp_path, monkeypatch, kind, decision):
    from agent_py_agent.agent.common.json_io import read_json_object_report
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.conversation.input_dependencies import (
        InputDependencyAccess,
        input_dependency_access,
    )
    from agent_py_agent.agent.gateway_parts.model_profile_service import execute_model_text_control
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        validated_private_interaction,
    )
    from agent_py_agent.agent.plugin_host.interactions import NativeInputBackend
    from agent_py_agent.tests.test_feishu_oauth import _authorize
    from agent_py_agent.tests.test_im_model_cards import callback, setup
    from agent_py_agent.tests.test_task_credentials import _card

    if kind == "model_configuration":
        from agent_py_agent.agent.gateway_parts.model_control_cards import _record_path
        base, owner, scope, model_host, thread = setup(tmp_path / "native-model", monkeypatch)
        execute_model_text_control(base, parse_conversation_control("/model"), scope)
        if decision == "save":
            from agent_py_agent.tests.test_im_model_cards import submit
            assert submit(base, callback(base))["ok"]
        body, actor = callback(base, decision=decision, fields={"profile_id": "default"}), "ou_actor"
        if decision == "save":
            body["fields"] = {"model_backend": "openai_compatible", "api_base": "https://example.test/v1",
                "model_name": "synthetic-private-model", "model_context_window_tokens": "64000", "api_key": "synthetic-private-key"}
        paths = model_host.home_paths
        inputs = InputDependencyAccess(model_host.conversation_store, owner, None, None, None)
    else:
        base, paths, actor = ctx.agent, ctx.agent.home_paths, "ou_synthetic"
        inputs = input_dependency_access(base)
        if kind == "task_credential":
            _, path, body, _ = _card(ctx)
            body = {**body, "decision": decision}
        else:
            from agent_py_agent.agent.user_space.feishu_oauth import oauth_path
            _, state, _ = _authorize(ctx)
            path = oauth_path(base, actor)
            row = read_json_object_report(path, context="test.native").payload
            body = {"reference": state, "decision": decision, **row["im_control"]}
    payload, locked = validated_private_interaction(base.home_paths.root, body, channel="feishu", actor_id=actor,
        lock_error=lambda: "", admin_identity=lambda: object())
    assert locked is None
    if kind == "model_configuration":
        path = _record_path(paths.owner_home_dir, payload["control_id"])
    host.owner, host.store = inputs.owner, inputs.store
    context = SimpleNamespace(base=base, home_paths=paths, inputs=inputs,
        capability=SimpleNamespace(im_task_credentials_enabled=True, im_model_cards_enabled=True,
                                   im_group_shared_credentials_enabled=True))
    proof = object()

    # LLM: 只接受测试所持opaque SDK对象，字符串或插件自报human=True不是认证证明。
    # 函数用途: 模拟真实适配器已验证的channel/actor身份，不替代MAC和原卡校验。
    def authenticate(value):
        if value is not proof:
            raise HostAPIError("HUMAN_TICKET_INVALID", 403)
        return {"channel": "feishu", "actor_id": actor}

    backend = NativeInputBackend(host, context, lambda *_: [(payload, "native-card")], authenticate)
    thread_id = payload.get("thread_id") or payload.get("scope", {}).get("thread_id") or ctx.thread.thread_id
    host.allowed_threads = {thread_id}
    host.allowed_tasks = {ctx.task_id}
    return backend, proof, body, path, thread_id


# LLM: 原卡真实状态与加密/模型选择须落盘；同票据同键重放不能再次修改原卡或输入依赖。
# 函数用途: 验证三类真实取消、凭据提交和模型选择，而非只向Cards.decisions追加。
@pytest.mark.parametrize("kind,decision,status", [("task_credential", "cancel", "cancelled"),
    ("model_configuration", "cancel", "cancelled"), ("feishu_oauth", "cancel", "cancelled"),
    ("task_credential", "submit", "submitted"), ("model_configuration", "select", "submitted"),
    ("model_configuration", "save", "submitted")])
def test_native_input_tickets_commit_original_records(host, oauth_context, tmp_path, monkeypatch, kind, decision, status):
    from agent_py_agent.agent.common.json_io import read_json_object_report

    backend, proof, body, path, thread = native_response_setup(host, oauth_context, tmp_path, monkeypatch, kind, decision)
    tickets = ResponseTickets(host, backend.verify_human)
    adapter = InteractionAdapter(host, backend, tickets)
    reader = host.auth("interactions.pending_inputs.list", {"thread_id": thread})
    before = files_at(backend.context.home_paths.owner_home_dir)
    public = adapter.call(reader).data["items"]
    assert len(public) == 1 and files_at(backend.context.home_paths.owner_home_dir) == before
    internal = backend.pending(reader, thread)[0]
    token = tickets.issue(reader, internal, proof, body)
    call = host.auth("interactions.inputs.respond", {"interaction_id": internal["interaction_id"],
        "expected_revision": internal["revision"], "response_ticket": token}, "native-input-001")
    result = adapter.call(call)
    assert result.data["status"] == status
    row = read_json_object_report(path, context="test.native.committed").payload
    assert row["status"] == ("cancelled" if decision == "cancel" else "ready" if kind == "task_credential" else "completed")
    if decision == "submit":
        assert row["ciphertext"] and body["fields"]["secret"] not in path.read_text()
        assert backend.context.inputs.read(row["input_id"])["status"] == "ready"
    if kind == "model_configuration" and decision == "select":
        assert host.store.threads.load(thread).model_profile_id == "default"
    if kind == "model_configuration" and decision == "save":
        from agent_py_agent.agent.settings.model_profiles import (
            model_profiles_path,
            read_model_profiles,
        )
        saved = read_model_profiles(model_profiles_path(backend.context.home_paths))
        assert any(p["api_key"] == body["fields"]["api_key"] for p in saved["providers"].values())
        assert body["fields"]["api_key"] not in path.read_text()
    after = files_at(backend.context.home_paths.owner_home_dir)
    assert adapter.call(call).data["replayed"] is True
    assert files_at(backend.context.home_paths.owner_home_dir) == after
    assert internal["interaction_id"] not in {r["interaction_id"] for r in backend.pending(reader, thread)}
    assert "reference" not in public[0] and "fields" not in public[0]


# LLM: 原卡第一次锁内读取后必须重新核权限和票据期限；锁前已授权不能替代锁内末核。
# 函数用途: 分别证明三类私卡拒绝版本变化、锁内撤权及票据到期，原记录不发生提交。
@pytest.mark.parametrize("kind", ["task_credential", "model_configuration", "feishu_oauth"])
@pytest.mark.parametrize("change", ["revision", "disabled_at_lock", "expired_at_lock"])
def test_native_input_lock_fences(host, oauth_context, tmp_path, monkeypatch, kind, change):
    from contextlib import contextmanager

    from agent_py_agent.agent.gateway_parts import model_control_cards
    from agent_py_agent.agent.user_space import task_credentials

    backend, proof, body, path, thread = native_response_setup(host, oauth_context, tmp_path, monkeypatch, kind, "cancel")
    tickets = ResponseTickets(host, backend.verify_human)
    reader = host.auth("interactions.pending_inputs.list", {"thread_id": thread})
    row = backend.pending(reader, thread)[0]
    token = tickets.issue(reader, row, proof, body)
    if change == "revision":
        value = json.loads(path.read_text())
        value["dispatch_id" if kind == "model_configuration" else "ticket_revision_probe"] = "changed-after-signing"
        path.write_text(json.dumps(value))
    else:
        from agent_py_agent.agent.common import json_io
        module = {"task_credential": task_credentials, "model_configuration": model_control_cards,
                  "feishu_oauth": json_io}[kind]
        original = module.locked_json_path

        # LLM: 使用真实原锁，注入只发生在锁取得之后；不替代原业务写入或CAS结果。
        # 函数用途: 在原锁内制造撤权或票据过期的竞态观察点。
        @contextmanager
        def locking(*args, **kwargs):
            with original(*args, **kwargs):
                if change == "disabled_at_lock":
                    host.enabled = False
                else:
                    monkeypatch.setattr(time, "time", lambda: tickets._tickets[token]["record"].expires_at + 1)
                yield

        monkeypatch.setattr(module, "locked_json_path", locking)
    before = path.read_bytes()
    call = host.auth("interactions.inputs.respond", {"interaction_id": row["interaction_id"],
        "expected_revision": row["revision"], "response_ticket": token}, "native-fence-001")
    with pytest.raises(HostAPIError) as error:
        InteractionAdapter(host, backend, tickets).call(call)
    expected = {"revision": "REVISION_CONFLICT", "disabled_at_lock": "USER_DISABLED",
                "expired_at_lock": "HUMAN_TICKET_EXPIRED"}
    assert error.value.code == expected[change] and path.read_bytes() == before


# LLM: 真人SDK证明、原MAC和原message/chat均必须成立，不能用插件自报标记或转发卡签新票据。
# 函数用途: 验证原输入票据签发拒绝伪造来源、引用或卡片展示身份。
@pytest.mark.parametrize("kind", ["task_credential", "model_configuration", "feishu_oauth"])
@pytest.mark.parametrize("change", ["proof", "reference", "message_id"])
def test_native_input_ticket_issuer_rejects_forgery(host, oauth_context, tmp_path, monkeypatch, kind, change):
    backend, proof, body, path, thread = native_response_setup(host, oauth_context, tmp_path, monkeypatch, kind, "cancel")
    tickets = ResponseTickets(host, backend.verify_human)
    reader = host.auth("interactions.pending_inputs.list", {"thread_id": thread})
    row = backend.pending(reader, thread)[0]
    before = path.read_bytes()
    if change == "proof":
        proof = {"human": True}
    else:
        body = {**body, change: "forged"}
    with pytest.raises(HostAPIError):
        tickets.issue(reader, row, proof, body)
    assert path.read_bytes() == before


# LLM: 原IM请求没有票据上下文时必须无动作；异常退出和并发线程不能继承别人的插件CAS。
# 函数用途: 验证原卡末核上下文的并发隔离及finally清理。
def test_native_input_guard_is_thread_local_and_resets():
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        check_plugin_input_record,
        plugin_input_record_check,
    )

    seen = []
    with pytest.raises(HostAPIError):
        with plugin_input_record_check(lambda row: seen.append(row)):
            worker = threading.Thread(target=check_plugin_input_record, args=({"other_thread": True},))
            worker.start()
            worker.join(2)
            assert not worker.is_alive()
            check_plugin_input_record({"same_thread": True})
            raise HostAPIError("REVISION_CONFLICT", 409)
    check_plugin_input_record({"outside": True})
    assert seen == [{"same_thread": True}]
