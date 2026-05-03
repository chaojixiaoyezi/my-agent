"""仓库内置的本地冒烟测试入口。"""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
RUN_ENV = os.environ.copy()
RUN_ENV.setdefault("PYTHONUTF8", "1")
RUN_ENV.setdefault("PYTHONIOENCODING", "utf-8")
TEST_TMP_HANDLE = tempfile.TemporaryDirectory(prefix="agent-full-smoke-")
TEST_TMP = Path(TEST_TMP_HANDLE.name)
TEST_MEMORY = TEST_TMP / "memory.jsonl"
TEST_LOCAL_STORE = TEST_TMP / "local_store"
TEST_SUBAGENTS = TEST_TMP / "subagents"
TEST_GATEWAY = TEST_TMP / "gateway"
TEST_CONFIG = TEST_TMP / "agent_config.yaml"
_memory_path = str(TEST_MEMORY).replace("\\", "/")
_local_store_db = str(TEST_LOCAL_STORE / "local.db").replace("\\", "/")
_local_store_files = str(TEST_LOCAL_STORE / "files").replace("\\", "/")
_local_store_events = str(TEST_LOCAL_STORE / "events.jsonl").replace("\\", "/")
_subagent_ws = str(TEST_SUBAGENTS).replace("\\", "/")
_gateway_ws = str(TEST_GATEWAY).replace("\\", "/")
TEST_CONFIG.write_text(
    (ROOT / "config" / "agent_config.yaml").read_text(encoding="utf-8")
    + "\n# full smoke test isolation\n"
    + f'memory_path: "{_memory_path}"\n'
    + f'local_store_path: "{_local_store_db}"\n'
    + f'local_store_files_dir: "{_local_store_files}"\n'
    + f'local_store_events_path: "{_local_store_events}"\n'
    + f'subagent_workspace: "{_subagent_ws}"\n'
    + f'gateway_workspace: "{_gateway_ws}"\n'
    + "daemon_planner: false\n"
    + "daemon_max_runners: 0\n"
    + "daemon_interval: 1\n"
    + "gateway_request_timeout: 180\n"
    + "gateway_request_poll_interval: 1\n",
    encoding="utf-8",
)


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


def agent_cmd(*args):
    return [sys.executable, "-m", "agent_py_agent", "--config", str(TEST_CONFIG), *args]


def create_real_api_subagent_run() -> str:
    """创建真实 API runner E2E 使用的隔离子代理工单。"""

    script = (
        "from pathlib import Path\n"
        "from agent_py_agent.agent.config import load_config\n"
        "from agent_py_agent.agent.core import SimpleAgent\n"
        f"cfg = load_config(r'{TEST_CONFIG}')\n"
        "agent = SimpleAgent(cfg, Path('agent_py_agent').resolve())\n"
        "task = agent.subagents.create_run(\n"
        "    goal='真实 API 子代理 E2E：读取 README.md 并生成验收证据',\n"
        "    thought='验证真实模型、工具调用、结构化输出和父代理验收闭环。',\n"
        "    plan=['读取 README.md', '输出 SUBAGENT_RESULT', '等待验收'],\n"
        "    allowed_tools=['read_file'],\n"
        "    acceptance_checks=['真实 API runner 执行成功', '结构化输出可解析', '父代理验收通过'],\n"
        ")\n"
        "print(task.id)\n"
    )
    completed = run_capture([sys.executable, "-c", script])
    print(completed.stdout)
    if completed.stderr:
        print(completed.stderr)
    assert completed.returncode == 0
    run_id = completed.stdout.strip().splitlines()[-1]
    assert run_id.startswith("subagent-")
    return run_id


