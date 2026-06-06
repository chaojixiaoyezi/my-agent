from __future__ import annotations


def test_materialized_delivery_contract_accepts_generic_artifacts_without_paths():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
        materializer_repair_feedback,
    )

    contract = materialized_delivery_contract(
        {
            "schema_version": "delivery_requirement_materializer.v1",
            "artifacts": [
                {
                    "artifact_id": "final_workbook",
                    "kind": "xlsx",
                    "required": True,
                    "allowed_output_roots": ["outputs"],
                }
            ],
            "delivery_quality_contract": {
                "metric_contracts": [
                    {
                        "field": "star_delta",
                        "expected_kind": "period_delta",
                        "required_window": True,
                    }
                ]
            },
        }
    )

    artifact = contract["artifacts"][0]
    assert contract["schema_version"] == "delivery_contract.v1"
    assert artifact["artifact_id"] == "final_workbook"
    assert artifact["kind"] == "xlsx"
    assert artifact["allowed_output_roots"] == ["outputs"]
    assert "preferred_path" not in artifact
    assert contract["delivery_quality_contract"]["metric_contracts"][0]["field"] == "star_delta"


def test_materialized_delivery_contract_drops_orchestration_contract():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "schema_version": "delivery_requirement_materializer.v1",
            "orchestration_contract": {
                "schema_version": "orchestration_contract.v1",
                "requires_orchestration": True,
                "required_tools": ["create_subagents"],
                "minimum_subagent_count": "11",
                "rework_budget": "2",
            },
        }
    )

    assert "orchestration_contract" not in contract


def test_materialized_delivery_contract_derives_fact_evidence_gate_from_quality_contract():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [{"artifact_id": "final_workbook", "kind": "xlsx", "allowed_output_roots": ["outputs"]}],
            "delivery_quality_contract": {
                "evidence_contract": {
                    "required_fields": ["measured_value"],
                    "allowed_value_types": ["exact"],
                    "require_verified": True,
                },
                "metric_contracts": [{"field": "measured_value", "required_window": True}],
            },
        }
    )

    fact_contract = contract["fact_evidence_contract"]
    assert fact_contract["require_tool_backed_sources"] is True
    assert fact_contract["evidence_contract"]["required_fields"] == ["measured_value"]
    assert fact_contract["evidence_contract"]["allowed_value_types"] == ["exact"]
    assert fact_contract["evidence_contract"]["require_verified"] is True


def test_materialized_delivery_contract_accepts_json_fenced_model_output():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        """```json
{"artifacts":[{"artifact_id":"report","kind":"md","preferred_path":"outputs/report.md"}]}
```"""
    )

    assert contract["artifacts"][0]["artifact_id"] == "report"
    assert contract["artifacts"][0]["preferred_path"] == "outputs/report.md"


def test_materialized_delivery_contract_accepts_single_root_artifact_object():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifact_id": "weekly_workbook",
            "kind": "xlsx",
            "required": True,
            "allowed_output_roots": ["outputs/reports"],
        }
    )

    artifact = contract["artifacts"][0]
    assert artifact["artifact_id"] == "weekly_workbook"
    assert artifact["kind"] == "xlsx"
    assert artifact["allowed_output_roots"] == ["outputs/reports"]


def test_materialized_delivery_contract_derives_user_requested_output_path_from_prompt():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
        materializer_repair_feedback,
    )

    contract = materialized_delivery_contract(
        {},
        user_prompt=(
            "请完整读完 data/long_field_journal.txt，不要只搜。\n"
            "最终把报告写到 lab_outputs/compact-stress/report.md。"
        ),
    )

    artifacts = contract["artifacts"]
    assert artifacts == [
        {
            "artifact_id": "user_requested_report_md",
            "preferred_path": "lab_outputs/compact-stress/report.md",
            "allowed_output_roots": ["lab_outputs/compact-stress"],
            "required": True,
            "kind": "md",
        }
    ]
    assert "target_coverage_contract" not in contract
    doctor = contract["_contract_doctor"]
    assert doctor["should_rematerialize"] is True
    assert doctor["findings"][0]["code"] == "DELIVERY_MATERIALIZER_SOURCE_COVERAGE_UNDECLARED"
    assert "data/long_field_journal.txt" in materializer_repair_feedback(contract)


