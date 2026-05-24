from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.tests.tool_delivery_repair_fixtures import _write_closeout


# LLM: Invalid artifact repair should move to mutation instead of repeated reads.
# 函数用途: 验证 artifact 已存在但结构无效时，read_file 不算修复推进，避免真实任务陷入读文件循环。
def test_delivery_repair_guard_treats_existing_invalid_artifact_read_as_nonproductive(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 0,
                "no_progress_block_threshold": 4,
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_id": "homepage_html",
                        "artifact_path": "outputs/site/index.html",
                        "finding_codes": ["HTML_INCOMPLETE_DOCUMENT"],
                        "finding_values": ["unbalanced_style"],
                        "write_tools": ["write_file", "replace_in_file", "file_write_session"],
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/other.html"}]) is False
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/site/index.html", "content": "<!doctype html><html></html>"}],
        )
        is True
    )


# LLM: repair-target reads are bounded inspection, not open-ended exploration.
# 函数用途: 验证模型可先读取结构化 repair_targets 指向的文件，但不能把任意 read_file 当修复进展。
def test_delivery_repair_guard_allows_reading_declared_repair_targets(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_id": "site",
                        "artifact_path": "outputs/site/index.html",
                        "repair_targets": ["outputs/site/index.html", "outputs/site/app.js"],
                        "finding_codes": ["STATIC_SITE_MISSING_JS_API_HITS"],
                        "write_tools": ["write_file", "replace_in_file"],
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/index.html"}]) is True
    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/other.html"}]) is False


# LLM: evidence repair context should keep structured source/claim shape details visible to the model.
# 函数用途: 验证 repair guard 不会把 closeout 里的 required_fields/evidence_shape_hint 裁掉。
def test_delivery_repair_context_includes_evidence_repair_shape(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
                        "recommended_action": "repair_evidence_refs",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "required_fields": ["记录名", "地址"],
                        "writer_tool": "write_structured_json",
                        "evidence_shape_hint": '{"source_refs":[],"claims":[]}',
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    context = delivery_repair_context(agent, repairs=0)

    assert "required_fields" in context
    assert "write_structured_json" in context
    assert "evidence_shape_hint" in context
    assert '"merge_existing": true' in context


# LLM: acceptance finding repair requires mutation, not repeated inspection.
# 函数用途: 验证产物验收已经给出结构化 finding 后，read_file 不再算修复推进，避免真实任务读文件空转。
def test_delivery_repair_guard_requires_mutation_for_artifact_finding_repair(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_id": "site",
                        "artifact_path": "outputs/site",
                        "finding_codes": ["STATIC_SITE_MISSING_DOM_ID_HITS"],
                        "finding_values": ["getElementById:app"],
                        "write_tools": ["write_file", "replace_in_file"],
                    }
                ]
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/index.html"}]) is False
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "replace_in_file", "path": "outputs/site/index.html", "old": "<body>", "new": '<body><div id="app">'}],
        )
        is True
    )
    context = delivery_repair_context(agent, repairs=0)
    assert "repair_artifact_against_findings" in context
    assert "getElementById:app" in context


def test_delivery_repair_guard_requires_write_after_repeated_artifact_finding_failure(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 4,
                "no_progress_block_threshold": 4,
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_id": "site",
                        "artifact_path": "outputs/site",
                        "finding_values": ["index.html:button:登录"],
                        "write_tools": ["write_file", "replace_in_file"],
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/index.html"}]) is False
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/site/app.js", "content": "console.log('ok')"}],
        )
        is True
    )


