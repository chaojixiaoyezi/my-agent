"""测试普通文件内容合同和父级 content_check 推断。"""

from agent_py_agent.agent.subagents.execution_test_items import (
    TestItemPreparationRequest,
    prepare_test_items,
)
from agent_py_agent.agent.subagents.required_content_lines import (
    required_content_lines_by_file_from_texts,
    required_content_lines_from_texts,
)


# LLM: Required content lines should become parent content checks for non-web artifacts.
# 函数用途: 子代理只报告一个普通文件产物时，父级能按机器字段检查关键内容，不依赖模型自评。
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


# LLM: Ambiguous multi-artifact outputs should not guess which file owns required lines.
# 函数用途: 有多个普通文件产物时先不自动猜目标文件，避免把验收内容套到错误文件上。
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


# LLM: Explicit per-file content contracts let parent acceptance validate multi-file outputs safely.
# 函数用途: 多个普通文件产物时，只按文件名映射生成对应 content_check，不靠顺序猜测。
def test_prepare_test_items_infers_content_checks_for_mapped_file_artifacts(tmp_path):
    report_dir = tmp_path / "deliverables" / "order-pack"
    report_dir.mkdir(parents=True)
    report = report_dir / "report.md"
    data = report_dir / "orders.csv"
    report.write_text("# 订单报告\n", encoding="utf-8")
    data.write_text("order_id,total\nA-1001,299.00\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [{"path": str(report)}, {"path": str(data)}]},
            workspace_root=tmp_path,
            required_content_files={
                "report.md": ["# 订单报告"],
                "orders.csv": ["order_id,total", "A-1001,299.00"],
            },
        )
    )

    assert prepared == [
        {
            "name": "inferred content check report.md 1",
            "validation_method": "content_check",
            "file_path": "deliverables/order-pack/report.md",
            "content_pattern": "# 订单报告",
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


# LLM: Structured required content text should keep CSV commas intact.
# 函数用途: 从内部验收合同提取必须出现的文本行，按竖线拆分，不把 CSV 逗号误当分隔符。
def test_required_content_lines_from_structured_acceptance_text():
    lines = required_content_lines_from_texts([
        "required_content_lines: order_id,customer,total,status,notes | A-1001,Lin Studio,299.00,PAID,first order",
        "普通说明不会被猜成内容验收",
    ])

    assert lines == [
        "order_id,customer,total,status,notes",
        "A-1001,Lin Studio,299.00,PAID,first order",
    ]


# LLM: Natural exact-line instructions should become content contracts without exposing internal field names.
# 函数用途: 用户只说“下面四行一字不差”时，也能抽取后续四行作为普通文件机器验收内容。
def test_required_content_lines_from_natural_exact_line_count_block():
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

    assert lines == [
        "order_id,customer,total,status,notes",
        "A-1001,Lin Studio,299.00,PAID,first order",
        "A-1002,North Home,188.50,SHIPPED,priority delivery",
        "SUMMARY,total_orders=2,total_amount=487.50,status=OK,notes=ready",
    ]


# LLM: Fenced expected content gives users a readable way to define exact file checks.
# 函数用途: 用户把必须包含的内容放进代码块时，逐行提取代码块内容，但不把外部说明当验收内容。
def test_required_content_lines_from_fenced_expected_block():
    lines = required_content_lines_from_texts([
        """
        报告必须包含以下内容：
        ```text
        # 周报
        - 完成数据清洗
        - 风险：等待验收
        ```
        其它解释不要加入内容检查。
        """
    ])

    assert lines == ["# 周报", "- 完成数据清洗", "- 风险：等待验收"]


# LLM: Per-file content contracts are explicit enough for multi-artifact parent checks.
# 函数用途: 从 `required_content_lines[file]` 和 “文件必须包含以下内容”提取文件到内容行的映射。
def test_required_content_lines_by_file_from_structured_and_natural_text():
    mapping = required_content_lines_by_file_from_texts([
        "required_content_lines[orders.csv]: order_id,total | A-1001,299.00",
        """
        report.md 必须包含以下内容：
        ```md
        # 订单报告
        - 已核对
        ```
        """,
    ])

    assert mapping == {
        "orders.csv": ["order_id,total", "A-1001,299.00"],
        "report.md": ["# 订单报告", "- 已核对"],
    }
