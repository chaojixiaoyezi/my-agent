"""Tests for rendering structured delivery contracts into model guidance."""

from __future__ import annotations


# LLM: staged contracts must be visible to the model without becoming machine facts.
# 函数用途: 验证 prompt 渲染会展示 checkpoint refs，系统事实仍保留在 JSON 合同里。
def test_render_delivery_contract_section_includes_staging_refs():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "artifacts": [
                {
                    "kind": "xlsx",
                    "preferred_path": "outputs/table_report/table_report.xlsx",
                    "validation_contract": {
                    "staging_contract": {
                        "strategy": "data_then_tool_builder_then_workbook",
                        "builder_tool": "data_to_workbook",
                        "source_json_ref": "outputs/table_report/source_data.json",
                        "workbook_ref": "outputs/table_report/table_report.xlsx",
                        "checkpoint_shape_hints": {
                            "outputs/table_report/source_data.json": '{"sheets":[{"name":"数据清单","rows":[{"记录名":"..."}]}]}'
                        },
                        "checkpoint_refs": [
                            "outputs/table_report/source_data.json",
                            "outputs/table_report/table_report.xlsx",
                        ],
                    }
                    },
                }
            ]
        }
    )

    assert "阶段产物" in text
    assert "outputs/table_report/source_data.json" in text
    assert "阶段构建工具: data_to_workbook" in text
    assert "source_json_path=outputs/table_report/source_data.json" in text
    assert "数据清单" in text
    assert "记录名" in text
    assert "generated_rows" in text


# LLM: One malformed artifact entry must not erase valid contract guidance.
# 函数用途: 验证 artifacts 混入脏项时只忽略脏项，仍渲染其它结构化 artifact。
def test_render_delivery_contract_section_skips_bad_artifact_items():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "artifacts": [
                "bad-artifact-entry",
                {
                    "kind": "xlsx",
                    "preferred_path": "outputs/report.xlsx",
                    "validation_contract": {
                        "staging_contract": {
                            "builder_tool": "data_to_workbook",
                            "source_json_ref": "outputs/source_data.json",
                            "workbook_ref": "outputs/report.xlsx",
                            "checkpoint_refs": ["outputs/source_data.json", "outputs/report.xlsx"],
                        }
                    },
                },
            ]
        }
    )

    assert "- 阶段产物:" in text
    assert "- 阶段构建工具: data_to_workbook" in text


# LLM: bootstrap contract guidance should push the model to materialize targets before repeated inspection.
# 函数用途: 验证通用开工合同会渲染结构化目标路径和 builder tool，而不是按任务专项写提示。
def test_render_delivery_contract_section_includes_bootstrap_targets():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "bootstrap_contract": {
                "materialization_targets": [
                    {
                        "target_type": "checkpoint",
                        "workspace_relative_path": "outputs/table_report/source_data.json",
                    },
                    {
                        "target_type": "required_file",
                        "workspace_relative_path": "outputs/static_site/index.html",
                    },
                ],
                "startup_actions": [
                    {"action": "materialize_target", "priority": 1},
                    {
                        "action": "materialize_checkpoint",
                        "priority": 1,
                        "checkpoint_ref": "outputs/table_report/source_data.json",
                    },
                    {
                        "action": "invoke_builder_tool",
                        "priority": 2,
                        "builder_tool": "data_to_workbook",
                        "source_ref": "outputs/table_report/source_data.json",
                        "output_ref": "outputs/table_report/table_report.xlsx",
                    },
                ],
            }
        }
    )

    assert "开工顺序" in text
    assert "checkpoint: outputs/table_report/source_data.json" in text
    assert "required_file: outputs/static_site/index.html" in text
    assert "不要连续两轮只做目录查看" in text
    assert "先真实写出 checkpoint: outputs/table_report/source_data.json" in text
    assert "先给 outputs/table_report/source_data.json 写可验收的非空结构骨架" in text
    assert "最小有效骨架" in text
    assert "空 JSON 数组" not in text
    assert "data_to_workbook" in text


