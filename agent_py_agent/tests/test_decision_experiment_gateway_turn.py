"""真实 Gateway 回合里的实验对照：原 SimpleAgent.run/设置/Registry/RuntimeDB/Gateway 请求文件与转换锁均不替换；
只把主模型生成换成 typed 假答复（可发起一次真实工具调用），Jev 连本地 HTTP 服务并统计请求，不调用真实供应商。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation.control_commands import parse_conversation_command
from agent_py_agent.agent.conversation.decision_policy import decision_owner_ref
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_binding import (
    CAPABILITY_OBSERVATION_KEY,
    GatewayTaskBindingWriter,
)
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as execute,
)
from agent_py_agent.tests.test_decision_capability_http import (
    capability_http as capability_http,  # noqa: F401
)
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_gateway_capability_compact import (
    gateway_surface as gateway_surface,  # noqa: F401
)
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)

KEY = "experiment_records"


# LLM: 原 gateway_surface 夹具再接本地 HTTP 决策连接；点普通模式 off、实验能力开，Skill 目录取前 10 个以落在经验上界标定内。
# 函数用途: 准备一个可以真实授权并发送一次只观察实验的 Gateway 会话。
@pytest.fixture
def gateway_lab(gateway_surface, capability_http):  # noqa: F811
    agent = gateway_surface.agent
    key, _ = decision(agent, api_base=capability_http.url)
    patch(agent, {"profile_id": key, "timeout_seconds": 1.5, "points.skill_tool.mode": "off", "experiment_enabled": True})
    full = agent.current_skill_snapshot()
    subset = replace(full, entries=full.entries[:10], fingerprint="experiment-subset")
    agent.current_skill_snapshot = lambda: subset
    agent.skill_snapshot_for_run_scope = lambda _root: subset
    gateway_surface.http = capability_http
    return gateway_surface


# LLM: 只改写本条排队请求的冻结 system_task 与已鉴权身份字段，与 HTTP 入口重推后的形状一致。
# 函数用途: 把夹具请求变成一条 /experiment 请求（mode 为 observe 或 apply）。
def experiment_request(fixture, mode):
    command = parse_conversation_command(f"/experiment {mode} skill_tool 10m 1 50000 核对来源")
    request = fixture.context.request
    request.update(system_task=command.to_request_payload(), user_id="alice", metadata={"channel": "chat", "user_id": "alice"})
    fixture.context.request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")


# LLM: 首次生成发起一次真实原生工具调用（经原 Registry/ToolExecutor 执行并入工具账），第二次给出最终答复。
# 函数用途: 以 typed 假答复驱动原工具循环，使实际工具用量来自真实工具账。
def fake_main_model(monkeypatch, tool_name):
    calls = []

    def generate(request):
        calls.append(request.params)
        if len(calls) == 1 and tool_name:
            return ModelResponse(text="", backend="fixture",
                                 tool_use_blocks=[{"id": "call-1", "name": tool_name, "input": {"text": "看一眼"}}])
        return ModelResponse(text="核对完成", backend="fixture")

    monkeypatch.setattr(_tool_loop_service, "generate_model_response", generate)
    return calls


def stored(fixture):
    return json.loads(fixture.context.request_path.read_text(encoding="utf-8"))


def test_experiment_turn_records_one_sample_and_realized_tools_from_the_real_tool_ledger(gateway_lab, monkeypatch):
    experiment_request(gateway_lab, "observe")
    fake_main_model(monkeypatch, "presentation_optional_a")
    result = request_execution._run_gateway_ask(gateway_lab.context)
    assert result.response == "核对完成" and gateway_lab.first.calls == 1
    payload = stored(gateway_lab)
    assert payload["experiment_grant"]["status"] == "granted" and len(gateway_lab.http.requests) == 1
    [entry] = payload[KEY]["entries"]
    assert entry["status"] == "completed" and entry["settlement"]["outcome"] == "charged"
    assert entry["settlement"]["provider_input_tokens"] == 41 and entry["candidate"]["status"] == "projected"
    assert entry["realized"]["known"] is True and entry["realized"]["tool_names"] == ["presentation_optional_a"]
    assert entry["refs"]["request_id"] == gateway_lab.context.request_id
    assert entry["refs"]["owner_ref"] == decision_owner_ref(gateway_lab.agent)
    [observation] = payload[CAPABILITY_OBSERVATION_KEY]["entries"]
    assert observation["mode"] == "observe" and "experiment_record" not in observation
    assert "promotion" not in payload[KEY] and "核对来源" not in json.dumps(payload[KEY], ensure_ascii=False)


def test_ordinary_turn_writes_no_experiment_keys(gateway_lab, monkeypatch):
    fake_main_model(monkeypatch, "presentation_optional_a")
    request_execution._run_gateway_ask(gateway_lab.context)
    payload = stored(gateway_lab)
    assert KEY not in payload and "experiment_grant" not in payload and CAPABILITY_OBSERVATION_KEY not in payload
    assert not gateway_lab.http.requests


# LLM: 历史实验请求沿真实 E1 授权入口在同一会话授予（形成授权指针链），其已完成样本按原终态目录落盘。
# 函数用途: 为 Gateway 会话准备若干条已完成、已归档的历史实验样本。
def prior_samples(fixture, count):
    agent, thread_id = fixture.agent, fixture.conversation.thread_id
    paths = gateway_paths(agent)
    paths.terminal.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        request_id = f"prior-{index}"
        request = {"id": request_id, "status": "processing", "turn_phase": "open", "execution_attempt_id": "exec-" + request_id,
                   "user_id": "alice", "metadata": {"channel": "chat"},
                   "system_task": parse_conversation_command("/experiment observe skill_tool 10m 1 50000 x").to_request_payload()}
        path = paths.processing / f"{request_id}.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        params = type("Params", (), {"thread_id": thread_id, "run_id": "run-" + request_id, "task_id": "",
                                     "attempt_id": "attempt-" + request_id, "request_id": request_id, "on_chunk": None,
                                     "task_attributes": {"conversation_thread_id": thread_id}})()
        assert GatewayTaskBindingWriter(path, request_id, request, "exec-" + request_id).grant_decision_experiment(agent, params)
        saved = {**json.loads(path.read_text(encoding="utf-8")), KEY: {"entries": [_completed(fixture, request_id)]}}
        (paths.terminal / path.name).write_text(json.dumps(saved), encoding="utf-8")
        path.unlink()


def _completed(fixture, request_id):
    return {"schema": "decision_experiment_record.v1", "record_id": "call-" + request_id, "status": "completed",
            "point": "skill_tool", "settlement": {"outcome": "charged"},
            "refs": {"owner_ref": decision_owner_ref(fixture.agent), "thread_id": fixture.conversation.thread_id},
            "candidate": {"status": "projected", "shortlist_names": ["presentation_optional_a"],
                          "deferred_names": ["presentation_optional_b"], "deferred_count": 1, "names_truncated": False},
            "realized": {"known": True, "tool_names": ["presentation_optional_a"], "names_truncated": False}}


@pytest.mark.parametrize("mode,applied", [("apply", True), ("observe", False)])
def test_apply_turn_promotes_through_the_real_chain_and_observe_turn_never_does(gateway_lab, monkeypatch, mode, applied):
    prior_samples(gateway_lab, 2)
    experiment_request(gateway_lab, mode)
    fake_main_model(monkeypatch, "presentation_optional_a")
    request_execution._run_gateway_ask(gateway_lab.context)
    payload = stored(gateway_lab)
    assert payload["experiment_grant"]["previous_request_id"] == "prior-1"
    view = execute(gateway_lab.agent, "read", {"scope": "thread"}, thread_id=gateway_lab.conversation.thread_id)
    mode_now = view["effective"]["points"]["skill_tool"]["effective_mode"]
    assert (mode_now == "apply") is applied and ("promotion" in payload[KEY]) is applied
    if applied:
        receipt = payload[KEY]["promotion"]
        assert receipt["status"] == "applied" and receipt["evaluation"]["comparable_count"] == 3
        assert receipt["after"]["revision"] == view["revision"]
