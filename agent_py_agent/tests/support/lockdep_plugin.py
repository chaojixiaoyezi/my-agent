"""仅测试开启严格锁诊断；生产配置和环境变量均不能开启。"""
import pytest

from agent_py_agent.agent.concurrency import lock_order


def pytest_sessionstart(session):
    lock_order.reset_for_tests()
    lock_order.configure(strict=True)


def pytest_sessionfinish(session, exitstatus):
    snapshot = lock_order.lockdep_snapshot()
    if snapshot["violations"] or snapshot["handler_overruns"]:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"lockdep: {snapshot}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    lock_order.configure(strict=False)
