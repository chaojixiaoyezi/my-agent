from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


# LLM: builder-ready delivery repair should treat read_artifact as inspection-only so the loop pivots to the builder tool.
# 函数用途: 验证进入 invoke_builder_tool 阶段后，单独 read_artifact 不再算推进动作。
def test_delivery_repair_guard_treats_read_artifact_as_nonproductive_when_builder_ready(tmp_path: Path):
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
                        "code": "STAGING_BUILDER_READY",
                        "recommended_action": "invoke_builder_tool",
                        "builder_tool": "data_to_workbook",
                        "source_ref": "outputs/report/source.json",
                        "output_ref": "outputs/report/report.xlsx",
                    }
                ]
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/demo.json"}],
        )
        is False
    )


# LLM: missing-checkpoint repair should still allow read_artifact because the model may need fetched evidence before materializing the file.
# 函数用途: 验证 materialize_checkpoint 阶段不会把 read_artifact 误判为空转，避免过早拦住资料整理动作。
def test_delivery_repair_guard_allows_read_artifact_before_checkpoint_exists(tmp_path: Path):
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
                        "code": "STAGING_CHECKPOINT_MISSING",
                        "recommended_action": "materialize_checkpoint",
                        "checkpoint_ref": "outputs/deepseek_papers/source_index.json",
                    }
                ]
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/demo.json"}],
        )
        is True
    )


def test_delivery_repair_guard_requires_write_action_after_repeated_no_progress(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 9,
                "no_progress_block_threshold": 5,
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_NO_ROWS",
                        "recommended_action": "write_non_empty_structured_rows",
                        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
                        "checkpoint_shape_hint": '{"sheets":[{"rows":[{"项目名":"..."}]}]}',
                        "required_columns": ["项目名", "地址"],
                    },
                    {
                        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
                        "recommended_action": "repair_evidence_refs",
                        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
                    },
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "run_command", "command": "curl https://example.com"}]) is False
    assert is_delivery_repair_productive_call(agent, [{"tool": "fetch_url", "url": "https://example.com"}]) is False
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/github_star_growth/source_data.json", "content": "{}"}],
        )
        is True
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_structured_json", "path": "outputs/github_star_growth/source_data.json", "rows": [{"项目名": "demo"}]}],
        )
        is True
    )


# LLM: writer_tool is an executable machine contract for structured checkpoints, not just a hint.
# 函数用途: 验证已指定 write_structured_json 的阶段修复不能用 write_file 绕过结构化校验。
def test_delivery_repair_guard_requires_declared_writer_tool_for_structured_checkpoint(tmp_path: Path):
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
                        "code": "STAGED_JSON_TOO_FEW_SHEETS",
                        "recommended_action": "repair_structured_checkpoint_json",
                        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
                        "writer_tool": "write_structured_json",
                    }
                ]
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_file", "path": "outputs/github_star_growth/source_data.json", "content": "{}"}],
        )
        is False
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "write_structured_json", "path": "outputs/github_star_growth/source_data.json", "rows": [{"项目名": "demo"}]}],
        )
        is True
    )


# LLM: evidence repairs require source_refs/claims machine fields, not arbitrary table rows.
# 函数用途: 验证缺证据恢复动作不会把普通 rows/sheets 写入误判成完成了证据修复。
def test_delivery_repair_guard_requires_evidence_shape_for_evidence_repair(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(tmp_path, _evidence_repair_closeout())
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [_sheet_only_write_call()]) is False
    assert is_delivery_repair_productive_call(agent, [_structured_evidence_write_call()]) is True


# LLM: Structure+evidence repair must happen as one machine checkpoint update.
# 函数用途: 验证同一 checkpoint 同时缺表格结构和证据时，不能只写 rows/sheets 而漏掉 source_refs/claims。
def test_delivery_repair_guard_requires_evidence_shape_for_combined_checkpoint_repair(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(tmp_path, _structure_and_evidence_repair_closeout())
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [_sheet_only_write_call()]) is False
    assert is_delivery_repair_productive_call(agent, [_structured_evidence_write_call()]) is True


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


# LLM: run_command must be classified by side effect, not by tool name alone.
# 函数用途: 验证阶段修复期间 ls/find/cat 等 shell 检查不会被误判为本地推进。
def test_delivery_repair_guard_treats_inspection_run_command_as_nonproductive(tmp_path: Path):
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
                        "code": "STAGED_JSON_TOO_FEW_SHEETS",
                        "recommended_action": "repair_structured_checkpoint_json",
                        "checkpoint_ref": "outputs/github_star_growth/source_data.json",
                        "writer_tool": "write_structured_json",
                    }
                ]
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "run_command", "command": "ls -la outputs"}]) is False
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "run_command", "command": "ls outputs; mkdir -p outputs/github_star_growth"}],
        )
        is False
    )
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "run_command", "command": "printf '{}' > outputs/github_star_growth/source_data.json"}],
        )
        is True
    )


def test_delivery_repair_guard_allows_evidence_gathering_before_strict_write_mode(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 1,
                "no_progress_block_threshold": 5,
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_NO_ROWS",
                        "recommended_action": "write_non_empty_structured_rows",
                        "checkpoint_ref": "outputs/report/source_data.json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "fetch_url", "url": "https://example.com/data"}]) is True
    assert is_delivery_repair_productive_call(agent, [{"tool": "search", "query": "release notes"}]) is True
    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/fetch-1.json"}],
        )
        is True
    )
    assert is_delivery_repair_productive_call(agent, [{"tool": "list_files", "path": "outputs/report"}]) is False