def assert_real_api_subagent_e2e(run_id: str) -> None:
    """确认真实 API runner 和验收状态都已闭环。"""

    run_dir = TEST_SUBAGENTS / run_id
    runner = json.loads((run_dir / "reports" / "runner_result.json").read_text(encoding="utf-8"))
    assert runner["ok"] is True
    assert runner["backend"] != "echo"
    assert runner["structured_output_found"] is True
    assert runner["structured_output_ok"] is True
    assert runner["evidence_count"] >= 1
    assert runner["tool_rounds"] >= 1

    task = json.loads((run_dir / "task.json").read_text(encoding="utf-8"))
    assert task["status"] == "DONE"
    assert task["verification_status"] == "VERIFIED"
    assert (run_dir / "ACCEPTANCE_REVIEW.md").exists()
    assert (run_dir / "reports" / "acceptance_review.json").exists()


print(f"FULL_SMOKE_TEST_WORKSPACE={TEST_TMP}")

run(
    [
        sys.executable,
        "-m",
        "py_compile",
        *[str(path) for path in (ROOT / "agent").glob("*.py")],
        str(ROOT / "__main__.py"),
    ]
)
run(agent_cmd("--help"))
run(agent_cmd("status", "--json"))
run(agent_cmd("run", "测试动态 prompt", "--inject", "请用三点回答", "--no-save"))
run(agent_cmd("remember", "我喜欢清晰的表格", "--kind", "preference"))
run(agent_cmd("memory-search", "表格"))
run(agent_cmd("local-store-status"))
run(agent_cmd("timeline", "--limit", "5"))
run(agent_cmd("local-index-memory"))
run(agent_cmd("local-search", "表格", "--source-type", "memory"))
run(agent_cmd("spawn-subagents", "开发 CLI 智能体", "--count", "2"))
run(agent_cmd("local-search", "开发", "--source-type", "subagent_run"))
run(agent_cmd("subagents-probe", "--limit", "2"))
run(agent_cmd("subagents-due-check", "--limit", "2"))
run(agent_cmd("subagents-plan-actions", "--limit", "2"))
run(agent_cmd("subagents-apply-actions", "--dry-run", "--limit", "2"))
run(agent_cmd("subagents-route-capabilities", "--dry-run", "--limit", "2"))
run(agent_cmd("subagents-acceptance", "--help"))
run(agent_cmd("subagents-patches", "--help"))
run(agent_cmd("subagents-dispatch", "--help"))
run(agent_cmd("subagents-dispatch", "--watch", "--max-cycles", "1", "--interval", "0", "--max-runners", "0"))
run(agent_cmd("subagents-dispatch", "--watch", "--planner", "--max-cycles", "1", "--interval", "0", "--max-runners", "0"))
run(agent_cmd("daemon", "--help"))
run(agent_cmd("daemon", "--max-cycles", "1", "--interval", "0", "--max-runners", "auto", "--no-planner"))
default_chat = run_capture(
    agent_cmd(),
    input="/status\nlogout\n",
    timeout=60,
)
print(default_chat.stdout)
if default_chat.stderr:
    print(default_chat.stderr)
assert default_chat.returncode == 0
assert "当前模式: gateway 客户端" in default_chat.stdout
assert "gateway status=running" in default_chat.stdout
run(agent_cmd("gateway", "stop", "--timeout", "10", "--kill"))
run(agent_cmd("gateway", "--help"))
run(agent_cmd("gateway", "status"))
run(agent_cmd("gateway", "run", "--max-cycles", "1", "--interval", "0", "--max-runners", "0", "--no-planner"))
run(agent_cmd("gateway", "ask", "--help"))
run(agent_cmd("gateway", "result", "--help"))
run(agent_cmd("gateway", "start", "--force"))
try:
    run(
        agent_cmd(
            "gateway",
            "ask",
            "真实 API gateway ask 冒烟：不要调用工具，请只用一句话回答 GATEWAY_OK。",
            "--timeout",
            "180",
            "--no-save",
        )
    )
    run(agent_cmd("local-search", "真实 API gateway ask", "--source-type", "gateway_request"))
    run(agent_cmd("gateway", "status"))
    gateway_chat_input = "/status\n你好\nlogout\n"
    gateway_chat = run_capture(
        agent_cmd("chat", "--gateway", "--no-save", "--gateway-timeout", "180"),
        input=gateway_chat_input,
        timeout=240,
    )
    print(gateway_chat.stdout)
    if gateway_chat.stderr:
        print(gateway_chat.stderr)
    assert gateway_chat.returncode == 0
    assert "当前模式: gateway 客户端" in gateway_chat.stdout
    assert "gateway status=" in gateway_chat.stdout
    assert "gateway_request=" in gateway_chat.stdout
