# LLM: 使用真实临时ConversationStore与槽位JSON验证独立领域；不把注入submitter视为生产Gateway端到端接通。
# 模块用途: 覆盖双Bot槽位、前台阈值、非打断轮换、禁学、工具策略和动作目录。
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from agent_py_agent.agent.conversation.bot_sessions import (
    BotSessionManager,
    bot_thread_fields,
    recent_bot_messages,
)
from agent_py_agent.agent.conversation.bot_tool_policy import bot_tool_allowed, normalize_bot_mode
from agent_py_agent.agent.conversation.learning_source import (
    bot_excluded_projection,
    facts_for_message,
    thread_source_projection,
    user_dialogue_projection,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.plugin_host.action_specs import action_spec, action_specs
from agent_py_agent.agent.plugin_host.action_specs_audit_bots import AUDIT_BOT_ACTIONS
from agent_py_agent.agent.plugin_host.composition import (
    HostComposition,
    dispatch_call,
    domain_name,
    domain_ready,
)
from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import thaw
from agent_py_agent.agent.plugin_manifest_host_v9 import validate_capabilities
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_plugin_host_v2_conversations import Harness


# LLM: 仅临时文件，clock和账号状态由测试显式注入；不访问部署账号或真实队列。
# 类用途: 提供一个可控制前台时间与停用状态的真实Bot槽位环境。
class BotCase:
    # LLM: 所有路径由pytest临时根投影，账号状态不依据消息正文。
    # 函数用途: 初始化独立owner和canonical线程/消息存储。
    def __init__(self, root):
        self.home = resolve_owner_home(root, OwnerIdentity.local_user("alice"))
        self.store = ConversationStore(self.home.data_dir / "conversations")
        self.now, self.enabled = 10000, True
        self.manager = BotSessionManager(self.home, self.store, self.access, clock=lambda: self.now)

    # LLM: 模拟领域宿主注入的鲜活账号检查；即使重放也必须调用。
    # 函数用途: 返回明确禁用错误。
    def access(self):
        if not self.enabled:
            raise HostAPIError("USER_DISABLED", 403)

    # LLM: 通过真实消息入口落盘，不手写摘要或计数来冒充完成轮数。
    # 函数用途: 给一个线程追加canonical消息。
    def append(self, tid, role, text, metadata=None):
        return self.store.messages.append({"thread_id": tid, "role": role, "content": text,
                                           "metadata": metadata or {}})


@pytest.fixture
def bot(tmp_path):
    return BotCase(tmp_path)


def test_modes_are_separate_and_alias_is_explicit(bot):
    readonly = bot.manager.ensure({}, "ensure_readonly_001")
    action = bot.manager.ensure({"bot_kind": "ops_config"}, "ensure_avatar_0001")
    assert readonly["active_thread_id"] != action["active_thread_id"]
    assert action["bot_mode"] == action["bot_kind"] == "action_avatar"
    thread = bot.store.threads.require(readonly["active_thread_id"])
    assert bot_thread_fields(thread)["learning_excluded"] is True
    assert facts_for_message({"learning_source": user_dialogue_projection()}, owner="local/alice",
                             thread_id=thread.thread_id, thread_source=thread.learning_source).can_learn is False
    with pytest.raises(HostAPIError) as error:
        normalize_bot_mode("query_readonly", "ops_config")
    assert (error.value.code, error.value.status) == ("BOT_MODE_MISMATCH", 409)


@pytest.mark.parametrize("thread_source", [
    {"bot_mode": "query_readonly"}, {"learning_excluded": True},
    {"metadata": {"learning_excluded": True}}, bot_excluded_projection(),
])
def test_explicit_learning_source_cannot_override_bot(thread_source):
    facts = facts_for_message({"learning_source": user_dialogue_projection(), "learning_excluded": False},
        owner="local/alice", thread_id="thread-one", thread_source=thread_source)
    assert not facts.can_learn
    assert facts.source_kind == "bot_query_readonly_excluded"
    projection = thread_source_projection(thread_source)
    assert projection["reason"] == "bot_query_readonly_excluded"
    assert thread_source_projection(projection) == projection


@pytest.mark.parametrize("tool,args,expected", [
    ("read_file", {}, True), ("session_search", {}, True), ("remember", {"action": "list"}, True),
    ("remember", {"action": "add"}, False), ("remember", {}, False),
    ("scripts", {"action": "get"}, True), ("scripts", {"action": "run"}, False),
    ("workflows", {"action": "run"}, False), ("run_command", {"command": "pwd"}, False),
    ("write_file", {}, False), ("create_subagents", {}, False), ("audit_group_invoke_asset", {}, False),
    ("mcp_unknown_get", {}, False), ("user_config", {"action": "set"}, False),
])
def test_readonly_is_positive_operation_allowlist(tool, args, expected):
    assert bot_tool_allowed("query_readonly", tool, args, allowed_tools={tool}, in_audit_group=True) is expected
    assert not bot_tool_allowed("query_readonly", tool, args, allowed_tools=set(), in_audit_group=True)


def test_avatar_is_owner_intersection_and_current_audit_membership():
    assert bot_tool_allowed("action_avatar", "run_command", {}, allowed_tools={"run_command"})
    assert not bot_tool_allowed("action_avatar", "run_command", {}, allowed_tools=set())
    assert not bot_tool_allowed("action_avatar", "audit_group_invoke_asset", {},
                                allowed_tools={"audit_group_invoke_asset"})
    assert bot_tool_allowed("action_avatar", "audit_group_invoke_asset", {},
                            allowed_tools={"audit_group_invoke_asset"}, in_audit_group=True)


@pytest.mark.parametrize("trigger,reason", [("idle", "idle_timeout"), ("turns", "max_turns"),
                                            ("bytes", "max_text_bytes")])
def test_automatic_rollover_uses_foreground_thresholds(bot, trigger, reason):
    original = bot.manager.ensure({}, "create_bot_slot_01")
    tid = original["active_thread_id"]
    if trigger == "idle":
        bot.now += 1800
        assert bot.manager.rollover_reason(bot.manager.read(("data", "bot_sessions", "slots.json"))["slots"]["query_readonly"]) is None
        bot.now += 1
        bot.store.threads.update_atomic(tid, lambda row: replace(row, updated_at=bot.now))
    elif trigger == "turns":
        for index in range(30):
            bot.append(tid, "user", "问", {"turn_id": str(index)})
            bot.append(tid, "assistant", "答", {"turn_id": str(index)})
    else:
        bot.append(tid, "user", "鲸" * 10923)
    result = bot.manager.ensure({}, "check_bot_slot_001")
    assert result["rolled_over"] and result["generation"] == 2
    assert result["rollover_reason"] == reason and result["previous_thread_id"] == tid


def test_background_and_tool_output_do_not_count_as_foreground_turns(bot):
    tid = bot.manager.ensure({}, "create_bot_slot_01")["active_thread_id"]
    bot.append(tid, "tool", "x" * 40000)
    for index in range(30):
        bot.append(tid, "assistant", "后台结果", {"background_transcript_request_id": str(index)})
    result = bot.manager.ensure({}, "check_bot_slot_001")
    assert not result["rolled_over"]
    slot = bot.manager.read(("data", "bot_sessions", "slots.json"))["slots"]["query_readonly"]
    assert slot["completed_foreground_turns"] == slot["visible_text_bytes"] == 0


def test_rollover_keeps_claim_goal_and_old_task_writes(bot):
    old = bot.manager.ensure({}, "create_bot_slot_01")
    tid = old["active_thread_id"]
    bot.store.threads.update_atomic(tid, lambda row: replace(row, active_task_ids=("task-one",)))
    claim = bot.store.claims.acquire({"thread_id": tid, "task_id": "task-one"})
    goal = bot.store.goals.create({"thread_id": tid, "objective": "后台继续执行"})
    result = bot.manager.rollover({"expected_generation": 1, "expected_revision": 1}, "manual_rollover_01")
    thread = bot.store.threads.require(tid)
    assert result["generation"] == 2 and result["previous_thread_id"] == tid
    assert thread.metadata["foreground_closed"] is True
    assert thread.metadata["conversation_archive_state"] == "archived"
    assert thread.status == "active" and thread.active_task_ids == ("task-one",)
    assert bot.store.claims.load_report(tid)[0]["task_id"] == claim["task_id"]
    assert bot.store.goals.list_report(tid)[0][0].goal_id == goal.goal_id
    bot.append(tid, "assistant", "旧代后台结果", {"background_transcript_request_id": "old-request"})
    assert recent_bot_messages(bot.store, tid)[0]["content"] == "旧代后台结果"
    # R2B改为同槽位跨代展示，旧代后台写入仍留在原线程而可在最近消息中读取。
    assert [row["content"] for row in recent_bot_messages(bot.store, result["active_thread_id"])] == ["旧代后台结果"]


def test_replays_conflicts_and_live_disabled_status(bot):
    first = bot.manager.ensure({}, "create_bot_slot_01")
    assert bot.manager.ensure({}, "create_bot_slot_01") == first
    payload = {"expected_generation": 1, "expected_revision": 1}
    rotated = bot.manager.rollover(payload, "manual_rollover_01")
    assert bot.manager.rollover(payload, "manual_rollover_01") == rotated
    with pytest.raises(HostAPIError) as error:
        bot.manager.rollover(payload, "manual_rollover_02")
    assert error.value.code == "REVISION_CONFLICT"
    with pytest.raises(HostAPIError) as error:
        bot.manager.ensure({"bot_mode": "action_avatar"}, "create_bot_slot_01")
    assert error.value.code == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"
    bot.enabled = False
    with pytest.raises(HostAPIError) as error:
        bot.manager.ensure({}, "create_bot_slot_01")
    assert (error.value.code, error.value.status) == ("USER_DISABLED", 403)


def test_concurrent_same_key_ensure_creates_one_thread(bot):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: bot.manager.ensure({}, "create_bot_slot_01"), range(4)))
    assert len({row["active_thread_id"] for row in results}) == 1
    assert len(bot.store.threads.list()) == 1