def test_structural_user_requested_output_contract_ignores_source_coverage_phrases():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        delivery_contract_from_user_requested_outputs,
    )

    contract = delivery_contract_from_user_requested_outputs(
        "请完整读完 data/long_field_journal.txt，不要只搜。\n"
        "最终把报告写到 lab_outputs/compact-stress/report.md。"
    )

    assert contract == {
        "schema_version": "delivery_contract.v1",
        "artifacts": [
            {
                "artifact_id": "user_requested_report_md",
                "preferred_path": "lab_outputs/compact-stress/report.md",
                "allowed_output_roots": ["lab_outputs/compact-stress"],
                "required": True,
                "kind": "md",
            }
        ],
    }


def test_materialized_delivery_contract_derives_user_requested_work_artifact_path_from_prompt():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
        materializer_repair_feedback,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "final_report",
                    "preferred_path": "lab_outputs/compact-stress/report.md",
                    "kind": "md",
                    "required": True,
                }
            ]
        },
        user_prompt=(
            "请完整分析 data/long_field_journal.txt。\n"
            "读的时候在 work/章节草稿记录表.md 做一张记录表。\n"
            "最后把报告写到 lab_outputs/compact-stress/report.md。"
        ),
    )

    artifact_paths = [artifact.get("preferred_path") for artifact in contract["artifacts"]]
    assert artifact_paths == [
        "lab_outputs/compact-stress/report.md",
        "work/章节草稿记录表.md",
    ]
    work_artifact = contract["artifacts"][1]
    assert work_artifact["allowed_output_roots"] == ["work"]
    assert work_artifact["kind"] == "md"
    assert "data/long_field_journal.txt" in materializer_repair_feedback(contract)


def test_materialized_delivery_contract_does_not_treat_source_under_output_named_ancestor_as_output():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
        materializer_repair_feedback,
    )

    root = "/tmp/cross_agent_benchmark/my_agent_current_after_inline_outputs"
    source_path = f"{root}/data/long_field_journal.txt"
    report_path = f"{root}/lab_outputs/compact-stress/report.md"

    contract = materialized_delivery_contract(
        {},
        user_prompt=(
            f"{source_path} 是一个很大的现场记录。请完整分析这个文件。\n"
            f"最终把报告写到 {report_path}。"
        ),
    )

    artifact_paths = [item.get("preferred_path") for item in contract["artifacts"]]
    assert artifact_paths == [report_path]
    assert source_path in materializer_repair_feedback(contract)


def test_materialized_delivery_contract_does_not_promote_subagent_internal_work_report_from_prompt():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {},
        user_prompt=(
            "参考 work/agents/child-1/final_report.md 的内容，"
            "最后把报告写到 lab_outputs/final.md。"
        ),
    )

    artifact_paths = [artifact.get("preferred_path") for artifact in contract["artifacts"]]
    assert artifact_paths == ["lab_outputs/final.md"]


def test_materializer_prompt_keeps_target_coverage_structural_not_hard_by_default():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        build_delivery_requirement_materializer_prompt,
    )

    prompt = build_delivery_requirement_materializer_prompt(
        "请完整读完 data/long_field_journal.txt，并写报告。"
    )

    assert "可以写 target_coverage_contract" in prompt
    assert "不要仅凭普通自然语言把覆盖要求升级成硬性验收门" in prompt
    assert "enforcement 设为 required" not in prompt


def test_materialized_delivery_contract_derives_absolute_output_path_from_prompt(tmp_path):
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    requested_path = tmp_path.parent / "requested-output" / "report.md"

    contract = materialized_delivery_contract(
        {},
        user_prompt=f"请读完项目源码，最后把报告写到 {requested_path}。",
    )

    assert contract["artifacts"] == [
        {
            "artifact_id": "user_requested_report_md",
            "preferred_path": str(requested_path),
            "allowed_output_roots": [str(requested_path.parent)],
            "required": True,
            "kind": "md",
        }
    ]


