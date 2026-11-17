"""Wave S：package_build 收到被模型工具调用序列化改写过的插件声明。

实测 MiniMax-M2.7 调 package_build 时，声明里的空数组、空对象都变成空字符串，值为 null 的键（动作参数的 default）被省略；
用户明确要求写 [] 和 {} 后仍如此。宿主读包器只回"声明集合必须是数组"/"声明字段不完整"，模型改不过来。
锁定：这种改写在协议集合位置上被恢复，打出的包与原样声明逐字节相同；不是集合位置的类型错误照旧拒绝。
"""
from __future__ import annotations

from agent_py_agent.tests.test_package_build_tool import _agent, _params, _run, _template_plugin


# 函数用途: 按实测的模型序列化方式改写声明：空数组/空对象写成空字符串，null 值的键整个省略。
def _as_serialized_by_model(value: object) -> object:
    if isinstance(value, dict):
        return {key: ("" if item in ([], {}) else _as_serialized_by_model(item))
                for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_as_serialized_by_model(item) for item in value]
    return value


# 函数用途: 被改写的模板声明照样打成 v6 插件，且与原样声明打出的包是同一个 sha256。
def test_model_serialized_empties_build_the_same_plugin(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    root, declaration = _template_plugin(agent)
    serialized = _as_serialized_by_model(declaration)
    assert serialized["entry"]["args"] == "" and "default" not in serialized["actions"][0]["arguments"][0]
    ok, body, code = _run(agent, _params(root, kind="plugin", declaration=serialized, origin="自己写的", license="自有"))
    assert ok, (code, body)
    clean_ok, clean, _ = _run(agent, _params(root, kind="plugin", declaration=declaration, origin="自己写的", license="自有"))
    assert clean_ok and body["build"]["sha256"] == clean["build"]["sha256"]


# 函数用途: 非集合位置的空字符串（如入口命令）不被猜补，仍按原校验拒绝且不写存储。
def test_empty_string_outside_collection_slots_is_still_refused(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    root, declaration = _template_plugin(agent)
    declaration["actions"][0]["arguments"][0]["summary"] = ""
    declaration["entry"]["args"] = "--flag"
    ok, message, code = _run(agent, _params(root, kind="plugin", declaration=declaration, origin="自己写的", license="自有"))
    assert not ok and code in {"PACKAGE_BUILD_OUTPUT_INVALID", "PACKAGE_BUILD_DECLARATION_INVALID"}, (code, message)