def test_submit_replay_stale_generation_and_mode_mismatch(bot):
    original = bot.manager.ensure({}, "create_bot_slot_01")
    calls = []

    # LLM: 只验证会话领域向原输入适配器交出的绑定；这不是生产Gateway入队或模型执行证据。
    # 函数用途: 记录本次提交的真实槽位ID。
    def submitter(slot, payload, key):
        calls.append(slot["active_thread_id"])
        return {"request_id": "request-one"}

    payload = {"text": "查告警", "expected_generation": 1,
               "page_context": {"bot_mode": "action_avatar", "learning_excluded": False}}
    first = bot.manager.submit(payload, "submit_bot_input_01", submitter)
    assert bot.manager.submit(payload, "submit_bot_input_01", submitter) == first
    assert calls == [original["active_thread_id"]]
    assert bot_thread_fields(bot.store.threads.require(calls[0]))["learning_excluded"] is True
    bot.manager.rollover({"expected_generation": 1, "expected_revision": 1}, "manual_rollover_01")
    with pytest.raises(HostAPIError) as error:
        bot.manager.submit(payload, "submit_bot_input_02", submitter)
    assert error.value.code == "BOT_SESSION_ROLLED_OVER"
    with pytest.raises(HostAPIError) as error:
        bot.manager.submit({"text": "问", "thread_id": "thread-another"}, "submit_bot_input_03", submitter)
    assert error.value.code == "BOT_MODE_MISMATCH"
    with pytest.raises(HostAPIError) as error:
        bot.manager.submit({"text": "问", "thread_id": original["active_thread_id"]}, "submit_bot_input_04", submitter)
    assert error.value.code == "BOT_SESSION_ROLLED_OVER"
    current = bot.manager.submit({"text": "问", "thread_id": original["active_thread_id"], "auto_rollover": True},
                                 "submit_bot_input_05", submitter)
    assert current["generation"] == 2 and calls[-1] == current["active_thread_id"]