def test_render_delivery_contract_section_keeps_research_first_before_skeletons():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "bootstrap_contract": {
                "startup_actions": [
                    {
                        "action": "materialize_checkpoint",
                        "priority": 1,
                        "checkpoint_ref": "outputs/source_data.json",
                        "research_first": True,
                        "required_structured_fields": ["source_refs", "claims"],
                    }
                ],
            }
        }
    )

    assert "先完成来源采集/读取，再写 checkpoint: outputs/source_data.json" in text
    assert "source_refs, claims" in text
    assert "最小有效骨架" not in text


# LLM: source-evidence checkpoints should not be rendered as skeleton-first recovery work.
# 函数用途: 验证需要来源证据的 checkpoint 会提示先采集/绑定 source refs，而不是写空骨架。
def test_render_delivery_contract_section_avoids_skeleton_hint_for_source_evidence_checkpoint():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "bootstrap_contract": {
                "startup_actions": [
                    {
                        "action": "materialize_checkpoint",
                        "checkpoint_materialization_mode": "source_evidence_first",
                        "checkpoint_ref": "outputs/table_report/source_data.json",
                        "requires_auditable_source_evidence": True,
                        "required_structured_fields": ["source_refs", "claims", "completion_evidence"],
                    }
                ]
            }
        }
    )

    assert "先真实写出 checkpoint: outputs/table_report/source_data.json" in text
    assert "source_refs" in text
    assert "claims" in text
    assert "最小有效骨架" not in text


# LLM: document builder prompts must use the source parameter declared by the tool schema.
# 函数用途: 验证 markdown_to_pdf 渲染 source_markdown_path，而不是 workbook 专用 source_json_path。
def test_render_delivery_contract_section_uses_document_builder_source_param():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "bootstrap_contract": {
                "startup_actions": [
                    {
                        "action": "invoke_builder_tool",
                        "priority": 2,
                        "builder_tool": "markdown_to_pdf",
                        "source_ref": "outputs/docs/draft.md",
                        "output_ref": "outputs/docs/final.pdf",
                    }
                ]
            },
            "artifacts": [
                {
                    "kind": "pdf",
                    "preferred_path": "outputs/docs/final.pdf",
                    "validation_contract": {
                        "staging_contract": {
                            "builder_tool": "markdown_to_pdf",
                            "source_markdown_ref": "outputs/docs/draft.md",
                            "pdf_ref": "outputs/docs/final.pdf",
                            "checkpoint_refs": ["outputs/docs/source_index.json", "outputs/docs/draft.md"],
                        }
                    },
                }
            ],
        }
    )

    assert "markdown_to_pdf: source_markdown_path=outputs/docs/draft.md, path=outputs/docs/final.pdf" in text
    assert "source_json_path=outputs/docs/draft.md" not in text


# LLM: staged JSON hints should come from per-checkpoint contract fields instead of leaking workbook-only shapes into unrelated tasks.
# 函数用途: 验证 source_index 这类数组索引文件会渲染自己的 shape hint，而不是默认提示成 sheets/rows。
def test_render_delivery_contract_section_uses_checkpoint_shape_hint_for_non_workbook_json():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "artifacts": [
                {
                    "kind": "pdf",
                    "preferred_path": "outputs/document_bundle/document_bundle_zh.pdf",
                    "validation_contract": {
                        "staging_contract": {
                            "checkpoint_refs": [
                                "outputs/document_bundle/source_index.json",
                                "outputs/document_bundle/document_bundle_zh.md",
                                "outputs/document_bundle/document_bundle_zh.pdf",
                            ],
                            "checkpoint_shape_hints": {
                                "outputs/document_bundle/source_index.json": '[{"title":"...","authors":["..."],"date":"...","url":"...","abstract":"...","translated":true}]'
                            },
                        }
                    },
                }
            ],
            "recovery": {
                "acceptance": {
                    "runtime_findings": [
                        {
                            "code": "STAGED_JSON_NO_ROWS",
                            "stage_ref": "outputs/document_bundle/source_index.json",
                        }
                    ]
                }
            },
        }
    )

    assert '[{"title":"...","authors":["..."],"date":"...","url":"...","abstract":"...","translated":true}]' in text
    assert '{"sheets":[{"name":"...","columns":[...],"rows":[{...}]}]}' not in text


