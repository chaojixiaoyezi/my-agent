"""learnpack 第 3 步：按开关装她打的包，或开一张待确认安装单。

锁定（假管理服务）：安装命令顺序（新装 / 已装同字节只启用 / 旧版本先停用再更新）；启用要确认时只在可代确认且预览不含联网、
读写目录、宿主接口时才发确认命令，预览自带的完整确认命令原样用、进度按宿主会用的授权编号记；任一步失败即停并留下全部请求编号；
包名归属检查不过时一条命令都不发；进度记不下来时这条命令不发。
锁定（真实 SimpleAgent + 真实 /plugins 命令链，能力包）：开关关着只开单不装，回执给出确认行与开关命令；用户在会话里发
/plugins confirm 才装上并启用，同一张单第二次确认只回已执行，换一个管理服务（相当于重启）仍有效；普通用户确认被拒；开关开着
直接装上并启用、账记在单独的 learnpack 线程、安装记录标 via=self；装新版本时先停用再更新并记下上一版摘要；带检查程序的包在
插件开关关着时停在确认并给出确认命令、开着时代确认后启用；不是她打的 sha 被拒且什么都不写；同名外来包（装着的，或卸载后留下
数据目录的）两条路都拒、不占单，她自己装过的包名卸载后再装照常；前置条件按宿主同一错误码拒绝、已执行的单先回放；
插件在插件开关开着时直接装（要不受限运行时停在启用确认）；包字节被改过就不认；模型说出确认行也不执行；/plugins revert 一路
退回她做的上一版、退到第一版为止，别处来的或没装的不退；删掉后名片目录里不留它。
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.learnpack_installer import (
    STATE_ENABLED,
    STATE_FAILED,
    STATE_NEEDS_USER,
    STATE_UNKNOWN,
    InstallConsent,
    InstallOutcome,
    InstallRequest,
    auto_confirmable,
    run_install,
)
from agent_py_agent.agent.capability.learnpack_service import (
    LEARNPACK_ACTOR,
    LEARNPACK_CHANNEL,
    LEARNPACK_CONVERSATION,
    STATE_BLOCKED,
    STATE_ID_TAKEN,
    ownership_problem,
)
from agent_py_agent.agent.capability.learnpack_store import (
    BuildProvenance,
    BuildRecord,
    LearnpackStore,
)
from agent_py_agent.agent.capability.package_build import build_capability_pack, read_declared_files
from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
from agent_py_agent.agent.plugin_runtime import plugin_data_dir
from agent_py_agent.agent.plugin_sandbox import plugin_sandbox_problem
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import (
    owner_identity_from_config,
    resolve_owner_home,
)

ROOT = Path(__file__).resolve().parents[2]
_RECORD = BuildRecord("a" * 64, "plugin", "hello", "1.0.0", 2, "自己写的", "自有", "t", "run")


# ---------- 假管理服务：只按脚本回执，记下收到的命令 ----------
class _FakeManager:
    def __init__(self, replies, installed=()):
        self.replies, self.sent = list(replies), []
        self.installations = type("S", (), {"snapshot": staticmethod(lambda: tuple(installed))})()

    def catalog(self):
        return type("C", (), {"revision": f"rev-{len(self.sent)}"})()

    def command(self, text, *, revision, request_id):
        self.sent.append(text)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _installed(sha: str, enabled: bool, plugin_id: str = "hello"):
    manifest = type("M", (), {"plugin_id": plugin_id})()
    return type("I", (), {"manifest": manifest, "package_sha256": sha, "enabled": enabled})()


def _confirm(**extra) -> dict:
    return {"ok": False, "details": {"reason": "confirmation_required",
                                     "confirmation": {"confirm_code": "c0de", **extra}}}


def _legacy(mode="restricted", host_api=(), **permissions) -> dict:
    base = {"network": False, "read_roots": [], "write_roots": [], "program_roots": []}
    full = "/plugins enable hello --authorization a1 --authorization-revision r1 --confirm c0de"
    return _confirm(kind="plugin_legacy_permissions", mode=mode, permissions={**base, **permissions}, confirm_command=full,
                    authorization_id="a1", host_api=list(host_api))


def _run(manager, *, programs=True, user_session=True):
    return run_install(manager, InstallRequest(_RECORD, "/s.zip", InstallConsent(programs, user_session)))


def test_fresh_install_then_enable():
    manager = _FakeManager([{"ok": True}, {"ok": True}])
    outcome = run_install(manager, InstallRequest(_RECORD, "/store/a b.zip", InstallConsent(False, True)))
    assert outcome.state == STATE_ENABLED and manager.sent == ["/plugins install '/store/a b.zip'", "/plugins enable hello"]
    assert len(outcome.request_ids) == 2 and outcome.previous_sha256 == ""


def test_new_version_disables_then_updates_and_same_bytes_only_enables():
    manager = _FakeManager([{"ok": True}] * 3, installed=[_installed("b" * 64, True)])
    outcome = _run(manager, programs=False)
    assert manager.sent == ["/plugins disable hello", "/plugins update hello /s.zip", "/plugins enable hello"]
    assert outcome.previous_sha256 == "b" * 64 and outcome.previous_enabled
    same = _FakeManager([{"ok": True}], installed=[_installed("a" * 64, False)])
    assert _run(same, programs=False).state == STATE_ENABLED and same.sent == ["/plugins enable hello"]


def test_checker_confirmation_is_given_only_with_consent():
    manager = _FakeManager([{"ok": True}, _confirm(kind="capability_verifiers"), {"ok": True}])
    assert _run(manager).state == STATE_ENABLED and manager.sent[-1] == "/plugins enable hello --confirm c0de"
    refused = _FakeManager([{"ok": True}, _confirm(kind="capability_verifiers")])
    outcome = _run(refused, programs=False)
    assert outcome.state == STATE_NEEDS_USER and outcome.next_command == "/plugins enable hello --confirm c0de"
    assert len(refused.sent) == 2


def test_restricted_legacy_preview_without_grants_uses_the_preview_command_verbatim():
    manager = _FakeManager([{"ok": True}, _legacy(), {"ok": True, "request_id": "permit-1"}])
    noted = []
    outcome = run_install(manager, InstallRequest(_RECORD, "/s.zip", InstallConsent(True, True),
                                                  lambda request_id, text: noted.append(request_id)))
    assert outcome.state == STATE_ENABLED and manager.sent[-1].startswith("/plugins enable hello --authorization a1")
    assert outcome.request_ids[-1] == "permit-1", "记宿主实际使用的请求编号"
    assert noted[-1] == "a1" and noted[0].startswith("lp-"), "确认命令的进度按宿主会用的授权编号先记"


@pytest.mark.parametrize("confirmation", [
    _legacy(network=True), _legacy(read_roots=[{"path": "/Users/me/docs"}]), _legacy(write_roots=[{"path": "/tmp/out"}]),
    _legacy(program_roots=[{"path": "/opt/tools"}]), _legacy(mode="wide"), _legacy(host_api=["memory.read"]),
    _confirm(network=True, events=[], tool_gates=[], sandbox="required"),
    _confirm(kind="something_new"), _confirm(),
])
def test_anything_outside_the_whitelist_goes_to_the_user(confirmation):
    preview = confirmation["details"]["confirmation"]
    assert not auto_confirmable(preview)
    manager = _FakeManager([{"ok": True}, confirmation])
    outcome = _run(manager)
    assert outcome.state == STATE_NEEDS_USER and len(manager.sent) == 2 and outcome.error_code == ""


def test_self_install_gives_a_session_independent_command():
    manager = _FakeManager([{"ok": True}, _legacy(mode="wide")])
    assert _run(manager, user_session=False).next_command == "/plugins enable hello"
    verifier = _FakeManager([{"ok": True}, _confirm(kind="capability_verifiers")])
    assert _run(verifier, programs=False, user_session=False).next_command == "/plugins enable hello --confirm c0de"


def test_failed_step_stops_the_chain_and_exception_is_reported_as_unknown():
    manager = _FakeManager([{"ok": False, "state": "rejected", "error_code": "PLUGIN_DISABLED"}])
    outcome = _run(manager)
    assert outcome.state == STATE_FAILED and outcome.error_code == "PLUGIN_DISABLED" and manager.sent == ["/plugins install /s.zip"]
    assert "/plugins status" in outcome.message
    broken = _FakeManager([{"ok": True}, RuntimeError("runtime.db busy")])
    unknown = _run(broken, user_session=False)
    assert unknown.state == STATE_UNKNOWN and len(unknown.request_ids) == 2 and "/plugins status" not in unknown.message


def test_stopping_after_disabling_the_old_version_says_so():
    manager = _FakeManager([{"ok": True}, {"ok": True}, _legacy(mode="wide")], installed=[_installed("b" * 64, True)])
    outcome = _run(manager)
    assert outcome.state == STATE_NEEDS_USER and "旧版本已经停用" in outcome.message


def test_guard_sees_the_same_snapshot_and_refusal_sends_nothing():
    seen = []
    refusal = InstallOutcome(STATE_ID_TAKEN, "hello", "包名被占", error_code="PACKAGE_INSTALL_ID_TAKEN")
    manager = _FakeManager([], installed=[_installed("b" * 64, True)])
    outcome = run_install(manager, InstallRequest(_RECORD, "/s.zip", InstallConsent(True, True),
                                                  guard=lambda existing: seen.append(existing) or refusal))
    assert outcome is refusal and manager.sent == [] and seen[0][0].package_sha256 == "b" * 64


def test_progress_that_cannot_be_saved_stops_before_sending():
    def broken(_request_id, _text):
        raise OSError("disk full")

    manager = _FakeManager([{"ok": True}])
    outcome = run_install(manager, InstallRequest(_RECORD, "/s.zip", InstallConsent(True, True), broken))
    assert outcome.state == STATE_FAILED and manager.sent == [] and outcome.request_ids == ()
    assert outcome.error_code == "PACKAGE_INSTALL_FAILED" and "都没有执行" in outcome.message


# ---------- 真实链路：SimpleAgent + 真实 /plugins 命令链（能力包，纯内容） ----------
def _agent(tmp_path, monkeypatch, *, owner_kind="main", owner_id="main", **config) -> SimpleAgent:
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_owner_provider="local",
                                   my_agent_owner_kind=owner_kind, my_agent_owner_id=owner_id, **config),
                       tmp_path / "project")


def _owner(agent) -> Path:
    return Path(agent.home_paths.owner_home_dir)


def _switches(agent, *, pack=False, plugin=False) -> None:
    path = Path(agent.capability_config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"capability_pack_self_install_enabled: {str(pack).lower()}\n"
                    f"plugin_self_install_enabled: {str(plugin).lower()}\n", encoding="utf-8")


def _build(agent, version="0.1.0", *, sample: str = "") -> dict:
    base = _owner(agent) / "runs" / "2026-10-06" / "learn"
    if sample:
        root = base / f"{sample}-{version}"
        shutil.copytree(ROOT / "examples" / "capability-packages" / sample, root)
        declaration = json.loads((root / "declaration.json").read_text(encoding="utf-8"))
    else:
        root = base / f"drama-scenes-{version}"
        root.mkdir(parents=True)
        (root / "CAPABILITY.md").write_text(f"# 短剧分场 {version}\n先列人物，再分场。\n", encoding="utf-8")
        declaration = {"plugin_id": "drama-scenes", "version": version, "summary": "短剧分场方法",
                       "capability": {"description": "把短剧故事拆成场次", "keywords": ["短剧", "分场"],
                                      "entry_document": "CAPABILITY.md"}}
    outcome = agent.tools.tools["package_build"].execute({"kind": "capability_pack", "source_dir": str(root),
                                                          "declaration": declaration, "origin": "自己写的",
                                                          "license": "自有"})
    assert outcome.ok, outcome.output
    return json.loads(outcome.output)["build"]


def _install(agent, sha: str):
    outcome = agent.tools.tools["package_install"].execute({"sha256": sha})
    return outcome, json.loads(outcome.output) if outcome.output.startswith("{") else outcome.output


def _user_manager(agent, *, conversation="tui-1", is_admin=True) -> PluginManagement:
    home = agent.home_paths
    owner = resolve_owner_home(home.root, owner_identity_from_config(agent.config))
    context = plugin_management_context(owner, home, agent.config, agent.conversation_store.threads,
                                        actor_id="local-agent", channel="chat", conversation_id=conversation,
                                        is_admin=is_admin)
    return PluginManagement(context)


def _installed_entry(agent, package_id: str):
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    return next((row for row in PluginInstallStore(owner).snapshot() if row.manifest.plugin_id == package_id), None)


def test_switch_off_only_opens_an_order_then_user_confirm_installs_once(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    build = _build(agent)
    outcome, body = _install(agent, build["sha256"])
    assert outcome.ok and body["state"] == "awaiting_user_confirmation"
    assert body["user_confirm_command"] == f"/plugins confirm {body['order_id']}"
    assert body["enable_auto_install_command"] == "/settings set capability_pack_self_install_enabled true"
    assert body["switches"]["capability_pack_self_install_enabled"] is False and body["package"]["runs_programs"] is False
    assert _installed_entry(agent, "drama-scenes") is None
    reply = _user_manager(agent).command(body["user_confirm_command"], revision="", request_id="")
    assert reply["ok"] and reply["state"] == STATE_ENABLED, reply
    entry = _installed_entry(agent, "drama-scenes")
    assert entry is not None and entry.enabled
    steps = LearnpackStore(_owner(agent)).order_outcome(body["order_id"])["steps"]
    assert [step["request_id"] for step in steps] == list(reply["learnpack_install"]["request_ids"]), "每条命令发出前先记进度"
    again = _user_manager(agent, conversation="tui-2").command(body["user_confirm_command"], revision="", request_id="")
    assert not again["ok"] and again["error_code"] == "PACKAGE_INSTALL_ORDER_USED" and "已经执行过" in again["message"]
    assert "当时的结果：已装上并启用。" in again["message"]
    rows = LearnpackStore(_owner(agent)).installs()
    assert [(row["via"], row["order_id"]) for row in rows] == [("order", body["order_id"])]
    # 再出新版本的单：回执写明现在装着的同名包。
    _outcome, newer = _install(agent, _build(agent, "0.2.0")["sha256"])
    assert newer["installed_now"] == {"version": "0.1.0", "sha256": entry.package_sha256, "enabled": True}
    assert "现在装着的同名包是 0.1.0（启用中）" in newer["message"]


def test_non_admin_cannot_confirm_and_unknown_order_is_refused(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _outcome, body = _install(agent, _build(agent)["sha256"])
    refused = _user_manager(agent, is_admin=False).command(body["user_confirm_command"], revision="", request_id="")
    assert not refused["ok"] and refused["error_code"] == "PLUGIN_PERMISSION_DENIED"
    assert _installed_entry(agent, "drama-scenes") is None
    # 非管理员的确认不能把单子用掉：管理员之后照样能确认成功。
    assert _user_manager(agent).command(body["user_confirm_command"], revision="", request_id="")["ok"]
    unknown = _user_manager(agent).command("/plugins confirm lp-00000000", revision="", request_id="")
    assert not unknown["ok"] and unknown["error_code"] == "PACKAGE_INSTALL_ORDER_NOT_FOUND"


def test_switch_on_installs_directly_in_the_learnpack_thread(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    build = _build(agent)
    outcome, body = _install(agent, build["sha256"])
    assert outcome.ok and body["state"] == STATE_ENABLED, body
    assert body["manage_commands"]["remove"] == "/plugins remove drama-scenes"
    assert _installed_entry(agent, "drama-scenes").enabled
    thread = agent.conversation_store.threads.resolve(channel=LEARNPACK_CHANNEL,
                                                     channel_conversation_id=LEARNPACK_CONVERSATION,
                                                     channel_user_id=LEARNPACK_ACTOR)
    assert thread is not None and LEARNPACK_ACTOR == "learnpack"
    # 自动装的账不能改动用户的“最近会话”（TUI 的 local-agent 没有被指到 learnpack 线程）。
    latest, _error = agent.conversation_store.threads.latest_for_user_report("local-agent")
    assert latest is None or latest.thread_id != thread.thread_id
    [row] = LearnpackStore(_owner(agent)).installs()
    assert row["via"] == "self" and row["sha256"] == build["sha256"] and len(row["request_ids"]) == 2
    assert row["switch_capability_pack"] is True and row["config_version"], "安装记录写明许可来源"
    assert LearnpackStore(_owner(agent)).owns_id("drama-scenes"), "发过宿主命令就记下这个包名是她的"


def test_new_version_replaces_the_old_one_and_remembers_it(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    first, second = _build(agent, "0.1.0"), _build(agent, "0.2.0")
    assert _install(agent, first["sha256"])[1]["state"] == STATE_ENABLED
    assert _install(agent, second["sha256"])[1]["state"] == STATE_ENABLED
    entry = _installed_entry(agent, "drama-scenes")
    assert entry.enabled and entry.package_sha256 == second["sha256"]
    rows = LearnpackStore(_owner(agent)).installs()
    assert rows[-1]["previous_sha256"] == first["sha256"]


def test_pack_checker_programs_follow_the_plugin_switch(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True, plugin=False)
    build = _build(agent, "0.5.4", sample="drama-text-a")
    outcome, body = _install(agent, build["sha256"])
    # 包里有检查程序而插件开关关着：不半路停下，改为出单（不碰任何已装版本）。
    assert outcome.ok and body["state"] == "awaiting_user_confirmation" and body["package"]["runs_programs"] is True
    assert body["enable_auto_install_command"] == "/settings set plugin_self_install_enabled true"
    assert _installed_entry(agent, "drama-text-a") is None
    _switches(agent, pack=True, plugin=True)
    outcome, body = _install(agent, build["sha256"])
    assert outcome.ok and body["state"] == STATE_ENABLED, body
    assert _installed_entry(agent, "drama-text-a").enabled


def test_not_self_built_sha_is_refused_and_nothing_written(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    outcome = agent.tools.tools["package_install"].execute({"sha256": "f" * 64})
    assert not outcome.ok and outcome.error_code == "PACKAGE_INSTALL_NOT_SELF_BUILT"
    assert "PACKAGE_INSTALL_NOT_SELF_BUILT" in ERROR_CONTRACTS
    assert not (_owner(agent) / "data" / "learnpack" / "orders").exists()


def test_install_tool_is_admin_only_and_folded(tmp_path, monkeypatch):
    admin = _agent(tmp_path, monkeypatch)
    hints = admin.tools.tools["package_install"].model_spec.hints
    assert hints.default_deferred is True and hints.deferred_summary
    assert set(admin.tools.tools["package_install"].model_spec.input_schema["properties"]) == {"sha256"}
    user = _agent(tmp_path / "u", monkeypatch, owner_kind="user", owner_id="alice")
    assert "package_install" not in user.tools.tools


def test_confirm_line_survives_a_new_manager_instance(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _outcome, body = _install(agent, _build(agent)["sha256"])
    fresh = replace(_user_manager(agent).context, conversation_id="feishu-dm")
    reply = PluginManagement(fresh).command(body["user_confirm_command"], revision="", request_id="")
    assert reply["ok"] and _installed_entry(agent, "drama-scenes").enabled


def test_install_handler_refuses_non_admin_and_writes_nothing(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.package_install_tool import PackageInstallTool

    home = SimpleNamespace(owner_provider="feishu", owner_kind="users", owner_id="ou_x", owner_home_dir=str(tmp_path))
    outcome = PackageInstallTool(SimpleNamespace(home_paths=home)).execute({"sha256": "a" * 64})
    assert not outcome.ok and outcome.error_code == "TOOL_PERMISSION_DENIED"
    assert not (tmp_path / "data" / "learnpack").exists()


def _install_foreign_pack(agent, tmp_path) -> None:
    root = tmp_path / "foreign"
    root.mkdir()
    (root / "CAPABILITY.md").write_text("# 别处来的同名包\n", encoding="utf-8")
    declaration = {"plugin_id": "drama-scenes", "version": "9.9.9", "summary": "别处来的",
                   "capability": {"description": "别处来的同名包", "keywords": ["短剧"], "entry_document": "CAPABILITY.md"},
                   "files": [{"path": "CAPABILITY.md"}], "settings_schema": {"type": "object", "properties": {}}}
    built = build_capability_pack(declaration, read_declared_files(root, ["CAPABILITY.md"]))
    package = _owner(agent) / "runs" / "2026-10-06" / "foreign.zip"
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(built.payload)
    assert _user_manager(agent).command(f"/plugins install {package}", revision=_user_manager(agent).catalog().revision,
                                        request_id="foreign-1")["ok"]


def _admin(agent, text: str, request_id: str) -> dict:
    manager = _user_manager(agent)
    return manager.command(text, revision=manager.catalog().revision, request_id=request_id)


def _assert_id_taken_both_ways(agent, build: dict, order: dict) -> None:
    refused = _user_manager(agent).command(order["user_confirm_command"], revision="", request_id="")
    assert not refused["ok"] and refused["error_code"] == "PACKAGE_INSTALL_ID_TAKEN", refused
    assert LearnpackStore(_owner(agent)).order_outcome(order["order_id"]) is None, "被拒的确认不占单"
    _switches(agent, pack=True)
    outcome, body = _install(agent, build["sha256"])
    assert not outcome.ok and outcome.error_code == "PACKAGE_INSTALL_ID_TAKEN", body
    assert outcome.effect_outcome == "not_started" and "换个包名" in outcome.output
    assert LearnpackStore(_owner(agent)).installs() == [], "被拒什么都不记"


def test_same_id_foreign_package_is_never_replaced(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    build = _build(agent)
    _outcome, order = _install(agent, build["sha256"])
    _install_foreign_pack(agent, tmp_path)
    before = _installed_entry(agent, "drama-scenes").package_sha256
    _assert_id_taken_both_ways(agent, build, order)
    assert _installed_entry(agent, "drama-scenes").package_sha256 == before
    _switches(agent, pack=False)
    late = _install(agent, build["sha256"])[0]
    assert not late.ok and late.error_code == "PACKAGE_INSTALL_ID_TAKEN", "包名被占时也不开一张注定被拒的单"


def test_leftover_data_of_a_removed_foreign_package_is_not_inherited(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    build = _build(agent)
    _outcome, order = _install(agent, build["sha256"])
    _install_foreign_pack(agent, tmp_path)
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    plugin_data_dir(owner, "drama-scenes").mkdir(parents=True)  # 别处同名插件用过的私有数据，卸载时不删
    assert _admin(agent, "/plugins remove drama-scenes", "foreign-2")["ok"]
    assert _installed_entry(agent, "drama-scenes") is None
    _assert_id_taken_both_ways(agent, build, order)


def test_her_own_leftover_data_does_not_block_a_reinstall(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    build = _build(agent)
    assert _install(agent, build["sha256"])[1]["state"] == STATE_ENABLED
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    plugin_data_dir(owner, "drama-scenes").mkdir(parents=True, exist_ok=True)
    assert _admin(agent, "/plugins disable drama-scenes", "mine-1")["ok"]
    assert _admin(agent, "/plugins remove drama-scenes", "mine-2")["ok"]
    outcome, body = _install(agent, build["sha256"])
    assert outcome.ok and body["state"] == STATE_ENABLED, body


def test_a_refusal_inside_the_driver_records_nothing(tmp_path, monkeypatch):
    import agent_py_agent.agent.capability.learnpack_service as service

    agent = _agent(tmp_path, monkeypatch)
    build = _build(agent)
    _outcome, order = _install(agent, build["sha256"])
    real, calls = service.ownership_problem, []

    # 前置检查时包名还空着，驱动读安装表快照时才被别处同名包占上（两次读之间的空档）。
    def late_refusal(store, owner, record, entries):
        calls.append(entries)
        if len(calls) == 1:
            return real(store, owner, record, entries)
        return InstallOutcome(STATE_ID_TAKEN, record.package_id, "被占了", error_code="PACKAGE_INSTALL_ID_TAKEN")

    monkeypatch.setattr(service, "ownership_problem", late_refusal)
    reply = _user_manager(agent).command(order["user_confirm_command"], revision="", request_id="")
    assert not reply["ok"] and reply["error_code"] == "PACKAGE_INSTALL_ID_TAKEN" and len(calls) == 2
    store = LearnpackStore(_owner(agent))
    assert store.installs() == [] and store.order_outcome(order["order_id"])["install_state"] == STATE_ID_TAKEN
    assert not store.owns_id("drama-scenes"), "一条命令都没发就不记包名归属"
    replay = _user_manager(agent).command(order["user_confirm_command"], revision="", request_id="")
    assert "当时的结果：没有安装（包名被别处的同名包占着）。" in replay["message"]
    _switches(agent, pack=True)
    outcome, _body = _install(agent, build["sha256"])
    assert not outcome.ok and outcome.error_code == "PACKAGE_INSTALL_ID_TAKEN" and outcome.effect_outcome == "not_started"
    assert store.installs() == [] and _installed_entry(agent, "drama-scenes") is None


def test_plugins_feature_off_refuses_without_using_the_order(tmp_path, monkeypatch):
    off = _agent(tmp_path, monkeypatch, enable_plugins=False)
    _outcome, body = _install(off, _build(off)["sha256"])
    refused = _user_manager(off).command(body["user_confirm_command"], revision="", request_id="")
    assert not refused["ok"] and refused["error_code"] == "PLUGIN_DISABLED"
    on = _agent(tmp_path, monkeypatch)
    blocked = PluginManagement(replace(_user_manager(on).context, enable_allowed=False))
    denied = blocked.command(body["user_confirm_command"], revision="", request_id="")
    assert not denied["ok"] and denied["error_code"] == "TOOL_DISABLED", "功能开着、动作被策略禁用用宿主同一错误码"
    assert _user_manager(on).command(body["user_confirm_command"], revision="", request_id="")["ok"]
    replay = _user_manager(off).command(body["user_confirm_command"], revision="", request_id="")
    assert replay["error_code"] == "PACKAGE_INSTALL_ORDER_USED", "已执行的单先回放，不被前置条件盖住"


def test_switches_do_not_cross_over(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=False, plugin=True)
    _outcome, body = _install(agent, _build(agent)["sha256"])
    assert body["state"] == "awaiting_user_confirmation", "能力包只看能力包开关"
    plugin_root = _owner(agent) / "runs" / "2026-10-06" / "learn" / "hello-node"
    shutil.copytree(ROOT / "plugins" / "hello-node", plugin_root)
    declaration = json.loads((plugin_root / "declaration.json").read_text(encoding="utf-8"))
    built = agent.tools.tools["package_build"].execute({"kind": "plugin", "source_dir": str(plugin_root),
                                                        "declaration": declaration, "origin": "自己写的", "license": "自有"})
    _switches(agent, pack=True, plugin=False)
    _outcome, body = _install(agent, json.loads(built.output)["build"]["sha256"])
    assert body["state"] == "awaiting_user_confirmation", "插件只看插件开关"
    assert body["user_confirm_command"].startswith("/plugins confirm ")


def test_stale_order_does_not_silently_downgrade(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    first = _install(agent, _build(agent, "0.1.0")["sha256"])[1]
    second = _install(agent, _build(agent, "0.2.0")["sha256"])[1]
    assert _user_manager(agent).command(second["user_confirm_command"], revision="", request_id="")["ok"]
    stale = _user_manager(agent).command(first["user_confirm_command"], revision="", request_id="")
    assert not stale["ok"] and stale["error_code"] == "PACKAGE_INSTALL_ORDER_STALE"
    assert _installed_entry(agent, "drama-scenes").manifest.version == "0.2.0"


def test_interrupted_order_is_replayed_honestly(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _outcome, body = _install(agent, _build(agent)["sha256"])
    store = LearnpackStore(_owner(agent))
    assert store.claim_order(body["order_id"])  # 模拟：上次执行到一半进程退出
    store.note_order_step(body["order_id"], "lp-abc123", "/plugins install /x.zip")
    replay = _user_manager(agent).command(body["user_confirm_command"], revision="", request_id="")
    assert not replay["ok"] and replay["error_code"] == "PACKAGE_INSTALL_ORDER_USED"
    assert "正在执行或已中断（结果未确认）" in replay["message"] and "lp-abc123" in replay["message"]


def test_two_confirms_racing_only_one_executes(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _outcome, body = _install(agent, _build(agent)["sha256"])
    store = LearnpackStore(_owner(agent))
    assert store.claim_order(body["order_id"])  # 另一条确认刚抢到执行权、还没发命令
    real, reads = LearnpackStore.order_outcome, []

    # 这条确认先读时，对方的占位还没落盘（两次确认几乎同时到达）。
    def first_read_misses(self, order_id):
        reads.append(order_id)
        return None if len(reads) == 1 else real(self, order_id)

    monkeypatch.setattr(LearnpackStore, "order_outcome", first_read_misses)
    reply = _user_manager(agent).command(body["user_confirm_command"], revision="", request_id="")
    assert not reply["ok"] and reply["error_code"] == "PACKAGE_INSTALL_ORDER_USED", reply
    assert _installed_entry(agent, "drama-scenes") is None and len(reads) == 2, "抢不到执行权就一条命令都不发"


def test_only_the_latest_line_is_valid(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.learnpack_service import _superseded_by

    agent = _agent(tmp_path, monkeypatch)
    old_build, new_build = _build(agent, "0.1.0"), _build(agent, "0.2.0")
    old, new = _install(agent, old_build["sha256"])[1], _install(agent, new_build["sha256"])[1]
    stale = _user_manager(agent).command(old["user_confirm_command"], revision="", request_id="")
    assert stale["error_code"] == "PACKAGE_INSTALL_ORDER_STALE", stale
    assert f"最近一次是 0.2.0（{new_build['sha256'][:12]}）" in stale["message"], "回执写出被哪一版取代"
    assert LearnpackStore(_owner(agent)).order_outcome(old["order_id"]) is None, "过期单不占单"
    assert _user_manager(agent).command(new["user_confirm_command"], revision="", request_id="")["ok"]
    # 她有意给旧版重新出一张单：这张是最新的，照样有效。
    again = _install(agent, old_build["sha256"])[1]
    assert _user_manager(agent).command(again["user_confirm_command"], revision="", request_id="")["ok"]
    assert _installed_entry(agent, "drama-scenes").manifest.version == "0.1.0"
    store = LearnpackStore(_owner(agent))
    latest = store.create_order(store.build(new_build["sha256"]))
    store.record_install({"package_id": "drama-scenes", "sha256": new_build["sha256"], "state": STATE_NEEDS_USER})
    assert not _superseded_by(store, latest), "同一份字节的记录不算取代"
    store.record_install({"package_id": "drama-scenes", "sha256": "c" * 64, "version": "9.9.9", "state": STATE_NEEDS_USER})
    assert _superseded_by(store, latest) == f"9.9.9（{'c' * 12}）", "停在确认的别的字节也算后来装过"


def test_which_line_is_newest_survives_a_clock_rollback(tmp_path, monkeypatch):
    import agent_py_agent.agent.capability.learnpack_store as store_module
    from agent_py_agent.agent.capability.learnpack_service import _superseded_by

    agent = _agent(tmp_path, monkeypatch)
    old_build, new_build = _build(agent, "0.1.0"), _build(agent, "0.2.0")
    store = LearnpackStore(_owner(agent))
    times = iter(["2026-10-06T10:00:00.000000+00:00", "2026-10-06T09:00:00.000000+00:00"])
    monkeypatch.setattr(store_module, "_now", lambda: next(times))
    old = store.create_order(store.build(old_build["sha256"]))
    new = store.create_order(store.build(new_build["sha256"]))  # 开新单前系统时钟被往回拨了一小时
    assert new.created_at < old.created_at and new.seq > old.seq
    assert _superseded_by(store, old) and not _superseded_by(store, new), "先后只看序号"


def test_a_second_line_for_the_same_bytes_does_not_void_the_first(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    build = _build(agent)
    first, second = _install(agent, build["sha256"])[1], _install(agent, build["sha256"])[1]
    assert first["order_id"] != second["order_id"]
    assert _user_manager(agent).command(first["user_confirm_command"], revision="", request_id="")["ok"]


@pytest.mark.parametrize("case", [
    (STATE_UNKNOWN, "X", "PACKAGE_INSTALL_UNCONFIRMED", "unknown", ("lp-1",)),
    (STATE_ID_TAKEN, "PACKAGE_INSTALL_ID_TAKEN", "PACKAGE_INSTALL_ID_TAKEN", "not_started", ()),
    (STATE_BLOCKED, "TOOL_DISABLED", "TOOL_DISABLED", "not_started", ()),
    (STATE_FAILED, "X", "PACKAGE_INSTALL_FAILED", "unknown", ("lp-1",)),
])
def test_install_tool_reports_each_failure_honestly(tmp_path, monkeypatch, case):
    import agent_py_agent.agent.tooling.package_install_tool as tool_module

    state, step_code, code, effect, sent = case
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    build = _build(agent)
    monkeypatch.setattr(tool_module, "install_now", lambda *_args: InstallOutcome(
        state, "drama-scenes", "停在半路", error_code=step_code, request_ids=sent))
    outcome, body = _install(agent, build["sha256"])
    assert not outcome.ok and outcome.error_code == code and outcome.effect_outcome == effect
    assert body["step_error_code"] == step_code and code in ERROR_CONTRACTS


def test_im_text_routes_to_the_same_plugin_command():
    command = parse_conversation_control("/plugins confirm lp-1a2b3c4d", reject_unknown_slash=True)
    assert command is not None and command.kind == "plugins"
    revert = parse_conversation_control("/plugins revert drama-scenes", reject_unknown_slash=True)
    assert revert is not None and revert.kind == "plugins"


def test_waiting_for_confirmation_is_not_reported_as_an_error():
    from agent_py_agent.agent.capability.learnpack_installer import InstallOutcome
    from agent_py_agent.agent.capability.learnpack_service import outcome_reply

    reply = outcome_reply(InstallOutcome(STATE_NEEDS_USER, "hello", "要你确认", "/plugins enable hello --confirm c0de",
                                         "PLUGIN_CONFIRMATION_REQUIRED"))
    assert "error_code" not in reply and reply["message"].endswith("/plugins enable hello --confirm c0de")


def test_revert_previews_then_walks_back_through_her_versions(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    builds = [_build(agent, version) for version in ("0.1.0", "0.2.0", "0.3.0")]
    bodies = [_install(agent, build["sha256"])[1] for build in builds]
    assert all(body["state"] == STATE_ENABLED for body in bodies) and "drama-scenes 0.3.0" in bodies[-1]["message"]
    assert bodies[-1]["manage_commands"]["revert"] == "/plugins revert drama-scenes"
    for expected, build in (("0.2.0", builds[1]), ("0.1.0", builds[0])):
        preview = _admin(agent, "/plugins revert drama-scenes", f"pv-{expected}")
        line = preview["message"].splitlines()[-1]
        assert preview["ok"] and line == f"/plugins revert drama-scenes {build['sha256'][:12]}", preview
        assert f"drama-scenes {expected}" in preview["message"] and "它不运行程序" in preview["message"]
        assert "\n她声明的来源（原文，宿主未核实）：自己写的\n" in preview["message"], "她声明的文字单独一行、标明出处"
        assert _installed_entry(agent, "drama-scenes").manifest.version != expected, "预览什么都不做"
        reply = _admin(agent, line, f"rv-{expected}")
        entry = _installed_entry(agent, "drama-scenes")
        assert reply["ok"] and entry.manifest.version == expected and entry.enabled, reply
        assert f"已装上并启用：drama-scenes {expected}" in reply["message"], "回执由宿主写明装的是哪一版"
        again = _admin(agent, line, f"rv2-{expected}")
        assert again["ok"] and "什么都没做" in again["message"], "同一行重发不会多退一步"
    end = _admin(agent, "/plugins revert drama-scenes", "rv-end")
    assert not end["ok"] and end["error_code"] == "PACKAGE_REVERT_UNAVAILABLE", "退到第一版为止"
    actions = {action.name: action for action in _user_manager(agent).catalog().management_actions}
    plain = {action.name: action for action in _user_manager(agent, is_admin=False).catalog().management_actions}
    assert actions["revert"].available and actions["confirm"].available and not plain["revert"].available
    assert [row["via"] for row in LearnpackStore(_owner(agent)).installs()][-2:] == ["revert", "revert"]


def test_a_versioned_revert_line_does_not_reinstall_a_removed_package(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    for version in ("0.1.0", "0.2.0"):
        assert _install(agent, _build(agent, version)["sha256"])[1]["state"] == STATE_ENABLED
    line = _admin(agent, "/plugins revert drama-scenes", "pv")["message"].splitlines()[-1]
    assert _admin(agent, "/plugins disable drama-scenes", "off")["ok"]
    assert _admin(agent, "/plugins remove drama-scenes", "rm")["ok"]
    refused = _admin(agent, line, "rv")
    assert not refused["ok"] and refused["error_code"] == "PACKAGE_REVERT_UNAVAILABLE" and "重新出一张单" in refused["message"]
    assert _installed_entry(agent, "drama-scenes") is None, "删掉的包不会借退回装回来"


def test_reinstalling_an_old_version_does_not_make_revert_bounce(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    builds = [_build(agent, version) for version in ("0.1.0", "0.2.0", "0.3.0")]
    for build in builds:
        assert _install(agent, build["sha256"])[1]["state"] == STATE_ENABLED
    assert _install(agent, builds[1]["sha256"])[1]["state"] == STATE_ENABLED  # 又装回 0.2.0
    preview = _admin(agent, "/plugins revert drama-scenes", "pv")
    assert preview["message"].splitlines()[-1].endswith(builds[0]["sha256"][:12]), "上一版按第一次装上的先后取：0.1.0"
    stopped = _admin(agent, preview["message"].splitlines()[-1], "rv")
    assert stopped["ok"] and _installed_entry(agent, "drama-scenes").manifest.version == "0.1.0"
    forward = _admin(agent, f"/plugins revert drama-scenes {builds[2]['sha256'][:12]}", "fw")
    assert forward["ok"] and _installed_entry(agent, "drama-scenes").manifest.version == "0.3.0", "装上过的版本都能点名换回"


def test_revert_refuses_what_is_not_hers(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    missing = _admin(agent, "/plugins revert drama-scenes", "rv-1")
    assert not missing["ok"] and missing["error_code"] == "PACKAGE_REVERT_UNAVAILABLE"
    _install_foreign_pack(agent, tmp_path)
    foreign = _admin(agent, "/plugins revert drama-scenes", "rv-2")
    assert not foreign["ok"] and foreign["error_code"] == "PACKAGE_REVERT_UNAVAILABLE"
    denied = _user_manager(agent, is_admin=False).command("/plugins revert drama-scenes", revision="", request_id="")
    assert not denied["ok"] and denied["error_code"] == "PLUGIN_PERMISSION_DENIED"
    assert "PACKAGE_REVERT_UNAVAILABLE" in ERROR_CONTRACTS and LearnpackStore(_owner(agent)).installs() == []
    never = _build(agent, "0.9.0")  # 她打过、但从没在这个包名下装上过
    failed = _build(agent, "0.8.0")  # 试装过但没装上
    LearnpackStore(_owner(agent)).record_install({"package_id": "drama-scenes", "sha256": failed["sha256"],
                                                  "state": STATE_FAILED})
    for target in ("zz", "abc", never["sha256"][:12], failed["sha256"][:12], "f" * 12):
        refused = _admin(agent, f"/plugins revert drama-scenes {target}", f"rv-{target}")
        assert not refused["ok"] and refused["error_code"] == "PACKAGE_REVERT_UNAVAILABLE", target
    off = _agent(tmp_path / "off", monkeypatch, enable_plugins=False)
    blocked = _user_manager(off).command("/plugins revert drama-scenes", revision="", request_id="")
    assert not blocked["ok"] and blocked["error_code"] == "PLUGIN_DISABLED"


def _build_hello_node(agent) -> str:
    plugin_root = _owner(agent) / "runs" / "2026-10-06" / "learn" / "hello-node"
    shutil.copytree(ROOT / "plugins" / "hello-node", plugin_root)
    declaration = json.loads((plugin_root / "declaration.json").read_text(encoding="utf-8"))
    built = agent.tools.tools["package_build"].execute({"kind": "plugin", "source_dir": str(plugin_root),
                                                        "declaration": declaration, "origin": "自己写的", "license": "自有"})
    return json.loads(built.output)["build"]["sha256"]


# 插件开关开着：她做的插件不开单、直接装。默认收紧运行（restricted、无联网与读写目录）时宿主代确认并启用。
@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
@pytest.mark.sandbox_capability("background_launcher_identity")
def test_plugin_switch_on_installs_and_enables_her_sandboxed_plugin(tmp_path, monkeypatch):
    if plugin_sandbox_problem(True, tmp_path):
        pytest.skip("本机平台沙箱不可用（Linux 需要可用的 bwrap，macOS 需要 sandbox-exec）")
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, plugin=True)
    outcome, body = _install(agent, _build_hello_node(agent))
    assert outcome.ok and body["state"] == STATE_ENABLED and "order_id" not in body, body
    assert _installed_entry(agent, "hello-node").enabled


# 插件开关开着、但老插件默认不受限运行（wide）：直接装上，停在启用确认，给用户在自己会话里能用的那一行。
@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_plugin_switch_on_stops_at_confirmation_for_an_unrestricted_plugin(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, plugin_legacy_sandbox_default=False)
    _switches(agent, plugin=True)
    outcome, body = _install(agent, _build_hello_node(agent))
    assert outcome.ok and body["state"] == STATE_NEEDS_USER and "order_id" not in body, body
    assert body["next_command"] == "/plugins enable hello-node" and not _installed_entry(agent, "hello-node").enabled


def test_changed_bytes_are_never_installed(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    build = _build(agent)
    _outcome, order = _install(agent, build["sha256"])
    LearnpackStore(_owner(agent)).build_path(build["sha256"]).write_bytes(b"changed")
    refused = _user_manager(agent).command(order["user_confirm_command"], revision="", request_id="")
    assert not refused["ok"] and refused["error_code"] == "PACKAGE_INSTALL_ORDER_NOT_FOUND"
    _switches(agent, pack=True)
    outcome = agent.tools.tools["package_install"].execute({"sha256": build["sha256"]})
    assert not outcome.ok and outcome.error_code == "PACKAGE_INSTALL_NOT_SELF_BUILT"
    assert _installed_entry(agent, "drama-scenes") is None


def test_the_model_cannot_confirm_an_order(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

    agent = _agent(tmp_path, monkeypatch)
    _outcome, body = _install(agent, _build(agent)["sha256"])
    line = body["user_confirm_command"]

    # 模型把确认行原样说出来：那只是回复文字，不是用户发的宿主命令。
    class ConfirmingBackend:
        name = "confirming_model"

        def probe_tool_capability(self):
            return ProviderToolCapability(provider=self.name, endpoint="local://confirm", model="", stream=False,
                                          native_supported=True, evidence="test_native_tools", observed_at=_utc_now_iso())

        def generate(self, prompt, on_chunk=None, **kwargs):
            return ModelResponse(text=line, backend=self.name)

    agent.backend = ConfirmingBackend()
    agent.run(f"帮我装上：{line}", save=False, task_id="model-confirm")
    assert LearnpackStore(_owner(agent)).order_outcome(body["order_id"]) is None
    assert _installed_entry(agent, "drama-scenes") is None


def test_removing_a_pack_leaves_no_name_card(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    build = _build(agent)
    assert _install(agent, build["sha256"])[1]["state"] == STATE_ENABLED
    assert "drama-scenes" in agent.capability_router.render_skill_metadata_index()
    assert _admin(agent, "/plugins disable drama-scenes", "rm-1")["ok"]
    assert _admin(agent, "/plugins remove drama-scenes", "rm-2")["ok"]
    assert "drama-scenes" not in agent.capability_router.render_skill_metadata_index()
    assert LearnpackStore(_owner(agent)).build(build["sha256"]) is not None, "她做的字节留在 learnpack 存储，供重装与退回"


def test_ownership_is_recorded_only_after_her_package_is_in_the_table():
    marks = []
    rejected = _FakeManager([{"ok": False, "state": "rejected", "error_code": "PLUGIN_DISABLED"}])
    run_install(rejected, InstallRequest(_RECORD, "/s.zip", InstallConsent(True, True), on_installed=lambda: marks.append(0)))
    assert marks == [], "第一条命令就被拒，包没进安装表，不记归属"
    fresh = _FakeManager([{"ok": True}, {"ok": True}])
    run_install(fresh, InstallRequest(_RECORD, "/s.zip", InstallConsent(True, True),
                                      on_installed=lambda: marks.append(len(fresh.sent))))
    same = _FakeManager([{"ok": True}], installed=[_installed("a" * 64, False)])
    run_install(same, InstallRequest(_RECORD, "/s.zip", InstallConsent(True, True),
                                     on_installed=lambda: marks.append(len(same.sent))))
    assert marks == [1, 0], "装上之后、启用之前记；本来装着同一份字节时也在启用之前记"


def test_ownership_that_cannot_be_saved_keeps_the_package_disabled():
    def broken():
        raise OSError("disk full")

    manager = _FakeManager([{"ok": True}])
    outcome = run_install(manager, InstallRequest(_RECORD, "/s.zip", InstallConsent(True, True), on_installed=broken))
    assert outcome.state == STATE_FAILED and manager.sent == ["/plugins install /s.zip"] and "没有启用" in outcome.message


def test_auto_install_checks_the_plugin_feature_first(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, enable_plugins=False)
    _switches(agent, pack=True)
    outcome, _body = _install(agent, _build(agent)["sha256"])
    assert not outcome.ok and outcome.error_code == "PLUGIN_DISABLED" and outcome.effect_outcome == "not_started"
    store = LearnpackStore(_owner(agent))
    assert store.installs() == [] and not store.owns_id("drama-scenes"), "什么都没做：不记账、不记归属"


def test_ids_differing_only_in_case_are_taken(tmp_path):
    from types import SimpleNamespace

    store, owner = LearnpackStore(tmp_path / "owner"), SimpleNamespace(plugins_dir=tmp_path / "plugins")
    upper = replace(_RECORD, package_id="Hello")
    refused = ownership_problem(store, owner, upper, (_installed("b" * 64, False, plugin_id="hello"),))
    assert refused is not None and refused.state == STATE_ID_TAKEN and "只差大小写" in refused.message
    assert ownership_problem(store, owner, _RECORD, ()) is None, "没装、没有残留数据：放行"
    root = tmp_path / "src"
    root.mkdir()
    (root / "CAPABILITY.md").write_text("# 短剧分场\n", encoding="utf-8")
    declaration = {"plugin_id": "drama-scenes", "version": "0.1.0", "summary": "短剧分场方法",
                   "capability": {"description": "把短剧故事拆成场次", "keywords": ["短剧"], "entry_document": "CAPABILITY.md"},
                   "files": [{"path": "CAPABILITY.md"}], "settings_schema": {"type": "object", "properties": {}}}
    mine = store.save_build(build_capability_pack(declaration, read_declared_files(root, ["CAPABILITY.md"])),
                            BuildProvenance("自己写的", "自有", "run"))
    installed = (_installed(mine.sha256, True, plugin_id="drama-scenes"),)
    assert ownership_problem(store, owner, mine, installed) is None, "装着的就是她这个包名：放行"
    assert ownership_problem(store, owner, replace(mine, package_id="Drama-Scenes"), installed) is not None, \
        "她自己的包只差大小写也不行"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root 不受目录权限限制")
def test_unreadable_plugin_data_counts_as_taken(tmp_path):
    from types import SimpleNamespace

    store, owner = LearnpackStore(tmp_path / "owner"), SimpleNamespace(plugins_dir=tmp_path / "plugins")
    owner.plugins_dir.mkdir()
    owner.plugins_dir.chmod(0)
    try:
        refused = ownership_problem(store, owner, _RECORD, ())
    finally:
        owner.plugins_dir.chmod(0o700)
    assert refused is not None and refused.error_code == "PACKAGE_INSTALL_ID_TAKEN", "查不了数据目录按有残留处理"


def test_confirm_and_revert_lines_work_through_the_im_entry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import control_service
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    agent = _agent(tmp_path, monkeypatch)
    host = SimpleNamespace(config=agent.config, home_paths=agent.home_paths)
    # 飞书私聊里已绑定的管理员：与 /settings 同一条管理员规则。
    scope = GatewayControlScope(user_id="alice", channel="feishu", conversation_id="dm-1",
                                metadata={"message_id": "im-1"}, resolved_owner=OwnerIdentity("local", "main", "main"))

    def im(text):
        command = parse_conversation_control(text, reject_unknown_slash=True)
        return control_service.execute_gateway_conversation_control(host, None, command, scope)

    first = _install(agent, _build(agent, "0.1.0")["sha256"])[1]
    assert im(first["user_confirm_command"]).ok
    second = _install(agent, _build(agent, "0.2.0")["sha256"])[1]
    assert im(second["user_confirm_command"]).ok
    assert _installed_entry(agent, "drama-scenes").manifest.version == "0.2.0"
    preview = im("/plugins revert drama-scenes")
    assert preview.ok and _installed_entry(agent, "drama-scenes").manifest.version == "0.2.0", preview.message
    reverted = im(preview.message.splitlines()[-1])
    assert reverted.ok and "drama-scenes 0.1.0" in reverted.message, reverted.message
    assert _installed_entry(agent, "drama-scenes").manifest.version == "0.1.0"
    again = im(first["user_confirm_command"])
    assert not again.ok and again.error_code == "PACKAGE_INSTALL_ORDER_USED" and "当时的结果" in again.message


def test_hooks_record_ownership_only_after_the_package_is_in_the_table(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability.learnpack_service import _InstallHooks

    store = LearnpackStore(tmp_path / "owner")
    hooks = _InstallHooks(store, SimpleNamespace(plugins_dir=tmp_path / "plugins"), _RECORD)
    rejected = _FakeManager([{"ok": False, "state": "rejected", "error_code": "PLUGIN_SOURCE_INVALID"}])
    run_install(rejected, hooks.request(InstallConsent(True, True)))
    assert len(rejected.sent) == 1 and not store.owns_id("hello"), "安装命令被宿主拒绝，不记归属"
    accepted = _FakeManager([{"ok": True}, {"ok": True}])
    assert run_install(accepted, hooks.request(InstallConsent(True, True))).state == STATE_ENABLED
    assert store.owns_id("hello")