def test_materialized_delivery_contract_does_not_duplicate_absolute_prompt_path(tmp_path):
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    requested_path = tmp_path.parent / "requested-output" / "report.md"

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "architecture_report",
                    "kind": "md",
                    "preferred_path": str(requested_path),
                }
            ]
        },
        user_prompt=f"请读完项目源码，最后把报告写到 {requested_path}。",
    )

    assert contract["artifacts"] == [
        {
            "artifact_id": "architecture_report",
            "preferred_path": str(requested_path),
            "required": True,
            "kind": "md",
        }
    ]


def test_materialized_delivery_contract_derives_windows_output_path_from_prompt():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {},
        user_prompt=r"请读完项目源码，最后把报告写到 C:\Users\alice\agent-output\report.md。",
    )

    assert contract["artifacts"] == [
        {
            "artifact_id": "user_requested_report_md",
            "preferred_path": r"C:\Users\alice\agent-output\report.md",
            "allowed_output_roots": [r"C:\Users\alice\agent-output"],
            "required": True,
            "kind": "md",
        }
    ]


def test_materialized_delivery_contract_derives_home_output_path_from_prompt():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {},
        user_prompt="请读完项目源码，最后把报告写到 ~/agent-output/report.md。",
    )

    assert contract["artifacts"] == [
        {
            "artifact_id": "user_requested_report_md",
            "preferred_path": "~/agent-output/report.md",
            "allowed_output_roots": ["~/agent-output"],
            "required": True,
            "kind": "md",
        }
    ]


def test_materialized_delivery_contract_promotes_prompt_output_path_to_existing_artifact():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {"artifacts": [{"artifact_id": "report", "required": True}]},
        user_prompt="请整理报告，保存到 outputs/final.md",
    )

    assert contract["artifacts"][0]["artifact_id"] == "report"
    assert contract["artifacts"][0]["preferred_path"] == "outputs/final.md"
    assert contract["artifacts"][0]["kind"] == "md"


def test_materialized_delivery_contract_corrects_same_name_artifact_to_prompt_path():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    wrong_path = "/tmp/validation/cross_agent_benchmark/20250605-192409/run/lab_outputs/compact-stress/report.md"
    requested_path = "/tmp/validation/cross_agent_benchmark/20260605-192409/run/lab_outputs/compact-stress/report.md"

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "final_report",
                    "kind": "md",
                    "preferred_path": wrong_path,
                    "required": True,
                }
            ]
        },
        user_prompt=f"读完数据后，最终报告写到 {requested_path}",
    )

    assert contract["artifacts"] == [
        {
            "artifact_id": "final_report",
            "kind": "md",
            "preferred_path": requested_path,
            "required": True,
            "allowed_output_roots": [
                "/tmp/validation/cross_agent_benchmark/20260605-192409/run/lab_outputs/compact-stress"
            ],
        }
    ]


def test_materialized_delivery_contract_does_not_stringify_object_kind():
    """模型把 kind 写成对象时，系统不能把字典文本当 artifact kind。"""
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "hello",
                    "kind": {"type": "file", "extensions": [".txt"]},
                    "preferred_path": "outputs/hello.txt",
                }
            ]
        }
    )

    artifact = contract["artifacts"][0]
    assert artifact["kind"] == "txt"
    assert "{" not in artifact["kind"]


def test_materialized_delivery_contract_promotes_file_root_to_preferred_path():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "weekly_workbook",
                    "kind": "xlsx",
                    "allowed_output_roots": ["outputs/result.xlsx"],
                }
            ]
        }
    )

    artifact = contract["artifacts"][0]
    assert artifact["preferred_path"] == "outputs/result.xlsx"
    assert artifact["allowed_output_roots"] == ["outputs"]


def test_materialized_delivery_contract_does_not_auto_bootstrap_plain_artifact(tmp_path):
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "weekly_workbook",
                    "kind": "xlsx",
                    "preferred_path": str(tmp_path / "outputs/result.xlsx"),
                }
            ]
        },
        workspace_root=tmp_path,
    )

    assert "bootstrap_contract" not in contract
    assert contract["artifacts"][0]["preferred_path"] == str(tmp_path / "outputs/result.xlsx")


