"""learnpack 整条链路：假模型 + 真实 SimpleAgent 回合 + 真实工具（只有模型是脚本）。

锁定（第 5 步）：她按 learn-external-agent 的流程在任务目录写好能力包 → 用 tool_search 找到并加载默认收起的打包、安装工具 →
package_build 打包 → 自动装开关关着时 package_install 只开单 → 最后把回执里的确认行原样交给用户；宿主这边包没装、单子在、
确认行就是能力包写法。内置技能 learn-external-agent 能被 skill_search 找到并读到正文。
"""
from __future__ import annotations

import json
import re

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso
from agent_py_agent.agent.capability.learnpack_store import LearnpackStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import (
    owner_identity_from_config,
    resolve_owner_home,
)

_DECLARATION = {"plugin_id": "drama-scenes", "version": "0.1.0", "summary": "短剧分场方法",
                "capability": {"description": "把短剧故事拆成场次；不适合长篇小说正文", "keywords": ["短剧", "分场"],
                               "entry_document": "CAPABILITY.md"}}


def _agent(tmp_path, monkeypatch) -> SimpleAgent:
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_owner_provider="local",
                                   my_agent_owner_kind="main", my_agent_owner_id="main"), tmp_path / "project")


# 类用途: 脚本化的"她"：按次序出招（写包 → 找工具 → 打包 → 安装 → 把确认行交给用户），需要的摘要和确认行从上一步工具结果里取。
#   写包用相对路径（落在当前工作目录），打包也照写文件回执里的相对路径填，和真模型照技能做的一样。
class _LearnerModel:
    name = "learnpack_flow_model"

    def __init__(self, pack_dir: str = "drama-scenes") -> None:
        self.pack_dir = pack_dir
        self.requests: list[str] = []
        self.final_text = ""

    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint="local://learnpack", model="", stream=False,
                                      native_supported=True, evidence="test_native_tools", observed_at=_utc_now_iso())

    def generate(self, prompt, on_chunk=None, **kwargs):
        seen = str(prompt) + json.dumps(kwargs.get("messages"), ensure_ascii=False, default=str)
        self.requests.append(seen)
        step = len(self.requests)
        calls = {
            1: ("write_file", {"path": f"{self.pack_dir}/CAPABILITY.md",
                               "content": "# 短剧分场\n\n## 适合\n- 把短剧故事拆成场次\n"}),
            2: ("tool_search", {"query": "package_build package_install 打包 安装 能力包", "limit": 5}),
            3: ("package_build", {"kind": "capability_pack", "source_dir": self.pack_dir, "declaration": _DECLARATION,
                                  "origin": "github.com/example/drama-agent@abc123", "license": "MIT"}),
        }
        if step in calls:
            return self._call(step, *calls[step])
        if step == 4:
            sha256 = re.findall(r"[0-9a-f]{64}", seen)[-1]
            return self._call(step, "package_install", {"sha256": sha256})
        line = re.findall(r"/plugins#drama-scenes 安装 lp-[0-9a-f]{8}", seen)[-1]
        self.final_text = f"能力包做好了，自动装开关关着所以还没装。要装就把这一行发给我：\n{line}"
        return ModelResponse(text=self.final_text, backend=self.name)

    def _call(self, step: int, name: str, payload: dict) -> ModelResponse:
        return ModelResponse(text="", backend=self.name,
                             tool_use_blocks=[{"id": f"learn-{step}", "name": name, "input": payload}])


