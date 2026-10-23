# LLM: v9测试复用旧固定fixture核兼容，仅解析静态清单，不启动插件。
# 模块用途: 锁定显式v9能力版本门及v1–v8序列化兼容。
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_manifest import PluginManifest, PluginPackageError
from agent_py_agent.tests.test_plugin_manifest_v8 import legacy_payload


# LLM: 只更改新协议声明，入口和文件沿旧v6合法样本。
# 函数用途: 提供可被安装器读回的v9清单。
def v9_payload(capabilities=None):
    payload = legacy_payload(6)
    payload.update(schema_version="plugin_package.v9", host_api=capabilities or ["read", "chat", "act_as_user"])
    return payload


# LLM: 初始基线不认识v9，断言在真实旧解析器上先变红。
# 函数用途: 验证显式版本、能力集合及序列化往返。
def test_v9_manifest_roundtrip():
    payload = v9_payload()
    try:
        manifest = PluginManifest.from_payload(payload)
    except PluginPackageError:
        manifest = None
    assert manifest is not None, "v9文件清单必须被显式解析而不是拒绝或降成v6"
    assert manifest.host_api == ("read", "chat", "act_as_user")
    assert manifest.host_api_v9 is True
    assert manifest.to_payload() == payload
    assert PluginManifest.from_payload(manifest.to_payload()) == manifest


# LLM: 新协议不能改变旧read包的版本与字节选择。
# 函数用途: 验证只有read的v9也保持显式v9。
def test_read_only_v9_keeps_explicit_version_and_legacy_stays_legacy():
    payload = v9_payload(["read"])
    manifest = PluginManifest.from_payload(payload)
    assert manifest.to_payload() == payload
    legacy = PluginManifest.from_payload(legacy_payload(6))
    assert legacy.host_api_v9 is False
    assert legacy.to_payload() == legacy_payload(6)


# LLM: 角色不能隐式展开，未知/重复/空申请一律拒绝。
# 函数用途: 验证v9能力枚举和类型边界。
@pytest.mark.parametrize("capabilities", [[], ["read", "read"], ["admin"], ["write"], ["*"],
                                          [True], "read", ["assets.source"], ["execute", None]])
def test_invalid_capabilities_rejected(capabilities):
    payload = v9_payload()
    payload["host_api"] = capabilities
    with pytest.raises(PluginPackageError):
        PluginManifest.from_payload(payload)


# LLM: 文件入口版本门保持闭合；空events也不能夹带进入v9。
# 函数用途: 拒绝v8订阅、Python入口、宿主授权和多余字段。
@pytest.mark.parametrize("field,value", [("events", []), ("tool_gates", []), ("permissions", {}),
                                        ("entry_module", "plugin"), ("host_api_grant", {}), ("owner_id", "main")])
def test_v9_closed_fields(field, value):
    payload = v9_payload()
    payload[field] = value
    with pytest.raises(PluginPackageError):
        PluginManifest.from_payload(payload)


# LLM: 旧版本不能通过新集合取得高权限，直接构造也同门。
# 函数用途: 验证v4/v6不接受chat及v9不得替换成轮子入口。
def test_legacy_cannot_gain_v9_capabilities():
    for version in (4, 6):
        payload = legacy_payload(version)
        payload["host_api"] = ["chat"]
        with pytest.raises(PluginPackageError):
            PluginManifest.from_payload(payload)
    manifest = PluginManifest.from_payload(v9_payload())
    with pytest.raises(ValueError):
        replace(manifest, host_api_v9=False)
    python = PluginManifest.from_payload(legacy_payload(1))
    with pytest.raises(ValueError):
        replace(python, host_api_v9=True, host_api=("read",))