def test_materialized_delivery_contract_preserves_explicit_soft_bootstrap():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [{"artifact_id": "report", "kind": "md", "preferred_path": "outputs/report.md"}],
            "bootstrap_contract": {
                "materialization_targets": [
                    {
                        "artifact_id": "report",
                        "target_type": "artifact",
                        "workspace_relative_path": "outputs/report.md",
                    }
                ],
            },
        }
    )

    assert contract["bootstrap_contract"]["enforcement"] == "soft"
    assert contract["bootstrap_contract"]["materialization_targets"][0]["workspace_relative_path"] == "outputs/report.md"


def test_materialized_delivery_contract_preserves_columns_without_auto_staging():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "weekly_workbook",
                    "kind": "xlsx",
                    "preferred_path": "outputs/result.xlsx",
                    "validation_contract": {
                        "required_columns": ["project", "metric"],
                    },
                }
            ]
        }
    )

    artifact = contract["artifacts"][0]
    validation = artifact["validation_contract"]
    assert validation["required_columns"] == ["project", "metric"]
    assert "staging_contract" not in validation
    assert "collection_contract" not in validation
    assert "bootstrap_contract" not in contract


def test_materialized_workbook_contract_preserves_llm_generated_fields_without_auto_collection():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "weekly_workbook",
                    "kind": "xlsx",
                    "preferred_path": "outputs/result.xlsx",
                    "llm_generated_fields": ["中文说明", "说明依据"],
                    "validation_contract": {
                        "required_columns": ["记录名", "来源地址", "本周新增 star 数", "中文说明", "说明依据"],
                    },
                }
            ],
            "delivery_quality_contract": {
                "metric_contracts": [{"field": "本周新增 star 数", "expected_kind": "period_delta"}],
            },
        }
    )

    validation = contract["artifacts"][0]["validation_contract"]
    artifact = contract["artifacts"][0]
    assert artifact["llm_generated_fields"] == ["中文说明", "说明依据"]
    assert validation["required_columns"] == ["记录名", "来源地址", "本周新增 star 数", "中文说明", "说明依据"]
    assert "collection_contract" not in validation
    assert "staging_contract" not in validation
    assert contract["fact_evidence_contract"]["evidence_contract"]["required_fields"] == ["本周新增 star 数"]


def test_materialized_delivery_contract_preserves_generic_target_coverage_contract():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [{"artifact_id": "final_workbook", "kind": "xlsx", "allowed_output_roots": ["outputs"]}],
            "target_coverage_contract": {
                "scope_label": "今年以来每周",
                "enforcement": "advisory",
                "target_items": [
                    {"target_id": "2026-W01", "label": "2026 第 1 周"},
                    {"target_id": "2026-W02", "label": "2026 第 2 周"},
                ],
            },
        }
    )

    coverage = contract["target_coverage_contract"]
    assert coverage["scope_label"] == "今年以来每周"
    assert coverage["enforcement"] == "advisory"
    assert [item["target_id"] for item in coverage["target_items"]] == ["2026-W01", "2026-W02"]


def test_materialized_delivery_contract_does_not_derive_coverage_from_prompt_phrases():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "compact-stress-report",
                    "kind": "markdown",
                    "preferred_path": "lab_outputs/compact-stress/report.md",
                }
            ]
        },
        user_prompt=(
            "data/long_field_journal.txt 是一个很大的现场记录。请完整读完，按顺序慢慢读，"
            "不要只抽样，也不要只搜几个关键词。\n"
            "最终把报告写到 lab_outputs/compact-stress/report.md。"
        ),
    )

    assert "target_coverage_contract" not in contract
    assert contract["_contract_doctor"]["findings"][0]["code"] == "DELIVERY_MATERIALIZER_SOURCE_COVERAGE_UNDECLARED"


def test_materialized_delivery_contract_preserves_structured_full_source_read_coverage():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [{"artifact_id": "report", "kind": "md", "preferred_path": "report.md"}],
            "target_coverage_contract": {
                "scope_label": "source files",
                "coverage_requirement": "full_source_read",
                "target_items": [
                    {
                        "target_id": "README.md",
                        "source_ref": "README.md",
                        "coverage_kind": "full_source_read",
                    }
                ],
            },
        },
        user_prompt="请完整读完 README.md，最后写到 report.md。",
    )

    coverage = contract["target_coverage_contract"]
    assert [item["target_id"] for item in coverage["target_items"]] == ["README.md"]
    assert "_contract_doctor" not in contract


