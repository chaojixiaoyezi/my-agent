# LLM: 仅开发测试使用；构造隔离目录的真实 host 请求，不连接模型/TUI，不替代实际产品验收。
# 模块用途: 为启动、取消、日志上限与冷查询合同提供同一份测试参数，测试负责回收自己创建的进程。
from __future__ import annotations

import os
import sys
from pathlib import Path

from agent_py_agent.agent.tooling.background_process_launch import BackgroundLaunchRequest
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope


# LLM: 所有地址均归 tmp_path；环境仅传进程，写盘的业务脚本不得包含真实用户配置。
# 函数用途: 建立一条测试 Python 命令，默认具备固定测试身份和足够的日志预算。
def managed_request(
    tmp_path: Path, code: str = "import time; time.sleep(30)", **overrides
) -> BackgroundLaunchRequest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    owner = str(tmp_path)
    values = dict(
        argv=[sys.executable, "-u", "-c", code],
        command="isolated test command",
        cwd=tmp_path,
        log_path=tmp_path / "output.log",
        env=dict(os.environ),
        max_log_bytes=1_000_000,
        store_root=tmp_path / "authority",
        access_scope=ProcessAccessScope("owner-test", "thread-test", owner),
        execution_scope=ProcessExecutionScope(
            owner, "thread-test", "task-test", "run-test", "attempt-test"
        ),
    )
    values.update(overrides)
    return BackgroundLaunchRequest(**values)