# LLM: HTML validation guidance should stay generic and avoid task-specific link rules.
# 函数用途: 验证 prompt 只渲染通用 HTML 结构/资源要求，不再携带占位 href 这类专项规则。
def test_render_delivery_contract_section_includes_generic_html_rules_only():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "artifacts": [
                {
                    "kind": "html",
                    "preferred_path": "outputs/html_report/index.html",
                    "validation_contract": {
                        "quality_requirements": {
                            "complete_html_document": True,
                            "single_file_no_external_assets": True,
                        },
                    },
                }
            ]
        }
    )

    assert "HTML 必须包含完整 doctype/html/head/body 闭合结构" in text
    assert "单文件产物不得引用 http/https 外部 CSS、字体、图片或脚本" in text
    assert "HTML 链接不得使用这些 href 占位值" not in text


# LLM: recovery runtime findings should reach the model as structured continuation hints.
# 函数用途: 验证 open file_write_session 从 recovery packet 渲染为明确续写/finish 提示，不读取旧 stdout。
def test_render_delivery_contract_section_includes_open_write_session_recovery():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "recovery": {
                "acceptance": {
                    "runtime_findings": [
                        {
                            "code": "OPEN_FILE_WRITE_SESSION",
                            "session_id": "session-123",
                            "next_chunk_index": 2,
                            "manifest_path": "workspace/.agent_file_write_sessions/session-123/manifest.json",
                            "preview_path": "workspace/.agent_file_write_sessions/session-123/write.tmp",
                            "preview_materialized": True,
                            "target_path": {"display": "outputs/report.py"},
                            "resume_action": "append_from_next_chunk_then_finish",
                            "chunk_content_read_required": False,
                            "existing_chunks_authoritative": True,
                        }
                    ]
                }
            }
        }
    )

    assert "恢复要求" in text
    assert "open_file_write_session" in text
    assert "session_id=session-123" in text
    assert "next_chunk_index=2" in text
    assert "preview_path=workspace/.agent_file_write_sessions/session-123/write.tmp" in text
    assert "preview_materialized=true" in text
    assert "staged_fact_source=preview_and_chunks" in text
    assert "preview_materialized_after_append=true" in text
    assert "resume_action=append_from_next_chunk_then_finish" in text
    assert "chunk_content_read_required=false" in text
    assert "existing_chunks_authoritative=true" in text
    assert "不要重复 begin 新 session" in text


# LLM: duplicate open write sessions for one target must render one recovery choice instead of asking the model to guess.
# 函数用途: 验证同一目标文件存在多个 open session 时，提示会推荐已有 chunk 最多的 session，并要求关闭空/重复 session。
def test_render_delivery_contract_section_recommends_one_duplicate_open_write_session():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(_duplicate_open_session_contract())

    assert "duplicate_open_file_write_sessions" in text
    assert "recommended_session_id=fetch-v3" in text
    assert "abort_duplicate_session_ids=empty-session" in text
    assert "staged_json_invalid" in text
    assert "Unterminated string starting at" in text
    assert "staged_json_no_rows" in text
    assert "required_columns=记录名, 地址" in text
    assert "min_groups=21" in text
    assert "min_items_per_group=10" in text
    assert "required_item_fields=记录名, 地址, 指标值" in text
    assert "item_evidence_required_fields=记录名, 地址, 指标值" in text
    assert "require_verified_evidence=true" in text
    assert "data_to_workbook" in text