def test_recent_messages_are_tail_bounded_public_and_at_most_15(bot):
    tid = bot.manager.ensure({}, "create_bot_slot_01")["active_thread_id"]
    for index in range(40):
        bot.append(tid, "user", str(index))
    bot.append(tid, "assistant", "不可见思考", {"phase": "analysis"})
    rows = recent_bot_messages(bot.store, tid)
    assert len(rows) == 15 and rows[0]["content"] == "25" and rows[-1]["content"] == "39"


def test_recover_after_new_thread_publication_without_receipt(bot, monkeypatch):
    bot.manager.ensure({}, "create_bot_slot_01")
    original_write = bot.manager.write
    failed = False

    # LLM: 只在持久化回执前注入一次崩溃，保留已经真实发表的槽位和线程用于重建测试。
    # 函数用途: 模拟线程创建后进程在幂等回执发表前退出。
    def crash_before_receipt(parts, value, admission):
        nonlocal failed
        if "receipts" in parts and not failed:
            failed = True
            raise OSError("simulated process crash")
        return original_write(parts, value, admission)

    monkeypatch.setattr(bot.manager, "write", crash_before_receipt)
    payload = {"expected_generation": 1, "expected_revision": 1}
    with pytest.raises(OSError):
        bot.manager.rollover(payload, "manual_rollover_01")
    result = bot.manager.rollover(payload, "manual_rollover_01")
    assert result["generation"] == 2
    assert len(bot.store.threads.list()) == 2


def test_ten_action_schemas_and_capability_vocabulary_are_registered():
    assert action_specs().keys() >= AUDIT_BOT_ACTIONS
    assert validate_capabilities(("audit_group.read", "audit_group.invoke"))
    for action in AUDIT_BOT_ACTIONS:
        assert action_spec(action).payload_schema["additionalProperties"] is False
    with pytest.raises(HostAPIError):
        action_spec("conversations.bots.submit").validate_payload({
            "text": "问", "page_context": {"learning_excluded": False}})


def test_host_composition_routes_real_bot_operations_and_reports_missing_dependencies(tmp_path, monkeypatch):
    case = Harness(tmp_path, monkeypatch)
    composition = HostComposition(HostDomainPorts(conversation=case.domain))
    enabled = True

    # LLM: 使用原Harness真实对象登记；此处只替代A和账号存储边界，不冒充认证HTTP或生产队列验收。
    # 函数用途: 为组合根测试提供可撤销的鲜活账号状态。
    def require_enabled(store, uid, revision=None):
        if not enabled:
            raise HostAPIError("USER_DISABLED", 403)
        from types import SimpleNamespace
        return SimpleNamespace(user_id=uid, revision=1, in_audit_group=False)

    monkeypatch.setattr("agent_py_agent.agent.plugin_host.composition.revalidate_call", case.resolve)
    monkeypatch.setattr("agent_py_agent.agent.user_space.local_user_store.LocalUserStore.require_enabled", require_enabled)

    # LLM: 原调用对象须经Harness登记并传入真正dispatch_call；不直接调用manager替代组合根。
    # 函数用途: 执行一条Bot Host动作。
    def call(action, payload, key=None):
        return thaw(dispatch_call(case.request("bots." + action, payload, key=key), composition.ports).data)

    first = call("ensure", {}, "host_ensure_bot_01")
    rotated = call("rollover", {"expected_generation": 1, "expected_revision": 1}, "host_rollover_001")
    assert rotated["generation"] == 2
    call("rollover", {"expected_generation": 2, "expected_revision": 2}, "host_rollover_002")
    page = call("history.list", {"limit": 1})
    assert len(page["items"]) == 1 and page["next_cursor"]
    tail = call("history.list", {"limit": 1, "cursor": page["next_cursor"]})
    assert tail["items"][0]["thread_id"] == first["active_thread_id"] and tail["complete"]
    with pytest.raises(HostAPIError) as error:
        call("history.list", {"limit": 1, "cursor": page["next_cursor"] + "bad"})
    assert (error.value.code, error.value.status) == ("CURSOR_EXPIRED", 409)
    with pytest.raises(HostAPIError) as error:
        call("history.list", {"thread_id": "thread-outside-owner"})
    assert (error.value.code, error.value.status) == ("RESOURCE_NOT_FOUND", 404)
    for action in AUDIT_BOT_ACTIONS:
        assert domain_name(action) == "audit_bots"
        assert domain_ready(action, composition.ports) is (action in {
            "conversations.bots.ensure", "conversations.bots.rollover", "conversations.bots.history.list",
            "audit_group.owners.list", "audit_group.assets.summary", "audit_group.alerts.list",
            "audit_group.posture.overview"})
    enabled = False
    with pytest.raises(HostAPIError) as error:
        call("ensure", {}, "host_ensure_bot_01")
    assert error.value.code == "USER_DISABLED"