def test_delivery_repair_context_includes_checkpoint_shape_hint(tmp_path: Path):
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
                        "code": "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
                        "recommended_action": "write_non_empty_structured_rows",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "checkpoint_shape_hint": '{"sheets":[{"name":"榜单","rows":[{"项目名":"..."}]}]}',
                        "required_columns": ["项目名", "地址"],
                        "missing_columns": "项目名,地址",
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    context = delivery_repair_context(agent, repairs=0)

    assert "checkpoint_shape_hint" in context
    assert "required_columns" in context
    assert "missing_columns" in context
    assert "required_tool_calls" in context
    assert '"tool": "write_structured_json"' in context
    assert '"path": "outputs/report/source_data.json"' in context


# LLM: rejected repair calls should come back as machine-readable feedback, not disappear silently.
# 函数用途: 验证阶段修复时被拦截的检查类调用会生成结构化拒绝上下文，下一轮可直接看到 required_tool_calls。
def test_delivery_repair_rejection_context_names_rejected_and_required_calls(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        delivery_repair_rejection_context,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "recovery_actions": [
                    {
                        "code": "STAGING_CHECKPOINT_MISSING",
                        "recommended_action": "materialize_checkpoint",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "checkpoint_shape_hint": '{"sheets":[{"name":"榜单","rows":[{"项目名":"..."}]}]}',
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path, backend=SimpleNamespace(name="fake"))

    context = delivery_repair_rejection_context(
        agent,
        [{"tool": "list_files", "path": "outputs/report"}],
        repairs=0,
    )
    payload = json.loads(context.splitlines()[1])

    assert "delivery-required-repair-rejected" in context
    assert payload["rejected_tool_calls"] == [{"path": "outputs/report", "tool": "list_files"}]
    assert payload["required_tool_calls"][0]["tool"] == "write_structured_json"
    assert payload["required_tool_calls"][0]["path"] == "outputs/report/source_data.json"
    assert payload["required_tool_calls"][0]["merge_existing"] is True


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
                        "required_fields": ["项目名", "地址"],
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


# LLM: structured checkpoint repair may inspect the checkpoint it is about to rewrite.
# 函数用途: 验证严格修复模式下只放行 checkpoint_ref 本身的 read_file，不放行普通 artifact 检查。
def test_delivery_repair_guard_allows_checkpoint_read_for_structured_repair(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 5,
                "no_progress_block_threshold": 5,
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_TOO_FEW_SHEETS",
                        "recommended_action": "repair_structured_checkpoint_json",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [{"tool": "read_file", "path": "outputs/report/source_data.json"}],
        )
        is True
    )
    assert is_delivery_repair_productive_call(agent, [{"tool": "read_file", "path": "outputs/site/index.html"}]) is False


# LLM: evidence repair may gather fresh sources before writing claims.
# 函数用途: 验证 strict repair 不会拦住为 source_refs/claims 收集证据的结构化抓取工具。
def test_delivery_repair_guard_allows_evidence_gathering_for_evidence_repair(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
        is_delivery_repair_productive_call,
    )

    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 5,
                "no_progress_block_threshold": 5,
                "recovery_actions": [
                    {
                        "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
                        "recommended_action": "repair_evidence_refs",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "required_fields": ["项目名"],
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert is_delivery_repair_productive_call(agent, [{"tool": "fetch_url", "url": "https://example.com"}]) is True


def _evidence_repair_closeout() -> dict[str, object]:
    return {
        "ok": False,
        "delivery_progress": {
            "recovery_actions": [
                {
                    "code": "EVIDENCE_REQUIRED_FIELD_MISSING",
                    "recommended_action": "repair_evidence_refs",
                    "checkpoint_ref": "outputs/report/source_data.json",
                    "required_fields": ["项目名", "地址"],
                    "writer_tool": "write_structured_json",
                }
            ]
        },
    }


def _structure_and_evidence_repair_closeout() -> dict[str, object]:
    closeout = _evidence_repair_closeout()
    actions = closeout["delivery_progress"]["recovery_actions"]
    actions.insert(
        0,
        {
            "code": "STAGED_JSON_TOO_FEW_SHEETS",
            "recommended_action": "repair_structured_checkpoint_json",
            "checkpoint_ref": "outputs/report/source_data.json",
            "required_columns": ["项目名", "地址"],
            "writer_tool": "write_structured_json",
        },
    )
    return closeout


def _sheet_only_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/report/source_data.json",
        "sheets": [{"name": "榜单", "rows": [{"项目名": "demo", "地址": "https://example.com"}]}],
    }


def _structured_evidence_write_call() -> dict[str, object]:
    return {
        "tool": "write_structured_json",
        "path": "outputs/report/source_data.json",
        "data": {
            "sheets": [{"name": "榜单", "rows": [{"项目名": "demo", "地址": "https://example.com"}]}],
            "source_refs": [{"source_id": "src-1", "uri": "https://example.com"}],
            "claims": [
                {"field": "项目名", "value": "demo", "source_ids": ["src-1"], "verification_status": "VERIFIED"},
                {"field": "地址", "value": "https://example.com", "source_ids": ["src-1"], "verification_status": "VERIFIED"},
            ],
        },
    }


# LLM: _write_closeout keeps the delivery-repair fixture tiny and grounded in the same machine report the runtime uses.
# 函数用途: 向测试工作区写入 .agent_delivery/closeout.json，供 repair guard 直接读取。
def _write_closeout(root: Path, payload: dict[str, object]) -> None:
    path = root / ".agent_delivery" / "closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
