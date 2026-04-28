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
run([sys.executable, "-m", "agent_py_agent", "subagents-probe", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagents-due-check", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagents-plan-actions", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagents-apply-actions", "--dry-run", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagents-route-capabilities", "--dry-run", "--limit", "2"])
run([sys.executable, "-m", "agent_py_agent", "subagent-context", "--help"])
run([sys.executable, "-m", "agent_py_agent", "subagent-run", "--help"])

agent_loop = subprocess.run(
    [
        sys.executable,
        "-c",
        (
            "from agent_py_agent.tests.test_agent import "
            "test_subagents, test_subagent_capability_records, "
            "test_subagent_fake_done_requires_evidence, "
            "test_subagent_work_order_validation, "
            "test_subagent_takeover_records_locked_files, "
            "test_subagent_board_scales_and_flags, "
            "test_subagent_due_check_report, "
            "test_subagent_channel_probe_records_status, "
            "test_subagent_channel_probe_report, "
            "test_subagent_action_plan_dry_run, "
            "test_subagent_action_apply_dry_run_and_apply, "
            "test_subagent_action_apply_repairs_work_order, "
            "test_subagent_capability_route_grants_tool, "
            "test_subagent_capability_route_grants_skill, "
            "test_subagent_capability_route_creates_gap_when_no_match, "
            "test_subagent_execution_context_uses_only_grants, "
            "test_subagent_runner_dry_run_and_execute; "
            "test_subagents(); "
            "test_subagent_capability_records(); "
            "test_subagent_fake_done_requires_evidence(); "
            "test_subagent_work_order_validation(); "
            "test_subagent_takeover_records_locked_files(); "
            "test_subagent_board_scales_and_flags(); "
            "test_subagent_due_check_report(); "
            "test_subagent_channel_probe_records_status(); "
            "test_subagent_channel_probe_report(); "
            "test_subagent_action_plan_dry_run(); "
            "test_subagent_action_apply_dry_run_and_apply(); "
            "test_subagent_action_apply_repairs_work_order(); "
            "test_subagent_capability_route_grants_tool(); "
            "test_subagent_capability_route_grants_skill(); "
            "test_subagent_capability_route_creates_gap_when_no_match(); "
            "test_subagent_execution_context_uses_only_grants(); "
            "test_subagent_runner_dry_run_and_execute(); "
            "print('AGENT_TEST_PASS')"
        ),
    ],
    cwd=PROJECT,
    text=True,
    capture_output=True,
)
print(agent_loop.stdout)
if agent_loop.stderr:
    print(agent_loop.stderr)
assert agent_loop.returncode == 0
assert "AGENT_TEST_PASS" in agent_loop.stdout

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
    "/btw 回答要短\n"
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
assert "已发送到后台" in chat.stdout
assert "subagent-" in chat.stdout

tool_loop = subprocess.run(
    [
        sys.executable,
        "-c",
        (
            "from agent_py_agent.tests.test_tools import "
            "test_tool_loop_and_prompt_transcript, "
            "test_tool_catalog_and_recommended_sections, "
            "test_tool_allowlist_limits_prompt_and_execution, "
            "test_write_and_append_file_tools, "
            "test_replace_in_file_tool, "
            "test_fetch_url_and_http_request_tools; "
            "test_tool_loop_and_prompt_transcript(); "
            "test_tool_catalog_and_recommended_sections(); "
            "test_tool_allowlist_limits_prompt_and_execution(); "
            "test_write_and_append_file_tools(); "
            "test_replace_in_file_tool(); "
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

capability_loop = subprocess.run(
    [
        sys.executable,
        "-c",
        (
            "from agent_py_agent.tests.test_capabilities import "
            "test_skill_card_parsing, "
            "test_skill_registry_and_capability_router, "
            "test_tool_specs_become_capability_cards, "
            "test_zero_limit_means_unlimited; "
            "test_skill_card_parsing(); "
            "test_skill_registry_and_capability_router(); "
            "test_tool_specs_become_capability_cards(); "
            "test_zero_limit_means_unlimited(); "
            "print('CAPABILITY_TEST_PASS')"
        ),
    ],
    cwd=PROJECT,
    text=True,
    capture_output=True,
)
print(capability_loop.stdout)
if capability_loop.stderr:
    print(capability_loop.stderr)
assert capability_loop.returncode == 0
assert "CAPABILITY_TEST_PASS" in capability_loop.stdout

print("ALL_TESTS_PASS")
