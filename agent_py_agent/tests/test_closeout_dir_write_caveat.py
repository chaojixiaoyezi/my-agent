"""落点纠偏钉子(codetask 实锤):主代理把交付物写进 .agent_delivery/(系统 closeout
账本目录,望文生义当成交付区),产物不进交付区也不被验收。write_file 写入该目录时应
附加软提示,引导模型把交付物写到 output_dir。"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling._filesystem_write import _closeout_dir_write_caveat


def test_caveat_fires_for_closeout_dir(tmp_path):
    caveat = _closeout_dir_write_caveat(tmp_path / ".agent_delivery" / "report.md", tmp_path)
    assert caveat
    assert "output_dir" in caveat


def test_caveat_fires_for_nested_closeout_dir(tmp_path):
    caveat = _closeout_dir_write_caveat(
        tmp_path / ".agent_delivery" / "sub" / "calculator.py", tmp_path
    )
    assert caveat


def test_no_caveat_for_output_dir(tmp_path):
    assert _closeout_dir_write_caveat(tmp_path / "output" / "report.md", tmp_path) == ""


def test_no_caveat_for_ordinary_file(tmp_path):
    assert _closeout_dir_write_caveat(tmp_path / "src" / "main.py", tmp_path) == ""


def test_no_caveat_for_similar_but_different_name(tmp_path):
    # 名字相近但不是 .agent_delivery 的目录不应误报
    assert _closeout_dir_write_caveat(tmp_path / "agent_delivery" / "x.md", tmp_path) == ""
    assert _closeout_dir_write_caveat(tmp_path / ".agent_deliverables" / "x.md", tmp_path) == ""


def test_caveat_robust_to_bad_inputs(tmp_path):
    # workspace_root 非法/路径越界不抛异常
    assert _closeout_dir_write_caveat(Path("/etc/passwd"), tmp_path) == ""
    assert _closeout_dir_write_caveat(tmp_path / ".agent_delivery" / "x", None) == ""


def test_write_file_tool_attaches_caveat(tmp_path):
    # 集成:write_file 写 .agent_delivery 时,结果文本含落点提示
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool

    tool = WriteFileTool(workspace_root=tmp_path)
    result = tool.execute(
        {"path": str(tmp_path / ".agent_delivery" / "report.md"), "content": "hi"}
    )
    assert result.ok
    assert ".agent_delivery" in result.output or "output_dir" in result.output


# --- 产出意图兜底:误写 .agent_delivery 的产物也触发 closeout(治本第6) ---


def test_misplaced_products_detects_model_artifact(tmp_path):
    from agent_py_agent.agent.agent_core._finalization_service import (
        _misplaced_products_in_closeout_dir,
    )

    cd = tmp_path / ".agent_delivery"
    cd.mkdir()
    (cd / "closeout.json").write_text("{}", encoding="utf-8")
    assert _misplaced_products_in_closeout_dir(tmp_path) is False  # 只有系统账本
    (cd / "calculator.py").write_text("x", encoding="utf-8")
    assert _misplaced_products_in_closeout_dir(tmp_path) is True  # 模型误写产物


def test_misplaced_products_ignores_ledger_jsonl(tmp_path):
    from agent_py_agent.agent.agent_core._finalization_service import (
        _misplaced_products_in_closeout_dir,
    )

    cd = tmp_path / ".agent_delivery"
    cd.mkdir()
    (cd / "delivery_quality_gate.jsonl").write_text("{}\n", encoding="utf-8")
    assert _misplaced_products_in_closeout_dir(tmp_path) is False


def test_misplaced_products_no_dir(tmp_path):
    from agent_py_agent.agent.agent_core._finalization_service import (
        _misplaced_products_in_closeout_dir,
    )

    assert _misplaced_products_in_closeout_dir(tmp_path) is False
