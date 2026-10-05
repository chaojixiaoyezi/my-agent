"""安装表旧协议迁移链覆盖（opp-final-fix）：v1/v2/v3 → v4 的只读解码与提交迁移。

只用合成包与临时 owner，不启动插件；断言只读查询不写盘、解码内容与当前记录等价、
提交后写 v4 且迁移来源正确、坏表不覆盖原字节。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.tests.test_plugin_configuration import configure_request, installed


# 函数用途: 读安装表文件，返回 (字节, mtime_ns)。
def _file_stamp(path):
    return path.read_bytes(), path.stat().st_mtime_ns


# 函数用途: v1 记录形态：删 v4 字段、补旧停用协议字段。
def _to_v1(row):
    del row["settings_json"], row["settings_revision"]
    del row["activation"], row["last_commit"]["activation_sha256"]
    row.update(enabled=False, activation_id="")
    del row["last_commit"]["action"], row["last_commit"]["settings_sha256"]


# 函数用途: v2 记录形态：保留设置，删激活与回执激活摘要。
def _to_v2(row):
    del row["activation"], row["last_commit"]["activation_sha256"]
    row.update(enabled=False, activation_id="")


# 函数用途: v3 记录形态：删 v4 顶层 legacy_permission_grant。
def _to_v3(row):
    del row["legacy_permission_grant"]


_DOWNGRADERS = {
    "plugin_installations.v1": _to_v1,
    "plugin_installations.v2": _to_v2,
    "plugin_installations.v3": _to_v3,
}


# LLM: 旧表由当前记录按协议删字段模拟；legacy_permission_grant 是 v4 顶层字段，
#   旧协议迁移忽略它（产物强制 None）——保留它可覆盖该兼容路径。
# 函数用途: 把当前 v4 记录改造成指定旧协议形态并写回文件。
def _downgrade_to(path, version):
    payload = json.loads(path.read_text())
    payload["schema_version"] = version
    if version == "plugin_installations.v1":
        del payload["migration"]  # v1 表没有文件级迁移来源字段
    for row in payload["installations"]:
        _DOWNGRADERS[version](row)
    path.write_text(json.dumps(payload))
    return payload


@pytest.mark.parametrize("version", [
    "plugin_installations.v1", "plugin_installations.v2", "plugin_installations.v3",
])
def test_legacy_table_reads_readonly_then_migrates_on_commit(tmp_path, version):
    """旧表只读解码不写盘；提交后写 v4 且迁移来源指向旧协议。"""
    store, entry, _ = installed(tmp_path)
    path = store.root / "installations.json"
    _downgrade_to(path, version)
    before = _file_stamp(path)

    assert store.snapshot() == (entry,)  # 可解码且与当前记录等价
    assert _file_stamp(path) == before  # 只读查询不写盘

    store.configure(configure_request(entry))
    current = json.loads(path.read_text())
    assert current["schema_version"] == "plugin_installations.v4"
    assert current["migration"]["from_schema"] == version
    assert current["migration"]["source_sha256"] == hashlib.sha256(before[0]).hexdigest()
    assert store.snapshot()[0].settings_json == '{"limit":3}'


# LLM: 损坏表必须显式拒绝且不产生任何写副作用；来源摘要只在真实提交时写。
# 函数用途: 坏表解码失败时原字节与 mtime 完全不变。
def test_broken_legacy_table_never_overwrites_original(tmp_path):
    store, _entry, _ = installed(tmp_path)
    path = store.root / "installations.json"
    payload = json.loads(path.read_text())
    payload["schema_version"] = "plugin_installations.v3"
    del payload["installations"][0]["legacy_permission_grant"]
    del payload["installations"][0]["manifest"]  # 旧协议必填字段缺失 → 解码必须拒绝
    path.write_text(json.dumps(payload))
    before = _file_stamp(path)

    with pytest.raises(PluginInstallationError):
        store.snapshot()
    assert _file_stamp(path) == before