def test_learn_build_and_hand_over_the_confirm_line_with_the_switch_off(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    model = _LearnerModel()
    agent.backend = model
    agent.run("学一下 drama-agent 的短剧分场方法，做成能力包", save=False, task_id="learn-drama")
    assert len(model.requests) == 5, "写包、找工具、打包、安装、回复各一次"
    assert "tool=package_build; status=succeeded" in model.requests[3], "打包靠 tool_search 加载后成功"
    assert "tool=package_install; status=succeeded" in model.requests[4]
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    orders = list((store.root / "orders").glob("lp-*.json"))
    assert len(orders) == 1 and model.final_text.endswith(f"/plugins#drama-scenes 安装 {orders[0].stem}")
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    assert all(row.manifest.plugin_id != "drama-scenes" for row in PluginInstallStore(owner).snapshot()), "开关关着不装"


def test_the_learning_skill_is_found_and_read_through_skill_search(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    search = agent.tools.tools["skill_search"]
    found = json.loads(search.execute({"query": "学外部 agent 收成能力包"}).output)
    skill_id = next(match["skill_id"] for match in found["matches"] if "learn-external-agent" in match["skill_id"])
    body = search.execute({"skill_id": skill_id})
    assert body.ok and "learnpack-notes.md" in body.output and "package_install" in body.output


# 类用途: 按回合出招的假模型：每个回合一串工具调用，调完就回一句话；回合按用户提问里的标记区分。
class _ScriptedTurns:
    name = "learnpack_turns_model"

    def __init__(self, turns: dict[str, list[tuple[str, dict]]]) -> None:
        self.turns, self.steps = turns, dict.fromkeys(turns, 0)

    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint="local://turns", model="", stream=False,
                                      native_supported=True, evidence="test_native_tools", observed_at=_utc_now_iso())

    def generate(self, prompt, on_chunk=None, **kwargs):
        key = next(key for key in self.turns if key in str(prompt) + json.dumps(kwargs.get("messages"), default=str))
        step = self.steps[key]
        self.steps[key] += 1
        if step < len(self.turns[key]):
            name, payload = self.turns[key][step]
            return ModelResponse(text="", backend=self.name,
                                 tool_use_blocks=[{"id": f"{key}-{step}", "name": name, "input": payload}])
        return ModelResponse(text="好了。", backend=self.name)


def test_summarize_after_the_work_is_done_queues_that_work_once(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_owner_provider="local",
                                    my_agent_owner_kind="main", my_agent_owner_id="main", enable_self_learning=True),
                        tmp_path / "project")
    thread = agent.conversation_store.threads.get_or_create(
        {"canonical_user_id": "learnpack-user", "channel": "internal", "channel_conversation_id": "summarize"})
    # 一条她自己的正式记忆：RUN-A 的提问会召回它，"总结一下"要把它一起交给自学习。
    agent.memory.add("user", "短剧第一场先交代人物关系", kind="fact", attributes={
        "origin": "reviewed", "subject_key": "learnpack.drama.first-scene", "scope_type": "personal", "scope_key": "personal"})
    search = ("tool_search", {"query": "总结 内部技能 自学习", "limit": 5})
    agent.backend = _ScriptedTurns({
        "RUN-A": [("write_file", {"path": "drama/scene-1.md", "content": "第一场：人物出场。\n"})],
        # 事后要总结：先读一眼刚写的文件（这一轮因此也升成了任务），再按用户的话选 previous。
        "RUN-B": [("read_file", {"path": "drama/scene-1.md"}), search,
                  ("skill_summarize", {"target": "previous", "focus": "分场顺序"})],
        "RUN-C": [("write_file", {"path": "drama/scene-2.md", "content": "第二场。\n"}), search,
                  ("skill_summarize", {"target": "current", "focus": "分场顺序"})],
    })

    def ask(text):
        agent.run(text, task_attributes={"conversation_thread_id": thread.thread_id}, source="gateway")

    ask("RUN-A 把第一场写出来")
    store = agent.skill_learning.store
    assert store.pending_requests() == [], "只写了一轮工具，自动总结不够门槛"
    ask("RUN-B 把刚才的做法总结一下")
    [request] = [json.loads(path.read_text(encoding="utf-8")) for path in store.pending_requests()]
    assert request["requested_by"] == "user" and request["user_focus"] == "分场顺序"
    assert "RUN-A" in request["user_prompt"], "先读了文件也照样总结上一次做完的活，不是说“总结一下”的这一句"
    assert any("短剧第一场先交代人物关系" in item["content"] for item in request["recalled_memories"]), "那次召回的记忆一起交"
    ask("RUN-C 写第二场，写完顺便把做法总结一下")
    requests = [json.loads(path.read_text(encoding="utf-8")) for path in store.pending_requests()]
    assert [item["user_prompt"][:5] for item in requests] == ["RUN-A", "RUN-C"], "边做边要总结的，在这次活收尾时入队"
