from __future__ import annotations


def test_target_coverage_ledger_reports_missing_items_without_blocking():
    from agent_py_agent.agent.agent_core.target_coverage_ledger import target_coverage_status

    status = target_coverage_status(
        {
            "scope_label": "今年以来每周",
            "enforcement": "advisory",
            "target_items": [
                {"target_id": "2026-W01", "label": "第1周"},
                {"target_id": "2026-W02", "label": "第2周"},
                {"target_id": "2026-W03", "label": "第3周"},
            ],
        },
        coverage_records=[
            {"target_id": "2026-W01", "status": "covered", "artifact_ref": "outputs/week1.xlsx"},
            {"target_id": "2026-W03", "status": "covered", "artifact_ref": "outputs/week3.xlsx"},
        ],
    )

    assert status["scope_label"] == "今年以来每周"
    assert status["enforcement"] == "advisory"
    assert status["expected_count"] == 3
    assert status["covered_count"] == 2
    assert status["missing_count"] == 1
    assert status["missing_items"] == [{"target_id": "2026-W02", "label": "第2周"}]
    assert status["should_block"] is False


def test_target_coverage_ledger_blocks_required_missing_read_targets(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source_a = tmp_path / "a.md"
    source_b = tmp_path / "b.md"
    source_a.write_text("A", encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source_a)},
            }
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "source shards",
            "enforcement": "required",
            "target_items": [
                {"target_id": str(source_a), "label": "a"},
                {"target_id": str(source_b), "label": "b"},
            ],
        },
        coverage_records=records,
    )

    assert status["covered_count"] == 1
    assert status["missing_count"] == 1
    assert status["missing_items"] == [{"target_id": str(source_b), "label": "b"}]
    assert status["should_block"] is True
    assert status["recommended_next_action"] == "cover_missing_targets_before_submit"


def test_target_coverage_ledger_accepts_delivery_contract_items_alias(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source_a = tmp_path / "a.md"
    source_b = tmp_path / "b.md"
    source_a.write_text("A", encoding="utf-8")
    source_b.write_text("B", encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {"tool": "read_file", "ok": True, "parameters": {"path": str(source_a)}},
            {"tool": "read_file", "ok": True, "parameters": {"path": str(source_b)}},
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "source shards",
            "enforcement": "required",
            "items": [
                {"target_id": str(source_a), "source_ref": str(source_a)},
                {"target_id": str(source_b), "source_ref": str(source_b)},
            ],
        },
        coverage_records=records,
    )

    assert status["expected_count"] == 2
    assert status["covered_count"] == 2
    assert status["missing_count"] == 0
    assert status["should_block"] is False


def test_target_coverage_ledger_collects_records_from_nested_runtime_payloads():
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    records = collect_target_coverage_records(
        [
            {
                "result": {
                    "coverage_records": [
                        {"target_id": "api-a", "status": "covered", "source_ref": "tool://query-a"},
                    ]
                }
            },
            {
                "artifact_registry_refs": [
                    {"artifact_id": "artifact-b", "metadata": {"coverage_items": ["api-b"]}},
                ]
            },
        ]
    )
    status = target_coverage_status(
        {"target_items": [{"target_id": "api-a"}, {"target_id": "api-b"}]},
        coverage_records=records,
    )

    assert {item["target_id"] for item in records} == {"api-a", "api-b"}
    assert status["covered_count"] == 2
    assert status["missing_count"] == 0


def test_target_coverage_ledger_does_not_count_partial_char_window_as_full_read(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "journal.txt"
    source.write_text("x" * 1000, encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source), "offset": 0, "max_chars": 100},
                "output_preview": "[char-window offset=0 chars=100 total_chars=1000]\nPARTIAL view only",
            }
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "target_items": [{"target_id": str(source), "source_ref": str(source)}],
        },
        coverage_records=records,
    )

    assert status["covered_count"] == 0
    assert status["missing_count"] == 1
    assert status["should_block"] is True
    assert status["repair_hints"] == [
        {
            "target_id": str(source),
            "source_ref": str(source),
            "covered_until_offset": 100,
            "total_chars": 1000,
            "recommended_tool_call": {"tool": "read_file", "path": str(source), "offset": 100},
        }
    ]


def test_target_coverage_ledger_merges_char_windows_to_cover_full_source(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "journal.txt"
    source.write_text("x" * 150, encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source), "offset": 0, "max_chars": 100},
                "output_preview": "[char-window offset=0 chars=100 total_chars=150]\nPARTIAL view only",
            },
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source), "offset": 100, "max_chars": 100},
                "output_preview": "[char-window offset=100 chars=50 total_chars=150]",
            },
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "target_items": [{"target_id": str(source), "source_ref": str(source)}],
        },
        coverage_records=records,
    )

    assert status["covered_count"] == 1
    assert status["missing_count"] == 0
    assert status["should_block"] is False


def test_target_coverage_ledger_reads_externalized_read_file_artifact(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "journal.txt"
    source.write_text("x" * 1000, encoding="utf-8")
    artifact = tmp_path / "read_file-output.json"
    artifact.write_text(
        '{"content":"[char-window offset=0 chars=100 total_chars=1000]\\nPARTIAL view only"}',
        encoding="utf-8",
    )
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source), "offset": 0, "max_chars": 100},
                "source_artifact_ref": str(artifact),
            }
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "target_items": [{"target_id": str(source), "source_ref": str(source)}],
        },
        coverage_records=records,
    )

    assert status["covered_count"] == 0
    assert status["missing_count"] == 1
    assert status["should_block"] is True


