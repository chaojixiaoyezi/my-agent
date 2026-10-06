"""learnpack 第 7 步：整条链路一起跑（假模型驱动真实 SimpleAgent 对话回合 + 真实工具 + 真实 /plugins# 与 /plugins 命令；
TUI 与飞书两个入口都走）。

锁定（目标文件第 7 步）：
- 能力包、开关关：学 → 打包 → 只给确认行 → 用户在 TUI 发确认装上 → 同一行再发（TUI 同一请求编号、飞书新消息）都回"这张单已经
  执行过"并回放当时的结果，不再装 → 重启（新建 agent）后 /plugins# 查看照常 → 飞书里停用、TUI 里启用 → 再学出新版本、飞书里确认 →
  飞书里退回（先预览、再发那一行）回到旧版 → TUI 里停用、删除；她做的字节仍留在 learnpack 存储。
- 能力包、开关开：学 → 打包 → 直接装上并启用，回执由宿主写明版本、给 /plugins# 管理命令；列表标"她做的"。
- 插件（样例 hello-node 的文件由她逐个写进工作区；没有 node 时跳过）：开关关只给 /plugins confirm 行 → 用户确认装上 → /plugins list
  标她做的、不进能力包列表 → 停用、删除；开关开时直接装上，不开单。
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.learnpack_store import LearnpackStore
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    owner_identity_from_config,
    resolve_owner_home,
)
from agent_py_agent.tests.test_learnpack_merge import _Turns

ROOT = Path(__file__).resolve().parents[2]
_LOAD = ("tool_search", {"query": "package_build package_install 打包 安装 能力包 插件", "limit": 5})
_SHA = re.compile(r"[0-9a-f]{64}")
_LINE = re.compile(r"/plugins(?:#[a-z0-9-]+ 安装| confirm) lp-[0-9a-f]{8}")


def _pack_turn(version: str) -> list:
    declaration = {"plugin_id": "drama-scenes", "version": version, "summary": "短剧分场方法",
                   "capability": {"description": "把短剧故事拆成场次", "keywords": ["短剧", "分场"],
                                  "entry_document": "CAPABILITY.md"}}
    return [("write_file", {"path": f"drama-{version}/CAPABILITY.md", "content": f"# 短剧分场 {version}\n先列人物，再分场。\n"}),
            _LOAD,
            ("package_build", {"kind": "capability_pack", "source_dir": f"drama-{version}", "declaration": declaration,
                               "origin": "github.com/example/drama-agent@abc", "license": "MIT"}),
            lambda seen: ("package_install", {"sha256": _SHA.findall(seen)[-1]}),
            _reply]


def _plugin_turn() -> list:
    sample = ROOT / "plugins" / "hello-node"
    writes = [("write_file", {"path": f"hello-node/{path.relative_to(sample).as_posix()}",
                              "content": path.read_text(encoding="utf-8")})
              for path in sorted(sample.rglob("*")) if path.is_file()]
    declaration = json.loads((sample / "declaration.json").read_text(encoding="utf-8"))
    return [*writes, _LOAD,
            ("package_build", {"kind": "plugin", "source_dir": "hello-node", "declaration": declaration,
                               "origin": "自己写的", "license": "自有"}),
            lambda seen: ("package_install", {"sha256": _SHA.findall(seen)[-1]}),
            _reply]


# 她的最后一句：只看最后一次安装回执（对话历史里有上一轮的确认行）；里面有确认行就原样交给用户，没有就照回执说装好了。
def _reply(seen: str) -> str:
    lines = _LINE.findall(seen[seen.rfind("tool=package_install"):])
    return f"做好了，开关关着所以还没装。要装就把这一行发给我：\n{lines[-1]}" if lines else "做好了，已经按开关装上。"


def _agent(tmp_path, monkeypatch) -> SimpleAgent:
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_owner_provider="local",
                                   my_agent_owner_kind="main", my_agent_owner_id="main"), tmp_path / "project")


def _switches(agent, *, pack=False, plugin=False) -> None:
    path = Path(agent.capability_config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"capability_pack_self_install_enabled: {str(pack).lower()}\n"
                    f"plugin_self_install_enabled: {str(plugin).lower()}\n", encoding="utf-8")


def _manager(agent) -> PluginManagement:
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    return PluginManagement(plugin_management_context(owner, agent.home_paths, agent.config, agent.conversation_store.threads,
                                                      actor_id="local-agent", channel="chat", conversation_id="tui-1",
                                                      is_admin=True))


# 和真 TUI 一样：每条命令带当前目录版本和一个新请求编号。
def _tui(agent, text) -> dict:
    manager = _manager(agent)
    return manager.command(text, revision=manager.catalog().revision, request_id=f"tui-{uuid.uuid4().hex}")


# 飞书私聊（已绑定管理员）：每条消息一个新消息编号。
def _im(agent, text) -> dict:
    host = SimpleNamespace(config=agent.config, home_paths=agent.home_paths)
    scope = GatewayControlScope(user_id="alice", channel="feishu", conversation_id="dm-1",
                                metadata={"message_id": f"im-{uuid.uuid4().hex}"},
                                resolved_owner=OwnerIdentity("local", "main", "main"))
    result = control_service.execute_gateway_conversation_control(
        host, None, parse_conversation_control(text, reject_unknown_slash=True), scope)
    return {"ok": result.ok, "message": result.message, "error_code": result.error_code}


def _entry(agent, package_id):
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    return next((row for row in PluginInstallStore(owner).snapshot() if row.manifest.plugin_id == package_id), None)


def _ask(agent, text) -> None:
    thread = agent.conversation_store.threads.get_or_create(
        {"canonical_user_id": "learnpack-user", "channel": "internal", "channel_conversation_id": "e2e"})
    agent.run(text, task_attributes={"conversation_thread_id": thread.thread_id}, source="gateway")


def test_pack_with_the_switch_off_through_tui_and_im(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    model = _Turns({"LEARN-1": _pack_turn("0.1.0"), "LEARN-2": _pack_turn("0.2.0")})
    agent.backend = model
    _ask(agent, "LEARN-1 学一下 drama-agent 的短剧分场方法，做成能力包")
    line = _LINE.findall(model.replies["LEARN-1"])[-1]
    assert line.startswith("/plugins#drama-scenes 安装 ") and _entry(agent, "drama-scenes") is None, "开关关着只给确认行"
    manager, request_id = _manager(agent), f"tui-{uuid.uuid4().hex}"
    revision = manager.catalog().revision
    first = manager.command(line, revision=revision, request_id=request_id)
    assert first["ok"] and _entry(agent, "drama-scenes").enabled
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    installs = len(store.installs())
    for again in (manager.command(line, revision=revision, request_id=request_id), _im(agent, line)):
        assert not again["ok"] and again["error_code"] == "PACKAGE_INSTALL_ORDER_USED", "同一张单只执行一次"
        assert "当时的说明：已装上并启用：drama-scenes 0.1.0" in again["message"], "重发回放当时的结果"
    assert len(store.installs()) == installs, "重发不再装"
    restarted = _agent(tmp_path, monkeypatch)
    view = _tui(restarted, "/plugins#drama-scenes 查看")
    assert view["ok"] and "drama-scenes 0.1.0（启用）" in view["message"] and "她做的" in view["message"], view
    assert _im(agent, "/plugins#drama-scenes 停用")["ok"] and not _entry(agent, "drama-scenes").enabled
    assert _tui(agent, "/plugins#drama-scenes 启用")["ok"] and _entry(agent, "drama-scenes").enabled
    _ask(agent, "LEARN-2 按新学的改进一下分场方法，出个新版本")
    assert _im(agent, _LINE.findall(model.replies["LEARN-2"])[-1])["ok"]
    assert _entry(agent, "drama-scenes").manifest.version == "0.2.0"
    preview = _im(agent, "/plugins#drama-scenes 退回")
    assert preview["ok"] and _entry(agent, "drama-scenes").manifest.version == "0.2.0", "预览什么都不做"
    assert _im(agent, preview["message"].splitlines()[-1])["ok"]
    assert _entry(agent, "drama-scenes").manifest.version == "0.1.0"
    assert _tui(agent, "/plugins#drama-scenes 停用")["ok"] and _tui(agent, "/plugins#drama-scenes 删除")["ok"]
    assert _entry(agent, "drama-scenes") is None and "当前没有装能力包" in _tui(agent, "/plugins#")["message"]
    assert [record.version for record in store.builds_for("drama-scenes")] == ["0.1.0", "0.2.0"], "她做的字节还在"


def test_pack_with_the_switch_on_installs_directly(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _switches(agent, pack=True)
    model = _Turns({"LEARN-1": _pack_turn("0.1.0")})
    agent.backend = model
    _ask(agent, "LEARN-1 学一下 drama-agent 的短剧分场方法，做成能力包")
    assert _entry(agent, "drama-scenes").enabled and not _LINE.findall(model.replies["LEARN-1"]), "开关开着不开单"
    receipt = model.seen["LEARN-1"]
    assert "已装上并启用：drama-scenes 0.1.0" in receipt and "/plugins#drama-scenes 停用" in receipt, "回执由宿主写明版本与管理命令"
    assert "- drama-scenes 0.1.0（启用，她做的）" in _im(agent, "/plugins#")["message"]


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_plugin_chain_with_both_switch_positions(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    model = _Turns({"PLUG-1": _plugin_turn()})
    agent.backend = model
    _ask(agent, "PLUG-1 把这个离线小工具做成插件")
    line = _LINE.findall(model.replies["PLUG-1"])[-1]
    assert line.startswith("/plugins confirm ") and _entry(agent, "hello-node") is None, "插件开关关着只给确认行"
    confirmed = _tui(agent, line)
    assert _entry(agent, "hello-node") is not None, confirmed
    listing = _im(agent, "/plugins list")
    assert "my-agent 自己做的：hello-node" in listing["message"] and "hello-node" not in _tui(agent, "/plugins#")["message"]
    if _entry(agent, "hello-node").enabled:
        assert _tui(agent, "/plugins disable hello-node")["ok"]
    assert _tui(agent, "/plugins remove hello-node")["ok"] and _entry(agent, "hello-node") is None
    _switches(agent, plugin=True)
    model.turns["PLUG-2"], model.steps["PLUG-2"] = _plugin_turn(), 0
    _ask(agent, "PLUG-2 再做一次这个插件")
    assert _entry(agent, "hello-node") is not None and not _LINE.findall(model.replies["PLUG-2"]), "插件开关开着直接装"