# LLM: artifact finding repair contexts must include concrete patch tool skeletons.
# 函数用途: 验证目录型产物失败时 required_tool_calls 不为空，模型能看到应修改的文件路径。
def test_delivery_repair_context_includes_artifact_repair_tool_calls(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_id": "site",
                        "artifact_path": "outputs/site",
                        "finding_values": ["index.html:href=styles.css", "getElementById:app"],
                        "repair_targets": ["outputs/site/index.html", "outputs/site/app.js"],
                        "write_tools": ["write_file", "replace_in_file"],
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)
    target = tmp_path / "outputs/site/index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("<button disabled>提交</button>", encoding="utf-8")

    payload = json.loads(delivery_repair_context(agent, repairs=0).splitlines()[1])

    assert payload["required_tool_calls"][0]["tool"] == "replace_in_file"
    assert payload["required_tool_calls"][0]["path"] == "outputs/site/index.html"
    assert "getElementById:app" in payload["required_tool_calls"][0]["finding_values"]
    assert payload["repair_target_snapshots"][0]["preview"] == "<button disabled>提交</button>"


# LLM: missing required artifact files need a create-capable tool skeleton, not a patch-only skeleton.
# 函数用途: 验证 required_tool_calls 根据结构化 finding code 选择能创建文件的写入工具。
def test_delivery_repair_context_uses_write_tool_for_missing_required_files(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_id": "site",
                        "artifact_path": "outputs/site",
                        "finding_codes": ["STATIC_SITE_MISSING_REQUIRED_FILES"],
                        "finding_values": ["app.js"],
                        "repair_targets": ["outputs/site/app.js"],
                        "write_tools": ["replace_in_file", "write_file"],
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    payload = json.loads(delivery_repair_context(agent, repairs=0).splitlines()[1])

    assert payload["required_tool_calls"][0]["tool"] == "write_file"
    assert payload["required_tool_calls"][0]["path"] == "outputs/site/app.js"
    assert payload["required_tool_calls"][0]["mutation_intent"] == "create_or_replace"


# LLM: malformed whole-file artifacts should route toward bounded full rewrites.
# 函数用途: 验证 HTML 结构类 finding 不会继续生成局部 replace skeleton，避免重复追加坏 HTML。
def test_delivery_repair_context_uses_rewrite_tool_for_html_structure_findings(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_id": "site",
                        "artifact_path": "outputs/site",
                        "finding_codes": ["STATIC_SITE_HTML_STRUCTURE_HITS"],
                        "finding_values": ["index.html:duplicate_html_close"],
                        "repair_targets": ["outputs/site/index.html"],
                        "write_tools": ["replace_in_file", "write_file", "file_write_session"],
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    payload = json.loads(delivery_repair_context(agent, repairs=0).splitlines()[1])

    assert payload["required_tool_calls"][0]["tool"] == "write_file"
    assert payload["required_tool_calls"][0]["path"] == "outputs/site/index.html"
    assert payload["required_tool_calls"][0]["mutation_intent"] == "rewrite"


# LLM: Web binding mismatches are whole-artifact consistency failures, not safe one-line patches.
# 函数用途: 验证缺 DOM id、惰性控件这类结构化 Web finding 会推动整文件重写。
def test_delivery_repair_context_uses_rewrite_tool_for_web_binding_findings(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_context,
    )

    finding_values = [
        "index.html:button:下一步 disabled",
        *[f"getElementById:field{i}" for i in range(24)],
    ]
    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
                        "recommended_action": "repair_artifact_against_findings",
                        "artifact_id": "site",
                        "artifact_path": "outputs/site",
                        "finding_codes": [
                            "STATIC_SITE_INERT_CONTROL_HITS",
                            "STATIC_SITE_MISSING_DOM_ID_HITS",
                        ],
                        "finding_values": finding_values,
                        "repair_targets": ["outputs/site/index.html", "outputs/site/app.js"],
                        "write_tools": ["replace_in_file", "write_file", "file_write_session"],
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    payload = json.loads(delivery_repair_context(agent, repairs=0).splitlines()[1])

    assert payload["required_tool_calls"][0]["tool"] == "write_file"
    assert payload["required_tool_calls"][0]["mutation_intent"] == "rewrite"
    assert payload["required_tool_calls"][0]["finding_values"] == finding_values
