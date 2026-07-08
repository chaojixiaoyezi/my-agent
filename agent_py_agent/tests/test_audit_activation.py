"""/audit 结构化激活(真产品激活路径,非直接 set audit_guarantee=True)。

治测试方 1.9 网关实测缺口:发 /audit 真任务,盯守委派给子代理做,子代理 goal 空、/audit 词元
落在 runner_prompt 里、后台唤醒轮 prompt 被回填 → 旧激活(只查 _current_user_prompt+goal 词元)
两条都落空 → /audit 静默没激活整轮跑 triage。harness+队列机制单测都漏这条,因为它们直接
set audit_guarantee=True 绕过激活层。

本文件专钉【激活层】:走 watch_stream(action=open) 真入口,验证结构化信号(task_attributes /
root_user_prompt)能确定性激活,且默认档不误开。铁律:激活用结构化确定性信号,不靠模型传参/
不靠词元恰好落 goal。
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
from agent.common.audit_activation import AUDIT_ATTR, attributes_request_audit, text_requests_audit
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
    assert text_requests_audit("盯这5个API几个月 /audit 不丢数据")
    assert text_requests_audit("/audit")
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
    """核心修法:task_attributes 带结构化保证档标志 → 激活,【不需要】audit 参数、
    不需要任何文本里有 /audit 词元(跨轮/委派可靠的那条路)。"""
    rp = SimpleNamespace(task_attributes={AUDIT_ATTR: True}, root_user_prompt="")
    opened = _open(_tool(owner_home, run_params=rp))
    assert opened.get("audit_guarantee") is True


def test_activate_via_root_user_prompt_token(owner_home):
    """前台创建路:root_user_prompt 是用户原文且含 /audit → 激活(词元兜底)。"""
    rp = SimpleNamespace(task_attributes=None, root_user_prompt="盯这5个API /audit 逐条研判不丢")
    opened = _open(_tool(owner_home, run_params=rp))
    assert opened.get("audit_guarantee") is True


def test_no_activation_without_any_signal(owner_home):
    """无 audit 参数、无结构化标志、无词元 → 默认档(不误开);档位一眼可见 false。"""
    rp = SimpleNamespace(task_attributes={"conversation_task_id": "t1"}, root_user_prompt="盯这5个API报异常")
    opened = _open(_tool(owner_home, run_params=rp, user_prompt="盯这5个API报异常"))
    assert opened.get("audit_guarantee") in (None, False)
    # status 也一眼可见默认档
    status = json.loads(_tool(owner_home, run_params=rp).execute(
        {"action": "status", "watch_id": opened["watch_id"]}).output)
    assert status["audit_guarantee"] is False


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


def test_gateway_stamps_audit_intent():
    from agent.gateway_parts.request_execution import _stamp_audit_intent

    stamped = _stamp_audit_intent({"conversation_task_id": "t1"}, "盯这5个API /audit 几个月不丢")
    assert stamped[AUDIT_ATTR] is True and stamped["conversation_task_id"] == "t1"
    # 非 /audit 任务不动(不误开)
    assert _stamp_audit_intent({"conversation_task_id": "t1"}, "盯这5个API报异常") == {"conversation_task_id": "t1"}
    # 空属性 + /audit → 也建出带标志的属性
    assert _stamp_audit_intent(None, "/audit 盯它")[AUDIT_ATTR] is True
