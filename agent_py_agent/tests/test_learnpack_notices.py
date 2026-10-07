"""learnpack：她自己装上包时宿主直接告诉用户（2026-10-07 生产复测：安装回执里的 user_notice 她不转告；用户担心开关开着时她陆续
装了几十个包自己都不知道）。

锁定：开关开着、在会话里自己装上能力包时，会话里排一条宿主提示（来源 learnpack:<包名>、码 self_installed，details 带本轮请求编号），
正文写装了什么、开关开着所以没问、和她做的哪些已装包同领域与怎么合并、怎么删、怎么关自动装；回执 host_notice_queued=true；同一个包
再装只留最新一条。没有会话（直接调工具）不排、回执 false。停在用户确认时正文带确认命令原文；插件用
/plugins 写法；装失败不排。真实回合（假模型 + 真实工具循环）里装包后，提示进入同一会话。
开关关着她开安装单时（第 2 项），宿主把现在还有效的确认行排成一条提示（来源 learnpack:orders、码 orders_open），以这条为准：执行过的单、
被同一个包后来的单取代的单都不列；最新的在前、最多列 3 张，多的只报张数；新单和她做的已装包同领域时说怎么合并；回执 open_orders
列同一批单，message 要她不要再给以前对话里的旧行；同一份字节已经装着的单（开关打开后她直接装了）也不再列。
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from agent_py_agent.agent.capability.learnpack_installer import (
    STATE_ENABLED,
    STATE_FAILED,
    STATE_NEEDS_USER,
)
from agent_py_agent.agent.capability.learnpack_notices import (
    queue_self_install_notice,
    self_install_text,
)
from agent_py_agent.agent.conversation.host_notices import pending_host_notices
from agent_py_agent.tests.test_learnpack_install import _agent, _install, _switches
from agent_py_agent.tests.test_learnpack_merge import _LOAD, _Turns
from agent_py_agent.tests.test_learnpack_same_domain import _confirm, _make

_PIPELINE = ("她自己装上了能力包 drama-pipeline 1.0.0（能力包自动装开关开着，所以没问你）。"
             "它和她做的 drama-scenes 0.1.0 同领域，想合并就跟她说“把 drama-pipeline 并进 drama-scenes”。"
             "要删就发 /plugins#drama-pipeline 删除；/plugins# 能看到全部。"
             "不想让她自己装，发 /settings set capability_pack_self_install_enabled false")


# 和真实回合一样：本轮运行参数带会话编号与 Gateway 请求编号（运行参数按线程挂，工具在同一线程里执行）。
def _in_turn(agent, request_id: str = "req-1") -> str:
    thread = agent.conversation_store.threads.get_or_create(
        {"canonical_user_id": "learnpack-user", "channel": "internal", "channel_conversation_id": "notices"})
    agent._current_run_params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id},
                                                request_id=request_id)
    return thread.thread_id


def test_self_install_tells_the_user_directly_once_per_pack(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    first = _install(agent, _make(agent, "drama-scenes", "0.1.0", ["短剧", "分场"]))[1]
    assert first["state"] == STATE_ENABLED and first["host_notice_queued"] is False, "没有会话就不排"
    thread_id = _in_turn(agent)
    body = _install(agent, _make(agent, "drama-pipeline", "1.0.0", ["短剧", "管线"]))[1]
    assert body["state"] == STATE_ENABLED and body["host_notice_queued"] is True and body["open_orders"] == []
    [notice] = pending_host_notices(agent.conversation_store, thread_id)
    assert (notice.source, notice.code, notice.text) == ("learnpack:drama-pipeline", "self_installed", _PIPELINE)
    assert dict(notice.details) == {"package_id": "drama-pipeline", "version": "1.0.0", "kind": "capability_pack",
                                    "sha256": body["package"]["sha256"][:12], "same_domain_count": "1",
                                    "turn_request_id": "req-1"}
    _install(agent, _make(agent, "drama-pipeline", "1.0.1", ["短剧", "管线"]))
    [again] = pending_host_notices(agent.conversation_store, thread_id)
    assert again.text.startswith("她自己装上了能力包 drama-pipeline 1.0.1"), "同一个包只留最新一条"


def test_an_order_notice_lists_only_lines_that_still_work_newest_first(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)

    def order(package_id, version, keywords=("长篇",)):
        return _install(agent, _make(agent, package_id, version, list(keywords)))[1]

    done = order("drama-scenes", "0.1.0", ("短剧", "分场"))
    assert _confirm(agent, done["user_confirm_command"])["ok"], "执行过的单不再列"
    older = order("novel-notes", "0.1.0")
    stale = order("poem-notes", "0.1.0")
    newer = order("poem-notes", "0.2.0")
    song = order("song-notes", "0.1.0")
    assert stale["order_id"] not in [row["order_id"] for row in newer["open_orders"]], "被同一个包后来的单取代的不再列"
    thread_id = _in_turn(agent)
    body = order("drama-pipeline", "1.0.0", ("短剧", "管线"))
    ids = [body["order_id"], song["order_id"], newer["order_id"], older["order_id"]]
    assert [row["order_id"] for row in body["open_orders"]] == ids and body["host_notice_queued"] is True
    assert body["open_orders"][0]["confirm_line"] == body["user_confirm_command"]
    assert "以前对话里给过的旧行可能已经执行过或作废了，不要再给" in body["message"]
    [notice] = pending_host_notices(agent.conversation_store, thread_id)
    assert (notice.source, notice.code) == ("learnpack:orders", "orders_open")
    assert notice.text == (
        "她做好了能力包 drama-pipeline 1.0.0，还没装。等你确认的安装单以这条为准（她以前给过的其它行可能已经执行过或作废了，"
        f"每行只执行一次）：drama-pipeline 1.0.0：/plugins#drama-pipeline 安装 {ids[0]}；song-notes 0.1.0：/plugins#song-notes 安装 {ids[1]}；"
        f"poem-notes 0.2.0：/plugins#poem-notes 安装 {ids[2]}（还有 1 张更早的单没列出）。"
        "新做的 drama-pipeline 和她做的 drama-scenes 0.1.0 同领域，想合并就别发它那行，跟她说“把 drama-pipeline 并进 drama-scenes”。")
    assert dict(notice.details)["order_ids"] == ",".join(ids) and dict(notice.details)["open_count"] == "4"


def test_an_order_whose_bytes_are_already_installed_no_longer_waits(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    sha = _make(agent, "drama-scenes", "0.1.0", ["短剧"])
    ordered = _install(agent, sha)[1]
    assert [row["order_id"] for row in ordered["open_orders"]] == [ordered["order_id"]]
    _switches(agent, pack=True)
    body = _install(agent, sha)[1]
    assert body["state"] == STATE_ENABLED and body["open_orders"] == [], "开关打开后她直接装了同一份，这张单不用再确认"


def test_wording_for_plugins_waiting_for_the_user_and_failures(tmp_path, monkeypatch):
    plugin = SimpleNamespace(kind="plugin", package_id="dialogue-length-check", version="0.1.0", sha256="a" * 64)
    waiting = SimpleNamespace(state=STATE_NEEDS_USER, next_command="/plugins enable dialogue-length-check --confirm K7Q2")
    assert self_install_text(plugin, waiting, []) == (
        "她自己装上了插件 dialogue-length-check 0.1.0（插件自动装开关开着，所以没问你），还没启用：启用要你确认，"
        "确认就发 /plugins enable dialogue-length-check --confirm K7Q2。要删就发 /plugins remove dialogue-length-check；"
        "/plugins list 能看到全部。不想让她自己装，发 /settings set plugin_self_install_enabled false")
    agent = _agent(tmp_path, monkeypatch)
    thread_id = _in_turn(agent)
    assert queue_self_install_notice(agent, plugin, SimpleNamespace(state=STATE_FAILED, next_command=""), []) is False
    assert queue_self_install_notice(agent, plugin, waiting, []) is True
    [notice] = pending_host_notices(agent.conversation_store, thread_id)
    assert notice.code == "self_installed_needs_user" and "--confirm K7Q2" in notice.text


def test_a_real_turn_puts_the_notice_into_that_conversation(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    sha = _make(agent, "drama-scenes", "0.1.0", ["短剧"])
    model = _Turns({"AUTO-1": [_LOAD, ("package_install", {"sha256": sha}), "装好了。"]})
    agent.backend = model
    thread = agent.conversation_store.threads.get_or_create(
        {"canonical_user_id": "learnpack-user", "channel": "internal", "channel_conversation_id": "real-turn"})
    agent.run("AUTO-1 把刚打好的短剧包装上", task_attributes={"conversation_thread_id": thread.thread_id}, source="gateway")
    assert re.search(r'host_notice_queued\\*": true', model.seen["AUTO-1"]), "回执写明程序已经告诉用户"
    [notice] = pending_host_notices(agent.conversation_store, thread.thread_id)
    assert notice.source == "learnpack:drama-scenes" and notice.text.startswith("她自己装上了能力包 drama-scenes 0.1.0")