# LLM: 重用真实生产安装、令牌和组合根；此fixture启动临时回环HTTP，不替换领域或授权发行器。
# 函数用途: 为Part2接线测试提供完整原宿主。
@pytest.fixture
def wired_host(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.tests.test_plugin_host_gateway_composition import production_host
    fixture = production_host.__wrapped__(tmp_path, SimpleNamespace(param={}))
    host = next(fixture)
    try:
        yield host
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


# LLM: caller由测试宿主注入真实IngressIssuer，payload不携带身份；此处不替代授权或dispatch。
# 函数用途: 执行一个经过原安装和当前账号检查的Host业务调用。
def business_call(host, action, payload=None, *, key=None, caller=None):
    import json

    from agent_py_agent.agent.plugin_host.authorization import authorize_call
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    composition = host[0].plugin_host.composition
    envelope = {"schema": "plugin_host_call.v2", "action": action, "payload": payload or {}}
    if key is not None:
        envelope["idempotency_key"] = key
    ingress = composition.issuer.ingress(host[1], caller_owner=caller)
    authorized = authorize_call(ingress, decode_call(json.dumps(envelope).encode()))
    return dispatch_call(authorized, composition.ports)


# LLM: 账号与资格都走真实Host v2写口，返回的owner不能由页面覆盖。
# 函数用途: 建立Part2测试中的实际审计组成员。
def host_member(host):
    business_call(host, "users.local.create", {"user_id": "alice"}, key="host_create_alice_001")
    result = business_call(host, "users.local.audit_group.set", {
        "user_id": "alice", "in_audit_group": True, "expected_revision": 1}, key="host_member_alice_001")
    assert result.data["revision"] == 2 and result.data["owner_id"] == "local/alice"
    return OwnerIdentity.local_user("alice")


def test_real_host_membership_observation_and_live_revocation(wired_host):
    host = wired_host
    caller = host_member(host)
    replay = business_call(host, "users.local.audit_group.set", {
        "user_id": "alice", "in_audit_group": True, "expected_revision": 1}, key="host_member_alice_001")
    assert replay.data["idempotent_replay"] is True
    actions = ("audit_group.owners.list", "audit_group.assets.summary", "audit_group.alerts.list",
               "audit_group.posture.overview")
    for action in actions:
        assert business_call(host, action, caller=caller).http_status == 200
    with pytest.raises(HostAPIError) as error:
        business_call(host, "users.local.audit_group.set", {
            "user_id": "alice", "in_audit_group": False, "expected_revision": 2},
            key="host_member_denied_01", caller=caller)
    assert error.value.status == 403
    business_call(host, "users.local.audit_group.set", {
        "user_id": "alice", "in_audit_group": False, "expected_revision": 2}, key="host_member_revoke_01")
    with pytest.raises(HostAPIError) as error:
        business_call(host, "audit_group.owners.list", caller=caller)
    assert error.value.status == 403 and error.value.code == "AUDIT_GROUP_ACCESS_DENIED"


@pytest.mark.parametrize("mode,tools", [("query_readonly", {"read_file"}),
                                       ("action_avatar", {"read_file", "write_file"})])
def test_real_bot_submit_binds_rolled_generation_and_preserves_receipt(wired_host, mode, tools):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    host = wired_host
    caller = host_member(host)
    ensure = business_call(host, "conversations.bots.ensure", {"bot_mode": mode},
                           key="production_bot_ensure01", caller=caller).data
    home = host[2].root / "owners/local/alice"
    slots_path = home / "data/bot_sessions/slots.json"
    slots = json.loads(slots_path.read_text())
    slots["slots"][mode]["last_foreground_activity_at"] = 0
    slots_path.write_text(json.dumps(slots))
    payload = {"bot_mode": mode, "text": "查看告警", "expected_generation": 1,
               "auto_rollover": True, "page_context": {"alert_id": "alert-one"}}
    result = business_call(host, "conversations.bots.submit", payload, key="production_bot_submit01", caller=caller)
    assert result.http_status == 202 and result.data["status"] == "queued"
    assert result.data["rolled_over"] and result.data["generation"] == 2
    assert result.data["previous_thread_id"] == ensure["active_thread_id"]
    assert result.data["thread_id"] != ensure["active_thread_id"]
    service = host[0].plugin_host.composition.ports.conversation.request_ports.prepare_request.__self__
    paths = service.resources.paths_for(SimpleNamespace(owner_home=LocalUserStore(host[2].root).owner_home("alice")))
    receipt = json.loads((paths.root / "input_receipts" / (result.data["request_id"] + ".json")).read_text())
    prepared = receipt["prepared_request"]
    assert prepared["conversation"]["thread_id"] == result.data["thread_id"]
    assert prepared["metadata"]["plugin_provenance"]["thread_id"] == result.data["thread_id"]
    policy = prepared["metadata"]["plugin_execution_policy"]
    assert policy["thread_id"] == result.data["thread_id"] and set(policy["tool_names"]) == tools
    assert prepared["metadata"]["learning_excluded"] is (mode == "query_readonly")
    replay = business_call(host, "conversations.bots.submit", payload, key="production_bot_submit01", caller=caller)
    assert replay.data["request_id"] == result.data["request_id"] and replay.data["idempotent_replay"]
    business_call(host, "users.local.disable", {"user_id": "alice", "expected_revision": 2},
                  key="production_bot_disable01")
    with pytest.raises(HostAPIError) as error:
        business_call(host, "conversations.bots.submit", payload, key="production_bot_submit01", caller=caller)
    assert error.value.code == "USER_DISABLED"


@pytest.mark.parametrize("status", [403, 404, 408, 409, 429, 503])
def test_real_host_maps_audit_error_status_and_details(wired_host, monkeypatch, status):
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    from agent_py_agent.agent.audit_group.blackbox_invoke_service import (
        AuditGroupBlackboxInvokeService,
    )
    from agent_py_agent.tests.test_audit_group_observation_and_invoke import asset
    caller = host_member(wired_host)
    asset(wired_host[2].root, "script", "source-one")

    # LLM: 仅替代领域报错边界，原Host来源、成员和源资产预检仍执行；不作为原生执行成功证据。
    # 函数用途: 注入每种准确HTTP分类的结构化领域拒绝。
    def reject(*_args, **_kwargs):
        raise AuditGroupError("TEST_AUDIT_REJECTION", status_code=status, details={"check": "safe"})

    monkeypatch.setattr(AuditGroupBlackboxInvokeService, "invoke_asset", reject)
    with pytest.raises(HostAPIError) as error:
        business_call(wired_host, "audit_group.assets.invoke", {
            "source_owner_id": "local/main", "kind": "script", "asset_id": "source-one"},
            key="production_asset_invoke01", caller=caller)
    assert error.value.code == "TEST_AUDIT_REJECTION" and error.value.status == status
    assert error.value.to_payload()["details"] == {"check": "safe"}


def test_real_runtime_snapshot_bot_intersection_keeps_original_handlers(tmp_path):
    from agent_py_agent.agent.conversation.bot_tool_policy import snapshot_for_bot_request
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
    from agent_py_agent.agent.tooling.models import ToolRuntime, ToolRuntimeSnapshot
    read, write = ReadFileTool(tmp_path, max_chars=1000), WriteFileTool(tmp_path)
    runtimes = tuple(ToolRuntime(tool.model_spec, tool.runtime_policy, tool)
                     for tool in (read, write))
    snapshot = ToolRuntimeSnapshot(run_id="bot-run", runtimes=runtimes,
                                  available_tool_names=frozenset({"read_file", "write_file"}),
                                  unavailable_tools=(), allowed_tools=None)
    policy = {"schema": "plugin_execution_policy.v1", "bot_mode": "query_readonly",
              "tool_names": ["read_file", "write_file", "unknown_mcp"], "in_audit_group": False}
    readonly = snapshot_for_bot_request(snapshot, policy)
    assert readonly.available_tool_names == readonly.allowed_tools == frozenset({"read_file"})
    assert readonly.runtimes[0].handler is read
    assert snapshot.available_tool_names == frozenset({"read_file", "write_file"})
    action = snapshot_for_bot_request(snapshot, {**policy, "bot_mode": "action_avatar"})
    assert action.allowed_tools == frozenset({"read_file", "write_file"})
    assert {row.handler for row in action.runtimes} == {read, write}


def test_real_host_membership_cas_and_key_conflict(wired_host):
    host_member(wired_host)
    for key, revision, code in [("host_member_alice_002", 1, "REVISION_CONFLICT"),
                                 ("host_member_alice_001", 1, "IDEMPOTENCY_CONFLICT")]:
        with pytest.raises(HostAPIError) as error:
            business_call(wired_host, "users.local.audit_group.set", {
                "user_id": "alice", "in_audit_group": False, "expected_revision": revision}, key=key)
        assert error.value.status == 409 and error.value.code == code


def test_real_host_invoke_replay_rechecks_source_and_membership(wired_host, monkeypatch):
    import json

    from agent_py_agent.agent.audit_group.blackbox_invoke_service import (
        IsolatedScriptUnit,
    )
    from agent_py_agent.tests.test_audit_group_observation_and_invoke import asset
    caller = host_member(wired_host)
    root = wired_host[2].root
    asset(root, "script", "source-one")
    calls = []

    # LLM: 只替代原生执行单元，原Host/原AG1冻结/治理/配额/发表/幂等均实际执行。
    # 函数用途: 为源启停和回执重放提供一个确定性安全结果。
    def execute(*_args, **_kwargs):
        calls.append(True)
        return {"value": 17}

    monkeypatch.setattr(IsolatedScriptUnit, "execute", execute)
    payload = {"source_owner_id": "local/main", "kind": "script", "asset_id": "source-one"}
    first = business_call(wired_host, "audit_group.assets.invoke", payload,
                          key="source_replay_key_001", caller=caller)
    replay = business_call(wired_host, "audit_group.assets.invoke", payload,
                           key="source_replay_key_001", caller=caller)
    assert first.data == replay.data and first.data["result"] == {"value": 17} and len(calls) == 1
    policy_path = root / "owners/local/alice/tool_policy.json"
    before = policy_path.read_bytes() if policy_path.exists() else None
    policy = json.loads(before) if before else {"schema_version": "tool-policy.v1"}
    policy["disabled_tools"] = [*policy.get("disabled_tools", []), "audit_group_invoke_asset"]
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(HostAPIError) as error:
        business_call(wired_host, "audit_group.assets.invoke", payload, key="source_replay_key_001", caller=caller)
    assert error.value.code == "GOVERNANCE_EXECUTION_DENIED" and error.value.status == 403 and len(calls) == 1
    if before is None:
        policy_path.unlink()
    else:
        policy_path.write_bytes(before)
    asset(root, "script", "source-one", enabled=False)
    with pytest.raises(HostAPIError) as error:
        business_call(wired_host, "audit_group.assets.invoke", payload, key="source_replay_key_001", caller=caller)
    assert error.value.code == "ASSET_DISABLED_BY_OWNER" and error.value.status == 409
    asset(root, "script", "source-one", enabled=True)
    business_call(wired_host, "users.local.audit_group.set", {
        "user_id": "alice", "in_audit_group": False, "expected_revision": 2}, key="source_revoke_key_01")
    with pytest.raises(HostAPIError) as error:
        business_call(wired_host, "audit_group.assets.invoke", payload, key="source_replay_key_001", caller=caller)
    assert error.value.code == "AUDIT_GROUP_ACCESS_DENIED" and len(calls) == 1


@pytest.mark.parametrize("target", ["bob", "main", "providers/feishu/users/ou_native"])
def test_membership_nonmanaged_scope_is_exact_host_403(wired_host, target):
    with pytest.raises(HostAPIError) as error:
        business_call(wired_host, "users.local.audit_group.set", {
            "user_id": target, "in_audit_group": True, "expected_revision": 1},
            key="nonmanaged_member_key01")
    assert (error.value.code, error.value.status) == ("MANAGED_USER_SCOPE_DENIED", 403)


def test_real_host_corrupt_source_replay_preflight_returns_structured_unavailable(wired_host):
    from agent_py_agent.tests.test_audit_group_observation_and_invoke import asset
    caller = host_member(wired_host)
    folder = asset(wired_host[2].root, "script", "corrupt-source")
    (folder / "script.json").write_text("broken")
    with pytest.raises(HostAPIError) as error:
        business_call(wired_host, "audit_group.assets.invoke", {
            "source_owner_id": "local/main", "kind": "script", "asset_id": "corrupt-source"},
            key="corrupt_source_key_001", caller=caller)
    assert error.value.code == "AUDIT_GROUP_EXECUTION_UNAVAILABLE" and error.value.status == 503


def test_r2b_recent_messages_walk_complete_lines_and_exclude_internal_roles(bot):
    tid = bot.manager.ensure({}, "r2b_ensure_recent01")["active_thread_id"]
    for index in range(16):
        bot.append(tid, "user", f"visible-{index}")
    bot.append(tid, "system", "private system")
    bot.append(tid, "tool", "x" * 70000)
    bot.append(tid, "assistant", "private reasoning", {"phase": "analysis"})
    rows = recent_bot_messages(bot.store, tid)
    assert [row["content"] for row in rows] == [f"visible-{index}" for index in range(1, 16)]
    assert all(row["role"] in {"user", "assistant"} for row in rows)


def test_r2b_host_cross_generation_message_history_and_before_cursor(wired_host):
    from types import SimpleNamespace
    host = wired_host
    caller = host_member(host)
    resources = host[0].plugin_host.composition.ports.observations.services.resources
    home = resources.users.owner_home("alice")
    store = resources.store_for(SimpleNamespace(owner_home=home))
    first = business_call(host, "conversations.bots.ensure", {}, key="r2b_history_ensure01", caller=caller).data
    for index in range(20):
        store.messages.append({"thread_id": first["active_thread_id"], "role": "user", "content": f"old-{index}"})
    second = business_call(host, "conversations.bots.rollover", {
        "expected_generation": 1, "expected_revision": 1}, key="r2b_history_rotate01", caller=caller).data
    for index in range(5):
        store.messages.append({"thread_id": second["active_thread_id"], "role": "assistant", "content": f"new-{index}"})
    page = thaw(business_call(host, "conversations.bots.history.list", {}, caller=caller).data)
    assert "archived_generations" in page and page["items"] == page["archived_generations"]
    assert [row["content"] for row in page["messages"]] == [f"old-{i}" for i in range(10, 20)] + [f"new-{i}" for i in range(5)]
    assert page["has_more"] and page["next_before_cursor"]
    tail = thaw(business_call(host, "conversations.bots.history.list", {
        "before_cursor": page["next_before_cursor"]}, caller=caller).data)
    assert [row["content"] for row in tail["messages"]] == [f"old-{i}" for i in range(10)]
    assert not tail["has_more"] and tail["next_before_cursor"] is None
    with pytest.raises(HostAPIError, match="CURSOR_EXPIRED"):
        business_call(host, "conversations.bots.history.list", {
            "before_cursor": page["next_before_cursor"], "bot_mode": "execution"}, caller=caller)


def test_r2b_execution_alias_is_host_compatible():
    assert normalize_bot_mode("execution", "ops_config") == "action_avatar"
    assert action_spec("conversations.bots.ensure").validate_payload({"bot_mode": "execution"})["bot_mode"] == "execution"


def test_r2b_recent_messages_fill_from_same_owner_mode_archived_generations(bot):
    first = bot.manager.ensure({}, "r2b_recent_cross01")
    bot.append(first["active_thread_id"], "user", "old readonly")
    action = bot.manager.ensure({"bot_mode": "execution"}, "r2b_other_mode_001")
    bot.append(action["active_thread_id"], "user", "not this mode")
    current = bot.manager.rollover({"expected_generation": 1, "expected_revision": 1}, "r2b_recent_rotate01")
    bot.append(current["active_thread_id"], "assistant", "new readonly")
    assert [row["content"] for row in recent_bot_messages(bot.store, current["active_thread_id"])] == [
        "old readonly", "new readonly"]


def test_r2b_queued_audit_query_rechecks_membership_after_queue(wired_host, monkeypatch):
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    from agent_py_agent.agent.gateway_parts.request_execution import _gateway_run_params
    from agent_py_agent.agent.plugin_host import gateway_runtime
    original = gateway_runtime.bot_execution_policy

    # 仅替代宿主已核工具集合边界；原入队回执、worker复验和实时撤权均运行真实实现。
    def issued_policy(*args):
        policy = original(*args)
        return {**policy, "tool_names": [*policy["tool_names"], "audit_group_query"]}

    monkeypatch.setattr(gateway_runtime, "bot_execution_policy", issued_policy)
    inputs = queued_bot_params(wired_host)
    business_call(wired_host, "users.local.audit_group.set", {
        "user_id": "alice", "in_audit_group": False, "expected_revision": 2}, key="r2b_queue_revoke01")
    with pytest.raises(AuditGroupError, match="AUDIT_GROUP_ACCESS_DENIED"):
        _gateway_run_params(inputs)


# LLM: 使用真正Host排队信封与canonical线程；只为参数构造器注入无模型的Agent上界，不代替完整worker运行。
# 函数用途: 把一条真实queued Bot请求交到原worker参数入口，供正反权限测试复用。
def queued_bot_params(host, *, mode="query_readonly"):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayAskRunContext,
        GatewayConversationLoadRequest,
        gateway_conversation_context,
    )
    from agent_py_agent.agent.gateway_parts.request_execution import _GatewayRunParamsRequest
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
    from agent_py_agent.agent.tooling.models import ToolRuntime, ToolRuntimeSnapshot

    caller = host_member(host)
    sent = business_call(host, "conversations.bots.submit", {"text": "查当前告警", "bot_mode": mode},
                         key="r2b_worker_submit01", caller=caller).data
    resources = host[0].plugin_host.composition.ports.observations.services.resources
    home = resources.users.owner_home("alice")
    paths = resources.paths_for(SimpleNamespace(owner_home=home))
    path = paths.inbox / (sent["request_id"] + ".json")
    request = json.loads(path.read_text())
    tools = (ReadFileTool(home.workspace_dir, max_chars=1000), WriteFileTool(home.workspace_dir))
    runtimes = tuple(ToolRuntime(tool.model_spec, tool.runtime_policy, tool) for tool in tools)
    snapshot = ToolRuntimeSnapshot(run_id="worker-bot", runtimes=runtimes,
        available_tool_names=frozenset({"read_file", "write_file"}), unavailable_tools=(), allowed_tools=None)
    agent = SimpleNamespace(config=SimpleNamespace(my_agent_home=str(home.root)), home_paths=home,
        conversation_store=resources.store_for(SimpleNamespace(owner_home=home)),
        tools=SimpleNamespace(runtime_snapshot=lambda **_kwargs: snapshot))
    conversation = gateway_conversation_context(GatewayConversationLoadRequest(agent, request, request["id"], "查当前告警"))
    ctx = GatewayAskRunContext(agent, request, path, paths.responses / (request["id"] + ".json"), request["id"], None)
    return _GatewayRunParamsRequest(request, ctx, conversation, "查当前告警")


