# LLM: 工具索引计量每例独立解释器；父进程pytest历史不能决定子进程的intern表或tracemalloc窗口，原测量断言原样执行。
# 模块用途: 将三个工具引用测量文件的每个用例放进新进程，不过滤分配来源或放宽峰值上限。
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPORTS: list[list[object]] = []


# LLM: 只供测试子进程防递归使用，不是产品配置；父进程未加载此插件也应按False处理。
# 函数用途: 给独立测量用例注册子进程标记。
def pytest_addoption(parser) -> None:
    parser.addoption("--toolrefs-measure-child", default="", help="isolated measurement receipt path")


# LLM: 收集结构化阶段，不解析pytest文案；skip/xfail不可以仅凭退出0冒充测量完成。
# 函数用途: 记录子用例各阶段的真实身份、结果与预期失败标记。
def pytest_runtest_logreport(report) -> None:
    _REPORTS.append([report.nodeid, report.when, report.outcome, hasattr(report, "wasxfail")])


# LLM: sessionfinish也会在collect-only后运行，空回执必须由父用例拒绝；三阶段全成功才代表完整原体完成。
# 函数用途: 将子pytest报告写入父用例指定的独立临时文件。
def pytest_sessionfinish(session, exitstatus) -> None:
    if path := session.config.getoption("--toolrefs-measure-child", default=""):
        Path(path).write_text(json.dumps(_REPORTS), encoding="utf-8")


# LLM: 保留正常解释器所需环境，但移除能重放pytest插件/collect-only/启动trace的继承变量，不读取或打印凭据值。
# 函数用途: 限定子计量解释器的pytest配置和初始tracemalloc状态。
def _measurement_environment(root: Path) -> dict[str, str]:
    excluded = {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONTRACEMALLOC", "PYTHONOPTIMIZE"}
    environment = {key: value for key, value in os.environ.items() if key not in excluded}
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(root))
    return environment


# LLM: 子进程仅跑当前nodeid并沿用当前解释器/工作树；结果、计数和峰值断言失败原样使父用例失败。
# 函数用途: 父用例启动全新pytest解释器，子用例返回False后执行原测量体，所有计量窗口均留在子进程。
def run_measurement_isolated(request) -> bool:
    if request.config.getoption("--toolrefs-measure-child", default=False):
        return False
    root = Path(__file__).resolve().parents[2]
    temporary = request.getfixturevalue("tmp_path")
    receipt = temporary / "measurement-report.json"
    receipt.write_text("[]", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", request.node.nodeid,
         "-p", "agent_py_agent.tests.helper_tool_ref_isolation", "--toolrefs-measure-child", str(receipt),
         "-o", "addopts=", "-q", "-s", "--tb=short", "-p", "no:cacheprovider",
         "--basetemp", str(temporary / "measurement-child")],
        cwd=root, text=True, capture_output=True, timeout=120, env=_measurement_environment(root),
    )
    print(result.stdout)
    # helper并非test模块，父解释器-O会删除普通assert；失败判据必须显式抛错。
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    expected = [[request.node.nodeid, phase, "passed", False] for phase in ("setup", "call", "teardown")]
    if json.loads(receipt.read_text(encoding="utf-8")) != expected:
        raise AssertionError("measurement child did not execute full original test")
    return True
