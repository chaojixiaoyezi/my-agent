"""user_config 的 decision_patch 通道（2026-09-28，生产上连续被拒三次，Jev 设置改不了）。

背景：user_config 各动作共用一份扁平 schema，reason、fields、profile_id、timeout_seconds 等别的动作的字段也能通过
schema 校验；工具把除 action 外的全部字段转给决策设置服务，服务按操作严格拒收多余字段，但旧回执只说
“决策设置请求包含未知字段”，模型看不出是哪个字段，只会反复改 changes，于是对任何 changes 都失败。
锁定：
- 经真实工具执行入口，合法 decision_patch 落盘、revision 递增；
- 多带的字段仍被拒（不替模型删字段），回执与 result_envelope 写明 unknown_fields 与 allowed_fields，什么都不写入；
- 设置服务本身仍严格拒绝未知字段，异常仍是 ModelProfileError，原捕获点不变；
- 假模型按自然语言要求调等待时间：先按生产里的写法多带 reason 被拒，按回执删掉字段重试后成功落盘。
"""
import json
import re

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.decision_settings import execute_decision_settings_operation
from agent_py_agent.agent.settings.decision_settings_schema import DecisionSettingsUnknownFields
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend
from agent_py_agent.tests.test_decision_settings import host_at

_PATCH_FIELDS = ["changes", "expected_revision", "scope"]


# 函数用途: 经真实工具执行器（schema 校验、授权门、handler）调一次 user_config，返回工具结果。
def _call(tmp_path, tool, arguments):
    return execute_canonical_test_call(tmp_path, tools={"user_config": tool}, tool_name="user_config",
                                       arguments=arguments).result


# 函数用途: 读当前 owner/thread revision 与后台等待有效值。
def _read(host):
    view = execute_decision_settings_operation(host, "read", {})
    return view["revision"], view["effective"]["background_timeout_seconds"]


def test_a_valid_patch_through_the_real_tool_entry_persists_and_bumps_the_revision(tmp_path):
    host = host_at(tmp_path)
    revision, _before = _read(host)
    result = _call(tmp_path, UserConfigTool(host), {
        "action": "decision_patch", "expected_revision": revision, "changes": {"background_timeout_seconds": 15}})
    assert result.ok
    after, seconds = _read(host)
    assert seconds == 15 and after == {"owner": revision["owner"] + 1, "thread": revision["thread"]}


@pytest.mark.parametrize("extra", [
    {"reason": "用户要求把后台等待调到 15 秒"}, {"fields": ["timeout_seconds"]}, {"timeout_seconds": 15},
    {"profile_id": "some-profile"},
])
def test_foreign_fields_are_still_rejected_and_named(tmp_path, extra):
    host = host_at(tmp_path)
    revision, before = _read(host)
    result = _call(tmp_path, UserConfigTool(host), {
        "action": "decision_patch", "expected_revision": revision, "changes": {"background_timeout_seconds": 15},
        **extra})
    # 与生产回执同形：schema 放行、handler 已执行、效果未开始；区别是现在指名道姓。
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert (result.handler_executed, result.effect_outcome) == (True, "not_started")
    payload = json.loads(result.output)
    assert (payload["unknown_fields"], payload["allowed_fields"]) == (sorted(extra), _PATCH_FIELDS)
    assert list(extra)[0] in payload["message"]
    assert result.metadata["handler_details"]["decision_request_fields"]["unknown_fields"] == sorted(extra)
    assert _read(host) == (revision, before)  # 什么都没写


def test_reset_names_the_field_it_does_not_take(tmp_path):
    host = host_at(tmp_path)
    revision, _before = _read(host)
    result = UserConfigTool(host).execute({"action": "decision_reset", "expected_revision": revision,
                                           "fields": ["timeout_seconds"], "changes": {"timeout_seconds": 3}})
    payload = json.loads(result.output)
    assert not result.ok and payload["unknown_fields"] == ["changes"]
    assert payload["allowed_fields"] == ["expected_revision", "fields", "scope"]


def test_the_settings_service_itself_stays_strict(tmp_path):
    host = host_at(tmp_path)
    revision, _before = _read(host)
    with pytest.raises(DecisionSettingsUnknownFields) as caught:
        execute_decision_settings_operation(host, "patch", {
            "expected_revision": revision, "changes": {"background_timeout_seconds": 15}, "bogus": 1, "action": "x"})
    assert (caught.value.fields, caught.value.allowed) == (("action", "bogus"), tuple(_PATCH_FIELDS))
    assert isinstance(caught.value, ModelProfileError)


# 函数用途: 从原生消息里取出指定工具调用的结果文本（兼容字符串与分块内容）。
def _tool_result(messages, call_id):
    blocks = [block for message in messages for block in message.get("content") or []
              if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") == call_id]
    if not blocks:
        return None
    content = blocks[0].get("content")
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


# 函数用途: 从工具结果文本里取出第一个 JSON 对象；取不到时返回 None（旧回执是纯文本）。
def _first_json(text):
    try:
        return json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
    except ValueError:
        return None


# 类用途: 按“把后台决策等待时间调到 15 秒”行事的假模型：先读设置，再按生产里的写法多带 reason 提交；
#   回执给出 unknown_fields 时删掉这些字段重试一次，否则收尾。
class _AdjustWaitBackend(_TestNativeBackend):
    name = "adjust-wait"

    def __init__(self) -> None:
        self.patch_results = []
        self._arguments = {}

    # 函数用途: 按已拿到的工具结果决定下一步。
    def generate(self, prompt, on_chunk=None, **kwargs):
        messages = [message for message in kwargs.get("messages") or [] if isinstance(message, dict)]
        read = _tool_result(messages, "read")
        if read is None:
            return self._use("read", {"action": "decision_read"})
        if "patch-1" not in json.dumps(messages, ensure_ascii=False):
            owner, thread = re.search(r'"revision": \{"owner": (\d+), "thread": (\d+)\}', read).groups()
            self._arguments = {"action": "decision_patch", "expected_revision": {"owner": int(owner), "thread": int(thread)},
                               "changes": {"background_timeout_seconds": 15}, "reason": "用户要求把后台等待调到 15 秒"}
            return self._use("patch-1", self._arguments)
        first = _tool_result(messages, "patch-1")
        second = _tool_result(messages, "patch-2")
        if second is None:
            refusal = _first_json(first) or {}
            self.patch_results.append(refusal)
            if refusal.get("unknown_fields"):
                retry = {key: value for key, value in self._arguments.items() if key not in refusal["unknown_fields"]}
                return self._use("patch-2", retry)
        return ModelResponse(text="已处理。", backend=self.name)

    # 函数用途: 返回一次原生工具调用。
    def _use(self, call_id, arguments):
        return ModelResponse(text="", backend=self.name, tool_use_blocks=[
            {"id": call_id, "name": "user_config", "input": arguments}])


def test_fake_model_adjusts_the_background_wait_from_a_natural_request(tmp_path):
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl", my_agent_home=str(tmp_path / "home")),
                        tmp_path)
    backend = _AdjustWaitBackend()
    agent.backend = backend
    revision, _before = _read(agent)
    agent.run("把后台决策等待时间调到 15 秒。", save=False, allowed_tools=["user_config"])
    assert backend.patch_results and backend.patch_results[0]["unknown_fields"] == ["reason"]
    after, seconds = _read(agent)
    assert seconds == 15 and after == {"owner": revision["owner"] + 1, "thread": revision["thread"]}
