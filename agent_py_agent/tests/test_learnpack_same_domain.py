"""learnpack：已经装着她做的同领域能力包时，让用户和她都早点看见（2026-10-07 生产复测：打包回执带着 PACKAGE_BUILD_SAME_DOMAIN，
她还是照装不误、没先问用户"并进去还是单独成包"）。

锁定（真实 SimpleAgent + 真实工具与 /plugins 命令链）：开关关着开单时，安装回执带 same_domain_installed（包名、版本、共同关键词）
和给用户看的 user_notice，message 要她先问用户；用户在 TUI 或 IM 里发确认行装上后，宿主回执末尾带同一句提醒（改成"不想要怎么删"）；
开关开着直接装时回执也带 user_notice；/plugins# 列表给她做的包多一行"同领域"；skill_search 的能力包结果带 made_by_me（她做的为
true，别处来的为 false，不是本机管理员都为 false）。没有同领域的包、只有别处装的同领域包时这些都不出现；读不到安装表时不提醒，
也不影响安装和检索。提醒里最多列 3 个包、每个包最多 5 个共同关键词。
"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.capability import learnpack_domain, skill_search_tool
from agent_py_agent.agent.capability.learnpack_domain import SameDomainPack, same_domain_text
from agent_py_agent.agent.capability.learnpack_installer import STATE_ENABLED
from agent_py_agent.tests.test_learnpack_install import (
    _agent,
    _install,
    _install_foreign_pack,
    _owner,
    _switches,
    _user_manager,
)
from agent_py_agent.tests.test_pack_commands import _im, _tui

_ORDERED = "awaiting_user_confirmation"


def _make(agent, package_id: str, version: str, keywords: list[str]) -> str:
    root = _owner(agent) / "runs" / "2026-10-07" / "learn" / f"{package_id}-{version}"
    root.mkdir(parents=True)
    (root / "CAPABILITY.md").write_text(f"# {package_id} {version}\n先列人物，再分场。\n", encoding="utf-8")
    declaration = {"plugin_id": package_id, "version": version, "summary": "短剧方法",
                   "capability": {"description": "短剧的做法", "keywords": keywords, "entry_document": "CAPABILITY.md"}}
    outcome = agent.tools.tools["package_build"].execute({"kind": "capability_pack", "source_dir": str(root),
                                                          "declaration": declaration, "origin": "自己写的", "license": "自有"})
    assert outcome.ok, outcome.output
    return json.loads(outcome.output)["build"]["sha256"]


def _confirm(agent, line: str) -> dict:
    return _user_manager(agent).command(line, revision="", request_id="")


def _made_by_me(agent, query: str) -> dict[str, bool]:
    outcome = agent.tools.tools["skill_search"].execute({"action": "search", "query": query})
    assert outcome.ok, outcome.output
    return {match["package_id"]: match["made_by_me"] for match in json.loads(outcome.output)["matches"]
            if match["kind"] == "capability_package"}


@pytest.mark.parametrize("entry", [_tui, _im])
def test_order_receipt_and_confirm_reply_name_her_same_domain_pack(tmp_path, monkeypatch, entry):
    agent = _agent(tmp_path, monkeypatch)
    _outcome, first = _install(agent, _make(agent, "drama-scenes", "0.1.0", ["短剧", "分场"]))
    assert first["same_domain_installed"] == [] and "user_notice" not in first and "先问用户" not in first["message"]
    alone = entry(agent, first["user_confirm_command"])
    assert alone["ok"] and "提醒" not in alone["message"], "没有同领域的包就不提醒"
    outcome, body = _install(agent, _make(agent, "drama-pipeline", "1.0.0", ["短剧", "管线"]))
    assert outcome.ok and body["state"] == _ORDERED
    assert body["same_domain_installed"] == [{"package_id": "drama-scenes", "version": "0.1.0", "shared_keywords": ["短剧"]}]
    assert body["user_notice"] == ("提醒：已经装着她做的同领域能力包 drama-scenes 0.1.0（共同关键词：短剧）。"
                                   "想合并成一个，跟她说“把 drama-pipeline 并进 drama-scenes”；要单独装 drama-pipeline，再发确认行。")
    assert "先问用户并进已有的包还是单独成包" in body["message"]
    reply = entry(agent, body["user_confirm_command"])
    assert reply["ok"] and "提醒：已经装着她做的同领域能力包 drama-scenes 0.1.0（共同关键词：短剧）。" in reply["message"]
    assert reply["message"].endswith("不想要 drama-pipeline，发 /plugins#drama-pipeline 删除。"), reply
    listing = entry(agent, "/plugins#")["message"]
    assert "- drama-pipeline 1.0.0（启用，她做的）\n" in listing, listing
    assert "\n  同领域：drama-scenes 0.1.0（共同关键词：短剧）" in listing
    assert "\n  同领域：drama-pipeline 1.0.0（共同关键词：短剧）" in listing, "两个包互相标出来"


def test_auto_install_receipt_carries_the_notice(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    first = _install(agent, _make(agent, "drama-scenes", "0.1.0", ["短剧", "分场"]))[1]
    assert first["state"] == STATE_ENABLED and first["same_domain_installed"] == [] and "user_notice" not in first
    body = _install(agent, _make(agent, "drama-pipeline", "1.0.0", ["分场", "管线", "短剧"]))[1]
    assert body["state"] == STATE_ENABLED and body["same_domain_installed"][0]["shared_keywords"] == ["分场", "短剧"]
    assert body["user_notice"].startswith("提醒：已经装着她做的同领域能力包 drama-scenes 0.1.0（共同关键词：分场、短剧）。")
    assert body["user_notice"].endswith("不想要 drama-pipeline，发 /plugins#drama-pipeline 删除。")


def test_foreign_packs_are_not_same_domain_and_not_made_by_her(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _install_foreign_pack(agent, tmp_path)  # 别处来的 drama-scenes 9.9.9，关键词"短剧"；管理员装上后要再启用
    manager = _user_manager(agent)
    assert manager.command("/plugins#drama-scenes 启用", revision=manager.catalog().revision, request_id="enable-1")["ok"]
    _outcome, body = _install(agent, _make(agent, "drama-pipeline", "1.0.0", ["短剧", "管线"]))
    assert body["same_domain_installed"] == [] and "user_notice" not in body, "别处装的包并不进去，不算同领域"
    assert _confirm(agent, body["user_confirm_command"])["ok"]
    assert _made_by_me(agent, "短剧") == {"drama-scenes": False, "drama-pipeline": True}


def test_made_by_me_is_false_outside_the_local_admin_or_without_facts(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_access

    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    assert _install(agent, _make(agent, "drama-scenes", "0.1.0", ["短剧"]))[1]["state"] == STATE_ENABLED
    assert _made_by_me(agent, "短剧") == {"drama-scenes": True}

    def unreadable(*_args):
        raise OSError("安装表读不了")

    with monkeypatch.context() as patch:
        patch.setattr(skill_search_tool, "self_made_package_ids", unreadable)
        assert _made_by_me(agent, "短剧") == {"drama-scenes": False}, "读不到就当不是她做的，检索照常"
    monkeypatch.setattr(owner_access, "is_complete_local_admin_owner", lambda _home: False)
    assert _made_by_me(agent, "短剧") == {"drama-scenes": False}


def test_an_unreadable_install_table_after_install_drops_only_the_notice(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _confirm(agent, _install(agent, _make(agent, "drama-scenes", "0.1.0", ["短剧"]))[1]["user_confirm_command"])
    body = _install(agent, _make(agent, "drama-pipeline", "1.0.0", ["短剧"]))[1]
    assert body["user_notice"]

    class _Unreadable:
        def __init__(self, _owner) -> None:
            pass

        def snapshot(self):
            raise ValueError("安装表坏了")

    monkeypatch.setattr(learnpack_domain, "PluginInstallStore", _Unreadable)
    reply = _confirm(agent, body["user_confirm_command"])
    assert reply["ok"] and reply["state"] == STATE_ENABLED and "提醒" not in reply["message"], "提醒读不到不影响装上"


def test_notice_lists_at_most_three_packs_and_five_keywords():
    words = ("一", "二", "三", "四", "五", "六")
    packs = [SameDomainPack(f"pack-{index}", "1.0.0", words) for index in range(4)]
    text = same_domain_text(packs)
    assert text.count("pack-") == 3 and "pack-3" not in text
    assert "（共同关键词：一、二、三、四、五）" in text and "六" not in text
