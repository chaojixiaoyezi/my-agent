"""仓库内置的本地冒烟测试入口。"""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent


def run(cmd):
    print("$", " ".join(cmd))
    completed = subprocess.run(cmd, cwd=PROJECT, text=True, capture_output=True)
    print(completed.stdout)
    if completed.stderr:
        print(completed.stderr)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


run(
    [
        sys.executable,
        "-m",
        "py_compile",
        *[str(path) for path in (ROOT / "agent").glob("*.py")],
        str(ROOT / "__main__.py"),
    ]
)
run([sys.executable, "-m", "agent_py_agent", "--help"])
run([sys.executable, "-m", "agent_py_agent", "run", "测试动态 prompt", "--inject", "请用三点回答", "--no-save"])
run([sys.executable, "-m", "agent_py_agent", "remember", "我喜欢清晰的表格", "--kind", "preference"])
run([sys.executable, "-m", "agent_py_agent", "memory-search", "表格"])
run([sys.executable, "-m", "agent_py_agent", "spawn-subagents", "开发 CLI 智能体", "--count", "2"])

bad = subprocess.run(
    [sys.executable, "-m", "agent_py_agent", "unknown-command"],
    cwd=PROJECT,
    text=True,
    capture_output=True,
)
print("bad-command-returncode", bad.returncode)
assert bad.returncode != 0

chat_input = (
    "/help\n"
    "/remember 循环测试记忆\n"
    "/memory 循环\n"
    "/inject 回答要短\n"
    "/subagents 1 循环智能体测试\n"
    "你好\n"
    "logout\n"
)
chat = subprocess.run(
    [sys.executable, "-m", "agent_py_agent", "chat", "--no-save"],
    cwd=PROJECT,
    input=chat_input,
    text=True,
    capture_output=True,
    timeout=30,
)
print(chat.stdout)
if chat.stderr:
    print(chat.stderr)
assert chat.returncode == 0
assert "交互循环已启动" in chat.stdout
assert "已记忆" in chat.stdout
assert "正在等待模型响应" in chat.stdout
assert "subagent-" in chat.stdout

tool_loop = subprocess.run(
    [
        sys.executable,
        "-c",
        (
            "from agent_py_agent.tests.test_tools import "
            "test_tool_loop_and_prompt_transcript, "
            "test_write_and_append_file_tools, "
            "test_fetch_url_and_http_request_tools; "
            "test_tool_loop_and_prompt_transcript(); "
            "test_write_and_append_file_tools(); "
            "test_fetch_url_and_http_request_tools(); "
            "print('TOOL_TEST_PASS')"
        ),
    ],
    cwd=PROJECT,
    text=True,
    capture_output=True,
)
print(tool_loop.stdout)
if tool_loop.stderr:
    print(tool_loop.stderr)
assert tool_loop.returncode == 0
assert "TOOL_TEST_PASS" in tool_loop.stdout

print("ALL_TESTS_PASS")
