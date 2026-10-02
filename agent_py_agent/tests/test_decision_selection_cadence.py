"""选模型询问节奏（J4）：points.model_selection.cadence = structure_change 时只在结构变化才问决策模型。

背景：选模型默认每条消息都问一次 Jev（2026-09 生产里是决策调用的大头），而多数消息的“结构”（会话是否压缩过、可选模型、
当前模型）并没有变，问出来的建议也一样。锁定：
1. 设置：字段登记、校验只收两个值、配置/随包 YAML 默认 every_turn（关）、owner 与会话两层都能覆盖；
2. structure_change：第一轮（新会话）照常问并记指纹；结构没变的下一轮不提交、不调用，到达原因记 structure_unchanged；
   压缩代数、候选目录、当前模型任一变化都重新问；询问失败不记指纹，下一轮照常再问；
3. every_turn（默认）：每轮都问，且不读不写指纹文件（行为与改动前一致）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.decision_selection_cadence import (
    SELECTION_STRUCTURE_THREADS_COUNT,
    STRUCTURE_UNCHANGED,
    remember_selection_structure,
    selection_structure,
    selection_structure_unchanged,
)
from agent_py_agent.agent.gateway_parts import request_binding, request_context, request_execution
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.decision_settings_defaults import decision_config_fields
from agent_py_agent.agent.settings.decision_settings_schema import validate_decision_field
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
from agent_py_agent.agent.settings.user_config_capability import packaged_config_path
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_gateway_model_observation import (  # noqa: F401  复用选模型观察的真实请求夹具
    _optional_admission,
    capture_main,
    install_backend,
    prepared,
    reach,
)
from agent_py_agent.tests.test_model_profiles import add

PATH = "points.model_selection.cadence"


# LLM: 测试辅助：在同一会话（同一 channel_conversation_id）里放一条新请求，经原预检得到同一线程；不改产品状态。
# 函数用途: 生成同一会话的下一条 Gateway 请求上下文。
def _next_request(fixture, request_id: str):
    owner = fixture.agent.home_paths.owner_id
    request = {"id": request_id, "kind": "ask", "prompt": "请整理当前资料并给出下一步建议", "status": "processing",
               "turn_phase": "open", "execution_attempt_id": f"transport-{request_id}",
               "conversation": {"canonical_user_id": owner, "channel": "chat", "channel_conversation_id": "session",
                                "channel_user_id": owner}}
    path = fixture.paths.processing / f"{request_id}.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    context = request_context.GatewayAskRunContext(fixture.agent, request, path, fixture.paths.responses / f"{request_id}.json",
                                                   request_id, None)
    request_context.preflight_gateway_conversation(request_context.GatewayConversationLoadRequest(
        fixture.agent, request, request_id, request["prompt"]))
    return context


# LLM: 测试辅助：直接调用 _run_gateway_ask 不会走请求终态收尾，前台车道认领一直占着，同一会话的下一条请求会永远等；
#   跑完后按产品同一结构化入口（claims.finish）释放本请求的认领。
# 函数用途: 跑一条请求、释放它的会话车道认领，并返回它的选模型观察标记。
def _run(fixture, context) -> dict:
    assert request_execution._run_gateway_ask(context) == "original-main"
    fixture.agent.conversation_store.claims.finish({
        "thread_id": fixture.thread_id, "expected_task_id": f"gateway:{context.request_id}", "recover_same_task_only": True,
        "status": "finished", "runtime_facts": {"execution_source": "gateway", "request_id": context.request_id}})
    return read_json_file(context.request_path)[request_binding.MODEL_OBSERVATION_KEY]


# 函数用途: 指纹文件路径（与产品同一规范目录）。
def _structure_file(fixture):
    return fixture.agent.home_paths.owner_decision_outcomes_jsonl.with_name("model_selection_structure.json")


def test_the_cadence_field_is_registered_validated_and_off_by_default(tmp_path):
    assert decision_config_fields()[PATH] == ("agent", "decision_model_selection_cadence")
    assert AgentConfig().decision_model_selection_cadence == "every_turn"
    assert load_config(packaged_config_path()).decision_model_selection_cadence == "every_turn"
    assert validate_decision_field(PATH, "structure_change") == "structure_change"
    for bad in ("weekly", "", True, None):
        with pytest.raises(ModelProfileError):
            validate_decision_field(PATH, bad)
    path = tmp_path / "agent.yaml"
    path.write_text("decision_model_selection_cadence: sometimes\n", encoding="utf-8")
    with pytest.raises(ModelProfileError):
        load_config(path)


def test_structure_change_asks_once_then_skips_until_the_structure_changes(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    backend = install_backend(monkeypatch, fixture)
    capture_main(monkeypatch)
    view = patch(fixture.agent, {PATH: "structure_change"})
    assert view["effective"]["points"]["model_selection"]["cadence"] == "structure_change"

    assert _run(fixture, fixture.context)["status"] == "observed" and len(backend.calls) == 1
    skipped = _run(fixture, _next_request(fixture, "second"))
    assert (skipped["status"], skipped["reason"]) == ("skipped", STRUCTURE_UNCHANGED) and len(backend.calls) == 1
    assert reach(fixture) == ({STRUCTURE_UNCHANGED: 1}, 1)

    # 当前模型被换掉（经正式的会话模型选择入口）也算结构变化。压缩代数变化见下面的指纹单测（真实压缩需要完整检查点）。
    thread_model_profile_id(fixture.agent, fixture.thread_id, select=fixture.candidate)
    assert _run(fixture, _next_request(fixture, "model-switched"))["status"] == "observed" and len(backend.calls) == 2
    assert _run(fixture, _next_request(fixture, "same-again"))["status"] == "skipped" and len(backend.calls) == 2

    add(fixture.agent, model_name="candidate-new", model_context_window_tokens=200_000)
    assert _run(fixture, _next_request(fixture, "catalog-changed"))["status"] == "observed" and len(backend.calls) == 3


def test_a_failed_ask_is_not_remembered_so_the_next_turn_asks_again(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    capture_main(monkeypatch)
    patch(fixture.agent, {PATH: "structure_change"})

    def broken(_request):
        raise RuntimeError("决策服务出错")

    failing = install_backend(monkeypatch, fixture, action=broken)
    assert _run(fixture, fixture.context)["status"] != "observed" and len(failing.calls) == 1
    assert not _structure_file(fixture).exists()
    healthy = install_backend(monkeypatch, fixture)
    assert _run(fixture, _next_request(fixture, "retry"))["status"] == "observed" and len(healthy.calls) == 1


def test_every_turn_default_asks_each_time_and_never_touches_the_structure_file(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    backend = install_backend(monkeypatch, fixture)
    capture_main(monkeypatch)

    for request_id in ("", "second", "third"):
        _run(fixture, _next_request(fixture, request_id) if request_id else fixture.context)

    assert len(backend.calls) == 3 and not _structure_file(fixture).exists()
    assert reach(fixture)[0].get(STRUCTURE_UNCHANGED) is None


def test_a_thread_override_wins_over_the_owner_setting(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    backend = install_backend(monkeypatch, fixture)
    capture_main(monkeypatch)
    patch(fixture.agent, {PATH: "structure_change"})
    patch(fixture.agent, {PATH: "every_turn"}, thread_id=fixture.thread_id, scope="thread")

    _run(fixture, fixture.context)
    _run(fixture, _next_request(fixture, "second"))

    assert len(backend.calls) == 2, "本会话覆盖成每轮都问，owner 的结构变化设置对它不生效"


def test_the_structure_file_is_bounded_and_unreadable_files_mean_ask_again(tmp_path):
    home = type("Home", (), {"owner_decision_outcomes_jsonl": tmp_path / "decision" / "outcomes.jsonl"})()
    structure = {"compact_generation": 0, "candidates_revision": "r", "profile_id": "p"}
    for index in range(SELECTION_STRUCTURE_THREADS_COUNT + 3):
        remember_selection_structure(home, f"thread-{index}", structure)
    stored = json.loads((tmp_path / "decision" / "model_selection_structure.json").read_text(encoding="utf-8"))
    assert len(stored["threads"]) == SELECTION_STRUCTURE_THREADS_COUNT and "thread-0" not in stored["threads"]
    assert selection_structure_unchanged(home, f"thread-{SELECTION_STRUCTURE_THREADS_COUNT + 2}", structure)
    assert not selection_structure_unchanged(home, "thread-1", {**structure, "compact_generation": 1})
    (tmp_path / "decision" / "model_selection_structure.json").write_text("{broken", encoding="utf-8")
    assert not selection_structure_unchanged(home, "thread-5", structure)
    assert not selection_structure_unchanged(object(), "thread-5", structure), "没有规范路径的宿主替身不读写"


def test_the_structure_fingerprint_changes_with_compaction_catalog_and_current_model():
    thread = SimpleNamespace(compact_generation=2)
    base = selection_structure(thread, "rev-a", "profile-a")
    assert base == {"compact_generation": 2, "candidates_revision": "rev-a", "profile_id": "profile-a"}
    assert selection_structure(SimpleNamespace(compact_generation=3), "rev-a", "profile-a") != base
    assert selection_structure(thread, "rev-b", "profile-a") != base
    assert selection_structure(thread, "rev-a", "profile-b") != base
