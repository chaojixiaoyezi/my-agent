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

import pytest
from agent.agent_core.orchestration.create_policy import _create_attributes
from agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_WINDOW_ATTR,
    attributes_request_audit,
    parse_audit_window_seconds,
    text_requests_audit,
)
from agent.ingestion import watch_state as ws
from agent.ingestion import watch_tool as wt
from agent.ingestion.watch_tool import WatchStreamTool

_URL = "http://127.0.0.1:9/pull"


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    return tmp_path / "owner"


def _fetch_ok(url: str) -> tuple[bool, object, str]:
    return True, {"items": [], "next_cursor": 0}, ""


def _tool(owner_home, *, run_params=None, user_prompt="", subagent_run_id=""):
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"),
        _current_user_prompt=user_prompt,
        _current_run_params=run_params,
        _current_subagent_run_id=subagent_run_id,
        subagents=None,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool.__dict__["_fetch_json"] = _fetch_ok
    return tool


def _open(tool) -> dict:
    result = tool.execute({"action": "open", "url": _URL})
    assert result.ok, result.output
    return json.loads(result.output)


# ── 判据模块本体 ──


def test_token_detects_slash_audit_word_boundary():
    assert text_requests_audit("/audit 盯这5个API几个月不丢数据")
    assert text_requests_audit("/audit")
    assert not text_requests_audit("盯这5个API几个月 /audit 不丢数据")
    assert not text_requests_audit("audit the logs")   # 无斜杠不算
    assert not text_requests_audit("/auditing")        # 词边界:/audit 后须断词
    assert not text_requests_audit("")


def test_attributes_flag_detects_structural():
    assert attributes_request_audit({AUDIT_ATTR: True})
    assert not attributes_request_audit({AUDIT_ATTR: False})
    assert not attributes_request_audit({})
    assert not attributes_request_audit(None)


# ── 主代理激活路径 ──


def test_activate_via_task_attributes_structural(owner_home):
    """核心修法:task_attributes 带结构化保证档标志 → 激活，不需要下游重新扫描
    任何文本里的 /audit 词元(跨轮/委派可靠的那条路)。"""
    rp = SimpleNamespace(task_attributes={AUDIT_ATTR: True}, root_user_prompt="")
    opened = _open(_tool(owner_home, run_params=rp))
    assert opened.get("audit_guarantee") is True


def test_root_prompt_text_is_not_a_downstream_authority_fallback(owner_home):
    """下游 watch 只认入口盖章后的结构化属性，不重新扫描提示词。"""
    rp = SimpleNamespace(task_attributes=None, root_user_prompt="/audit 盯这5个API逐条研判不丢")
    opened = _open(_tool(owner_home, run_params=rp))
    assert opened.get("audit_guarantee") in (None, False)


def test_no_activation_without_any_signal(owner_home):
    """无结构化标志时保持默认档；模型不能自行把普通任务升级成保证档。"""
    rp = SimpleNamespace(task_attributes={"conversation_task_id": "t1"}, root_user_prompt="盯这5个API报异常")
    opened = _open(_tool(owner_home, run_params=rp, user_prompt="盯这5个API报异常"))
    assert opened.get("audit_guarantee") in (None, False)
    # status 也一眼可见默认档
    status = json.loads(_tool(owner_home, run_params=rp).execute(
        {"action": "status", "watch_id": opened["watch_id"]}).output)
    assert status["audit_guarantee"] is False


def test_model_tool_param_cannot_enable_audit_without_structured_command(owner_home):
    """旧 audit=1 工具入口已撤销；只有 Gateway 盖章后的 task_attributes 有权限开启特殊模式。"""
    rp = SimpleNamespace(task_attributes={}, root_user_prompt="普通盯守")
    tool = _tool(owner_home, run_params=rp)

    result = tool.execute({"action": "open", "url": _URL, "audit": 1})

    assert result.ok
    assert json.loads(result.output).get("audit_guarantee") in (None, False)


# ── 委派子代理激活路径(测试方挖的确切场景)──


