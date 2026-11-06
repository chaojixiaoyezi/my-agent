from __future__ import annotations

# LLM: MEM-W5 让 plain 终端（显式 --plain，或缺少 prompt_toolkit 自动降级）连 Gateway 时与 TUI 一样只构造瘦客户端；
#   direct 聊天（不连 Gateway）仍构造完整智能体，由既有 test_fresh_direct_chat_keeps_original_mcp_construction_policy 覆盖。
# 模块用途: 回归保护 plain + Gateway 聊天进程不再常驻完整 SimpleAgent/ToolRegistry（实测每进程约 44 MB）。
import subprocess
import sys

import pytest

from agent_py_agent.tests.support.ci_authenticated_gateway import seed_gateway_credential

_SCRIPT = r'''
import sys
from argparse import Namespace
from agent_py_agent.cli import chat
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
chat._has_prompt_toolkit = lambda: sys.argv[3] == "toolkit"
args = Namespace(config=sys.argv[1], workspace_root="", gateway=True, plain=sys.argv[2] == "plain", session_id="")
route = chat._select_chat_client_mode(args)
client = chat.make_agent(args)
assert route == (True, False), route
assert args._gateway_client_mode is True
assert isinstance(client, GatewayChatClientAgent), type(client).__name__
assert "agent_py_agent.agent.core" not in sys.modules
print("THIN-OK")
'''


# LLM: 新解释器观察真实构造与导入集合；同一 pytest 进程里 agent.core 早已被别的用例导入，观察会失真。
# 函数用途: 确认 --plain 与“缺少 prompt_toolkit”两种 plain + Gateway 入口都只构造瘦客户端、不加载完整智能体。
@pytest.mark.parametrize("plain,toolkit", [("plain", "toolkit"), ("auto", "missing")])
def test_plain_gateway_chat_constructs_only_thin_client(tmp_path, plain, toolkit):
    home = tmp_path / "home"
    seed_gateway_credential(home)
    config = tmp_path / "agent.yaml"
    config.write_text("model_backend: echo\naccess_mode: full-access\n", encoding="utf-8")
    env = {"HOME": str(tmp_path), "MY_AGENT_HOME": str(home), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, "-c", _SCRIPT, str(config), plain, toolkit],
                            capture_output=True, text=True, env=env, timeout=120, check=False)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "THIN-OK" in result.stdout
