"""测试普通文件内容合同和父级 content_check 推断。"""

from agent_py_agent.agent.subagents.execution.test_items import (
    TestItemPreparationRequest,
    prepare_test_items,
)


def test_prepare_test_items_infers_content_checks_for_single_file_artifact(tmp_path):
    report_dir = tmp_path / "deliverables" / "orders"
    report_dir.mkdir(parents=True)
    artifact = report_dir / "orders.csv"
    artifact.write_text("order_id,total\nA-1001,299.00\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
            required_content_lines=[
                "order_id,customer,total,status,notes",
                "A-1001,Lin Studio,299.00,PAID,first order",
            ],
        )
    )

    assert prepared == [
        {
            "name": "inferred content check 1",
            "validation_method": "content_check",
            "file_path": "deliverables/orders/orders.csv",
            "content_pattern": "order_id,customer,total,status,notes",
        },
        {
            "name": "inferred content check 2",
            "validation_method": "content_check",
            "file_path": "deliverables/orders/orders.csv",
            "content_pattern": "A-1001,Lin Studio,299.00,PAID,first order",
        },
    ]


def test_prepare_test_items_does_not_guess_required_content_file_for_multiple_artifacts(tmp_path):
    report_dir = tmp_path / "deliverables" / "orders"
    report_dir.mkdir(parents=True)
    orders = report_dir / "orders.csv"
    readme = report_dir / "README.md"
    orders.write_text("order_id,total\n", encoding="utf-8")
    readme.write_text("# report\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [{"path": str(orders)}, {"path": str(readme)}]},
            workspace_root=tmp_path,
            required_content_lines=["order_id,customer,total,status,notes"],
        )
    )

    assert prepared == []


def test_prepare_test_items_infers_content_checks_for_mapped_file_artifacts(tmp_path):
    report_dir = tmp_path / "deliverables" / "order-pack"
    report_dir.mkdir(parents=True)
    report = report_dir / "report.md"
    data = report_dir / "orders.csv"
    report.write_text("# 记录报告\n", encoding="utf-8")
    data.write_text("order_id,total\nA-1001,299.00\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [{"path": str(report)}, {"path": str(data)}]},
            workspace_root=tmp_path,
            required_content_files={
                "report.md": ["# 记录报告"],
                "orders.csv": ["order_id,total", "A-1001,299.00"],
            },
        )
    )

    assert prepared == [
        {
            "name": "inferred content check report.md 1",
            "validation_method": "content_check",
            "file_path": "deliverables/order-pack/report.md",
            "content_pattern": "# 记录报告",
        },
        {
            "name": "inferred content check orders.csv 1",
            "validation_method": "content_check",
            "file_path": "deliverables/order-pack/orders.csv",
            "content_pattern": "order_id,total",
        },
        {
            "name": "inferred content check orders.csv 2",
            "validation_method": "content_check",
            "file_path": "deliverables/order-pack/orders.csv",
            "content_pattern": "A-1001,299.00",
        },
    ]
