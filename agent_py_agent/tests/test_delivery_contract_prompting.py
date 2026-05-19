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
                    "preferred_path": "outputs/github_star_growth/github_star_growth.xlsx",
                    "validation_contract": {
                        "staging_contract": {
                            "strategy": "data_then_tool_builder_then_workbook",
                            "builder_tool": "data_to_workbook",
                            "source_json_ref": "outputs/github_star_growth/source_data.json",
                            "workbook_ref": "outputs/github_star_growth/github_star_growth.xlsx",
                            "checkpoint_refs": [
                                "outputs/github_star_growth/source_data.json",
                                "outputs/github_star_growth/github_star_growth.xlsx",
                            ],
                        }
                    },
                }
            ]
        }
    )

    assert "阶段产物" in text
    assert "outputs/github_star_growth/source_data.json" in text
    assert "阶段构建工具: data_to_workbook" in text
    assert "source_json_path=outputs/github_star_growth/source_data.json" in text


# LLM: HTML validation fields should reach the model before it writes the first artifact.
# 函数用途: 验证 forbidden_hrefs 这类结构化验收字段会渲染成执行提示，减少先写坏再修的真实任务耗时。
def test_render_delivery_contract_section_includes_html_forbidden_href_rules():
    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )

    text = render_delivery_contract_section(
        {
            "artifacts": [
                {
                    "kind": "html",
                    "preferred_path": "outputs/furniture_homepage/index.html",
                    "validation_contract": {
                        "forbidden_hrefs": ["", "#", "javascript:void(0)"],
                        "quality_requirements": {
                            "clickable_links_must_resolve": True,
                            "complete_html_document": True,
                            "single_file_no_external_assets": True,
                        },
                    },
                }
            ]
        }
    )

    assert "HTML 链接不得使用这些 href 占位值" in text
    assert "#, javascript:void(0)" in text
    assert "所有 a[href] 必须指向真实页面锚点" in text


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
    assert "staged_json_no_rows" in text
    assert "required_columns=项目名, 地址" in text
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
                        "code": "STAGED_JSON_NO_ROWS",
                        "stage_ref": "outputs/github_star_growth/source_data.json",
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
        "preferred_path": "outputs/github_star_growth/github_star_growth.xlsx",
        "validation_contract": {
            "required_columns": ["项目名", "地址"],
            "staging_contract": {
                "builder_tool": "data_to_workbook",
                "source_json_ref": "outputs/github_star_growth/source_data.json",
                "workbook_ref": "outputs/github_star_growth/github_star_growth.xlsx",
                "checkpoint_refs": [
                    "outputs/github_star_growth/source_data.json",
                    "outputs/github_star_growth/github_star_growth.xlsx",
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
        "target_path": {"display": "outputs/github_star_growth/fetch.py"},
    }
