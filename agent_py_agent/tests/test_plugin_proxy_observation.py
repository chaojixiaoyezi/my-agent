"""插件代理的观察路径：只读工具成功结果铸 ID 并重写模型投影、不合规整份丢弃、动作工具按候选 ID 发送前复核并回填 _meta。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.plugin_manifest import PluginManifest
from agent_py_agent.agent.plugin_observation import (
    OBSERVATION_CANDIDATE_UNKNOWN,
    OBSERVATION_KEY,
    OBSERVATION_META_EXTENSION,
    OBSERVATION_SCHEMA,
    OBSERVATION_STALE,
)
from agent_py_agent.agent.plugin_runtime import PluginProxyTool, plugin_tool_name
from agent_py_agent.tests.test_plugin_observation import _payload, _Repo
from agent_py_agent.tests.test_plugin_package import _v5_manifest, _wheel

ACTIVATION = "c" * 64
RUN_SCOPE = {"owner_id": "local/main", "owner_home": "/owner", "run_id": "run-1", "task_id": "task-1", "attempt_id": "attempt-1", "turn_id": "turn-1"}


# 类用途: 假插件客户端：固定 v5 安装描述与激活代次，记录发出的参数与 _meta，按预设返回结果。
class _Client:
    def __init__(self, result, repo=None):
        manifest = PluginManifest.from_payload(json.loads(json.dumps(_v5_manifest(_wheel()))))
        self.installation = SimpleNamespace(manifest=manifest)
        self.activation_ref = SimpleNamespace(require=lambda **_k: None, scope=SimpleNamespace(activation_id=ACTIVATION))
        self.runtime_repo = repo
        self.result, self.sent = result, []

    def call_tool(self, tool_name, arguments, **options):
        self.sent.append({"tool": tool_name, "arguments": dict(arguments), "meta": options.get("request_meta")})
        return self.result


# 函数用途: 造一个指向 v5 描述里某工具的代理。
def _proxy(client, tool: str) -> PluginProxyTool:
    return PluginProxyTool(client, tool, SimpleNamespace(name=plugin_tool_name("sample-peek", tool)), SimpleNamespace())


# 函数用途: 插件成功结果：文本正文只放业务字段，观察载荷按合同只进 structuredContent（click 是本包同类动作工具）。
def _read_result(observation):
    body = {"url": "file:///form.html", "count": 2}
    return {"content": json.dumps(body, ensure_ascii=False), "content_blocks": [{"type": "text", "text": json.dumps(body)}],
            "structuredContent": {**body, OBSERVATION_KEY: observation}, "isError": False}


def _observation():
    return {"schema": OBSERVATION_SCHEMA, "target": {"ref": "tab-1", "generation": "3"},
            "candidates": [{"key": "e1", "role": "button", "label": "提交", "actions": ["click"]},
                           {"key": "e2", "role": "button", "label": "取消", "actions": ["click"]}]}


def test_read_result_gets_host_ids_in_the_model_projection_and_the_full_record_in_the_envelope():
    client = _Client(_read_result(_observation()))
    outcome = _proxy(client, "read").execute({"path": "x", "__run_scope": RUN_SCOPE, "__operation_id": "op-1"})
    assert outcome.ok and client.sent[0]["arguments"] == {"path": "x"}, "宿主参数不转发给插件"
    visible = json.loads(outcome.output)["structuredContent"][OBSERVATION_KEY]
    assert set(visible) == {"observation_id", "candidates"} and len(visible["candidates"]) == 2
    assert visible["candidates"][0]["candidate_id"].startswith("cand-") and "key" not in visible["candidates"][0]
    assert visible["candidates"][0]["actions"] == [plugin_tool_name("sample-peek", "click")], "actions 是宿主注册名"
    assert "tab-1" not in outcome.output, "目标引用不进模型可见结果"
    record = outcome.result_envelope["observation"]
    assert record["observation_id"] == visible["observation_id"] and record["activation_id"] == ACTIVATION
    assert record["candidates"][0]["key"] == "e1" and record["target_ref"] == "tab-1" and record["generation"] == "3"
    assert "observation_rejected" not in outcome.result_envelope


def test_malformed_observation_is_dropped_from_the_model_view_and_recorded_as_rejected():
    bad = _observation()
    bad["candidates"][0]["actions"] = ["open"]
    client = _Client(_read_result(bad))
    outcome = _proxy(client, "read").execute({"path": "x", "__run_scope": RUN_SCOPE, "__operation_id": "op-1"})
    assert outcome.ok, "工具结果照常交给模型"
    structured = json.loads(outcome.output)["structuredContent"]
    assert OBSERVATION_KEY not in structured and structured["count"] == 2
    assert outcome.result_envelope == {"observation_rejected": "action_not_declared"}


def test_result_without_observation_key_and_non_observation_tools_are_untouched():
    plain = {"content": "{}", "structuredContent": {"count": 0}, "isError": False}
    outcome = _proxy(_Client(plain), "read").execute({"path": "x", "__run_scope": RUN_SCOPE})
    assert outcome.ok and outcome.result_envelope == {} and json.loads(outcome.output)["structuredContent"] == {"count": 0}


def _recorded(repo, client):
    outcome = _proxy(client, "read").execute({"path": "x", "__run_scope": RUN_SCOPE, "__operation_id": "op-1"})
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
    # 只读插件工具的归档没有 runtime_gate，必须照样进权威事件流（同伴真实链路发现的缺口）
    archive = {"run_id": "run-1", "task_id": "task-1", "operation_id": "op-1", "attempt_id": "attempt-1", "tool": outcome.tool, "ok": True,
               "error_code": "", "idempotency_key": "", "tool_result_envelope": outcome.result_envelope}
    persist_tool_runtime_ledger(SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo), local_store=SimpleNamespace()), archive)
    return json.loads(outcome.output)["structuredContent"][OBSERVATION_KEY]["candidates"]


def test_action_with_a_current_candidate_sends_the_plugin_key_and_generation_in_meta():
    repo = _Repo()
    client = _Client(_read_result(_observation()), repo)
    candidates = _recorded(repo, client)
    client.result = {"content": "{\"clicked\": \"button\"}", "structuredContent": {"clicked": "button"}, "isError": False}
    outcome = _proxy(client, "click").execute({"selector": "#go", "candidate_id": candidates[1]["candidate_id"], "__run_scope": RUN_SCOPE})
    assert outcome.ok and client.sent[-1]["tool"] == "click"
    assert client.sent[-1]["arguments"] == {"selector": "#go", "candidate_id": candidates[1]["candidate_id"]}
    assert client.sent[-1]["meta"][OBSERVATION_META_EXTENSION] == {
        "version": "1", "observation_id": repo.events[-1]["payload"]["observation"]["observation_id"], "key": "e2",
        "target": {"ref": "tab-1", "generation": "3"}}


def test_action_with_unknown_or_stale_candidate_is_rejected_before_sending():
    repo = _Repo()
    client = _Client(_read_result(_observation()), repo)
    candidates = _recorded(repo, client)
    sent_before = len(client.sent)
    outcome = _proxy(client, "click").execute({"selector": "#go", "candidate_id": "cand-0123456789abcdef", "__run_scope": RUN_SCOPE})
    assert (outcome.ok, outcome.error_code, outcome.reported_error_code, outcome.effect_outcome) == (False, "TOOL_INVALID_ARGUMENTS", OBSERVATION_CANDIDATE_UNKNOWN, "not_started")
    assert outcome.result_envelope == {"observation_rejected": OBSERVATION_CANDIDATE_UNKNOWN} and len(client.sent) == sent_before, "未知候选不发送"
    newer = _observation()
    newer["target"]["generation"] = "4"
    client.result = _read_result(newer)
    _recorded(repo, client)
    outcome = _proxy(client, "click").execute({"selector": "#go", "candidate_id": candidates[0]["candidate_id"], "__run_scope": RUN_SCOPE})
    assert outcome.reported_error_code == OBSERVATION_STALE and outcome.effect_outcome == "not_started"
    plain = _proxy(client, "click").execute({"selector": "#go", "__run_scope": RUN_SCOPE})
    assert plain.ok is True and client.sent[-1]["meta"] is None, "不填候选参数保持原选择器路径，不附观察 _meta"
