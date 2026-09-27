"""CI 工作流里点名的测试文件必须真实存在（2026-09-27）。

背景：参数减量第 1 批删掉了 test_watchdog.py，但 .github/workflows/cross-platform-guard.yml 的测试清单还列着它，
pytest 因“文件不存在”直接报用法错误退出，macOS 与 Windows 两个作业一个用例都没跑。本地全量分片只按文件扫描，发现不了。
锁定：工作流 YAML 里出现的每个 agent_py_agent/tests/...py 路径都必须在仓库里存在；删除或改名测试文件时同一批改工作流。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TEST_PATH = re.compile(r"agent_py_agent/tests/[\w/]+\.py")


def test_every_test_file_named_in_ci_workflows_exists():
    workflow_dir = _REPO / ".github" / "workflows"
    if not workflow_dir.is_dir():
        pytest.skip("不在源码检出里运行（安装包里没有 .github）")
    workflows = sorted(workflow_dir.glob("*.y*ml"))
    assert workflows, "没有找到 CI 工作流文件"
    missing = sorted({
        f"{workflow.name}: {path}"
        for workflow in workflows
        for path in _TEST_PATH.findall(workflow.read_text(encoding="utf-8"))
        if not (_REPO / path).is_file()
    })
    assert not missing, f"工作流里点名的测试文件不存在（删除或改名测试时要同步改工作流）：{missing}"