# LLM: Reconciled sessions should override stale recovery findings in model-visible hints.
# 函数用途: 验证系统已 abort 的旧 session 不再被渲染为续写目标，只展示保留 session。
def test_render_delivery_contract_section_skips_reconciled_aborted_sessions():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "recovery_reconciliation": {
                "groups": [
                    {
                        "target_path": "outputs/report.py",
                        "kept_session_id": "kept-session",
                        "kept_next_chunk_index": 3,
                        "kept_received_chunks": [0, 1, 2],
                        "aborted_session_ids": ["stale-session"],
                    }
                ]
            },
            "recovery": {
                "acceptance": {
                    "runtime_findings": [
                        {
                            "code": "OPEN_FILE_WRITE_SESSION",
                            "session_id": "stale-session",
                            "next_chunk_index": 1,
                            "target_path": {"display": "outputs/report.py"},
                        }
                    ]
                }
            },
        }
    )

    assert "reconciled_open_file_write_session" in text
    assert "kept_session_id=kept-session" in text
    assert "kept_next_chunk_index=3" in text
    assert "kept_received_chunks=0, 1, 2" in text
    assert "session_id=stale-session" not in text


# LLM: _duplicate_open_session_contract keeps the duplicate-session fixture reusable and under code-size limits.
# 函数用途: 构造包含重复 open file_write_session 和空 staged JSON 的交付合同测试数据。
def _duplicate_open_session_contract() -> dict[str, object]:
    return {
        "artifacts": [_xlsx_artifact_contract()],
        "recovery": {
            "acceptance": {
                "runtime_findings": [
                    _open_write_session("empty-session", 0, []),
                    _open_write_session("fetch-v3", 2, [0, 1]),
                    {
                        "code": "STAGED_JSON_INVALID",
                        "stage_ref": "outputs/table_report/source_data.json",
                        "parse_error": "Unterminated string starting at: line 12 column 9",
                    },
                    {
                        "code": "STAGED_JSON_NO_ROWS",
                        "stage_ref": "outputs/table_report/source_data.json",
                    },
                ]
            }
        },
    }


# LLM: _xlsx_artifact_contract supplies a staged workbook contract for prompt rendering tests.
# 函数用途: 返回带 required_columns 和 data_to_workbook staging_contract 的 xlsx 产物合同。
def _xlsx_artifact_contract() -> dict[str, object]:
    return {
        "kind": "xlsx",
        "preferred_path": "outputs/table_report/table_report.xlsx",
        "validation_contract": {
            "collection_contract": {
                "groups_path": "sheets",
                "items_path": "rows",
                "min_groups": 21,
                "min_items_per_group": 10,
                "required_item_fields": ["记录名", "地址", "指标值"],
            },
            "evidence_contract": {
                "require_verified": True,
                "required_fields": ["记录名", "地址", "指标值"],
            },
            "required_columns": ["记录名", "地址"],
            "staging_contract": {
                "builder_tool": "data_to_workbook",
                "checkpoint_shape_hints": {
                    "outputs/table_report/source_data.json": '{"sheets":[{"name":"数据清单","rows":[{"记录名":"..."}]}]}'
                },
                "source_json_ref": "outputs/table_report/source_data.json",
                "workbook_ref": "outputs/table_report/table_report.xlsx",
                "checkpoint_refs": [
                    "outputs/table_report/source_data.json",
                    "outputs/table_report/table_report.xlsx",
                ],
            },
        },
    }


# LLM: _open_write_session builds one structured runtime finding without embedding prose facts.
# 函数用途: 构造 open file_write_session finding，用于测试重复 session 的恢复选择。
def _open_write_session(session_id: str, next_chunk: int, chunks: list[int]) -> dict[str, object]:
    return {
        "code": "OPEN_FILE_WRITE_SESSION",
        "session_id": session_id,
        "next_chunk_index": next_chunk,
        "received_chunks": chunks,
        "preview_path": f"workspace/.agent_file_write_sessions/{session_id}/write.tmp",
        "preview_materialized": bool(chunks),
        "target_path": {"display": "outputs/table_report/fetch.py"},
    }
