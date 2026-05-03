from __future__ import annotations

"""LLM: smoke tests for the code-size governance script.

给人看的解释：
这个测试保证代码规模检查脚本至少能在 warn 模式运行并生成报告。
"""

import subprocess
from pathlib import Path


def test_check_code_size_warn_generates_report() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["python3", "scripts/check_code_size.py", "--mode", "warn"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "CODE_SIZE_REPORT.md" in result.stdout
    assert (repo_root / "CODE_SIZE_REPORT.md").exists()
