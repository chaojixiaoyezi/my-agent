"""第 14 条：派子代理时把父会话的图片/视频按结构化引用交给子代理（默认关）。

锁定：
- 开关关：工具 schema/说明与原来逐字节一致、不注入附件清单、传 input_media_refs 整批 SUBAGENT_INPUT_MEDIA_DISABLED；
- 开关开：本轮附件清单（media_ref=sha256）作为宿主事实进当前回合 IR；create_subagents 多 input_media_refs；
  宿主只在父级本轮附件和父级 transcript 的 canonical 用户媒体块里查找，未知/重复/格式错/超限整批 not_started；
  模型塞进 attributes 的 input_media 被丢弃；解析结果进 child 任务属性 input_media；
- 真实 child 首个业务请求的用户消息带该媒体（供应商看到的是原件 base64），首轮选模的冻结请求按同一模态规则判定；
  child 线程 canonical 行带 local_file 引用，压缩侧的媒体事实统计看得到它。
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core import orchestration_tools
from agent_py_agent.agent.agent_core.orchestration.input_media_refs import (
    INPUT_MEDIA_DISABLED_ERROR_CODE,
    INPUT_MEDIA_INVALID_ERROR_CODE,
    SubagentInputMediaError,
    bind_subagent_input_media,
)
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.agent_core.runtime.loop_support import _with_input_media_manifest
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.input_media import (
    INPUT_MEDIA_MANIFEST_SOURCE,
    import_input_media,
    input_media_manifest_text,
    input_media_root,
)
from agent_py_agent.agent.conversation.native_history import CANONICAL_NATIVE_MESSAGES_METADATA_KEY
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


# 函数用途: 把开关写进该 agent 的用户 capability 配置文件（<root>/config/capability_config.yaml），走运行时真实读法。
def _enable_input_media(agent: SimpleAgent, enabled: bool = True) -> Path:
    from agent_py_agent.agent.capability.runtime_config_reload import default_capability_config_path

    path = default_capability_config_path(agent.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"subagent_input_media_enabled: {'true' if enabled else 'false'}\n", encoding="utf-8")
    agent._capability_config_runtime_snapshot = None
    return path


# 函数用途: 建一个真实 SimpleAgent（echo 后端，不发请求），开关按参数写进 capability 配置（主配置里没有这个键）。
def _agent(tmp_path: Path, *, enabled: bool, **overrides) -> SimpleAgent:
    config = AgentConfig(model_backend="echo", enable_subagents=True, max_subagents=10, **overrides)
    agent = SimpleAgent(config, tmp_path)
    _enable_input_media(agent, enabled)
    return agent


# 函数用途: 把一张图导进当前 owner 的附件根，返回已验证引用（与 Gateway 入口写进 task_attributes 的形状一致）。
def _import_image(agent: SimpleAgent, tmp_path: Path, name: str = "chart.png", data: bytes = _PNG) -> dict:
    source = tmp_path / "incoming" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)
    return import_input_media(source, input_media_root(agent))


# 函数用途: 模拟主会话本轮的运行参数：Gateway 已把附件引用写进 task_attributes.input_media。
def _bind_current_turn(agent: SimpleAgent, refs: list[dict], **extra) -> None:
    agent._current_run_params = SimpleNamespace(task_attributes={"input_media": list(refs), **extra})


def test_manifest_lists_media_refs_without_paths():
    ref = {"path": "/owner/media/input/x", "sha256": "a" * 64, "media_type": "image/png", "size_bytes": 5, "name": "c.png"}
    text = input_media_manifest_text([ref, {"bad": True}])
    assert text.startswith("[INPUT_MEDIA_MANIFEST]")
    assert "不需要工具读取或解码" in text.splitlines()[1], "首行软提示：附件已直接给模型"
    payload = json.loads(text.splitlines()[-1])
    assert payload == {"schema_version": "input-media-manifest.v1",
                       "media": [{"media_ref": "a" * 64, "name": "c.png", "media_type": "image/png", "size_bytes": 5}]}
    assert "/owner/media" not in text, "清单不含路径"
    assert input_media_manifest_text([]) == "" and input_media_manifest_text(None) == ""


def test_manifest_is_appended_only_when_switch_is_true():
    media = ({"sha256": "b" * 64, "media_type": "image/png", "size_bytes": 3, "name": "b.png", "path": "/p"},)
    history = [UserTurn("# User Task\n看图", media=media), RuntimeFactsTurn("x", source="carried")]
    enabled = SimpleNamespace(_capability_config_runtime_snapshot=SimpleNamespace(
        config=CapabilityConfig(subagent_input_media_enabled=True)))
    result = _with_input_media_manifest(enabled, history)
    assert result[:2] == history and len(result) == 3, "清单追加在末尾，不动开头项与交接"
    assert isinstance(result[2], RuntimeFactsTurn) and result[2].source == INPUT_MEDIA_MANIFEST_SOURCE
    assert ("b" * 64) in result[2].text
    # 关闭（随包默认）、MagicMock（替身自动属性不是 CapabilityConfig）、主配置里写了也不算、没有附件：原样返回
    disabled = SimpleNamespace(_capability_config_runtime_snapshot=SimpleNamespace(config=CapabilityConfig()))
    main_config_only = SimpleNamespace(config=SimpleNamespace(subagent_input_media_enabled=True))
    for agent in (disabled, main_config_only, MagicMock()):
        assert _with_input_media_manifest(agent, history) is history
    assert _with_input_media_manifest(enabled, [UserTurn("纯文字")]) == [UserTurn("纯文字")]


def test_tool_spec_adds_input_media_refs_only_when_switch_is_true(tmp_path):
    off = CreateSubagentsTool(_agent(tmp_path / "off", enabled=False)).model_spec
    on = CreateSubagentsTool(_agent(tmp_path / "on", enabled=True)).model_spec
    assert off.schema_hash == CreateSubagentsTool.model_spec.schema_hash, "关闭时与原说明逐字节一致"
    assert "input_media_refs" not in off.input_schema["properties"]
    assert "input_media_refs" not in off.parameter_descriptions
    assert on.input_schema["properties"]["input_media_refs"]["type"] == "array"
    item_props = on.input_schema["properties"]["items"]["items"]["properties"]
    assert "media_ref" in item_props["input_media_refs"]["description"]
    assert "不传路径" in on.parameter_descriptions["input_media_refs"]
    mock_agent = MagicMock()
    assert CreateSubagentsTool(mock_agent).model_spec.schema_hash == off.schema_hash, "MagicMock 不算开"
    main_only = SimpleNamespace(config=SimpleNamespace(subagent_input_media_enabled=True), root=tmp_path / "main-only")
    assert CreateSubagentsTool(main_only).model_spec.schema_hash == off.schema_hash, "主配置里的同名键不算开"


def test_refs_are_rejected_when_switch_is_off(tmp_path):
    agent = _agent(tmp_path, enabled=False)
    ref = _import_image(agent, tmp_path)
    _bind_current_turn(agent, [ref])
    outcome = orchestration_tools.execute_create_subagents_service(
        agent, {"goal": "分析这张图", "input_media_refs": [ref["sha256"]]})
    assert outcome.ok is False and outcome.error_code == INPUT_MEDIA_DISABLED_ERROR_CODE
    assert outcome.effect_outcome == "not_started" and agent.subagents.list_runs() == []
    assert json.loads(outcome.output)["requested_media_refs"] == [ref["sha256"]]


def test_refs_resolve_from_current_turn_and_bind_child_attributes(tmp_path):
    agent = _agent(tmp_path, enabled=True)
    ref = _import_image(agent, tmp_path)
    _bind_current_turn(agent, [ref])
    outcome = orchestration_tools.execute_create_subagents_service(
        agent, {"items": [{"goal": "分析这张图的趋势", "input_media_refs": [ref["sha256"]]},
                          {"goal": "只做文字整理"}]})
    assert outcome.ok is True, outcome.output
    runs = {task.attributes.get("goal", task.goal): task for task in agent.subagents.list_runs()}
    with_media = next(task for task in agent.subagents.list_runs() if task.goal == "分析这张图的趋势")
    text_only = next(task for task in agent.subagents.list_runs() if task.goal == "只做文字整理")
    assert with_media.attributes["input_media"] == [ref], "child 任务属性里是已验证的完整引用"
    assert "input_media" not in text_only.attributes
    assert len(runs) == 2
    from agent_py_agent.agent.subagents.context_bundle_refs import runtime_task_attributes

    assert runtime_task_attributes(agent.subagents.load(with_media.id))["input_media"] == [ref]


def test_unknown_duplicate_or_malformed_refs_reject_the_whole_batch(tmp_path):
    agent = _agent(tmp_path, enabled=True)
    ref = _import_image(agent, tmp_path)
    other = _import_image(agent, tmp_path, name="other.png", data=_PNG + b"\n")
    _bind_current_turn(agent, [ref])  # other 已导入 owner 附件根，但不属于本会话：不能靠文件存在放行
    unknown = "f" * 64
    outcome = orchestration_tools.execute_create_subagents_service(
        agent, {"items": [{"goal": "一", "input_media_refs": [ref["sha256"]]},
                          {"goal": "二", "input_media_refs": [other["sha256"], unknown, "not-a-sha", ref["sha256"], ref["sha256"]]}]})
    assert outcome.ok is False and outcome.error_code == INPUT_MEDIA_INVALID_ERROR_CODE
    assert outcome.effect_outcome == "not_started" and agent.subagents.list_runs() == [], "整批零创建"
    issues = {(row["media_ref"], row["reason_code"]) for row in json.loads(outcome.output)["invalid_media_refs"]}
    assert issues == {(other["sha256"], "not_found"), (unknown, "not_found"),
                      ("not-a-sha", "malformed"), (ref["sha256"], "duplicate")}


def test_refs_resolve_from_parent_transcript_rows(tmp_path):
    agent = _agent(tmp_path, enabled=True)
    ref = _import_image(agent, tmp_path)
    thread = agent.conversation_store.threads.get_or_create(
        {"channel": "test", "channel_conversation_id": "parent-chat", "channel_user_id": "u1"})
    envelope = {"schema": "conversation_native_messages.v1", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "看图"},
                                     {"type": "image", "source": {"type": "local_file", **ref}}]}]}
    agent.conversation_store.messages.append({
        "thread_id": thread.thread_id, "role": "user", "content": "看图",
        "metadata": {CANONICAL_NATIVE_MESSAGES_METADATA_KEY: envelope},
    })
    # 本轮没有附件，只有 conversation_thread_id：从父级 transcript 的 canonical 行找到历史附件
    agent._current_run_params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    attrs: dict = {"input_media": [{"path": "/etc/passwd", "sha256": "0" * 64}]}
    bind_subagent_input_media(attrs, {"input_media_refs": [ref["sha256"]]}, agent)
    assert attrs["input_media"] == [ref], "模型塞进 attributes 的 input_media 被丢弃，换成宿主解析结果"
    # 别的线程的附件对本会话不可见
    other_thread = agent.conversation_store.threads.get_or_create(
        {"channel": "test", "channel_conversation_id": "other-chat", "channel_user_id": "u2"})
    assert other_thread.thread_id != thread.thread_id
    agent._current_run_params = SimpleNamespace(task_attributes={"conversation_thread_id": other_thread.thread_id})
    with pytest.raises(SubagentInputMediaError) as info:
        bind_subagent_input_media({}, {"input_media_refs": [ref["sha256"]]}, agent)
    assert info.value.payload["invalid_media_refs"] == [{"media_ref": ref["sha256"], "reason_code": "not_found"}]


def test_model_supplied_attributes_input_media_is_dropped_without_refs(tmp_path):
    agent = _agent(tmp_path, enabled=True)
    attrs = {"input_media": [{"path": "/etc/passwd", "sha256": "0" * 64}], "keep": 1}
    bind_subagent_input_media(attrs, {"goal": "x"}, agent)
    assert attrs == {"keep": 1}


def test_media_limits_apply_to_child_batch(tmp_path):
    agent = _agent(tmp_path, enabled=True, input_media_max_files=1)
    first = _import_image(agent, tmp_path)
    second = _import_image(agent, tmp_path, name="two.png", data=_PNG + b"\n")
    _bind_current_turn(agent, [first, second])
    with pytest.raises(SubagentInputMediaError) as info:
        bind_subagent_input_media({}, {"input_media_refs": [first["sha256"], second["sha256"]]}, agent)
    rows = info.value.payload["invalid_media_refs"]
    assert {row["reason_code"] for row in rows} == {"limit"} and "最多允许 1 个附件" in rows[0]["message"]


def test_nested_child_creation_resolves_media_from_child_turn(tmp_path):
    from agent_py_agent.agent.agent_core.hierarchy_tools import _hierarchy_child_spec
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    agent = _agent(tmp_path, enabled=True)
    ref = _import_image(agent, tmp_path)
    previous = set_current_subagent_context(agent, run_id="child-run", attempt_id="a1",
                                            task_attributes={"input_media": [ref]})
    try:
        spec = _hierarchy_child_spec(agent, {"goal": "孙代理看图", "input_media_refs": [ref["sha256"]]},
                                     tool_name="create_subagents")
        assert spec.attributes["input_media"] == [ref]
        rejected = _hierarchy_child_spec(agent, {"goal": "孙代理看图", "input_media_refs": ["e" * 64]},
                                         tool_name="create_subagents")
        assert rejected.error_code == INPUT_MEDIA_INVALID_ERROR_CODE and rejected.effect_outcome == "not_started"
    finally:
        restore_current_subagent_context(agent, previous)


# LLM: 真实产品链：根服务按 media_ref 创建 child（属性绑定）→ 真实 runner 首个业务请求（假 HTTP 只回放回复）。
#   带宿主模型建议的 child 才进首轮选模准备，所以用于发送的 child 由 manager 带与服务绑定同形状的 attributes 建。
#   不启动 Gateway、不发真实请求。
# 函数用途: 建一个父代理点名了图片的 child 并真实跑完首个业务请求，返回供应商看到的载荷、冻结请求、任务与引用。
def _run_child_with_media(tmp_path: Path, monkeypatch) -> tuple[dict, object, object, dict, SimpleAgent]:
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.backends import http
    from agent_py_agent.agent.settings.thread_model_selection import PendingSubagentModelAdvice
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(
        model_backend="anthropic_compatible", model_name="vision-model", api_base="https://relay.example.test/anthropic",
        api_key="fake-private-key", stream_enabled=False, enable_tools=False, enable_subagents=True, max_subagents=10,
        max_tool_rounds=1, model_context_window_tokens=200_000, model_input_modalities=["text", "image"],
    ), tmp_path)
    _enable_input_media(agent)
    ref = _import_image(agent, tmp_path)
    _bind_current_turn(agent, [ref])
    outcome = orchestration_tools.execute_create_subagents_service(
        agent, {"goal": "描述这张图里有什么", "input_media_refs": [ref["sha256"]]})
    assert outcome.ok is True, outcome.output
    bound = agent.subagents.list_runs()[0].attributes["input_media"]
    assert bound == [ref]
    params = CreateRunParams(goal="描述这张图里有什么", thought="", plan=[], attributes={"input_media": bound})
    prepared = agent.subagents.base_service.prepare_run(params=params)
    advice = PendingSubagentModelAdvice(
        profile_id="00000000-0000-0000-0000-000000000001", operation_id="create-operation",
        source_owner_ref="owner", source_thread_id="parent-thread", source_run_id="parent-run", source_task_id="parent-task",
        source_owner_revision=1, source_thread_revision=0, child_run_id=prepared.task.id, child_thread_id=prepared.task.agent_thread_id,
    )
    task = agent.subagents.create_run(params=params, prepared=replace(prepared, model_advice=advice))
    observed: list[tuple[dict, object]] = []

    def send(request):
        wire = json.loads(json.dumps(request.payload, ensure_ascii=False))
        if not observed:
            preparation = model_selection._PREPARATION.get()
            observed.append((wire, None if preparation is None else preparation.request_input))
        return {"content": [{"type": "text", "text": "图里是一个像素点。"}], "stop_reason": "end_turn"}

    monkeypatch.setattr(http, "post_json", send)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert observed, f"必须抵达真实 child 首次业务发送: {result}"
    wire, frozen = observed[0]
    return wire, frozen, task, ref, agent


# 函数用途: 子代理首个请求的用户消息真的带上父代理点名的图片原件（base64），不泄露本机路径，并自带附件清单。
def test_child_first_request_carries_parent_media_bytes(tmp_path, monkeypatch):
    wire, _frozen, _task, _ref, _agent = _run_child_with_media(tmp_path, monkeypatch)
    user_messages = [row for row in wire["messages"] if row.get("role") == "user"]
    images = [block for row in user_messages for block in row["content"] if block.get("type") == "image"]
    assert len(images) == 1 and images[0]["source"]["type"] == "base64"
    assert base64.b64decode(images[0]["source"]["data"]) == _PNG, "供应商拿到的是父会话附件的原件字节"
    assert images[0]["source"]["media_type"] == "image/png"
    assert all("local_file" not in json.dumps(row) for row in user_messages), "发送前已展开，不泄露本机路径"
    assert "[INPUT_MEDIA_MANIFEST]" in json.dumps(wire["messages"], ensure_ascii=False), "child 自己也能把图再派给孙代理"


# LLM: 首轮选模冻结的请求按 J11 同一函数判定模态；child 线程 canonical 行带 local_file，压缩侧 media_archive_facts 统计得到。
# 函数用途: 子代理带图时，首轮选模与压缩统计看到的是同一份结构化媒体事实。
def test_child_media_reaches_first_request_modality_and_compact_facts(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_request_projection import (
        candidate_input_modality_decision,
    )
    from agent_py_agent.agent.conversation.compact_media_policy import media_archive_facts

    _wire, frozen, task, ref, agent = _run_child_with_media(tmp_path, monkeypatch)
    assert frozen is not None, "带宿主模型建议的 child 首轮冻结请求必须存在"
    assert candidate_input_modality_decision(frozen, ["text"]).reason_code == "candidate_input_modalities_missing"
    assert candidate_input_modality_decision(frozen, ["text", "image"]).status == "applicable"
    rows = agent.conversation_store.messages.recent(task.agent_thread_id, limit=0)
    facts = media_archive_facts(rows)
    assert facts.blocks == 1 and facts.refs == (ref["sha256"],)


def test_requested_refs_accept_json_string_and_trim(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.input_media_refs import requested_media_refs

    assert requested_media_refs(json.dumps([" a ", "", "b"])) == ["a", "b"]
    assert requested_media_refs(["x", None, "  "]) == ["x"]
    assert requested_media_refs(None) == []
    assert re.fullmatch(r"[a-f0-9]{64}", _PNG and ("0" * 64))


# LLM: 开关住在 capability 配置（AGENTS.md：子代理相关参数不进主配置），是改变模型可见内容的功能开关：登记表默认 False、
#   模型不可写、在 USER_SETTINGS_BOUNDARY_KEYS 里；只有已认证管理员 /settings 的写作用域能开关，写进运行时读的 capability 文件。
# 函数用途: 开关只能由管理员 /settings 翻，模型翻被拒；主配置 AgentConfig 没有这个字段。
def test_switch_lives_in_capability_config_and_only_admin_settings_can_flip(tmp_path):
    import os
    import stat

    from agent_py_agent.agent.capability.config import load_capability_config
    from agent_py_agent.agent.settings.parameter_changes import (
        ChangeOrigin,
        WritePaths,
        set_parameter,
        user_settings_write_scope,
    )
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    from agent_py_agent.agent.settings.user_config_capability import USER_SETTINGS_BOUNDARY_KEYS

    assert not hasattr(AgentConfig(), "subagent_input_media_enabled"), "主配置里没有这个键"
    assert CapabilityConfig().subagent_input_media_enabled is False
    spec = parameter_registry()["subagent_input_media_enabled"]
    assert spec.default is False and spec.writable is False and spec.source == "capability"
    assert "subagent_input_media_enabled" in USER_SETTINGS_BOUNDARY_KEYS
    user_config = tmp_path / "user.yaml"
    user_config.write_text("", encoding="utf-8")
    capability = tmp_path / "owner" / "config" / "capability_config.yaml"
    paths = WritePaths(user_path=user_config, capability_path=capability)
    refused = set_parameter("subagent_input_media_enabled", True, paths=paths, origin=ChangeOrigin("model"))
    assert refused["ok"] is False and refused["code"] == "PARAMETER_BOUNDARY" and not capability.exists()
    with user_settings_write_scope():
        report = set_parameter("subagent_input_media_enabled", True, paths=paths, origin=ChangeOrigin("chat"))
    assert report["ok"] is True, report
    assert load_capability_config(capability).subagent_input_media_enabled is True
    assert stat.S_IMODE(os.stat(capability).st_mode) == 0o600