def test_activate_in_subagent_with_empty_goal(owner_home, monkeypatch):
    """测试方实锤场景回归:盯守委派给子代理,子代理 goal 空、/audit 不在 goal 里,但父任务
    保证档标志经 task.attributes 继承进 runner 上下文 → 子代理开 watch 照样激活保证档。
    这正是旧实现(只查 goal 词元)漏掉、导致 6 watch 全 None 的洞。"""
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"),
        _current_user_prompt="",              # 唤醒轮无用户原文
        _current_run_params=SimpleNamespace(task_attributes=None, root_user_prompt=""),
        subagents=None,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool.__dict__["_fetch_json"] = _fetch_ok
    # runner 起子代理:从 task.attributes 设线程本地上下文(含继承来的保证档标志)
    prev = set_current_subagent_context(agent, run_id="sub-1", task_attributes={AUDIT_ATTR: True})
    try:
        opened = _open(tool)
    finally:
        restore_current_subagent_context(agent, prev)
    assert opened.get("audit_guarantee") is True


# ── 契约沿 spawn 树继承(create_subagents 层)──


def test_create_subagents_inherits_audit_from_parent(owner_home):
    """父任务(主代理)保证档 → 派出的子代理 task.attributes 自动带保证档标志(结构化继承,
    不靠 item 的 goal 是否写了 /audit)。委派做盯守的判读子代理照样在保证档。"""
    parent = SimpleNamespace(_current_run_params=SimpleNamespace(task_attributes={AUDIT_ATTR: True}))
    attrs = _create_attributes({"goal": "盯API-1"}, parent)  # item 里没写 /audit
    assert attrs.get(AUDIT_ATTR) is True


def test_create_subagents_inherits_audit_window_without_overriding_child_value(owner_home):
    parent = SimpleNamespace(
        _current_run_params=SimpleNamespace(
            task_attributes={AUDIT_ATTR: True, AUDIT_WINDOW_ATTR: 30 * 86400}
        )
    )

    inherited = _create_attributes({"goal": "盯API-1", "attributes": {AUDIT_ATTR: True}}, parent)
    explicit = _create_attributes(
        {
            "goal": "盯API-2",
            "attributes": {AUDIT_ATTR: True, AUDIT_WINDOW_ATTR: 7 * 86400},
        },
        parent,
    )

    assert inherited[AUDIT_WINDOW_ATTR] == 30 * 86400
    assert explicit[AUDIT_WINDOW_ATTR] == 7 * 86400


def test_create_subagents_no_audit_when_parent_plain(owner_home):
    """父任务非保证档 → 子代理不被误标。"""
    parent = SimpleNamespace(_current_run_params=SimpleNamespace(task_attributes={"conversation_task_id": "t"}))
    attrs = _create_attributes({"goal": "盯API-1"}, parent)
    assert AUDIT_ATTR not in attrs


# ── 网关源头盖章 ──


def test_owner_audit_watch_drives_background_inheritance(owner_home):
    """残留边界闭合:主代理在后台唤醒轮新派判读子代理时,后台 _run_params 词元/前台 attributes
    都不在——从 owner 已有的保证档 watch(持久棘轮)反推,盖回后台 task_attributes,让新派子代理
    照样继承。owner 无保证档 watch 则不盖(默认档不误开)。"""
    from agent.conversation.runtime import _background_audit_attributes
    from agent.ingestion.watch_state import new_state, owner_home_has_audit_watch, persist_state

    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home)))
    assert not owner_home_has_audit_watch(owner_home)          # 还没有 watch
    assert _background_audit_attributes(agent) is None
    # 落一个保证档 watch(持久棘轮)
    st = new_state(owner_home, "http://src/pull", {})
    st.audit_guarantee = True
    persist_state(st)
    assert owner_home_has_audit_watch(owner_home) is True
    assert _background_audit_attributes(agent) == {AUDIT_ATTR: True}
    # 该后台 attributes 喂给 create_subagents → 新派子代理继承
    attrs = _create_attributes({"goal": "盯新增分片"}, SimpleNamespace(
        _current_run_params=SimpleNamespace(task_attributes=_background_audit_attributes(agent))))
    assert attrs.get(AUDIT_ATTR) is True