def test_materialized_contract_preserves_artifact_intent_extensions_for_broad_formats():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "cad_drawing",
                    "kind_label": "CAD 图纸",
                    "artifact_intent": {
                        "kind_label": "CAD 图纸",
                        "preferred_extension": "dxf",
                        "acceptable_extensions": ["dxf", "step", "stp"],
                    },
                    "allowed_output_roots": ["outputs"],
                }
            ]
        }
    )

    artifact = contract["artifacts"][0]
    assert artifact["kind_label"] == "CAD 图纸"
    assert artifact["artifact_intent"]["acceptable_extensions"] == ["dxf", "step", "stp"]
    assert artifact["file_extensions"] == ["dxf", "step", "stp"]


def test_materialized_workbook_staging_infers_kind_from_output_path_and_metric_name():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "result.xlsx",
                    "preferred_path": "outputs/result.xlsx",
                    "llm_generated_fields": ["中文说明", "说明依据"],
                    "validation_contract": {
                        "required_columns": [
                            "记录名",
                            "来源地址",
                            "指标值",
                            "当前总star数",
                            "中文说明",
                            "说明依据",
                            "技术栈",
                        ],
                    },
                }
            ],
            "delivery_quality_contract": {
                "metric_contracts": [
                    {
                        "field": "指标值",
                        "type": "number",
                    }
                ]
            },
        }
    )

    artifact = contract["artifacts"][0]
    assert artifact["kind"] == "xlsx"
    validation = artifact["validation_contract"]
    assert "staging_contract" not in validation
    assert "collection_contract" not in validation
    assert artifact["llm_generated_fields"] == ["中文说明", "说明依据"]
    assert contract["fact_evidence_contract"]["evidence_contract"]["required_fields"] == ["指标值"]


def test_materialized_delivery_contract_allows_user_requested_absolute_artifact_path(tmp_path):
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    requested_dir = tmp_path.parent / "requested-output"
    requested_path = requested_dir / "report.pdf"
    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "external_report",
                    "kind": "pdf",
                    "preferred_path": str(requested_path),
                }
            ]
        },
        workspace_root=tmp_path,
    )

    assert contract["artifacts"][0]["preferred_path"] == str(requested_path)
    assert "_contract_doctor" not in contract


def test_materialized_delivery_contract_drops_internal_work_draft_artifact():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {"artifact_id": "chapter_draft", "allowed_output_roots": ["work"], "required": True},
                {
                    "artifact_id": "final_report",
                    "preferred_path": "lab_outputs/compact-stress/report.md",
                    "kind": "md",
                    "required": True,
                },
            ],
        }
    )

    assert [artifact["artifact_id"] for artifact in contract["artifacts"]] == ["final_report"]
    assert "_contract_doctor" not in contract


def test_materialized_delivery_contract_attaches_doctor_findings_for_bad_contract_shape():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract({"artifacts": "output.md"})

    doctor = contract["_contract_doctor"]
    assert doctor["ok"] is False
    assert doctor["repair_actions"][0]["recommended_action"] == "repair_effective_contract"
    assert "DELIVERY_CONTRACT_ARTIFACTS_NOT_LIST" in {
        finding["code"] for finding in doctor["findings"]
    }
    assert "normalized_contract" not in doctor


def test_materializer_prompt_asks_for_structured_contract_not_task_template():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        build_delivery_requirement_materializer_prompt,
    )

    prompt = build_delivery_requirement_materializer_prompt("帮我生成一个文件，放到输出目录")

    assert "delivery_contract.v1" in prompt
    assert "不要写具体执行步骤模板" in prompt
    assert "required_columns" in prompt
    assert "metric_contracts" in prompt
    assert "代码平台" not in prompt
    assert "论文" not in prompt


def test_materializer_prompt_mentions_target_coverage_without_task_templates():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        build_delivery_requirement_materializer_prompt,
    )

    prompt = build_delivery_requirement_materializer_prompt("今年以来每周都要覆盖")

    assert "target_coverage_contract" in prompt
    assert "目标清单" in prompt
    assert "GitHub 固定列" not in prompt
