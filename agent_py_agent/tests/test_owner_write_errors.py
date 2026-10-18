# LLM: 跨层公开导入必须是同一个原异常类；这里只查fresh process依赖与负向协议，不模拟producer授权。
# 模块用途: 防止异常纯搬迁出现第二定义、导入环或丢失原因链。
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("first", ["common", "control", "management"])
def test_public_source_errors_keep_one_identity_in_fresh_process(first):
    modules = {
        "common": "agent_py_agent.agent.common.owner_write_errors",
        "control": "agent_py_agent.agent.gateway_parts.multiprocess_local_control",
        "management": "agent_py_agent.agent.gateway_parts.owner_management_control",
    }
    script = "import importlib; importlib.import_module(" + repr(modules[first]) + ")\n"
    script += """from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable
from agent_py_agent.agent.gateway_parts.multiprocess_local_control import LocalControlCoordinatorUnavailable as PublicControl
from agent_py_agent.agent.gateway_parts.owner_management_control import OwnerManagementIngressUnavailable as PublicManagement
from agent_py_agent.agent.runtime_errors import DataCorruptionError
assert PublicControl is LocalControlCoordinatorUnavailable
assert PublicManagement is OwnerManagementIngressUnavailable
assert issubclass(PublicControl, DataCorruptionError)
assert issubclass(PublicManagement, ValueError)
assert not hasattr(PublicControl, 'error_code')
assert PublicManagement('binding').reason == 'binding'
cause = OSError('original')
error = PublicControl('original-control')
try:
    raise error from cause
except PublicControl as observed:
    assert observed is error and observed.__cause__ is cause
"""
    completed = subprocess.run([sys.executable, "-B", "-c", script],
        capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert completed.returncode == 0, completed.stderr
