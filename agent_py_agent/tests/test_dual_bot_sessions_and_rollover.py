# LLM: 使用真实临时ConversationStore与槽位JSON验证独立领域；不把注入submitter视为生产Gateway端到端接通。
# 模块用途: 覆盖双Bot槽位、前台阈值、非打断轮换、禁学、工具策略和动作目录。
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

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
    assert not recent_bot_messages(bot.store, result["active_thread_id"])


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
            "conversations.bots.ensure", "conversations.bots.rollover", "conversations.bots.history.list"})
    enabled = False
    with pytest.raises(HostAPIError) as error:
        call("ensure", {}, "host_ensure_bot_01")
    assert error.value.code == "USER_DISABLED"