# ── /audit <时长> 显式窗口(/audit 30d 语法,同 /loop 的间隔)──


def test_parse_audit_window_units():
    assert parse_audit_window_seconds("/audit 30d 盯这5个源逐条判") == 30 * 86400
    assert parse_audit_window_seconds("/audit 999h 不丢") == 999 * 3600
    assert parse_audit_window_seconds("/audit 100m") == 100 * 60
    assert parse_audit_window_seconds("/AUDIT 2D") == 2 * 86400        # 大小写不敏感
    assert parse_audit_window_seconds("/audit 1 d") == 86400           # 数字与单位间空格容忍


def test_parse_audit_window_bare_or_absent_is_none():
    assert parse_audit_window_seconds("/audit 盯这5个源逐条判") is None  # 裸 /audit=无窗口
    assert parse_audit_window_seconds("/audit") is None
    assert parse_audit_window_seconds("盯API报异常") is None            # 无 /audit
    assert parse_audit_window_seconds("") is None
    assert parse_audit_window_seconds("/auditing 30d") is None         # 词边界:/auditing 不算


def test_parse_audit_window_caps_absurd_value():
    assert parse_audit_window_seconds("/audit 999999d") == 400 * 86400  # 上限 400 天防误写


def test_open_with_audit_duration_pins_window(owner_home):
    """用户原文 /audit 30d → 开盯守时 watch_window_seconds 被钉成 30 天(用户显式意图,
    模型没传窗口也照钉)。"""
    rp = SimpleNamespace(
        task_attributes={AUDIT_ATTR: True, AUDIT_WINDOW_ATTR: 30 * 86400},
        root_user_prompt="/audit 30d 盯这5个API逐条研判不丢",
    )
    opened = _open(_tool(owner_home, run_params=rp))
    assert opened.get("audit_guarantee") is True
    assert ws.list_states(owner_home)[0]["watch_window_seconds"] == 30 * 86400


def test_open_bare_audit_stays_windowless(owner_home):
    """裸 /audit(无时长)→ 无窗口(watch_window_seconds=0),判到 close 为止(补岗按积压兜底)。"""
    rp = SimpleNamespace(
        task_attributes={AUDIT_ATTR: True},
        root_user_prompt="/audit 盯这5个API逐条研判不丢",
    )
    opened = _open(_tool(owner_home, run_params=rp))
    assert opened.get("audit_guarantee") is True
    assert int(ws.list_states(owner_home)[0]["watch_window_seconds"] or 0) == 0


def test_open_audit_duration_overrides_model_window(owner_home):
    """用户 /audit 30d 与模型传的 watch_window_seconds 冲突时,用户显式意图权威(盖过模型)。"""
    rp = SimpleNamespace(
        task_attributes={AUDIT_ATTR: True, AUDIT_WINDOW_ATTR: 30 * 86400},
        root_user_prompt="/audit 30d 盯API不丢",
    )
    tool = _tool(owner_home, run_params=rp)
    result = tool.execute({"action": "open", "url": _URL, "watch_window_seconds": 3600})
    assert result.ok, result.output
    assert ws.list_states(owner_home)[0]["watch_window_seconds"] == 30 * 86400


def test_gateway_stamps_audit_intent():
    from agent.gateway_parts.request_execution import _stamp_audit_intent

    stamped = _stamp_audit_intent({"conversation_task_id": "t1"}, "/audit 盯这5个API几个月不丢")
    assert stamped[AUDIT_ATTR] is True and stamped["conversation_task_id"] == "t1"
    # 非 /audit 任务不动(不误开)
    assert _stamp_audit_intent({"conversation_task_id": "t1"}, "盯这5个API报异常") == {"conversation_task_id": "t1"}
    # 空属性 + /audit → 也建出带标志的属性
    assert _stamp_audit_intent(None, "/audit 盯它")[AUDIT_ATTR] is True
    timed = _stamp_audit_intent(None, "/audit 30d 盯它")
    assert timed[AUDIT_WINDOW_ATTR] == 30 * 86400
    assert _stamp_audit_intent(None, "先聊聊 /audit 30d") is None
