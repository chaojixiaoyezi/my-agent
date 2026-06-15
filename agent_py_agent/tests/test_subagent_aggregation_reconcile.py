from __future__ import annotations

import tempfile
from pathlib import Path

# 防回归: 真实任务测试(M2.7 子代理协作)暴露——子代理声明 output_ref 路径目录名拼写错,
# 但实际产物在 workspace 内同名 basename, 交付门却照声明的错路径判"缺失"→死循环 rework
# (即使产物在、集成测试过),违背永不停机。修复: _declared_ref_missing 在声明路径不存在时,
# 在该子代理 workspace_root 内按 basename 兜底对账,命中即放行;找不到才记缺失(护 R4 声明40实交1)。

from agent_py_agent.agent.agent_core.delivery_closeout.subagent_aggregation import (
    _declared_ref_missing,
)


def _workspace_with_file(rel_path: str) -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp())
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    return root, target


def test_typo_declared_path_reconciled_by_basename():
    # 声明路径目录名拼写错(实际产物在 workspace 内同名 basename)→ 不应误判缺失。
    root, _ = _workspace_with_file("output/analyzer.py")
    typo = "/Users/WRONGUSER/some/wrong/dir/output/analyzer.py"
    assert _declared_ref_missing(typo, str(root)) is False


def test_truly_missing_still_flagged():
    # 护 R4 声明40实交1: workspace 内无同名产物时仍判缺失(不放跑吹牛)。
    root, _ = _workspace_with_file("output/analyzer.py")
    typo = "/Users/WRONGUSER/wrong/dir/output/never_written_zzz.py"
    assert _declared_ref_missing(typo, str(root)) is True


def test_declared_path_exists_unchanged():
    # 原行为: 声明路径真实存在 → 不缺失。
    root, real = _workspace_with_file("output/report.md")
    assert _declared_ref_missing(str(real), str(root)) is False


def test_no_workspace_root_absolute_missing_is_conservative():
    # workspace_root 为空、绝对路径不存在 → 无对账依据,保守判缺失(与原行为一致)。
    assert _declared_ref_missing("/Users/x/nonexistent_abc_zzz.py", "") is True