@pytest.mark.parametrize("mode,expected", [("query_readonly", {"read_file"}),
                                           ("action_avatar", {"read_file", "write_file"})])
def test_r2b_queued_bot_worker_accepts_only_bot_owner_intersection(wired_host, mode, expected):
    from agent_py_agent.agent.gateway_parts.request_execution import _gateway_run_params
    inputs = queued_bot_params(wired_host, mode=mode)
    params = _gateway_run_params(inputs)
    assert set(params.allowed_tools) == expected


@pytest.mark.parametrize("field,value", [("bot_generation", 999), ("learning_excluded", False),
                                         ("thread_id", "thread-other"), ("bot_mode", "action_avatar")])
def test_r2b_queued_bot_rejects_forged_policy(wired_host, field, value):
    from agent_py_agent.agent.gateway_parts.request_execution import _gateway_run_params
    inputs = queued_bot_params(wired_host)
    inputs.request["metadata"]["plugin_execution_policy"][field] = value
    with pytest.raises((RuntimeError, HostAPIError)):
        _gateway_run_params(inputs)


def test_r2b_queued_bot_cannot_expand_the_host_issued_tool_names(wired_host):
    from agent_py_agent.agent.gateway_parts.request_execution import _gateway_run_params
    inputs = queued_bot_params(wired_host)
    inputs.request["metadata"]["plugin_execution_policy"]["tool_names"].append("write_file")
    with pytest.raises(HostAPIError, match="BOT_TOOL_DENIED"):
        _gateway_run_params(inputs)


