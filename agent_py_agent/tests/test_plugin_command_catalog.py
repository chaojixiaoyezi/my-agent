from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.command_arguments import ArgumentSpec, CommandActionSpec
from agent_py_agent.agent.plugin_command_catalog import PluginCommandCatalog
from agent_py_agent.agent.plugin_commands import PluginCommandSpec, parse_plugin_command
from agent_py_agent.agent.plugin_completion import complete_plugin_command


def _plugin():
    return PluginCommandSpec(
        "sample",
        "说明",
        (
            CommandActionSpec(
                "read",
                "读取",
                (
                    ArgumentSpec("files", "输入", multiple=True, required=True),
                    ArgumentSpec("limit", "条数", ("-n", "--limit"), "integer", default=2),
                    ArgumentSpec("strict", "严格", ("--strict",), "boolean", default=False),
                ),
                kind="tool",
                target="sample.read",
            ),
        ),
        enabled=True,
        package_version="1.0",
        activation_id="activation-a",
    )


def test_wire_declarations_drive_parser_and_completion_without_importing_plugin():
    source = PluginCommandCatalog("owner-view-a", plugins=(_plugin(),))
    result = PluginCommandCatalog.from_payload(json.loads(json.dumps(source.to_payload())))
    assert result == source
    parsed = parse_plugin_command(
        '/plugins@sample read "中文 路径" "C:\\new folder\\" -n 5 --strict',
        plugins=result.plugins,
        management_actions=result.management_actions,
    )
    assert dict(parsed.arguments.values) == {
        "files": ("中文 路径", "C:\\new folder\\"),
        "limit": 5,
        "strict": True,
    }
    assert parsed.action.target == "sample.read"
    assert [
        item.text
        for item in complete_plugin_command(
            "/plugins@s",
            plugins=result.plugins,
            management_actions=result.management_actions,
        )
    ] == ["/plugins@sample"]


def test_owner_schema_version_and_activation_changes_invalidate_revision():
    plugin = _plugin()
    base = PluginCommandCatalog("owner-view-a", plugins=(plugin,))
    alternatives = [
        PluginCommandCatalog("owner-view-b", plugins=(plugin,)),
        PluginCommandCatalog("owner-view-a", plugins=(replace(plugin, enabled=False),)),
        PluginCommandCatalog("owner-view-a", plugins=(replace(plugin, package_version="2.0"),)),
        PluginCommandCatalog("owner-view-a", plugins=(replace(plugin, installation_revision=2, installation_ref="a" * 64),)),
        PluginCommandCatalog(
            "owner-view-a", plugins=(replace(plugin, activation_id="activation-b"),)
        ),
        PluginCommandCatalog("owner-view-a", plugins=()),
    ]
    assert all(item.revision != base.revision for item in alternatives)
    assert len({item.revision for item in alternatives}) == len(alternatives)


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row.update(revision="forged"),
        lambda row: row.update(schema_version="unknown.v2"),
        lambda row: row.update(schema_version="plugin_command_catalog.v1"),
        lambda row: row.update(schema_version="plugin_command_catalog.v2"),
        lambda row: row.update(owner="forged"),
        lambda row: row.pop("management_actions"),
        lambda row: row["plugins"][0].update(enabled="false"),
        lambda row: row["plugins"][0].pop("installation_revision"),
        lambda row: row["plugins"][0].update(installation_revision=True),
        lambda row: row["plugins"][0].update(installation_revision=-1),
        lambda row: row["plugins"][0].pop("installation_ref"),
        lambda row: row["plugins"][0].update(installation_revision=1, installation_ref=""),
        lambda row: row["plugins"][0].update(installation_revision=1, installation_ref="../bad"),
        lambda row: row["plugins"][0].update(installation_ref="a" * 64),
        lambda row: row["plugins"][0]["actions"][0]["arguments"][1].update(required="false"),
        lambda row: row["plugins"][0].update(actions="invalid"),
        lambda row: row["plugins"].append(row["plugins"][0]),
    ],
)
def test_invalid_wire_catalog_is_not_accepted(change):
    payload = PluginCommandCatalog("owner-view-a", plugins=(_plugin(),)).to_payload()
    change(payload)
    with pytest.raises(ValueError):
        PluginCommandCatalog.from_payload(payload)


def test_catalog_payload_does_not_alias_the_frozen_snapshot():
    source = PluginCommandCatalog("owner-view-a", plugins=(_plugin(),))
    payload = source.to_payload()
    payload["plugins"][0]["actions"][0]["arguments"][0]["summary"] = "changed"
    assert source.plugins[0].actions[0].arguments[0].summary == "输入"


def test_management_completion_uses_the_host_schema():
    action = CommandActionSpec(
        "show", "宿主声明", (ArgumentSpec("name", "名称", choices=("甲", "乙")),)
    )
    catalog = PluginCommandCatalog("owner-view-a", management_actions=(action,))
    result = complete_plugin_command("/plugins sh", management_actions=catalog.management_actions)
    assert [item.label for item in result] == ["show"]
    parsed = parse_plugin_command("/plugins show 甲", management_actions=catalog.management_actions)
    assert parsed.action is action and parsed.arguments.values["name"] == "甲"
