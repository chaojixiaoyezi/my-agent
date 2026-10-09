# LLM: 子pytest退出0不等于执行；用结构化phase回执测试collect-only/skip/xfail/错误身份不能冒充原测量成功。
# 模块用途: 锁住工具索引计量的真实执行回执和子进程环境边界，不碰产品读取器。
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.tests import helper_tool_ref_isolation as isolation

NODE = "synthetic_measure.py::test_measure[control]"
REPORTS = [[NODE, phase, "passed", False] for phase in ("setup", "call", "teardown")]


# LLM: 只伪造测试request，不运行外部命令，子成功/失败由各用例注入。
# 函数用途: 准备独立临时目录和固定参数nodeid的请求替身。
def _request(tmp_path, child=""):
    return SimpleNamespace(config=SimpleNamespace(getoption=lambda *args, **kwargs: child),
                           node=SimpleNamespace(nodeid=NODE), getfixturevalue=lambda name: tmp_path)


# LLM: 只给退出码0而不给完整执行回执，旧helper会错误通过；新helper必须明确失败。
# 函数用途: 拒绝未执行、skip、xfail、错误nodeid和缺teardown的假成功。
@pytest.mark.parametrize("reports", [[], [[NODE, "setup", "skipped", False]],
                                    [[NODE, "call", "passed", True]],
                                    [["wrong-node", phase, "passed", False] for phase in ("setup", "call", "teardown")],
                                    REPORTS[:2]])
def test_child_exit_zero_requires_full_execution(tmp_path, monkeypatch, reports):
    def fake_run(command, **kwargs):
        (tmp_path / "measurement-report.json").write_text(json.dumps(reports))
        return SimpleNamespace(returncode=0, stdout="synthetic child", stderr="")
    monkeypatch.setattr(isolation.subprocess, "run", fake_run)
    with pytest.raises(AssertionError, match="did not execute"):
        isolation.run_measurement_isolated(_request(tmp_path))


# LLM: 仅允许完整成功的当前参数身份；显式去掉控制pytest/tracemalloc的继承变量，不能外推隔离所有系统环境。
# 函数用途: 检查正常回执、精确nodeid和受控子环境。
def test_child_full_execution_and_environment(tmp_path, monkeypatch):
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONTRACEMALLOC"):
        monkeypatch.setenv(key, "synthetic-parent-pollution")
    def fake_run(command, **kwargs):
        assert command[4] == NODE
        environment = kwargs["env"]
        assert all(key not in environment for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONTRACEMALLOC"))
        assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
        (tmp_path / "measurement-report.json").write_text(json.dumps(REPORTS))
        return SimpleNamespace(returncode=0, stdout="synthetic child", stderr="")
    monkeypatch.setattr(isolation.subprocess, "run", fake_run)
    assert isolation.run_measurement_isolated(_request(tmp_path))


# LLM: 子标记必须落回原体，而不能继续派子进程或将自身标记为通过。
# 函数用途: 验证子进程防递归分支。
def test_child_marker_runs_original_body(tmp_path, monkeypatch):
    monkeypatch.setattr(isolation.subprocess, "run", lambda *args, **kwargs: pytest.fail("recursive child"))
    assert isolation.run_measurement_isolated(_request(tmp_path, "report-path")) is False


# LLM: 非零进程结果不能被回执或父解释器优化模式掩盖。
# 函数用途: 验证子进程运行失败原样向父用例抛出。
@pytest.mark.parametrize("code", [1, 2])
def test_child_failure_propagates(tmp_path, monkeypatch, code):
    monkeypatch.setattr(isolation.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=code, stdout="synthetic failure", stderr="child error"))
    with pytest.raises(AssertionError, match="synthetic failurechild error"):
        isolation.run_measurement_isolated(_request(tmp_path))