# LLM: 消息和claim都走真实存储，时间由测试注入，不依赖线程updated_at或伪造槽位完成计数。
# 函数用途: 回归长前台执行/排队期间ensure和submit不封闭原代，final完成时间推进idle基准。
@pytest.mark.parametrize("claimed", [False, True])
def test_holistic_inflight_foreground_keeps_generation_and_completion_time(bot, claimed):
    first = bot.manager.ensure({}, "holistic_ensure_0001")
    tid = first["active_thread_id"]
    # 没有claim的等待消息只证明刚进入活动窗口；超窗长执行由真实恢复专属claim覆盖，不能把孤立日志当永久运行。
    if not claimed:
        bot.now += 1900
    bot.store.messages.append({"thread_id": tid, "role": "user", "content": "Long running question",
        "now": bot.now, "metadata": {"conversation_request_id": "holistic-request"}})
    claim = bot.store.claims.acquire({"thread_id": tid, "task_id": "gateway:holistic-request",
        "reason": "gateway_foreground_turn", "now": bot.now, "lease_seconds": 60,
        "recover_same_task_only": True}) if claimed else None
    if claimed:
        bot.now += 1900
    current = bot.manager.ensure({}, "holistic_ensure_0002")
    assert not current["rolled_over"] and current["active_thread_id"] == tid
    assert bot.store.threads.require(tid).metadata["foreground_closed"] is False
    submitted = bot.manager.prepare_submit({"text": "Follow-up", "auto_rollover": True}, "holistic_submit_0001")
    assert submitted["thread_id"] == tid and not submitted["rolled_over"]
    bot.now += 1900
    bot.store.messages.append({"thread_id": tid, "role": "assistant", "content": "Done", "now": bot.now,
        "metadata": {"phase": "final", "conversation_request_id": "holistic-request"}})
    if claim:
        bot.store.claims.finish({"thread_id": tid, "claim_id": claim["claim_id"], "status": "finished", "now": bot.now})
    slot = bot.manager.read(("data", "bot_sessions", "slots.json"))["slots"]["query_readonly"]
    bot.manager.refresh_metrics(slot)
    assert slot["foreground_turn_in_flight"] is False
    assert slot["last_foreground_activity_at"] == bot.now and bot.manager.rollover_reason(slot) is None
    bot.now += 1801
    assert bot.manager.rollover_reason(slot) == "idle_timeout"