def test_target_coverage_ledger_reads_externalized_output_from_index_path(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "journal.txt"
    source.write_text("x" * 150, encoding="utf-8")
    blob_a = tmp_path / "read-a.json"
    blob_b = tmp_path / "read-b.json"
    blob_a.write_text(
        '{"content":"[char-window offset=0 chars=100 total_chars=150]\\nfirst"}',
        encoding="utf-8",
    )
    blob_b.write_text(
        '{"content":"[char-window offset=100 chars=50 total_chars=150]\\nsecond"}',
        encoding="utf-8",
    )
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "parameters": {"path": str(source), "offset": 0, "max_chars": 100},
                "path": str(blob_a),
            },
            {
                "tool": "read_file",
                "parameters": {"path": str(source), "offset": 100, "max_chars": 100},
                "path": str(blob_b),
            },
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "coverage_requirement": "full_source_read",
            "target_items": [{"target_id": str(source), "source_ref": str(source)}],
        },
        coverage_records=records,
    )

    assert status["covered_count"] == 1
    assert status["missing_count"] == 0
    assert status["should_block"] is False


def test_target_coverage_ledger_does_not_count_truncated_line_read_as_full_source(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "journal.txt"
    source.write_text("\n".join(f"line {index}" for index in range(1, 11)), encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source)},
                "output_preview": "\n".join(
                    [
                        "1: line 1",
                        "2: line 2",
                        "... 已截断；PARTIAL view only; 这不是完整文件。 total_lines=10; "
                        "next_start_line=3; limit_chars=30。 继续读取请调用 read_file(start_line=3)。",
                    ]
                ),
            }
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "target_items": [{"target_id": str(source), "source_ref": str(source)}],
        },
        coverage_records=records,
    )

    assert status["covered_count"] == 0
    assert status["missing_count"] == 1
    assert status["should_block"] is True
    assert status["repair_hints"] == [
        {
            "target_id": str(source),
            "source_ref": str(source),
            "covered_until_line": 2,
            "total_lines": 10,
            "recommended_tool_call": {"tool": "read_file", "path": str(source), "start_line": 3},
        }
    ]


def test_target_coverage_ledger_merges_line_windows_to_cover_full_source(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "journal.txt"
    source.write_text("\n".join(f"line {index}" for index in range(1, 6)), encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source), "start_line": 1, "end_line": 3},
                "output_preview": "1: line 1\n2: line 2\n3: line 3",
            },
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source), "start_line": 4, "end_line": 5},
                "output_preview": "4: line 4\n5: line 5",
            },
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "target_items": [{"target_id": str(source), "source_ref": str(source)}],
        },
        coverage_records=records,
    )

    assert status["covered_count"] == 1
    assert status["missing_count"] == 0
    assert status["should_block"] is False


def test_target_coverage_ledger_does_not_count_bare_line_range_as_full_source(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "journal.txt"
    source.write_text("\n".join(f"line {index}" for index in range(1, 11)), encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source), "start_line": 1, "end_line": 3},
                "output_preview": "1: line 1\n2: line 2\n3: line 3",
            }
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "target_items": [{"target_id": str(source), "source_ref": str(source)}],
        },
        coverage_records=records,
    )

    assert status["covered_count"] == 0
    assert status["missing_count"] == 1
    assert status["repair_hints"][0]["recommended_tool_call"] == {
        "tool": "read_file",
        "path": str(source),
        "start_line": 4,
    }


def test_full_source_read_ignores_search_text_hits(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("a\nb\nc\n", encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "search_text",
                "ok": True,
                "parameters": {"path": "data/journal.txt", "query": "a"},
                "output_preview": "1:a",
            }
        ],
        workspace_root=tmp_path,
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "coverage_requirement": "full_source_read",
            "target_items": [
                {
                    "target_id": "data/journal.txt",
                    "source_ref": "data/journal.txt",
                    "coverage_kind": "full_source_read",
                }
            ],
        },
        coverage_records=records,
        workspace_root=tmp_path,
    )

    assert status["covered_count"] == 0
    assert status["missing_count"] == 1
    assert status["should_block"] is True


def test_full_source_read_window_is_not_deduped_by_later_search_text(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("x" * 150, encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source), "offset": 0, "max_chars": 100},
                "output_preview": "[char-window offset=0 chars=100 total_chars=150]",
            },
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source), "offset": 100, "max_chars": 100},
                "output_preview": "[char-window offset=100 chars=50 total_chars=150]",
            },
            {
                "tool": "search_text",
                "ok": True,
                "parameters": {"path": str(source), "query": "needle"},
                "output_preview": "hit preview",
            },
        ],
        workspace_root=tmp_path,
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "coverage_requirement": "full_source_read",
            "target_items": [
                {
                    "target_id": "data/journal.txt",
                    "source_ref": "data/journal.txt",
                    "coverage_kind": "full_source_read",
                }
            ],
        },
        coverage_records=records,
        workspace_root=tmp_path,
    )

    assert status["covered_count"] == 1
    assert status["missing_count"] == 0
    assert status["should_block"] is False


def test_full_source_read_matches_relative_read_file_against_workspace_root(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("a\nb\nc\n", encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": "data/journal.txt"},
                "output_preview": "1: a\n2: b\n3: c",
            }
        ],
        workspace_root=tmp_path,
    )
    status = target_coverage_status(
        {
            "scope_label": "完整读取源文件",
            "enforcement": "required",
            "coverage_requirement": "full_source_read",
            "target_items": [
                {
                    "target_id": "data/journal.txt",
                    "source_ref": "data/journal.txt",
                    "coverage_kind": "full_source_read",
                }
            ],
        },
        coverage_records=records,
        workspace_root=tmp_path,
    )

    assert status["covered_count"] == 1
    assert status["missing_count"] == 0
    assert status["should_block"] is False
