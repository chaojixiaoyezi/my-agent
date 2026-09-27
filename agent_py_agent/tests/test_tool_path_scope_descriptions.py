"""read_file / list_files 的说明末句按本 run 实际路径范围给出（ROADMAP 条目，出自 87cf12677）。"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_snapshots_for_run
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactModelSurface,
    prepare_conversation_compact_provider_surface,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.path_access_policy import (
    PATH_SCOPE_FULL,
    PATH_SCOPE_NORMAL,
    PATH_SCOPE_OWNER_WALL,
    PathAccessPolicy,
    effective_owner_scope_root,
    path_scope_regime,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.tooling._filesystem_list import ListFilesTool
from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call
from agent_py_agent.tests.test_tool_runtime_scope import _register_run

REGIMES = (PATH_SCOPE_OWNER_WALL, PATH_SCOPE_FULL, PATH_SCOPE_NORMAL)
# 原说明的末句逐字留在 full 范围里；有墙版本不能再带这句。
ORIGINAL_TAILS = {
    "read_file": "可直接读任意绝对路径，包括 workspace 外、用户在任务里指定的输入目录/文件，无需 shell 或额外授权——"
                 "不要为读取输入文件提 capability_request。",
    "list_files": "可直接列任意绝对路径，包括 workspace 外、用户在任务里指定的输入目录，无需 shell 或额外授权——"
                  "不要为查看输入目录提 capability_request。",
}
NORMAL_EXCEPTIONS = "危险目录和凭据文件除外（PATH_DANGEROUS_ROOT_BLOCKED / PATH_CREDENTIAL_FILE_BLOCKED）。"
SCOPED = ("read_file", "list_files")


def _tools(tmp_path):
    return ReadFileTool(tmp_path, 1000), ListFilesTool(tmp_path, 100)


def _texts(tmp_path, regime):
    return {tool.model_spec.name: tool.model_spec_for_path_scope(regime).description for tool in _tools(tmp_path)}


# 函数用途: 造一个本机管理员 Full Access 的真实 Agent：父注册表没有 owner 墙，子代理仍有自己的墙（事故现场）。
def _full_access_agent(tmp_path):
    return SimpleAgent(AgentConfig(
        model_backend="anthropic_compatible", model_name="MiniMax-M2.7", api_base="https://api.minimaxi.com/anthropic",
        api_key="fake-key", stream_enabled=False, enable_tools=True, enable_subagents=True, access_mode="full-access",
        max_tool_rounds=1,
    ), tmp_path)


def _scoped_rows(tools):
    return {row["name"]: (row["description"], row["input_schema"]) for row in tools or () if row.get("name") in SCOPED}


def test_each_regime_changes_only_the_last_sentence(tmp_path):
    for tool in _tools(tmp_path):
        specs = {regime: tool.model_spec_for_path_scope(regime) for regime in REGIMES}
        texts = {regime: spec.description for regime, spec in specs.items()}
        assert all(text.startswith(tool.path_scope_description_base) for text in texts.values())
        assert texts[PATH_SCOPE_FULL] == tool.model_spec.description
        assert texts[PATH_SCOPE_FULL] == tool.path_scope_description_base + ORIGINAL_TAILS[tool.model_spec.name]
        assert texts[PATH_SCOPE_NORMAL] == texts[PATH_SCOPE_FULL] + NORMAL_EXCEPTIONS
        walled = texts[PATH_SCOPE_OWNER_WALL]
        assert "任意绝对路径" not in walled and "capability_request" not in walled
        assert "PATH_OWNER_SCOPE_BLOCKED" in walled and "如实说明缺哪个文件" in walled
        assert {spec.schema_hash for spec in specs.values()} == {tool.model_spec.schema_hash}


def test_each_regime_text_matches_what_the_path_gate_decides(tmp_path):
    owner = tmp_path / ".my-agent" / "owners" / "local" / "main"
    danger = tmp_path / "danger"
    inside, shared = owner / "notes.md", tmp_path / ".my-agent" / "shared" / "skill.md"
    outside, risky, secret = tmp_path / "project" / "a.txt", danger / "a.txt", tmp_path / "project" / ".netrc"
    cases = {
        PATH_SCOPE_OWNER_WALL: ({"mode": "full", "owner_scope_root": owner},
                                {inside: "", shared: "", outside: "PATH_OWNER_SCOPE_BLOCKED",
                                 risky: "PATH_OWNER_SCOPE_BLOCKED"}),
        PATH_SCOPE_FULL: ({"mode": "full"}, {outside: "", risky: "", secret: ""}),
        PATH_SCOPE_NORMAL: ({"mode": "normal"}, {outside: "", risky: "PATH_DANGEROUS_ROOT_BLOCKED",
                                                  secret: "PATH_CREDENTIAL_FILE_BLOCKED"}),
    }
    for regime, (kwargs, expected) in cases.items():
        assert path_scope_regime(kwargs.get("owner_scope_root"), kwargs["mode"]) == regime, "有墙时 full 也越不过墙"
        policy = PathAccessPolicy.from_values(dangerous_roots=[danger], **kwargs)
        texts = _texts(tmp_path, regime).values()
        for path, code in expected.items():
            decision = policy.check(path)
            assert (decision.allowed, decision.code) == (not code, code), (regime, path)
            assert all(code in text for text in texts), f"{regime} 的说明没写出门会返回的 {code}"


def test_effective_owner_scope_has_one_precedence_for_boundary_gate_and_snapshot():
    child = SimpleNamespace(tools=SimpleNamespace(owner_scope_root=""), subagents=SimpleNamespace(owner_scope_root="/h/child"))
    walled = SimpleNamespace(tools=SimpleNamespace(owner_scope_root="/h/own"), subagents=child.subagents)
    frozen = {"effective_owner_scope_root": "/h/frozen"}
    assert effective_owner_scope_root(child) == "", "Full Access 主 run 没有 owner 墙"
    assert effective_owner_scope_root(child, context_scope=" Task_Local ") == "/h/child"
    assert effective_owner_scope_root(child, context_scope="control_plane", write_boundary=frozen) == "/h/child"
    assert effective_owner_scope_root(child, write_boundary=frozen) == "/h/frozen"
    assert effective_owner_scope_root(walled, write_boundary=frozen) == "/h/own", "agent 自己的墙先于调用方带来的旧值"
    assert effective_owner_scope_root(write_boundary=frozen, registry_scope="/h/own") == "/h/frozen", "执行门先读已冻结的值"
    assert effective_owner_scope_root(write_boundary=None, registry_scope="/h/own") == "/h/own"


# 执行门：Full Access 父注册表自己没有墙，task_local 子代理的墙只经写边界里的冻结值到达门上，门必须先读它。
# 目标放在工作区和 owner home 之外，拒绝应落在授权阶段的 owner 墙上（与 87cf12677 现场同码）。
def test_gate_enforces_the_frozen_child_wall_on_a_full_access_registry(tmp_path):
    (tmp_path / "ws").mkdir()
    agent = _full_access_agent(tmp_path / "ws")
    wall = str(agent.subagents.owner_scope_root)
    target = tmp_path / "outside.txt"
    target.write_text("墙外的材料", encoding="utf-8")
    snapshot = agent.tools.runtime_snapshot(allowed_tools=["read_file"], run_id="gate-run", owner_scope_root=wall)
    outcomes = {}
    for label, boundary in (("child", {"effective_owner_scope_root": wall}), ("main", None)):
        call = canonical_test_call(snapshot, "read_file", {"path": str(target)}, call_id=f"gate-{label}")
        call = replace(call, attempt_id=_register_run(agent, call.run_id) or call.attempt_id)
        result = agent.tools.execute_tool(call, write_boundary=boundary, runtime_snapshot=snapshot).result
        outcomes[label] = (result.ok, result.error_code)
    assert outcomes == {"child": (False, "PATH_OWNER_SCOPE_BLOCKED"), "main": (True, "")}


def test_scope_keeps_schema_and_snapshot_hashes_and_defaults_to_the_registry_wall(tmp_path):
    agent = _full_access_agent(tmp_path)
    wall = str(agent.subagents.owner_scope_root)
    unwalled = agent.tools.runtime_snapshot(run_id="r")
    walled = agent.tools.runtime_snapshot(run_id="r", owner_scope_root=wall)
    assert unwalled.snapshot_hash == walled.snapshot_hash
    for name in SCOPED:
        before, after = unwalled.runtime(name).model_spec, walled.runtime(name).model_spec
        assert before.schema_hash == after.schema_hash
        assert (before.description, after.description) == (_texts(tmp_path, PATH_SCOPE_FULL)[name],
                                                           _texts(tmp_path, PATH_SCOPE_OWNER_WALL)[name])
    view = agent.tools.with_access_policy(access_mode="workspace-write", path_access_mode="normal", owner_scope_root=wall)
    default = view.runtime_snapshot(run_id="r")
    assert {name: default.runtime(name).model_spec.description for name in SCOPED} == _texts(tmp_path, PATH_SCOPE_OWNER_WALL)


# 事故回归：Full Access 父注册表上跑 task_local 子代理，模型实际收到的 tools 必须是墙内说明；
# 子代理 compact 缓存面走同一冻结点、与模型轮逐字一致；同一注册表的主 run（带着像“受限”的自然语言）仍是原句。
def test_full_access_parent_child_run_sends_walled_descriptions(tmp_path, monkeypatch):
    agent = _full_access_agent(tmp_path)
    assert (agent.tools.owner_scope_root, agent.tools.path_access_mode) == ("", "full"), "父注册表没有 owner 墙"
    assert agent.subagents.owner_scope_root, "子代理有自己的 owner 墙"
    allowed = list(SCOPED)
    task = agent.subagents.create_run(params=CreateRunParams(
        goal="读取提供的材料并给出结论", thought="", plan=[], allowed_tools=allowed))
    sent = []

    def send(request):
        wire = json.loads(json.dumps(request.payload, ensure_ascii=False))
        if [row.get("name") for row in wire.get("tools", [])] == ["my_agent_capability_probe"]:
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(wire))[1]
            probe = {"type": "tool_use", "id": "probe", "name": "my_agent_capability_probe", "input": {"nonce": nonce}}
            return {"content": [probe], "stop_reason": "end_turn"}
        sent.append(wire.get("tools", []))
        return {"content": [{"type": "text", "text": "检查完成。"}], "stop_reason": "end_turn"}

    monkeypatch.setattr(http, "post_json", send)
    agent.run_subagent(task.id, dry_run=False, probe=False)
    assert sent, "必须抵达子代理的真实业务请求"
    child = _scoped_rows(sent[0])
    assert {name: row[0] for name, row in child.items()} == _texts(tmp_path, PATH_SCOPE_OWNER_WALL)

    surface = ConversationCompactModelSurface(allowed_tools=tuple(allowed), context_scope="task_local")
    compact = prepare_conversation_compact_provider_surface(agent, surface, run_id=task.id).tools
    assert _scoped_rows(compact) == child, "子代理 compact 缓存面与模型轮读同一份冻结快照"
    again = prepare_conversation_compact_provider_surface(agent, surface, run_id=task.id).tools
    assert json.dumps(again, ensure_ascii=False) == json.dumps(compact, ensure_ascii=False), "同一范围两次渲染字节一致"

    main, _protocol = _tool_snapshots_for_run(agent, RuntimeContextRequest(
        user_prompt="你是受限子代理，只能访问自己的数据目录", inject=[], resume_context=False, allowed_tools=allowed,
        run_id="main-run", task_attributes={"role": "受限的只读助手"}))
    assert {spec.name: spec.description for spec in main.specs if spec.name in SCOPED} == _texts(tmp_path, PATH_SCOPE_FULL)