# LLM: 即使尚未产生用户日志或final已写但租约未结算，原宿主前台claim仍阻止自动分卷；后台claim不阻止。
# 函数用途: 核真实执行权优先于idle/轮数/字节阈值，且不误伤后台责任。
@pytest.mark.parametrize("reason,expected", [("gateway_foreground_turn", False), ("goal_continue", True)])
@pytest.mark.parametrize("trigger", ["idle", "turns", "bytes"])
def test_holistic_active_foreground_claim_blocks_all_rollover_thresholds(bot, reason, expected, trigger):
    tid = bot.manager.ensure({}, "holistic_claim_ensure")["active_thread_id"]
    bot.store.claims.acquire({"thread_id": tid, "task_id": "gateway:claim-only", "reason": reason,
        "now": bot.now, "lease_seconds": 5000})
    state = bot.manager.read(("data", "bot_sessions", "slots.json"))
    slot = state["slots"]["query_readonly"]
    if trigger == "turns":
        slot["completed_foreground_turns"] = 30
    elif trigger == "bytes":
        slot["visible_text_bytes"] = 32768
    else:
        slot["last_foreground_activity_at"] = bot.now - 1801
    result = bot.manager.rollover_reason(slot)
    assert (result is not None) is expected


# LLM: 终态claim会被后续后台片正常替换；不依赖只保留一代的previous_claim，也不允许孤立请求ID永久阻挡分卷。
# 函数用途: 回归前台无final失败/取消后后台接手及从未执行的过窗消息。
@pytest.mark.parametrize("terminal", [None, "failed", "cancelled"])
def test_holistic_orphaned_foreground_message_does_not_block_forever(bot, terminal):
    tid = bot.manager.ensure({}, "holistic_orphan_start")["active_thread_id"]
    bot.store.messages.append({"thread_id": tid, "role": "user", "content": "Old input", "now": bot.now,
        "metadata": {"conversation_request_id": "abandoned"}})
    if terminal:
        claim = bot.store.claims.acquire({"thread_id": tid, "task_id": "gateway:abandoned",
            "reason": "gateway_foreground_turn", "now": bot.now, "recover_same_task_only": True})
        bot.store.claims.finish({"thread_id": tid, "claim_id": claim["claim_id"], "status": terminal, "now": bot.now})
        for index in range(2):
            background = bot.store.claims.acquire({"thread_id": tid, "task_id": f"task-bg-{index}",
                "reason": "goal_continue", "now": bot.now})
            bot.store.claims.finish({"thread_id": tid, "claim_id": background["claim_id"], "status": "finished", "now": bot.now})
    bot.now += 1801
    current = bot.manager.ensure({}, "holistic_orphan_check")
    assert current["rolled_over"] and current["rollover_reason"] == "idle_timeout"
    assert current["previous_thread_id"] == tid


