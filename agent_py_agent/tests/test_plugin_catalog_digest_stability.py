"""激活目录摘要必须跨运行时版本稳定：声明新增的可选字段为空时不改变旧安装的摘要（2026-09-25 真实 owner 回归）。"""
from __future__ import annotations

import hashlib
import json

from agent_py_agent.agent.plugin_activation_record import plugin_catalog_digest
from agent_py_agent.agent.plugin_manifest import PluginToolDeclaration, PluginToolObservation

_SCHEMA = json.dumps({"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False})


def _tool(name: str, **extra) -> PluginToolDeclaration:
    return PluginToolDeclaration(name=name, description=f"{name} 工具", input_schema_json=_SCHEMA, requested_effect="read_only", **extra)


def _legacy_digest(tools) -> str:
    # v5 之前的公式：只有四个声明字段，这是所有既有激活记录里 catalog_sha256 的算法。
    rows = sorted(
        ({"name": t.name, "description": t.description, "input_schema_json": t.input_schema_json, "requested_effect": t.requested_effect}
         for t in tools),
        key=lambda row: row["name"],
    )
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_digest_of_a_pre_v5_manifest_is_unchanged_by_the_new_optional_fields():
    tools = (_tool("tree"), _tool("read"))
    manifest = type("M", (), {"tools": tools})()
    assert plugin_catalog_digest(manifest) == _legacy_digest(tools), "旧安装升级后必须仍对上原 catalog_sha256"


def test_declared_observation_changes_the_digest_and_order_does_not():
    plain = type("M", (), {"tools": (_tool("read"), _tool("tree"))})()
    observed = type("M", (), {"tools": (_tool("read", observation=PluginToolObservation("page", 50)), _tool("tree"))})()
    reordered = type("M", (), {"tools": (_tool("tree"), _tool("read"))})()
    assert plugin_catalog_digest(plain) != plugin_catalog_digest(observed)
    assert plugin_catalog_digest(plain) == plugin_catalog_digest(reordered)
