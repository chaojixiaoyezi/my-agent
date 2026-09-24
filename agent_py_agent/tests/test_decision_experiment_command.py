"""/experiment 授权入口：命令解析、HTTP 入口重新推导 system_task、Gateway 单次授权与重放/重启/Compact 再入；无真实模型或网络。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger
from agent_py_agent.agent.conversation.control_commands import (
    ConversationTaskCommand,
    conversation_task_attributes,
    decision_experiment_task,
    parse_conversation_command,
)
from agent_py_agent.agent.conversation.decision_policy import decision_owner_ref
from agent_py_agent.agent.conversation.decision_service import begin_decision_stage
from agent_py_agent.agent.gateway_parts.http_handlers import handle_ask
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as execute,
)
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at, patch

_COMMAND = "/experiment observe skill_tool 10m 1 50000 分析仓库\n只看结构"
_FROZEN = {"mode": "observe", "point": "skill_tool", "duration_seconds": 600, "max_http_requests": 1, "max_input_tokens": 50000}


def test_command_freezes_parameters_and_leaves_only_task_text():
    command = parse_conversation_command(_COMMAND)
    assert isinstance(command, ConversationTaskCommand) and command.valid
    assert command.prompt == "分析仓库\n只看结构"
    assert command.to_request_payload() == {"kind": "decision_experiment", "attributes": _FROZEN}
    assert conversation_task_attributes(command.to_request_payload()) == {}
    assert decision_experiment_task(command.to_request_payload()) == _FROZEN
    seconds = parse_conversation_command("/EXPERIMENT OBSERVE skill_tool 2h 3 700 任务").attributes["duration_seconds"]
    assert seconds == 7200


@pytest.mark.parametrize("text", [
    "/experiment", "/experiment observe skill_tool 10m 1 50000", "/experiment promote skill_tool 10m 1 50000 任务",
    "/experiment apply planning 10m 1 50000 任务",
    "/experiment observe planning 10m 1 50000 任务", "/experiment observe skill_tool 10m 0 50000 任务",
    "/experiment observe skill_tool 10m 1 0 任务", "/experiment observe skill_tool 0m 1 50000 任务",
    "/experiment observe skill_tool 10x 1 50000 任务", "/experiment observe skill_tool 10m 1.5 50000 任务",
])
def test_invalid_experiment_commands_return_usage_without_task(text):
    command = parse_conversation_command(text)
    assert isinstance(command, ConversationTaskCommand) and command.kind == "decision_experiment"
    assert command.valid is False and "用法" in command.usage and "经验值" in command.usage


@pytest.mark.parametrize("attributes", [{**_FROZEN, "extra": 1}, {**_FROZEN, "mode": "Apply"}, {**_FROZEN, "mode": ["apply"]},
                                        {**_FROZEN, "max_http_requests": True},
                                        {**_FROZEN, "max_input_tokens": 0}, {**_FROZEN, "point": ["skill_tool"]},
                                        {key: value for key, value in _FROZEN.items() if key != "point"}])
def test_frozen_task_payload_is_validated_strictly(attributes):
    assert decision_experiment_task({"kind": "decision_experiment", "attributes": attributes}) is None
    assert decision_experiment_task({"kind": "audit_prepare", "attributes": _FROZEN}) is None


# LLM: 只模拟 HTTP handler 的读正文和回写接口；无认证中间件时按原合同视为本机可信入口，不访问网络。
# 类用途: 驱动真实 handle_ask，把结果留在内存中供断言。
class _AskHandler:
    def __init__(self, body):
        self.body, self.headers, self.client_address, self.responses = body, {}, ("127.0.0.1", 1), []

    def _read_json(self):
        return dict(self.body)

    def _send_json(self, status, payload):
        self.responses.append((status, payload))


@pytest.mark.parametrize("goal,prompt,expected", [
    (_COMMAND, "分析仓库\n只看结构", {"kind": "decision_experiment", "attributes": _FROZEN}),
    ("普通消息，不是命令", "普通消息，不是命令", None),
])
def test_http_ingress_discards_client_system_task_and_rederives_from_text(tmp_path, goal, prompt, expected):
    forged = {"kind": "decision_experiment", "attributes": {**_FROZEN, "max_http_requests": 99, "max_input_tokens": 10**9}}
    handler = _AskHandler({"goal": goal, "system_task": forged})
    handle_ask(handler, SimpleNamespace(agent=object(), paths=SimpleNamespace(inbox=tmp_path)), lambda: "req-1")
    assert handler.responses == [(202, {"request_id": "req-1", "status": "queued"})]
    written = json.loads((tmp_path / "req-1.json").read_text(encoding="utf-8"))
    assert written.get("system_task") == expected and written["goal"] == prompt


def test_invalid_command_is_answered_at_ingress_and_never_queued(tmp_path):
    handler = _AskHandler({"goal": "/experiment observe skill_tool 10m 1 50000"})
    handle_ask(handler, SimpleNamespace(agent=object(), paths=SimpleNamespace(inbox=tmp_path)), lambda: "req-1")
    assert handler.responses[0][1]["ok"] is False and handler.responses[0][1]["kind"] == "decision_experiment"
    assert not list(tmp_path.iterdir())


# LLM: 真实 settings/ConversationStore/Gateway 请求文件与精确回合转换锁；params 模拟主轮已绑定的 run/attempt。
# 函数用途: 建立一条处于 processing 的 /experiment 请求及其写入器，用于授权入口测试。
@pytest.fixture
def turn(tmp_path):
    host = host_at(tmp_path / "owner")
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    key, _ = decision(host)
    patch(host, {"enabled": True, "experiment_enabled": True, "profile_id": key})
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.processing.mkdir(parents=True)
    command = parse_conversation_command(_COMMAND)
    request = {"id": "req-1", "kind": "ask", "status": "processing", "turn_phase": "open", "execution_attempt_id": "exec-1",
               "user_id": "alice", "metadata": {"channel": "chat", "user_id": "alice"}, "goal": command.prompt,
               "system_task": command.to_request_payload()}
    request_path = paths.processing / "req-1.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    notices = []
    params = SimpleNamespace(thread_id=thread.thread_id, run_id="run-1", task_id="", attempt_id="attempt-1", request_id="req-1",
                             task_attributes={"conversation_thread_id": thread.thread_id}, on_chunk=notices.append)
    return SimpleNamespace(host=host, thread=thread, request_path=request_path, notices=notices, params=params,
                           writer=GatewayTaskBindingWriter(request_path, "req-1", request, "exec-1"))


def _authorization(turn):
    return execute(turn.host, "read", {"scope": "thread"}, thread_id=turn.thread.thread_id)["experiment_authorization"]


def _stored(turn):
    return json.loads(turn.request_path.read_text(encoding="utf-8")).get("experiment_grant")


def test_gateway_grant_binds_exact_turn_once_and_informs_user(turn):
    receipt = turn.writer.grant_decision_experiment(turn.host, turn.params)
    assert receipt["status"] == "granted" and (receipt["run_id"], receipt["attempt_id"]) == ("run-1", "attempt-1")
    assert _stored(turn) == receipt == turn.writer.request["experiment_grant"]
    authorization = _authorization(turn)
    assert authorization["authorization_id"] == receipt["authorization_id"]
    assert authorization["binding"] == {"owner_ref": decision_owner_ref(turn.host), "thread_id": turn.thread.thread_id,
                                        "task_id": "run-1", "run_id": "run-1", "attempt_id": "attempt-1", "request_id": "req-1",
                                        "ledger_id": turn.host._model_call_ledger.ledger_id}
    assert {key: authorization["source"][key] for key in ("owner_id", "actor_id", "channel", "request_id")} == {
        "owner_id": "alice", "actor_id": "alice", "channel": "chat", "request_id": "req-1"}
    assert (authorization["points"], authorization["duration_seconds"], authorization["max_http_requests"],
            authorization["max_input_tokens"]) == (["skill_tool"], 600, 1, 50000)
    assert authorization["input_bound_policy"] == "empirical:jev_wire_bytes.v1"
    assert len(turn.notices) == 1 and "empirical:jev_wire_bytes.v1" in turn.notices[0]
    assert turn.writer.grant_decision_experiment(turn.host, turn.params) is None
    assert _authorization(turn)["authorization_id"] == receipt["authorization_id"] and len(turn.notices) == 1
    stage = begin_decision_stage(turn.host, turn.params, operation_id="skill_tool:op", experiment=True)
    assert stage.error_code == "" and stage.enabled_points == ("skill_tool",)


def test_restart_replay_never_regrants_and_old_grant_cannot_follow_new_ledger(turn):
    receipt = turn.writer.grant_decision_experiment(turn.host, turn.params)
    turn.host._model_call_ledger = ModelCallLedger()
    reloaded = {**json.loads(turn.request_path.read_text(encoding="utf-8")), "execution_attempt_id": "exec-2"}
    turn.request_path.write_text(json.dumps(reloaded, ensure_ascii=False), encoding="utf-8")
    writer = GatewayTaskBindingWriter(turn.request_path, "req-1", reloaded, "exec-2")
    assert writer.grant_decision_experiment(turn.host, turn.params) is None
    assert _stored(turn) == receipt and _authorization(turn)["authorization_id"] == receipt["authorization_id"]
    stage = begin_decision_stage(turn.host, turn.params, operation_id="skill_tool:op", experiment=True)
    assert stage.error_code == "experiment_ledger_changed"


def test_compact_reentry_attempt_neither_regrants_nor_inherits_old_grant(turn):
    turn.writer.grant_decision_experiment(turn.host, turn.params)
    reentry = SimpleNamespace(**{**vars(turn.params), "attempt_id": "attempt-2"})
    assert turn.writer.grant_decision_experiment(turn.host, reentry) is None
    stage = begin_decision_stage(turn.host, reentry, operation_id="skill_tool:op", experiment=True)
    assert stage.error_code == "experiment_identity_changed" and stage.enabled_points == ()


@pytest.mark.parametrize("change,code", [({"experiment_enabled": False}, "experiment_disabled"), ({"enabled": False}, "experiment_disabled")])
def test_rejected_grant_is_recorded_and_only_informs_user(turn, change, code):
    patch(turn.host, change)
    receipt = turn.writer.grant_decision_experiment(turn.host, turn.params)
    assert receipt["status"] == "rejected" and receipt["code"] == code and _stored(turn) == receipt
    assert code in turn.notices[0] and "照常执行" in turn.notices[0]
    assert _authorization(turn) is None and not hasattr(turn.host, "_model_call_ledger")
    assert turn.writer.grant_decision_experiment(turn.host, turn.params) is None


def test_missing_authenticated_actor_is_rejected_without_authorization(turn):
    turn.writer.request.pop("user_id")
    assert turn.writer.grant_decision_experiment(turn.host, turn.params)["code"] == "source_identity_invalid"
    assert _authorization(turn) is None


@pytest.mark.parametrize("change", [{"status": "done"}, {"cancel_requested": True}, {"execution_attempt_id": "exec-other"},
                                    {"turn_phase": "closing"}])
def test_closed_or_foreign_turn_cannot_grant_or_write_receipt(turn, change):
    current = json.loads(turn.request_path.read_text(encoding="utf-8"))
    turn.request_path.write_text(json.dumps({**current, **change}, ensure_ascii=False), encoding="utf-8")
    assert turn.writer.grant_decision_experiment(turn.host, turn.params) is None
    assert _stored(turn) is None and _authorization(turn) is None and not turn.notices


@pytest.mark.parametrize("system_task", [None, {"kind": "decision_experiment", "attributes": {**_FROZEN, "mode": "promote"}},
                                         {"kind": "audit_prepare", "attributes": _FROZEN}])
def test_requests_without_valid_frozen_experiment_are_ignored(turn, system_task):
    turn.writer.request["system_task"] = system_task
    assert turn.writer.grant_decision_experiment(turn.host, turn.params) is None
    assert _stored(turn) is None and _authorization(turn) is None and not turn.notices


def test_core_grants_after_binding_and_before_first_model_call(monkeypatch):
    order = []
    monkeypatch.setattr(runtime_mixin, "bind_cli_run_conversation", lambda _agent, params, _prompt: params)
    monkeypatch.setattr(runtime_mixin, "_bind_main_agent_turn_params",
                        lambda _agent, _prompt, params: (order.append("bind"), replace(params, attempt_id="attempt-1"))[1])
    monkeypatch.setattr(runtime_mixin, "_run_once_with_params",
                        lambda _agent, _prompt, params: order.append("model:" + params.attempt_id) or SimpleNamespace())
    monkeypatch.setattr(runtime_mixin, "compact_auto_continuation_decision", lambda *_a, **_k: SimpleNamespace(should_continue=False))
    for name in ("finish_run_task_workspace_if_needed", "_settle_main_agent_run", "persist_cli_run_assistant"):
        monkeypatch.setattr(runtime_mixin, name, lambda *_a, **_k: None)
    callback = SimpleNamespace(grant_decision_experiment=lambda _agent, params: order.append("grant:" + params.attempt_id))
    runtime_mixin._run_with_params(object(), "任务", RunParams(request_id="req-1", conversation_task_binding_callback=callback))
    assert order == ["bind", "grant:attempt-1", "model:attempt-1"]


def test_core_hook_ignores_callbacks_without_grant_and_never_blocks_on_failure():
    def broken(_agent, _params):
        raise RuntimeError("授权回调故障")

    runtime_mixin._grant_host_decision_experiment(object(), RunParams(conversation_task_binding_callback=object()))
    runtime_mixin._grant_host_decision_experiment(
        object(), RunParams(conversation_task_binding_callback=SimpleNamespace(grant_decision_experiment=broken)))
