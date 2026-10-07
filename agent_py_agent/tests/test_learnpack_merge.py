"""learnpack 第 6 步：同领域合并（假模型 + 真实 SimpleAgent 回合 + 真实工具与 /plugins# 命令，只有模型是脚本）。

锁定：已装 drama-scenes 0.1.0（分场）后再学同领域的台词润色方法：她用 skill_search 查到已装的同领域包、先问用户"并进去还是
单独成包"；用户说并进去后出新版本 0.2.0（同一个包名，入口按方向分节，来源和许可证按方向分开保留、打包时按方向列全），自动装
开关关着只给确认行，装不装仍看确认；用户确认后装上的是 0.2.0、两个方向都在；/plugins#drama-scenes 退回（先预览、再发那一行）
回到合并前的 0.1.0，合并后 0.1.0 的文件 0.2.0 里都还在。打包回执写现在装着的版本和她做过的版本，三种软提醒都不挡打包：
和装上过的某一版同号、字节不同（PACKAGE_BUILD_VERSION_REUSED；没装过的同号重打不提醒）；比现在装着的旧（PACKAGE_BUILD_VERSION_OLDER，
只比纯数字点分的版本号）；比上一版少了文件（PACKAGE_BUILD_FILES_DROPPED）；包名被别处的同名包占着时只报 PACKAGE_BUILD_ID_TAKEN
（不报版本提醒），回执写明装着的是不是她做的。找旧包超过上限时看最近的记录。
新能力包和她自己做的、装着的另一个能力包声明的关键词重合（casefold 整词）时提醒 PACKAGE_BUILD_SAME_DOMAIN（先问用户并进去还是
单独成包；生产测试里她没问就另起了一个包），同名包和别处装的包不算。说明是中文这类非拉丁文字、关键词却全是拉丁字母时提醒
PACKAGE_BUILD_KEYWORDS_SCRIPT（生产测试里小说包关键词全是英文，中文提问推荐不到）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso
from agent_py_agent.agent.capability.learnpack_store import LearnpackStore
from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import (
    owner_identity_from_config,
    resolve_owner_home,
)

_SCENES = "# 短剧方法\n\n## 方向一：分场\n来源：github.com/example/drama-agent@abc（MIT）\n- 把短剧故事拆成场次\n"
_DIALOGUE = "\n## 方向二：台词润色\n来源：github.com/example/dialogue-agent@def（Apache-2.0）\n- 按人物口吻润色台词\n"
_LOAD = ("tool_search", {"query": "package_build package_install 打包 安装 能力包", "limit": 5})
_SHA = re.compile(r"[0-9a-f]{64}")
_CONFIRM = re.compile(r"/plugins#drama-scenes 安装 lp-[0-9a-f]{8}")


def _build(source_dir: str, version: str, origin: str, license_name: str) -> tuple[str, dict]:
    keywords = ["短剧", "分场"] + (["台词"] if version != "0.1.0" else [])
    declaration = {"plugin_id": "drama-scenes", "version": version, "summary": "短剧分场与台词方法",
                   "capability": {"description": "把短剧故事拆成场次" + ("，并按人物口吻润色台词" if version != "0.1.0" else ""),
                                  "keywords": keywords, "entry_document": "CAPABILITY.md"}}
    return "package_build", {"kind": "capability_pack", "source_dir": source_dir, "declaration": declaration,
                             "origin": origin, "license": license_name}


def _install(seen: str) -> tuple[str, dict]:
    return "package_install", {"sha256": _SHA.findall(seen)[-1]}


def _hand_over(seen: str) -> str:
    return "做好了，自动装开关关着所以还没装。要装就把这一行发给我：\n" + _CONFIRM.findall(seen)[-1]


_TURNS = {
    "LEARN-1": [("write_file", {"path": "v1/CAPABILITY.md", "content": _SCENES}),
                ("write_file", {"path": "v1/provenance/scenes.md", "content": "原项目：github.com/example/drama-agent@abc\n许可证：MIT\n"}),
                _LOAD, _build("v1", "0.1.0", "github.com/example/drama-agent@abc", "MIT"), _install, _hand_over],
    "LEARN-2": [("skill_search", {"action": "search", "query": "短剧 台词"}),
                "已有同领域的能力包 drama-scenes（短剧分场）。要并进 drama-scenes（新增“台词润色”方向），还是单独成包？"],
    "LEARN-3": [("write_file", {"path": "v2/CAPABILITY.md", "content": _SCENES + _DIALOGUE}),
                ("write_file", {"path": "v2/provenance/scenes.md", "content": "原项目：github.com/example/drama-agent@abc\n许可证：MIT\n"}),
                ("write_file", {"path": "v2/provenance/dialogue.md",
                                "content": "原项目：github.com/example/dialogue-agent@def\n许可证：Apache-2.0\n"}),
                _LOAD, _build("v2", "0.2.0", "分场：github.com/example/drama-agent@abc；台词润色：github.com/example/dialogue-agent@def",
                              "分场 MIT；台词润色 Apache-2.0"), _install, _hand_over],
}


# 类用途: 按回合出招的假模型：回合按提问里的标记区分（从后往前找，带着对话历史时最新一轮优先）；每步是固定工具调用，或按已看到的
#   上下文现算（取包指纹、确认行），最后一步是给用户的话。
class _Turns:
    name = "learnpack_merge_model"

    def __init__(self, turns: dict[str, list]) -> None:
        self.turns, self.steps, self.seen, self.replies = turns, dict.fromkeys(turns, 0), {}, {}

    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint="local://merge", model="", stream=False,
                                      native_supported=True, evidence="test_native_tools", observed_at=_utc_now_iso())

    def generate(self, prompt, on_chunk=None, **kwargs):
        seen = str(prompt) + json.dumps(kwargs.get("messages"), ensure_ascii=False, default=str)
        key = next(key for key in reversed(self.turns) if key in seen)
        step, self.seen[key] = self.steps[key], seen
        self.steps[key] += 1
        action = self.turns[key][step] if step < len(self.turns[key]) else "好了。"
        action = action(seen) if callable(action) else action
        if isinstance(action, tuple):
            return ModelResponse(text="", backend=self.name,
                                 tool_use_blocks=[{"id": f"{key}-{step}", "name": action[0], "input": action[1]}])
        self.replies[key] = action
        return ModelResponse(text=action, backend=self.name)


def _agent(tmp_path, monkeypatch) -> SimpleAgent:
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_owner_provider="local",
                                   my_agent_owner_kind="main", my_agent_owner_id="main"), tmp_path / "project")


# 和真 TUI 一样：每条命令带当前目录版本和一个新请求编号。
def _tui(agent, text) -> dict:
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    manager = PluginManagement(plugin_management_context(owner, agent.home_paths, agent.config, agent.conversation_store.threads,
                                                         actor_id="local-agent", channel="chat", conversation_id="tui-1",
                                                         is_admin=True))
    return manager.command(text, revision=manager.catalog().revision, request_id=f"tui-{uuid.uuid4().hex}")


def _installed(agent) -> tuple[str, str]:
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    row = next(row for row in PluginInstallStore(owner).snapshot() if row.manifest.plugin_id == "drama-scenes")
    path = LearnpackStore(agent.home_paths.owner_home_dir).build_path(row.package_sha256)
    with zipfile.ZipFile(path) as archive:
        names = sorted(archive.namelist())
        entry = archive.read("CAPABILITY.md").decode("utf-8")
    return row.manifest.version, entry + "\n" + "\n".join(names)


def test_merge_into_the_same_domain_pack_bumps_the_version_and_reverts(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    model = _Turns(_TURNS)
    agent.backend = model
    thread = agent.conversation_store.threads.get_or_create(
        {"canonical_user_id": "learnpack-user", "channel": "internal", "channel_conversation_id": "merge"})

    def ask(text):
        agent.run(text, task_attributes={"conversation_thread_id": thread.thread_id}, source="gateway")

    ask("LEARN-1 学一下 drama-agent 的短剧分场方法，做成能力包")
    assert _tui(agent, _CONFIRM.findall(model.replies["LEARN-1"])[-1])["ok"]
    assert _installed(agent)[0] == "0.1.0"
    ask("LEARN-2 再学一下 dialogue-agent 的短剧台词润色方法")
    assert "tool=skill_search; status=succeeded" in model.seen["LEARN-2"] and "并进 drama-scenes" in model.replies["LEARN-2"]
    found = json.loads(agent.tools.tools["skill_search"].execute({"action": "search", "query": "短剧 台词"}).output)
    assert any(match.get("package_id") == "drama-scenes" for match in found["matches"]), "同领域的包查得到"
    ask("LEARN-3 并进 drama-scenes")
    assert _installed(agent)[0] == "0.1.0", "开关关着，合并出的新版本也要用户确认才装"
    assert _tui(agent, _CONFIRM.findall(model.replies["LEARN-3"])[-1])["ok"]
    version, content = _installed(agent)
    assert version == "0.2.0", "合并就是出一个新版本，版本号递增"
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    v1, v2 = (store.build_files(record.sha256) for record in store.builds_for("drama-scenes"))
    assert set(v1) <= set(v2), "合并前那一版的文件都带上了"
    assert "## 方向一：分场" in content and "## 方向二：台词润色" in content, "两个方向都在"
    assert "provenance/scenes.md" in content and "provenance/dialogue.md" in content, "来源按方向分开保留"
    view = _tui(agent, "/plugins#drama-scenes 查看")
    assert view["ok"] and "台词润色：github.com/example/dialogue-agent@def" in view["message"]
    preview = _tui(agent, "/plugins#drama-scenes 退回")
    line = preview["message"].splitlines()[-1]
    assert preview["ok"] and line.startswith("/plugins#drama-scenes 退回 ") and _installed(agent)[0] == "0.2.0"
    assert _tui(agent, line)["ok"]
    version, content = _installed(agent)
    assert version == "0.1.0" and "方向二" not in content, "退回到合并前那一版"


# 和真用户一样：把 /plugins#<包名> 安装 <单号> 发回去，或者开关开着时直接装。
def _maker(agent):
    root = Path(agent.home_paths.owner_home_dir) / "runs" / "2026-10-06" / "learn" / "pack"
    build = agent.tools.tools["package_build"]

    def make(version, files, package_id="drama-scenes", keywords=None, description=None):
        shutil.rmtree(root, ignore_errors=True)
        for name, body in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(body, encoding="utf-8")
        _name, payload = _build(str(root), version, "github.com/example/drama-agent@abc", "MIT")
        payload["declaration"]["plugin_id"] = package_id
        if keywords is not None:
            payload["declaration"]["capability"]["keywords"] = keywords
        if description is not None:
            payload["declaration"]["capability"]["description"] = description
        outcome = build.execute(payload)
        assert outcome.ok, outcome.output
        return json.loads(outcome.output)

    return make


def _install_now(agent, body) -> None:
    order = json.loads(agent.tools.tools["package_install"].execute({"sha256": body["build"]["sha256"]}).output)
    assert _tui(agent, order["user_confirm_command"])["ok"], order


def _codes(body) -> list[str]:
    return [item["code"] for item in body["warnings"]]


def test_version_notes_only_flag_installed_reuse_and_downgrades(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    make = _maker(agent)
    assert make("0.1.0", {"CAPABILITY.md": "# 别的包\n"}, package_id="novel-notes")["warnings"] == []
    first = make("0.1.0", {"CAPABILITY.md": _SCENES})
    assert first["warnings"] == [] and first["installed_version"] == "" and first["previous_versions"] == []
    assert make("0.1.0", {"CAPABILITY.md": _SCENES + "补一句。\n"})["warnings"] == [], "没装过的同号重打是正常迭代"
    _install_now(agent, make("0.1.0", {"CAPABILITY.md": _SCENES}))
    assert make("0.1.0", {"CAPABILITY.md": _SCENES})["warnings"] == [], "同一份字节重打不算"
    reused = make("0.1.0", {"CAPABILITY.md": _SCENES + _DIALOGUE})
    assert _codes(reused) == ["PACKAGE_BUILD_VERSION_REUSED"] and "升一级" in reused["warnings"][0]["message"]
    assert reused["installed_version"] == "0.1.0" and reused["installed_by_me"] is True and reused["previous_versions"] == ["0.1.0"]
    _install_now(agent, make("0.3.0", {"CAPABILITY.md": _SCENES + _DIALOGUE}))
    older = make("0.2.0", {"CAPABILITY.md": _SCENES + _DIALOGUE + "再补。\n"})
    assert _codes(older) == ["PACKAGE_BUILD_VERSION_OLDER"] and "0.3.0" in older["warnings"][0]["message"]
    assert make("0.3", {"CAPABILITY.md": _SCENES + _DIALOGUE + "三。\n"})["warnings"] == [], "末尾的 0 不算，0.3 不比 0.3.0 旧"
    assert make("0.4.0-beta", {"CAPABILITY.md": _SCENES})["warnings"] == [], "比不出先后的写法不提醒"
    assert _codes(make("0.4.0", {"CAPABILITY.md": _SCENES + _DIALOGUE, "x.md": "x\n"})) == []
    assert {"PACKAGE_BUILD_VERSION_REUSED", "PACKAGE_BUILD_VERSION_OLDER", "PACKAGE_BUILD_FILES_DROPPED"} <= set(ERROR_CONTRACTS)


def test_dropping_files_of_the_previous_version_warns_but_still_builds(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    make = _maker(agent)
    scenes = {"CAPABILITY.md": _SCENES, "provenance/scenes.md": "原项目：github.com/example/drama-agent@abc\n"}
    _install_now(agent, make("0.1.0", scenes))
    dropped = make("0.2.0", {"CAPABILITY.md": _SCENES + _DIALOGUE, "provenance/dialogue.md": "原项目：dialogue\n"})
    assert _codes(dropped) == ["PACKAGE_BUILD_FILES_DROPPED"] and "provenance/scenes.md" in dropped["warnings"][0]["message"]
    assert dropped["build"]["sha256"], "提醒不挡打包"
    again = make("0.2.0", {"CAPABILITY.md": _SCENES + _DIALOGUE + "改一句。\n", "provenance/dialogue.md": "原项目：dialogue\n"})
    assert _codes(again) == ["PACKAGE_BUILD_FILES_DROPPED"], "和现在装着的那版比，不和上一次的草稿比"
    kept = make("0.2.0", {**scenes, "CAPABILITY.md": _SCENES + _DIALOGUE, "provenance/dialogue.md": "原项目：dialogue\n"})
    assert kept["warnings"] == [], "旧方向的文件都带上了"
    other = make("0.1.0", {"CAPABILITY.md": "# 别的包\n"}, package_id="novel-notes", keywords=["长篇", "伏笔"])
    assert other["warnings"] == [], "第一次打的包没有上一版，关键词也不重合"


def test_a_new_pack_sharing_keywords_with_her_installed_pack_warns_to_ask_first(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    make = _maker(agent)
    _install_now(agent, make("0.1.0", {"CAPABILITY.md": _SCENES}, keywords=["短剧", "Storyboard"]))
    body = make("1.0.0", {"CAPABILITY.md": "# 短剧管线\n"}, package_id="drama-pipeline", keywords=["短剧", "管线"])
    assert _codes(body) == ["PACKAGE_BUILD_SAME_DOMAIN"] and body["build"]["sha256"], "提醒不挡打包"
    message = body["warnings"][0]["message"]
    assert "drama-scenes 0.1.0（共同关键词：短剧）" in message and "先问用户" in message
    folded = make("1.0.0", {"CAPABILITY.md": "# 管线\n"}, package_id="drama-pipeline", keywords=["STORYBOARD", "管线"])
    assert "共同关键词：storyboard" in folded["warnings"][0]["message"], "关键词按 casefold 整词比"
    assert make("1.0.0", {"CAPABILITY.md": "# 管\n"}, package_id="drama-pipeline", keywords=["短剧分场"])["warnings"] == [], "只比整词"
    assert make("0.1.0", {"CAPABILITY.md": "# 别的\n"}, package_id="novel-notes", keywords=["长篇", "伏笔"])["warnings"] == []
    assert _codes(make("0.2.0", {"CAPABILITY.md": _SCENES + _DIALOGUE})) == [], "同名包走版本提醒，不算同领域"
    assert "PACKAGE_BUILD_SAME_DOMAIN" in ERROR_CONTRACTS


def test_latin_only_keywords_for_a_chinese_pack_are_flagged(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    make = _maker(agent)
    english = make("0.1.0", {"CAPABILITY.md": _SCENES}, package_id="novel-craft", keywords=["long novel", "web novel"])
    assert _codes(english) == ["PACKAGE_BUILD_KEYWORDS_SCRIPT"] and english["build"]["sha256"], "提醒不挡打包"
    assert "推荐不到" in english["warnings"][0]["message"] and "不是可以忽略的建议" in english["warnings"][0]["message"]
    mixed = make("0.1.1", {"CAPABILITY.md": _SCENES}, package_id="novel-craft", keywords=["long novel", "长篇"])
    assert mixed["warnings"] == [], "有一个中文关键词就够"
    latin = make("0.1.2", {"CAPABILITY.md": _SCENES}, package_id="novel-craft", keywords=["long novel"],
                 description="Plans and drafts long novels, 2nd ed.")
    assert latin["warnings"] == [], "说明本身是拉丁字母就不提醒"
    assert "PACKAGE_BUILD_KEYWORDS_SCRIPT" in ERROR_CONTRACTS


def test_the_newest_builds_are_scanned_first(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import learnpack_store

    agent = _agent(tmp_path, monkeypatch)
    make = _maker(agent)
    shas = [make(f"0.{index}.0", {"CAPABILITY.md": f"# 第 {index} 版\n"})["build"]["sha256"] for index in range(3)]
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    for age, sha in enumerate(sorted(shas, reverse=True)):  # 文件名最大的那个最新，和按文件名排的结果正相反
        os.utime(store.root / "builds" / f"{sha}.json", (1_000_000 - age, 1_000_000 - age))
    monkeypatch.setattr(learnpack_store, "_MAX_BUILD_SCAN_COUNT", 1)
    assert [record.sha256 for record in store.builds_for("drama-scenes")] == [max(shas)], "超过上限时看最近的"


def test_a_foreign_same_name_pack_is_reported_instead_of_version_hints(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_learnpack_install import _install_foreign_pack

    agent = _agent(tmp_path, monkeypatch)
    _install_foreign_pack(agent, tmp_path)  # 别处来的 drama-scenes 9.9.9
    body = _maker(agent)("0.2.0", {"CAPABILITY.md": _SCENES})
    assert _codes(body) == ["PACKAGE_BUILD_ID_TAKEN"], "不报版本倒退，直接说装不上、只能换包名"
    assert body["installed_version"] == "9.9.9" and body["installed_by_me"] is False and "换个包名" in body["warnings"][0]["message"]
    mine = _maker(agent)("0.1.0", {"CAPABILITY.md": _SCENES}, package_id="drama-dialogue")
    assert mine["warnings"] == [] and mine["installed_by_me"] is False and mine["installed_version"] == ""


def test_an_unreadable_install_table_does_not_fail_the_build(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    make = _maker(agent)
    _install_now(agent, make("0.1.0", {"CAPABILITY.md": _SCENES}))
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    (owner.plugins_dir / "installations.json").write_text("{坏了", encoding="utf-8")
    body = make("0.2.0", {"CAPABILITY.md": _SCENES + _DIALOGUE})
    assert body["build"]["sha256"] and body["installed_version"] == "", "安装表读不了按没装着算，提醒不让打包失败"
