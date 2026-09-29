"""列本 owner 会话工具（list_owner_sessions）的合同测试，不请求模型、不启动 Gateway。

覆盖：只给管理员并跟随两个管理员开关（纯判定 + 真实 SimpleAgent 注册）；缺配置/坏配置取 CapabilityConfig() 默认值；
清单只读本 owner 的真实会话存储，别的 owner 的会话既不列出也不计数；输出不含标题、摘要等正文；
调用方自己的会话有标记且不作为发送目标；allowed_kinds 跟随开关与渠道；身份缺失 fail closed；limit 校验。
正向对照与反向断言成对出现。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.list_owner_sessions import (
    LIST_OWNER_SESSIONS_TOOL,
    ListOwnerSessionsTool,
    list_owner_sessions_tool_visible,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.background_tool_policy import (
    DEFAULT_BACKGROUND_ALLOWED_TOOLS,
    SESSION_TASK_WAKE_ALLOWED_TOOLS,
)
from agent_py_agent.agent.conversation.models import ChannelBinding, ConversationThread
from agent_py_agent.agent.conversation.session_messaging import SESSION_IDENTITY_UNAVAILABLE
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig

SECRET_TITLE = "机密标题-不该出现"
SECRET_SUMMARY = "机密摘要-不该出现"
ROW_KEYS = {"thread_id", "status", "last_activity_at", "channel", "is_current", "allowed_kinds"}
MALFORMED = "decision_subagent_model_mode: bogus\n"


def _thread(thread_id, owner_home, updated_at, **extra):
    channel = extra.pop("channel", "")
    bindings = (ChannelBinding(channel, f"conv-{thread_id}", "u", "u", thread_id),) if channel else ()
    return ConversationThread(thread_id=thread_id, canonical_user_id="u", owner_id="local/main",
                              owner_home=str(owner_home), title=SECRET_TITLE, summary=SECRET_SUMMARY,
                              updated_at=updated_at, channel_bindings=bindings, **extra)


def _agent(tmp_path, *, name="owner", current="thread-a", config=None):
    home = tmp_path / name / "home"
    home.mkdir(parents=True, exist_ok=True)
    store = ConversationStore(tmp_path / name / "conv")
    return SimpleNamespace(
        conversation_store=store,
        home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id=name, owner_home_dir=home),
        _capability_config_runtime_snapshot=SimpleNamespace(config=config or CapabilityConfig()),
        _current_run_params=SimpleNamespace(task_attributes={"conversation_thread_id": current} if current else {}),
    )


def _seed(agent, *threads):
    for thread in threads:
        agent.conversation_store.threads.write(thread)


def _list(agent, **params):
    outcome = ListOwnerSessionsTool(agent).execute(params)
    return outcome, json.loads(outcome.output)


def _rows(payload):
    return {row["thread_id"]: row for row in payload["sessions"]}


def _standard(tmp_path, **agent_kwargs):
    agent = _agent(tmp_path, **agent_kwargs)
    home = agent.home_paths.owner_home_dir
    _seed(agent,
          _thread("thread-a", home, updated_at=300.0),
          _thread("thread-b", home, updated_at=200.0, status="archived"),
          _thread("thread-c", home, updated_at=100.0, channel="feishu"))
    return agent


class TestVisibility:
    @pytest.mark.parametrize(("kind", "messaging", "task", "visible"), [
        ("main", True, True, True),
        ("main", True, False, True),
        ("main", False, True, True),
        ("main", False, False, False),
        ("user", True, True, False),
        ("group", True, True, False),
        ("", True, True, False),
    ])
    def test_admin_only_and_follows_admin_switches(self, kind, messaging, task, visible):
        config = CapabilityConfig(session_messaging_admin_enabled=messaging, session_task_admin_enabled=task,
                                  session_messaging_user_enabled=True)
        assert list_owner_sessions_tool_visible(SimpleNamespace(owner_kind=kind), config) is visible


def _real_agent(tmp_path, capability_yaml=None, **owner):
    if capability_yaml is not None:
        path = tmp_path / "agent_py_agent" / "config" / "capability_config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(capability_yaml, encoding="utf-8")
    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_plugins=False, **owner)
    return SimpleAgent(config, tmp_path)


class TestRegistration:
    @pytest.mark.parametrize("capability_yaml", [None, MALFORMED], ids=["missing", "malformed"])
    def test_admin_gets_the_tool_from_dataclass_defaults(self, tmp_path, capability_yaml):
        agent = _real_agent(tmp_path, capability_yaml)
        assert agent.home_paths.owner_kind == "main"
        assert LIST_OWNER_SESSIONS_TOOL in agent.tools.tools

    def test_admin_with_both_admin_switches_off_does_not_get_it(self, tmp_path):
        agent = _real_agent(tmp_path, "session_messaging_admin_enabled: false\nsession_task_admin_enabled: false\n")
        assert LIST_OWNER_SESSIONS_TOOL not in agent.tools.tools
        assert "send_session_message" not in agent.tools.tools

    def test_user_owner_never_gets_it_even_when_user_messaging_is_open(self, tmp_path):
        agent = _real_agent(tmp_path, "session_messaging_user_enabled: true\n", my_agent_owner_provider="feishu",
                            my_agent_owner_kind="user", my_agent_owner_id="u1")
        assert agent.home_paths.owner_kind == "user"
        assert "send_session_message" in agent.tools.tools  # 对照：用户开关确实打开了发送工具
        assert LIST_OWNER_SESSIONS_TOOL not in agent.tools.tools

    def test_session_task_wake_profile_carries_it_but_default_background_does_not(self):
        assert LIST_OWNER_SESSIONS_TOOL in SESSION_TASK_WAKE_ALLOWED_TOOLS
        assert LIST_OWNER_SESSIONS_TOOL not in DEFAULT_BACKGROUND_ALLOWED_TOOLS


class TestListing:
    def test_rows_are_structured_newest_first_and_carry_no_body(self, tmp_path):
        outcome, payload = _list(_standard(tmp_path))
        assert outcome.ok is True
        assert [row["thread_id"] for row in payload["sessions"]] == ["thread-a", "thread-b", "thread-c"]
        assert all(set(row) == ROW_KEYS for row in payload["sessions"])
        assert _rows(payload)["thread-b"]["status"] == "archived"
        assert _rows(payload)["thread-b"]["last_activity_at"] == 200.0
        assert SECRET_TITLE not in outcome.output and SECRET_SUMMARY not in outcome.output
        assert (payload["returned"], payload["truncated"], payload["unreadable_records"]) == (3, False, 0)

    def test_own_session_is_marked_and_is_not_a_target(self, tmp_path):
        _, payload = _list(_standard(tmp_path))
        rows = _rows(payload)
        assert payload["current_thread_id"] == "thread-a"
        assert (rows["thread-a"]["is_current"], rows["thread-a"]["allowed_kinds"]) == (True, [])
        assert (rows["thread-b"]["is_current"], rows["thread-b"]["allowed_kinds"]) == (False, ["message", "task"])
        # IM 渠道会话照常列出，但第一期不是发送目标。
        assert (rows["thread-c"]["channel"], rows["thread-c"]["allowed_kinds"]) == ("feishu", [])

    def test_without_current_session_nothing_is_marked(self, tmp_path):
        _, payload = _list(_standard(tmp_path, current=""))
        assert payload["current_thread_id"] == ""
        assert not any(row["is_current"] for row in payload["sessions"])
        assert _rows(payload)["thread-a"]["allowed_kinds"] == ["message", "task"]

    @pytest.mark.parametrize(("messaging", "task", "kinds"), [(True, False, ["message"]), (False, True, ["task"])])
    def test_allowed_kinds_follow_the_admin_switches(self, tmp_path, messaging, task, kinds):
        config = CapabilityConfig(session_messaging_admin_enabled=messaging, session_task_admin_enabled=task)
        _, payload = _list(_standard(tmp_path, config=config))
        assert _rows(payload)["thread-b"]["allowed_kinds"] == kinds

    def test_subagent_threads_are_not_sessions(self, tmp_path):
        agent = _standard(tmp_path)
        home = agent.home_paths.owner_home_dir
        _seed(agent, _thread("thread-agent", home, updated_at=400.0, metadata={"thread_kind": "agent"}),
              _thread("thread-d", home, updated_at=50.0, metadata={"thread_kind": "chat"}))
        _, payload = _list(agent)
        assert "thread-agent" not in _rows(payload)
        assert "thread-d" in _rows(payload)

    def test_limit_keeps_the_newest_and_reports_truncation(self, tmp_path):
        _, payload = _list(_standard(tmp_path), limit=2)
        assert [row["thread_id"] for row in payload["sessions"]] == ["thread-a", "thread-b"]
        assert (payload["returned"], payload["truncated"]) == (2, True)

    @pytest.mark.parametrize("limit", [0, 101, -1, "5", True, 2.5])
    def test_invalid_limit_is_rejected(self, tmp_path, limit):
        outcome, payload = _list(_standard(tmp_path), limit=limit)
        assert (outcome.ok, outcome.error_code) == (False, "TOOL_INVALID_ARGUMENTS")
        assert "sessions" not in payload

    def test_unreadable_records_are_counted_not_echoed(self, tmp_path):
        agent = _standard(tmp_path)
        (agent.conversation_store.storage.threads_dir / "thread-broken.json").write_text(
            '{"thread_id": "thread-broken", "title": "' + SECRET_TITLE, encoding="utf-8")
        outcome, payload = _list(agent)
        assert payload["unreadable_records"] == 1
        assert "thread-broken" not in outcome.output and SECRET_TITLE not in outcome.output
        assert payload["returned"] == 3


class TestOwnerIsolation:
    def test_foreign_owner_home_record_is_neither_listed_nor_counted(self, tmp_path):
        agent = _standard(tmp_path)
        foreign = tmp_path / "other" / "home"
        foreign.mkdir(parents=True)
        _seed(agent, _thread("thread-foreign", foreign, updated_at=500.0))
        outcome, payload = _list(agent)
        assert "thread-foreign" not in outcome.output
        assert (payload["returned"], payload["truncated"], payload["unreadable_records"]) == (3, False, 0)

    def test_same_record_under_own_home_is_listed(self, tmp_path):
        agent = _standard(tmp_path)
        _seed(agent, _thread("thread-own", agent.home_paths.owner_home_dir, updated_at=500.0))
        _, payload = _list(agent)
        assert payload["sessions"][0]["thread_id"] == "thread-own"
        assert payload["returned"] == 4

    def test_each_owner_only_sees_its_own_store(self, tmp_path):
        first = _agent(tmp_path, name="first", current="first-a")
        second = _agent(tmp_path, name="second", current="second-a")
        _seed(first, _thread("first-a", first.home_paths.owner_home_dir, updated_at=1.0),
              _thread("first-b", first.home_paths.owner_home_dir, updated_at=2.0))
        _seed(second, _thread("second-a", second.home_paths.owner_home_dir, updated_at=3.0))
        first_out, first_payload = _list(first)
        second_out, second_payload = _list(second)
        assert set(_rows(first_payload)) == {"first-a", "first-b"} and "second-" not in first_out.output
        assert set(_rows(second_payload)) == {"second-a"} and "first-" not in second_out.output

    @pytest.mark.parametrize("missing", ["owner_provider", "owner_kind", "owner_id"])
    def test_incomplete_identity_fails_closed_without_listing(self, tmp_path, missing):
        agent = _standard(tmp_path)
        setattr(agent.home_paths, missing, "")
        outcome, payload = _list(agent)
        assert (outcome.ok, outcome.error_code, outcome.effect_outcome) == (
            False, SESSION_IDENTITY_UNAVAILABLE, "not_started")
        assert "sessions" not in payload and "thread-a" not in outcome.output
