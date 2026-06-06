"""测试普通文件内容合同和父级 content_check 推断。"""

from types import SimpleNamespace

from agent_py_agent.agent.subagents.execution.test_items import (
    TestItemPreparationRequest,
    prepare_test_items,
)
from agent_py_agent.agent.subagents.required_content_lines import (
    required_content_lines_by_file_for_task,
    required_content_lines_by_file_from_texts,
    required_content_lines_for_task,
    required_content_lines_from_texts,
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


def test_required_content_lines_from_structured_acceptance_text():
    lines = required_content_lines_from_texts([
        "required_content_lines: order_id,customer,total,status,notes | A-1001,Lin Studio,299.00,PAID,first order",
        "普通说明不会被猜成内容验收",
    ])

    assert lines == [
        "order_id,customer,total,status,notes",
        "A-1001,Lin Studio,299.00,PAID,first order",
    ]


def test_required_content_lines_ignores_natural_exact_line_count_block():
    lines = required_content_lines_from_texts([
        """
        下面四行要一字不差出现在 CSV 里：
        order_id,customer,total,status,notes
        A-1001,Lin Studio,299.00,PAID,first order
        A-1002,North Home,188.50,SHIPPED,priority delivery
        SUMMARY,total_orders=2,total_amount=487.50,status=OK,notes=ready
        修好后请安排检查。
        """
    ])

    assert lines == []


def test_required_content_lines_ignores_natural_fenced_expected_block():
    lines = required_content_lines_from_texts([
        """
        报告必须包含以下内容：
        ```text
        # 周报
        - 完成数据清洗
        - 风险：等待收口
        ```
        其它解释不要加入内容检查。
        """
    ])

    assert lines == []


def test_required_content_lines_by_file_from_structured_text():
    mapping = required_content_lines_by_file_from_texts([
        "required_content_lines[orders.csv]: order_id,total | A-1001,299.00",
        "required_content_lines[report.md]: # 记录报告 | - 已核对",
    ])

    assert mapping == {
        "orders.csv": ["order_id,total", "A-1001,299.00"],
        "report.md": ["# 记录报告", "- 已核对"],
    }


def test_required_content_for_task_ignores_text_fields_and_reads_attributes():
    task = SimpleNamespace(
        goal="required_content_lines: should-not-count",
        thought="required_content_lines[report.md]: should-not-count",
        acceptance_checks=["required_content_lines: should-not-count"],
        attributes={
            "required_content_lines": ["order_id,total", "A-1001,299.00"],
            "required_content_files": {"report.md": ["# 记录报告"]},
        },
    )

    assert required_content_lines_for_task(task) == ["order_id,total", "A-1001,299.00"]
    assert required_content_lines_by_file_for_task(task) == {"report.md": ["# 记录报告"]}


def test_required_content_for_task_does_not_parse_goal_or_acceptance_checks():
    task = SimpleNamespace(
        goal="required_content_lines: should-not-count",
        thought="required_content_lines[report.md]: should-not-count",
        acceptance_checks=["required_content_lines: should-not-count"],
        attributes={},
    )

    assert required_content_lines_for_task(task) == []
    assert required_content_lines_by_file_for_task(task) == {}


def test_create_run_persists_required_content_attributes(tmp_path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="写记录报表",
        thought="普通说明不承载机器验收事实。",
        plan=["write"],
        attributes={
            "required_content_lines": ["order_id,total"],
            "required_content_files": {"orders.csv": ["A-1001,299.00"]},
        },
    )
    loaded = manager.load(task.id)

    assert required_content_lines_for_task(loaded) == ["order_id,total"]
    assert required_content_lines_by_file_for_task(loaded) == {"orders.csv": ["A-1001,299.00"]}
