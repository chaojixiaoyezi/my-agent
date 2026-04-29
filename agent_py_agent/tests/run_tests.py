"""仓库内置的本地冒烟测试入口。"""

from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
RUN_ENV = os.environ.copy()
RUN_ENV.setdefault("PYTHONUTF8", "1")
RUN_ENV.setdefault("PYTHONIOENCODING", "utf-8")


def configure_stdio() -> None:
    """让测试脚本自己的输出也固定为 UTF-8。"""

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def run_capture(cmd, **kwargs):
    """用 UTF-8 捕获子进程输出，避免 Windows 默认 GBK 解码中文失败。"""

    return subprocess.run(
        cmd,
        cwd=PROJECT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=RUN_ENV,
        **kwargs,
    )


configure_stdio()


def run(cmd):
    print("$", " ".join(cmd))
    completed = run_capture(cmd)
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
run([sys.executable, "-m", "agent_py_agent", "subagents-probe", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagents-due-check", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagents-plan-actions", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagents-apply-actions", "--dry-run", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagents-route-capabilities", "--dry-run", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagents-acceptance", "--help"])
run([sys.executable, "-m", "agent_py_agent", "subagent-context", "--help"])
run([sys.executable, "-m", "agent_py_agent", "subagent-run", "--help"])

discovered_tests = run_capture(
    [
        sys.executable,
        "-c",
        (
            "from pathlib import Path\n"
            "import importlib\n"
            "import inspect\n"
            "\n"
            "total = 0\n"
            "for path in sorted(Path('agent_py_agent/tests').glob('test_*.py')):\n"
            "    module_name = f'agent_py_agent.tests.{path.stem}'\n"
            "    module = importlib.import_module(module_name)\n"
            "    tests = [\n"
            "        (name, func)\n"
            "        for name, func in inspect.getmembers(module, inspect.isfunction)\n"
            "        if name.startswith('test_') and func.__module__ == module.__name__\n"
            "    ]\n"
            "    for name, func in sorted(tests):\n"
            "        print(f'RUN {module_name}.{name}')\n"
            "        func()\n"
            "        total += 1\n"
            "print(f'ALL_DISCOVERED_TESTS_PASS total={total}')\n"
        ),
    ],
)
print(discovered_tests.stdout)
if discovered_tests.stderr:
    print(discovered_tests.stderr)
assert discovered_tests.returncode == 0
assert "ALL_DISCOVERED_TESTS_PASS" in discovered_tests.stdout

bad = run_capture(
    [sys.executable, "-m", "agent_py_agent", "unknown-command"],
)
print("bad-command-returncode", bad.returncode)
assert bad.returncode != 0

chat_input = (
    "/help\n"
    "/remember 循环测试记忆\n"
    "/memory 循环\n"
    "/btw 回答要短\n"
    "/subagents 1 循环智能体测试\n"
    "你好\n"
    "logout\n"
)
chat = run_capture(
    [sys.executable, "-m", "agent_py_agent", "chat", "--no-save"],
    input=chat_input,
    timeout=30,
)
print(chat.stdout)
if chat.stderr:
    print(chat.stderr)
assert chat.returncode == 0
assert "交互循环已启动" in chat.stdout
assert "已记忆" in chat.stdout
assert "已发送到后台" in chat.stdout
assert "subagent-" in chat.stdout

print("ALL_TESTS_PASS")