# LLM: barrier停在检查已完成但尚未归档处，探测原claim锁并实际竞争领取，不靠sleep制造“同时执行”。
# 函数用途: 确认ensure/submit自动归档和原claim领取共用原短事务锁，不在检查之后插入运行claim。
@pytest.mark.parametrize("operation", ["ensure", "submit"])
def test_holistic_rollover_serializes_with_real_foreground_claim(bot, monkeypatch, operation):
    from agent_py_agent.agent.gateway_parts.io import try_locked_file_transition

    tid = bot.manager.ensure({}, "holistic_race_initial")["active_thread_id"]
    bot.now += 1801
    checked, proceed, entering = Event(), Event(), Event()
    original = bot.manager.rollover_reason

    # LLM: 只暂停判定返回点，判定和真正归档/claim写入仍走原实现。
    # 函数用途: 为主线程在检查和归档之间安排一次确定性交错。
    def pause(slot):
        reason = original(slot)
        checked.set()
        assert proceed.wait(5)
        return reason

    # LLM: 与Gateway同一ClaimStore和同一reason，事件只标记进入领取，不伪造运行状态。
    # 函数用途: 实际领取原线程前台claim并观测是否已经归档。
    def acquire():
        entering.set()
        claim = bot.store.claims.acquire({"thread_id": tid, "task_id": "gateway:race",
            "reason": "gateway_foreground_turn", "now": bot.now, "lease_seconds": 5000})
        return claim, bot.store.threads.require(tid).metadata["foreground_closed"]

    monkeypatch.setattr(bot.manager, "rollover_reason", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(bot.manager.ensure, {}, "holistic_race_ensure") if operation == "ensure" else pool.submit(
            bot.manager.prepare_submit, {"text": "New input", "auto_rollover": True}, "holistic_race_submit")
        try:
            assert checked.wait(5)
            with try_locked_file_transition(bot.store.storage.background_claim_path(tid)) as available:
                claim_lock_was_free = available
            contender = pool.submit(acquire)
            assert entering.wait(5)
            acquired_before = contender.result(timeout=5) if claim_lock_was_free else None
        finally:
            proceed.set()
        result = future.result(timeout=5)
        claim, closed_when_acquired = contender.result(timeout=5)
    assert claim is not None
    assert not (result["rolled_over"] and acquired_before and not acquired_before[1])
    assert claim_lock_was_free is False and closed_when_acquired is True


# LLM: 真实journal已发表而archive尚未开始时崩溃，再领取真实恢复专属前台claim；重试不得绕过正常路径保护。
# 函数用途: 回归自动轮换恢复先延后，再在前台终态后只完成原冻结代次，不因新增final回滚到旧代。
@pytest.mark.parametrize("operation", ["ensure", "submit"])
def test_holistic_pending_automatic_recovery_defers_for_foreground(bot, monkeypatch, operation):
    tid = bot.manager.ensure({}, "holistic_pending_init")["active_thread_id"]
    bot.now += 1801
    original_write, crashed = bot.manager.write, False

    # LLM: 先走原atomic写，再抛一次异常，确保重试读取的确是磁盘pending而非内存伪造。
    # 函数用途: 注入pending发表和archive之间的进程中断。
    def crash_after_pending(parts, value, admission):
        nonlocal crashed
        result = original_write(parts, value, admission)
        if not crashed and value.get("pending"):
            crashed = True
            raise OSError("crash after publishing pending")
        return result

    # LLM: 重试保持相同动作、payload和key；不以新请求改绑原事务。
    # 函数用途: 调用原ensure或submit的同一自动轮换意图。
    def invoke():
        if operation == "ensure":
            return bot.manager.ensure({}, "holistic_pending_retry")
        return bot.manager.prepare_submit({"text": "Next input", "auto_rollover": True}, "holistic_pending_retry")

    monkeypatch.setattr(bot.manager, "write", crash_after_pending)
    with pytest.raises(OSError, match="publishing pending"):
        invoke()
    pending = bot.manager.read(("data", "bot_sessions", "slots.json"))["pending"]
    target = pending["thread"]["thread_id"]
    claim = bot.store.claims.acquire({"thread_id": tid, "task_id": "gateway:pending-foreground",
        "reason": "gateway_foreground_turn", "recover_same_task_only": True, "lease_seconds": 60, "now": bot.now})
    bot.now += 4000
    with pytest.raises(HostAPIError) as error:
        invoke()
    assert error.value.code == "BOT_FOREGROUND_TURN_BUSY"
    assert bot.manager.read(("data", "bot_sessions", "slots.json"))["pending"]["thread"]["thread_id"] == target
    assert bot.store.threads.require(tid).metadata["foreground_closed"] is False
    assert bot.store.threads.load(target) is None
    assert bot.store.claims.load_report(tid)[0]["claim_id"] == claim["claim_id"]
    bot.store.messages.append({"thread_id": tid, "role": "assistant", "content": "Complete", "now": bot.now,
        "metadata": {"phase": "final", "conversation_request_id": "pending-foreground"}})
    bot.store.claims.finish({"thread_id": tid, "claim_id": claim["claim_id"], "status": "finished", "now": bot.now})
    result = invoke()
    assert result["active_thread_id"] == target and result["generation"] == 2 and result["rolled_over"]
    assert invoke() == {**result, "idempotent_replay": True} if operation == "submit" else invoke() == result
    assert len(bot.store.threads.list()) == 2
