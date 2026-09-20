"""/audit 结构化激活(真产品激活路径,非直接 set audit_guarantee=True)。

治测试方 1.9 网关实测缺口:发 /audit 真任务,盯守委派给子代理做,子代理 goal 空、/audit 词元
落在 runner_prompt 里、后台唤醒轮 prompt 被回填 → 旧激活(只查 _current_user_prompt+goal 词元)
两条都落空 → /audit 静默没激活整轮跑 triage。harness+队列机制单测都漏这条,因为它们直接
set audit_guarantee=True 绕过激活层。

本文件专钉【激活层】:Gateway 只在用户原文开头识别 /audit 并盖入 task_attributes，
watch_stream(action=open) 下游只认该结构化信号；默认档不误开，模型参数和提示词回扫都不能绕过。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from agent.agent_core.orchestration.create_policy import (
    _role_policy,
    create_task_attributes,
)
from agent.agent_core.orchestration.tool_grants import CODING_SUBAGENT_TOOLS
from agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_DEADLINE_ATTR,
    AUDIT_OBJECTIVE_ATTR,
    AUDIT_RUN_EPOCH_ATTR,
    AUDIT_SOURCE_BINDING_PENDING_ATTR,
    AUDIT_SOURCE_BINDINGS_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    AUDIT_WINDOW_ATTR,
    attributes_request_audit,
    audit_source_worker_key,
)
from agent.conversation.audit_lifecycle import project_audit_runtime_attributes
from agent.conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
)
from agent.conversation.control_commands import (
    parse_conversation_control,
    parse_conversation_task_command,
)
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion import watch_tool as wt
from agent.ingestion.watch_tool import WatchStreamTool
from agent.tooling.runtime_contracts import ProviderToolCapability, ToolProtocolSnapshot

_BASE_URL = "http://127.0.0.1:9/pull"
_URL = f"{_BASE_URL}?position=<next>&count=<limit>"


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    yield tmp_path / "owner"
    # 每个 open 的 audit 保证档 watch 都会起收割线程;teardown 必须停掉,
    # 否则泄漏进后续测试(monkeypatch 窗口内先提交导致 DID NOT RAISE)。
    for watch_id in fresh.ids():
        hv.stop_harvester(watch_id)


def _fetch_ok(request) -> tuple[bool, object, str]:
    query = parse_qs(urlsplit(getattr(request, "url", request)).query)
    cursor = int((query.get("position") or [0])[0])
    return True, {"items": [], "next_cursor": cursor}, ""


def _text_protocol_snapshot() -> ToolProtocolSnapshot:
    return ToolProtocolSnapshot(
        run_id="audit-activation-test",
        source_protocol="text",
        capability=ProviderToolCapability(
            provider="test",
            endpoint="local://audit-activation-test",
            model="fake",
            stream=False,
            native_supported=False,
            evidence="explicit_test_protocol_fixture",
        ),
    )


def _tool(owner_home, *, run_params=None, user_prompt="", subagent_run_id=""):
    effective_run_params = run_params or SimpleNamespace()
    if not hasattr(effective_run_params, "tool_protocol_snapshot"):
        effective_run_params.tool_protocol_snapshot = _text_protocol_snapshot()
    task_attributes = getattr(effective_run_params, "task_attributes", None)
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"),
        _current_user_prompt=user_prompt,
        _current_run_params=effective_run_params,
        _current_subagent_run_id=subagent_run_id,
        _current_task_attributes=task_attributes if subagent_run_id else None,
        subagents=(
            SimpleNamespace(load=lambda _run_id: SimpleNamespace(goal="研判当前来源"))
            if subagent_run_id
            else None
        ),
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool.__dict__["_fetch_json"] = _fetch_ok
    return tool


def _audit_source_attrs(**extra: object) -> dict[str, object]:
    return {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: "audit-root-1",
        AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
        **extra,
    }


def _open(tool) -> dict:
    result = tool.execute({"action": "open", "url": _URL})
    assert result.ok, result.output
    return json.loads(result.output)


# ── 判据模块本体 ──


def test_command_parser_detects_slash_audit_word_boundary():
    command = parse_conversation_control("/audit 30d 安全巡检 盯这5个API几个月不丢数据")
    assert command is not None and command.valid
    assert command.operation == "start"
    assert command.value == "盯这5个API几个月不丢数据"
    assert parse_conversation_task_command(
        "/audit 30d 安全巡检 盯这5个API几个月不丢数据"
    ) is None
    assert parse_conversation_task_command("/audit") is None
    assert parse_conversation_task_command("盯这5个API几个月 /audit 不丢数据") is None
    assert parse_conversation_task_command("audit the logs") is None
    assert parse_conversation_task_command("/auditing") is None
    assert parse_conversation_task_command("") is None


def test_attributes_flag_detects_structural():
    assert attributes_request_audit({AUDIT_ATTR: True})
    assert not attributes_request_audit({AUDIT_ATTR: False})
    assert not attributes_request_audit({})
    assert not attributes_request_audit(None)


# ── 主代理激活路径 ──


def test_activate_via_task_attributes_structural(owner_home):
    """核心修法:task_attributes 带结构化保证档标志 → 激活，不需要下游重新扫描
    任何文本里的 /audit 词元(跨轮/委派可靠的那条路)。"""
    rp = SimpleNamespace(task_attributes=_audit_source_attrs(), root_user_prompt="")
    opened = _open(_tool(owner_home, run_params=rp, subagent_run_id="sub-1"))
    assert opened.get("audit_guarantee") is True


def test_root_prompt_text_is_not_a_downstream_authority_fallback(owner_home):
    """下游 watch 只认入口盖章后的结构化属性，不重新扫描提示词。"""
    rp = SimpleNamespace(task_attributes=None, root_user_prompt="/audit 盯这5个API逐条研判不丢")
    opened = _open(_tool(owner_home, run_params=rp))
    assert opened.get("audit_guarantee") in (None, False)


def test_no_activation_without_any_signal(owner_home):
    """无结构化标志时保持默认档；模型不能自行把普通任务升级成保证档。"""
    rp = SimpleNamespace(
        task_attributes={"conversation_task_id": "t1"}, root_user_prompt="盯这5个API报异常"
    )
    opened = _open(_tool(owner_home, run_params=rp, user_prompt="盯这5个API报异常"))
    assert opened.get("audit_guarantee") in (None, False)
    # status 也一眼可见默认档
    status = json.loads(
        _tool(owner_home, run_params=rp)
        .execute({"action": "status", "watch_id": opened["watch_id"]})
        .output
    )
    assert status["audit_guarantee"] is False


def test_model_tool_param_cannot_enable_audit_without_structured_command(owner_home):
    """旧 audit=1 工具入口已撤销；只有 Gateway 盖章后的 task_attributes 有权限开启特殊模式。"""
    rp = SimpleNamespace(task_attributes={}, root_user_prompt="普通盯守")
    tool = _tool(owner_home, run_params=rp)

    result = tool.execute({"action": "open", "url": _URL, "audit": 1})

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.handler_executed is False
    assert "audit" in result.output


# ── 委派子代理激活路径(测试方挖的确切场景)──


def test_activate_in_subagent_with_empty_goal(owner_home, monkeypatch):
    """测试方实锤场景回归:盯守委派给子代理,子代理 goal 空、/audit 不在 goal 里,但父任务
    保证档标志经 task.attributes 继承进 runner 上下文 → 子代理开 watch 照样激活保证档。
    这正是旧实现(只查 goal 词元)漏掉、导致 6 watch 全 None 的洞。"""
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"),
        _current_user_prompt="",  # 唤醒轮无用户原文
        _current_run_params=SimpleNamespace(
            task_attributes=None,
            root_user_prompt="",
            tool_protocol_snapshot=_text_protocol_snapshot(),
        ),
        subagents=None,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool.__dict__["_fetch_json"] = _fetch_ok
    # runner 起子代理:从 task.attributes 设线程本地上下文(含继承来的保证档标志)
    agent.subagents = SimpleNamespace(load=lambda _run_id: SimpleNamespace(goal="研判当前来源"))
    prev = set_current_subagent_context(
        agent,
        run_id="sub-1",
        task_attributes=_audit_source_attrs(),
    )
    try:
        opened = _open(tool)
    finally:
        restore_current_subagent_context(agent, prev)
    assert opened.get("audit_guarantee") is True


# ── 契约沿 spawn 树继承(create_subagents 层)──


def test_create_subagents_inherits_audit_from_parent(owner_home):
    """父任务(主代理)保证档 → 派出的子代理 task.attributes 自动带保证档标志(结构化继承,
    不靠 item 的 goal 是否写了 /audit)。委派做盯守的判读子代理照样在保证档。"""
    parent = SimpleNamespace(
        _current_run_params=SimpleNamespace(
            task_attributes={
                AUDIT_ATTR: True,
                AUDIT_OBJECTIVE_ATTR: "逐条检查五路日志",
                AUDIT_RUN_EPOCH_ATTR: 7,
                CONVERSATION_REQUEST_ID_ATTR: "audit-task-1",
            }
        )
    )
    attrs = create_task_attributes({"goal": "盯API-1"}, parent)  # item 里没写 /audit
    assert attrs.get(AUDIT_ATTR) is True
    assert attrs[AUDIT_OBJECTIVE_ATTR] == "逐条检查五路日志"
    assert attrs[AUDIT_RUN_EPOCH_ATTR] == 7
    assert attrs[CONVERSATION_REQUEST_ID_ATTR] == "audit-task-1"


def test_create_subagents_inherits_audit_window_without_overriding_child_value(owner_home):
    parent = SimpleNamespace(
        _current_run_params=SimpleNamespace(
            task_attributes={AUDIT_ATTR: True, AUDIT_WINDOW_ATTR: 30 * 86400}
        )
    )

    inherited = create_task_attributes({"goal": "盯API-1", "attributes": {AUDIT_ATTR: True}}, parent)
    explicit = create_task_attributes(
        {
            "goal": "盯API-2",
            "attributes": {AUDIT_ATTR: True, AUDIT_WINDOW_ATTR: 7 * 86400},
        },
        parent,
    )

    assert inherited[AUDIT_WINDOW_ATTR] == 30 * 86400
    assert explicit[AUDIT_WINDOW_ATTR] == 7 * 86400


def test_create_subagents_inherits_audit_deadline_without_replacing_parent_scope(owner_home):
    parent = SimpleNamespace(
        _current_run_params=SimpleNamespace(
            task_attributes={
                AUDIT_ATTR: True,
                AUDIT_DEADLINE_ATTR: 12345.5,
            }
        ),
        subagents=None,
    )

    attrs = create_task_attributes({"goal": "盯API-1"}, parent)
    policy = _role_policy(
        parent,
        {"goal": "盯API-1"},
        "盯API-1",
        list(CODING_SUBAGENT_TOOLS),
    )

    assert attrs[AUDIT_DEADLINE_ATTR] == 12345.5
    assert policy.allowed_tools == list(CODING_SUBAGENT_TOOLS)


def test_plain_subagent_keeps_existing_tool_policy(owner_home):
    parent = SimpleNamespace(
        _current_run_params=SimpleNamespace(task_attributes={}),
        subagents=None,
    )
    requested = ["read_file", "run_command"]

    policy = _role_policy(parent, {"goal": "普通任务"}, "普通任务", requested)

    assert policy.allowed_tools == requested


def test_create_subagents_no_audit_when_parent_plain(owner_home):
    """父任务非保证档 → 子代理不被误标。"""
    parent = SimpleNamespace(
        _current_run_params=SimpleNamespace(task_attributes={"conversation_task_id": "t"})
    )
    attrs = create_task_attributes({"goal": "盯API-1"}, parent)
    assert AUDIT_ATTR not in attrs


# ── 网关源头盖章 ──


def test_exact_audit_task_drives_background_inheritance_without_owner_pollution(
    owner_home,
):
    """后台保证档只沿精确 task_id 恢复；同 owner 的普通任务不能被另一个 Audit 污染。"""
    from agent.conversation import ConversationStore
    from agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_task_attributes,
    )

    store = ConversationStore(owner_home / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "u-test",
            "channel": "feishu",
            "channel_conversation_id": "c-test",
            "channel_user_id": "u-test",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-task",
            "goal": "盯新增分片",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全巡检",
            "duration_seconds": 30 * 86400,
            "run_epoch": 2,
            "effective_source_bindings": [
                {"source_id": "source-a", "url": "https://example.invalid/a"}
            ],
            "cancellation_scope": "detached",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "plain-task",
            "goal": "普通整理",
            "status": "active",
        }
    )
    agent = SimpleNamespace(conversation_store=store, runtime_guard_policy=None)

    audit_attrs = _background_task_attributes(
        thread.thread_id,
        BackgroundRunRequest(thread_id=thread.thread_id, task_id="audit-task"),
        agent,
    )
    plain_attrs = _background_task_attributes(
        thread.thread_id,
        BackgroundRunRequest(thread_id=thread.thread_id, task_id="plain-task"),
        agent,
    )

    assert audit_attrs is not None and audit_attrs[AUDIT_ATTR] is True
    assert audit_attrs[AUDIT_WINDOW_ATTR] == 30 * 86400
    assert audit_attrs[AUDIT_OBJECTIVE_ATTR] == "盯新增分片"
    link = next(item for item in store.tasks.list(thread.thread_id) if item.task_id == "audit-task")
    assert link is not None
    assert audit_attrs[AUDIT_DEADLINE_ATTR] == link.expires_at
    assert audit_attrs[CONVERSATION_REQUEST_ID_ATTR] == "audit-task"
    assert audit_attrs[AUDIT_RUN_EPOCH_ATTR] == 2
    assert audit_attrs[AUDIT_SOURCE_BINDINGS_ATTR] == [
        {"source_id": "source-a", "url": "https://example.invalid/a"}
    ]
    assert AUDIT_ATTR not in (plain_attrs or {})
    inherited = create_task_attributes(
        {"goal": "盯新增分片"},
        SimpleNamespace(_current_run_params=SimpleNamespace(task_attributes=audit_attrs)),
    )
    assert inherited.get(AUDIT_ATTR) is True


# ── /audit <时长> <名称> <任务> 显式窗口 ──


def test_parse_audit_window_units():
    cases = {
        "/audit 30d 安全巡检 盯这5个源逐条判": 30 * 86400,
        "/audit 999h 长期巡检 不丢": 999 * 3600,
        "/audit 100m 临时巡检 继续盯": 100 * 60,
        "/AUDIT 2D 大写命令 继续盯": 2 * 86400,
        "/audit 1 d 空格时长 继续盯": 86400,
    }
    for text, expected in cases.items():
        command = parse_conversation_control(text)
        assert command is not None and command.valid
        assert command.duration_seconds == expected


def test_parse_audit_window_caps_absurd_value():
    command = parse_conversation_control("/audit 999999d 长期巡检 继续盯")
    assert command is not None
    assert command.duration_seconds == 400 * 86400


def test_open_with_audit_duration_pins_window(owner_home):
    """用户原文 /audit 30d → 开盯守时 watch_window_seconds 被钉成 30 天(用户显式意图,
    模型没传窗口也照钉)。"""
    rp = SimpleNamespace(
        task_attributes=_audit_source_attrs(**{AUDIT_WINDOW_ATTR: 30 * 86400}),
        root_user_prompt="/audit 30d 盯这5个API逐条研判不丢",
    )
    opened = _open(_tool(owner_home, run_params=rp, subagent_run_id="sub-1"))
    assert opened.get("audit_guarantee") is True
    assert ws.list_states(owner_home)[0]["watch_window_seconds"] == 30 * 86400


def test_open_audit_duration_overrides_model_window(owner_home):
    """用户 /audit 30d 与模型传的 watch_window_seconds 冲突时,用户显式意图权威(盖过模型)。"""
    rp = SimpleNamespace(
        task_attributes=_audit_source_attrs(**{AUDIT_WINDOW_ATTR: 30 * 86400}),
        root_user_prompt="/audit 30d 盯API不丢",
    )
    tool = _tool(owner_home, run_params=rp, subagent_run_id="sub-1")
    result = tool.execute({"action": "open", "url": _URL, "watch_window_seconds": 3600})
    assert result.ok, result.output
    assert ws.list_states(owner_home)[0]["watch_window_seconds"] == 30 * 86400


def test_open_audit_deadline_keeps_one_absolute_endpoint_across_reopen(
    owner_home,
    monkeypatch,
):
    now = [1000.0]
    monkeypatch.setattr(wt.time, "time", lambda: now[0])
    rp = SimpleNamespace(
        task_attributes=_audit_source_attrs(
            **{
                AUDIT_WINDOW_ATTR: 600,
                AUDIT_DEADLINE_ATTR: 1120.0,
            }
        ),
        root_user_prompt="",
    )
    tool = _tool(owner_home, run_params=rp, subagent_run_id="sub-1")

    first = _open(tool)
    assert ws.list_states(owner_home)[0]["watch_window_seconds"] == 120
    now[0] = 1010.0
    second = _open(tool)

    assert second["watch_id"] == first["watch_id"]
    state = ws.load_state(owner_home, first["watch_id"])
    assert state is not None
    assert state.opened_at == 1000.0
    assert state.watch_window_seconds == 120


def test_same_audit_reopen_after_deadline_does_not_start_another_window(
    owner_home,
    monkeypatch,
):
    now = [1000.0]
    monkeypatch.setattr(wt.time, "time", lambda: now[0])
    rp = SimpleNamespace(
        task_attributes=_audit_source_attrs(
            **{
                AUDIT_WINDOW_ATTR: 60,
                AUDIT_DEADLINE_ATTR: 1060.0,
            }
        ),
        root_user_prompt="",
    )
    tool = _tool(owner_home, run_params=rp, subagent_run_id="sub-1")

    first = _open(tool)
    now[0] = 1070.0
    second = _open(tool)

    assert second["watch_id"] == first["watch_id"]
    state = ws.load_state(owner_home, first["watch_id"])
    assert state is not None
    assert state.opened_at == 1000.0
    assert state.watch_window_seconds == 60
    assert second["watch"]["window_complete"] is True


def test_same_audit_reopen_after_named_close_remains_closed(owner_home):
    rp = SimpleNamespace(
        task_attributes=_audit_source_attrs(
            **{
                AUDIT_WINDOW_ATTR: 60,
            }
        ),
        root_user_prompt="",
    )
    tool = _tool(owner_home, run_params=rp, subagent_run_id="sub-1")

    opened = _open(tool)
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None
    ws.close_watch_state(state, reason="named_audit_clear")
    reopened = _open(tool)

    assert reopened["watch_id"] == opened["watch_id"]
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None and state.closed is True


def test_published_audit_coordinator_advertises_only_read_surface(owner_home):
    tool = _tool(
        owner_home,
        run_params=SimpleNamespace(
            task_attributes={
                AUDIT_ATTR: True,
                CONVERSATION_REQUEST_ID_ATTR: "audit-root-1",
                AUDIT_SOURCE_BINDINGS_ATTR: [
                    {"source_id": "one", "url": "https://example.invalid/events"}
                ],
            },
            root_user_prompt="",
        ),
    )

    availability = tool.availability()

    assert availability.available is True
    model_spec = tool.model_spec
    properties = model_spec.input_schema["properties"]
    assert properties["action"]["enum"] == [
        "inspect",
        "status",
        "list",
    ]
    assert set(properties) == {
        "action",
        "watch_id",
        "ack_id",
        "source_ref",
    }


def test_watch_stream_surface_follows_typed_worker_phase(owner_home):
    pending = _tool(
        owner_home,
        run_params=SimpleNamespace(task_attributes=_audit_source_attrs()),
        subagent_run_id="source-pending",
    )
    pending_properties = pending.model_spec.input_schema["properties"]
    assert pending_properties["action"]["enum"] == [
        "open",
        "pull",
        "verdict",
        "inspect",
        "status",
        "list",
    ]
    assert "close" not in pending_properties

    audit_id = "audit-root-1"
    watch_id = "ws-0123456789"
    bound_attrs = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: "source-a",
        AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(audit_id, watch_id),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
    }
    bound = _tool(
        owner_home,
        run_params=SimpleNamespace(task_attributes=bound_attrs),
        subagent_run_id="source-bound",
    )
    bound_properties = bound.model_spec.input_schema["properties"]
    assert bound_properties["action"]["enum"] == [
        "pull",
        "verdict",
        "inspect",
        "status",
        "list",
    ]
    assert "url" not in bound_properties
    assert "open" not in bound_properties["action"]["enum"]

    ordinary = _tool(owner_home)
    ordinary_actions = ordinary.model_spec.input_schema["properties"]["action"]["enum"]
    assert "open" in ordinary_actions
    assert "close" in ordinary_actions


def test_audit_prepare_watch_surface_exposes_only_transport_probe_actions(owner_home):
    prepare = _tool(
        owner_home,
        run_params=SimpleNamespace(
            task_attributes={
                CONVERSATION_AUDIT_PREPARE_ATTR: True,
                CONVERSATION_TRANSIENT_WORKSPACE_ATTR: True,
            },
            root_user_prompt="",
        ),
    )

    properties = prepare.model_spec.input_schema["properties"]
    assert properties["action"]["enum"] == [
        "open",
        "pull",
        "inspect",
        "status",
        "close",
        "list",
    ]
    assert "sample_count" not in properties
    assert "spec" not in properties
    assert "judgment_note" not in properties
    assert "source_profile_ref" in properties
    assert "document_refs" in properties
    assert "open 可选" in properties["source_profile_ref"]["description"]
    assert "不要为了满足底座格式专门制造" in properties["source_profile_ref"]["description"]
    assert "cursor_binding 不是顶层参数" in properties["url"]["description"]
    assert "sample(" not in properties["action"]["description"]


def test_single_audit_source_resolves_omitted_watch_id_but_multiple_fail_closed(
    owner_home,
):
    rp = SimpleNamespace(
        task_attributes=_audit_source_attrs(),
        root_user_prompt="",
    )
    tool = _tool(owner_home, run_params=rp, subagent_run_id="sub-1")
    first = _open(tool)

    status = tool.execute({"action": "status"})
    assert status.ok, status.output
    assert json.loads(status.output)["watch_id"] == first["watch_id"]

    second = tool.execute(
        {
            "action": "open",
            "url": f"{_BASE_URL}?source=second&position=<next>&count=<limit>",
        }
    )
    assert second.ok, second.output
    ambiguous = tool.execute({"action": "status"})
    assert not ambiguous.ok
    assert ambiguous.error_code == "TOOL_PARAMETER_REQUIRED"
    assert "多个数据源" in ambiguous.output


def test_cursor_open_strips_only_declared_placeholders_and_preserves_static_query(
    owner_home,
):
    rp = SimpleNamespace(task_attributes=_audit_source_attrs(), root_user_prompt="")
    tool = _tool(owner_home, run_params=rp, subagent_run_id="sub-1")

    result = tool.execute(
        {
            "action": "open",
            "url": (
                f"{_BASE_URL}?tenant=blue&position=%3Cnext%3E&count=%3Climit%3E&limit=999&flag="
            ),
        }
    )

    assert result.ok, result.output
    state = ws.load_state(owner_home, json.loads(result.output)["watch_id"])
    assert state is not None
    assert state.source_url == f"{_BASE_URL}?tenant=blue&limit=999&flag="
    assert state.source_envelope["request"]["cursor_binding"]["name"] == "position"
    assert state.source_envelope["request"]["page_size_binding"]["name"] == "count"


def test_cursor_request_conflicting_with_poll_mode_fails_closed(owner_home):
    """现场说明互相冲突时停止让 Agent 修配置，程序不能自行选一种解释。"""
    rp = SimpleNamespace(task_attributes=_audit_source_attrs(), root_user_prompt="")
    tool = _tool(owner_home, run_params=rp, subagent_run_id="sub-1")

    result = tool.execute(
        {
            "action": "open",
            "url": f"{_BASE_URL}?tenant=blue&position=<next>&count=<limit>",
            "mode": "poll",
        }
    )

    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "poll" in result.output
    assert ws.list_states(owner_home) == []


def test_explicit_poll_mode_is_not_changed_by_a_cursor_shaped_response(owner_home):
    """响应碰巧有 items/next_cursor 也不能覆盖 Agent 现场确认的快照方式。"""
    rp = SimpleNamespace(task_attributes=_audit_source_attrs(), root_user_prompt="")
    tool = _tool(owner_home, run_params=rp, subagent_run_id="sub-1")

    result = tool.execute(
        {
            "action": "open",
            "url": f"{_BASE_URL}?tenant=blue",
            "mode": "poll",
        }
    )

    assert result.ok, result.output
    state = ws.load_state(owner_home, json.loads(result.output)["watch_id"])
    assert state is not None
    assert state.source_url == f"{_BASE_URL}?tenant=blue"
    assert state.source_mode == "poll"
    assert state.source_envelope["mode"] == "poll"
    assert state.source_envelope["record_boundary"] == "whole_response"
    assert "record_list_key" not in state.source_envelope


def test_true_snapshot_poll_without_cursor_envelope_remains_poll(owner_home):
    rp = SimpleNamespace(task_attributes=_audit_source_attrs(), root_user_prompt="")
    tool = _tool(owner_home, run_params=rp, subagent_run_id="sub-1")
    tool.__dict__["_fetch_json"] = lambda _url: (
        True,
        {"service": "ready", "healthy": True},
        "",
    )

    result = tool.execute(
        {
            "action": "open",
            "url": f"{_BASE_URL}?tenant=blue",
            "mode": "poll",
        }
    )

    assert result.ok, result.output
    state = ws.load_state(owner_home, json.loads(result.output)["watch_id"])
    assert state is not None
    assert state.source_mode == "poll"
    assert state.source_envelope["mode"] == "poll"
    assert state.source_envelope["record_boundary"] == "whole_response"


def test_control_start_projects_typed_audit_scope_without_prompt_rescan():
    stamped = project_audit_runtime_attributes(
        {AUDIT_ATTR: True},
        {
            "audit_id": "audit-task",
            "name": "安全巡检",
            "task_path": "/tmp/audit-task",
            "run_epoch": 2,
            "duration_seconds": 30 * 86400,
            "expires_at": 1234.5,
            "effective_prompt": "按已发布要求研判",
            "run_prompt": "本轮启动说明",
            "source_bindings": [],
        },
        thread_id="thread-1",
    )
    assert stamped[AUDIT_ATTR] is True
    assert stamped[AUDIT_WINDOW_ATTR] == 30 * 86400
    assert stamped[AUDIT_DEADLINE_ATTR] == 1234.5
    assert stamped["conversation_task_id"] == "audit-task"
    assert stamped[CONVERSATION_REQUEST_ID_ATTR] == "audit-task"


def test_control_start_does_not_accept_model_supplied_deadline():
    command = parse_conversation_control("/audit 10m demo 逐条检查")
    assert command is not None and command.operation == "start"
    assert command.duration_seconds == 600
    assert not hasattr(command, "attributes")