finally:
    run(agent_cmd("gateway", "stop", "--timeout", "10", "--kill"))
run(agent_cmd("subagent-context", "--help"))
run(agent_cmd("subagent-run", "--help"))
run(agent_cmd("scenario-test", "--case", "verification"))
run(agent_cmd("scenario-test", "--case", "gateway-restart"))
run(agent_cmd("scenario-test", "--case", "gateway-cross-day-resume"))
run(agent_cmd("scenario-test", "--case", "gateway-delayed-response"))
run(agent_cmd("scenario-test", "--case", "gateway-multi-worker"))
run(agent_cmd("scenario-test", "--case", "gateway-stale-lease"))
run(agent_cmd("scenario-test", "--case", "parent-subagent-cross-day-resume"))
run(agent_cmd("scenario-test", "--case", "structured-repair"))
run(agent_cmd("scenario-test", "--case", "runner-retry"))
run(agent_cmd("scenario-test", "--count", "1", "--max-runners", "1", "--max-cycles", "2", "--timeout", "180"))

e2e_run_id = create_real_api_subagent_run()
run(
    agent_cmd(
        "subagent-run",
        e2e_run_id,
        "--execute",
        "--instruction",
        (
            "真实 API E2E 测试：必须先调用 read_file，"
            "且工具调用 payload 必须精确使用 {\"tool\":\"read_file\",\"path\":\"README.md\"}；"
            "不要读取 task_dir、Temp 目录、绝对路径或 execution_context 路径。"
            "最终回复必须只包含一个结构化结果块，格式精确为："
            "[SUBAGENT_RESULT]\\n{JSON}\\n[/SUBAGENT_RESULT]。"
            "JSON 必须可被 json.loads 解析，必须包含："
            "\"status\":\"AWAITING_ACCEPTANCE\"，"
            "\"message\":\"已读取 README.md 并生成证据\"，"
            "\"evidence\":[{\"kind\":\"file_read\",\"summary\":\"README.md 已通过 read_file 读取\",\"path\":\"README.md\",\"ok\":true}]，"
            "\"tests\":[{\"name\":\"read_file README.md\",\"command\":\"read_file README.md\",\"ok\":true,\"summary\":\"工具调用成功\"}]，"
            "\"artifacts\":[]，\"patches\":[]。"
            "不要输出 Markdown 代码围栏、解释文字或 DONE。"
        ),
    )
)
run(agent_cmd("subagents-acceptance", "--apply", "--run-id", e2e_run_id, "--reviewer", "full-smoke"))
assert_real_api_subagent_e2e(e2e_run_id)

discovered_tests = run_capture([sys.executable, "-m", "pytest", "agent_py_agent/tests", "-q"])
print(discovered_tests.stdout)
if discovered_tests.stderr:
    print(discovered_tests.stderr)
assert discovered_tests.returncode == 0
assert "passed" in discovered_tests.stdout

bad = run_capture(
    agent_cmd("unknown-command"),
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
    agent_cmd("chat", "--no-save"),
    input=chat_input,
    timeout=30,
)
print(chat.stdout)
if chat.stderr:
    print(chat.stderr)
assert chat.returncode == 0
assert "交互循环已启动" in chat.stdout
assert "已记忆" in chat.stdout
assert "subagent-" in chat.stdout

print("ALL_TESTS_PASS")
