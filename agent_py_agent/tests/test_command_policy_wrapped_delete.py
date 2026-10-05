# LLM: 钉住"包一层 shell 的删除命令"被结构化拦截（shellwrap）：sh -c / eval / xargs / find 动作都递归拆包，
#   参数是数据（echo/printf/git -m）不误伤，嵌套删除不放行；去掉递归、深度上限、find 判定或 xargs 拆包时这些用例必须变红。
# 模块用途: 验证 contracts/gates/command_policy 的嵌套 Shell 程序检查、分类升级与四个调用点接线。

from __future__ import annotations

import shlex
import time

import pytest

from agent_py_agent.agent.contracts.gates.command_policy import (
    analyze_command,
    evaluate_command_policy,
)


def _decision(command: str, **kwargs):
    return evaluate_command_policy(command, allow_shell_operators=True, **kwargs)


@pytest.mark.parametrize(
    "command",
    [
        'sh -c "rm -rf x"',
        'bash -c "rm -rf x"',
        'zsh -c "rm -rf x"',
        'dash -c "rm -rf x"',
        'ksh -c "rm -rf x"',
        'sh -lc "rm -rf x"',
        'bash -ec "rm -rf x"',
        'env sh -c "rm -rf x"',
        'cd /tmp && sh -c "rm -rf x"',
        'sh -c \'sh -c "rm -rf x"\'',
        'eval "rm -rf x"',
        "xargs rm -rf",
        "xargs -n1 rm -rf",
        "xargs -I{} rm {}",
        "xargs -a file rm -rf",
        "xargs -J % rm %",
        "xargs --max-args 1 rm -rf",
        "xargs --max-args=1 rm -rf",
        "xargs --null rm -rf x",
        "xargs --no-run-if-empty rm -rf x",
        "xargs --verbose rm -rf x",
        "xargs --eof rm -rf x",
        "xargs --replace rm -rf x",
        "xargs --max-lines rm -rf x",
        "xargs -0n 1 rm -rf x",
        "xargs -rn 1 rm -rf x",
        "xargs -tI {} rm -rf {}",
        "xargs -R 1 rm -rf x",
        "xargs -S 255 rm -rf x",
        "xargs --max-args 1 rm -rf x",
        "xargs -n1 rm -rf x",
        "xargs --null -- rm -rf x",
        "find . -delete",
        'find . -name "*.tmp" -delete',
        "find . -exec rm -rf {} +",
        "find . -execdir rm {} ;",
        'find . -exec sh -c "rm -rf x" {} +',
        r"find . -exec echo {} \; -exec rm {} \;",
        r"find . -exec echo {} \; -exec rm {} +",
        "find . -exec echo '{}' ';' -exec rm '{}' ';'",
        "find . -exec echo {} + -exec rm {} ;",
        "timeout 5 stdbuf -o0 xargs --null rm -rf x",
        "stdbuf -o0 sh -c 'rm -rf x'",
    ],
)
def test_wrapped_delete_is_blocked(command: str) -> None:
    decision = _decision(command)

    assert decision.allowed is False
    assert decision.finding_codes == ("COMMAND_DESTRUCTIVE_DELETE_BLOCKED",)
    assert decision.findings[0].evidence["nested_depth"] >= 1


@pytest.mark.parametrize(
    "command",
    [
        "sudo rm -rf /tmp/x",
        "sudo -u root rm -rf /tmp/x",
        "env rm -rf /tmp/x",
        "env FOO=1 rm -rf /tmp/x",
        "nice rm -rf /tmp/x",
        "nice -n 10 rm -rf /tmp/x",
        "timeout 5 rm -rf /tmp/x",
        "nohup rm -rf /tmp/x",
        "time rm -rf /tmp/x",
        "command rm -rf /tmp/x",
        "exec rm -rf /tmp/x",
        "builtin rm -rf /tmp/x",
        "stdbuf -o0 rm -rf /tmp/x",
        "stdbuf -oL rm -rf /tmp/x",
        "stdbuf --output=L rm -rf /tmp/x",
        "ionice -c 3 rm -rf /tmp/x",
        "chrt -f 50 rm -rf /tmp/x",
        "taskset -c 0 rm -rf /tmp/x",
        "setsid rm -rf /tmp/x",
        "flock /tmp/l rm -rf /tmp/x",
        "caffeinate rm -rf /tmp/x",
        "unbuffer rm -rf /tmp/x",
        "sudo nice -n 5 rm -rf /tmp/x",
    ],
)
def test_prefix_runner_wrapped_delete_is_blocked(command: str) -> None:
    """前缀运行器（sudo/env/nice/...）解包到内层 rm 后由顶层路径拦下：finding 带 executable 证据，
    不是嵌套程序路径，所以不带 nested_depth（那是 sh -c/xargs/find 拆包才有的标记）。"""
    decision = _decision(command)

    assert decision.allowed is False
    assert decision.finding_codes == ("COMMAND_DESTRUCTIVE_DELETE_BLOCKED",)
    assert decision.findings[0].evidence["executable"] == "rm"


def test_wrapped_rm_of_protected_target_keeps_pattern_finding() -> None:
    decision = _decision('sh -c "rm -rf /"')

    assert decision.allowed is False
    assert "COMMAND_DANGEROUS_PATTERN_BLOCKED" in decision.finding_codes
    assert any(
        finding.evidence.get("pattern") == "RM_PROTECTED_TARGET"
        for finding in decision.findings
    )


def test_xargs_unknown_long_option_is_blocked_and_recoverable() -> None:
    """GNU 长选项缩写不在已知表里：从严给 unknown finding（可能吃下一个词），并给可恢复写法建议。"""
    decision = _decision("xargs --nu rm -rf x")

    assert decision.allowed is False
    assert "COMMAND_XARGS_UNKNOWN_OPTION" in decision.finding_codes
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.finding_codes
    unknown = next(
        finding for finding in decision.findings if finding.code == "COMMAND_XARGS_UNKNOWN_OPTION"
    )
    assert unknown.evidence.get("option") == "--nu"
    assert "recovery" in unknown.evidence


def test_xargs_unknown_short_letter_is_blocked() -> None:
    """未知短选项字母同样从严：宿主无法确认它是否吃下一个词。"""
    decision = _decision("xargs -z rm -rf x")

    assert decision.allowed is False
    assert "COMMAND_XARGS_UNKNOWN_OPTION" in decision.finding_codes


def test_wrapper_unknown_option_is_blocked_and_recoverable() -> None:
    """前缀运行器的表外选项从严：按“不吃值”继续检查（后面的命令仍在扫描范围内），
    内层 rm 照样被拦，并给可恢复写法建议。"""
    decision = _decision("sudo --weird rm -rf /tmp/x")

    assert decision.allowed is False
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.finding_codes
    assert "COMMAND_WRAPPER_UNKNOWN_OPTION" in decision.finding_codes
    unknown = next(
        finding
        for finding in decision.findings
        if finding.code == "COMMAND_WRAPPER_UNKNOWN_OPTION"
    )
    assert unknown.evidence.get("option") == "--weird"
    assert "recovery" in unknown.evidence


@pytest.mark.parametrize(
    "command",
    [
        "env -- FOO=1 rm -rf /tmp/x",
        'sh -c "env -- FOO=1 rm -rf /tmp/x"',
    ],
)
def test_env_assignments_after_option_terminator_still_block_delete(command: str) -> None:
    decision = _decision(command)

    assert decision.allowed is False
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.finding_codes


@pytest.mark.parametrize(
    "command",
    [
        "env -- FOO=1 ls",
        'sh -c "env -- FOO=1 ls"',
    ],
)
def test_env_assignments_after_option_terminator_keep_safe_command(command: str) -> None:
    assert _decision(command).allowed is True


@pytest.mark.parametrize(
    "command",
    [
        'env -S "rm -rf /tmp/x"',
        'env --split-string="rm -rf /tmp/x"',
        'env --split-string "rm -rf /tmp/x"',
        "flock /tmp/l -c 'rm -rf /tmp/x'",
        "flock /tmp/l --command='rm -rf /tmp/x'",
        "flock /tmp/l --command 'rm -rf /tmp/x'",
        'sh -c "env -S \'rm -rf /tmp/x\'"',
        'sh -c "flock /tmp/l -c \'rm -rf /tmp/x\'"',
    ],
)
def test_command_string_wrapper_options_block_delete(command: str) -> None:
    decision = _decision(command)

    assert decision.allowed is False
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.finding_codes


@pytest.mark.parametrize(
    "command",
    [
        'env -S "echo \'rm -rf /tmp/x\'"',
        'env --split-string="echo \'rm -rf /tmp/x\'"',
        'flock /tmp/l -c "echo \'rm -rf /tmp/x\'"',
        'flock /tmp/l --command="echo \'rm -rf /tmp/x\'"',
    ],
)
def test_command_string_wrapper_options_preserve_quoted_data(command: str) -> None:
    assert _decision(command).allowed is True


def test_command_string_wrapper_options_use_existing_nested_depth_limit() -> None:
    source = "rm -rf x"
    for _ in range(4):
        source = f"env -S {shlex.quote(source)}"

    within_limit = _decision(source)
    assert within_limit.allowed is False
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in within_limit.finding_codes
    assert "COMMAND_NESTED_DEPTH_EXCEEDED" not in within_limit.finding_codes

    source = f"env -S {shlex.quote(source)}"
    beyond_limit = _decision(source)
    assert beyond_limit.allowed is False
    assert "COMMAND_NESTED_DEPTH_EXCEEDED" in beyond_limit.finding_codes


@pytest.mark.parametrize(
    "command",
    [
        "sudo --weird=foo ls",
        'sh -c "sudo --weird=foo ls"',
        'sh -c "sudo --weird ls"',
    ],
)
def test_wrapper_unknown_options_with_equals_or_nested_are_blocked(command: str) -> None:
    decision = _decision(command)

    assert decision.allowed is False
    assert "COMMAND_WRAPPER_UNKNOWN_OPTION" in decision.finding_codes
    unknown = next(
        finding
        for finding in decision.findings
        if finding.code == "COMMAND_WRAPPER_UNKNOWN_OPTION"
    )
    assert unknown.evidence.get("recovery") == "remove the option or put -- before the command"


@pytest.mark.parametrize(
    "command",
    [
        "env --unset=FOO ls",
        "timeout --signal=TERM 5 ls",
        "sudo -u nobody ls",
        "caffeinate -m make",
    ],
)
def test_known_equals_options_and_caffeinate_m_remain_allowed(command: str) -> None:
    assert _decision(command).allowed is True


def test_caffeinate_m_still_blocks_wrapped_delete() -> None:
    decision = _decision("caffeinate -m rm -rf /tmp/x")

    assert decision.allowed is False
    assert decision.finding_codes == ("COMMAND_DESTRUCTIVE_DELETE_BLOCKED",)


def test_find_semicolon_followed_by_shell_command_is_blocked() -> None:
    """`;` 之后是另一个 shell 命令时也要拦：find 段扫描跨段、rm 由自己的命令位拦。"""
    decision = _decision(r"find . -name x \; rm -rf y")

    assert decision.allowed is False
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.finding_codes


@pytest.mark.parametrize(
    "command",
    [
        'echo "rm -rf x"',
        "printf '%s' 'rm -rf x' > f",
        'grep "rm -rf" f',
        'git commit -m "rm -rf old"',
        "python3 -c \"print('rm -rf')\"",
        'sh -c "ls"',
        'bash -lc "echo rm"',
        'env FOO=1 sh -c "printf ok"',
        "xargs grep",
        r"find . -exec grep {} \;",
        "find . -name x -print",
        "xargs grep foo",
        "xargs -n 1 grep foo",
        "xargs --max-args=1 echo hi",
        "xargs -0 -I {} cp {} /tmp/out",
        "xargs --null grep foo",
        "xargs -tn 2 echo",
        "timeout 60 python3 x.py",
        "nice -n 10 make",
        "env FOO=1 pytest -q",
        "stdbuf -oL tail -f log",
        "sudo -u root ls",
        "flock /tmp/l echo hi",
        "caffeinate -t 60 make",
    ],
)
def test_data_arguments_are_not_treated_as_programs(command: str) -> None:
    decision = _decision(command)

    assert decision.allowed is True
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" not in decision.finding_codes


def test_python_dash_c_stays_mutating_without_delete_finding() -> None:
    analysis = analyze_command("python3 -c \"print('rm -rf')\"")

    assert analysis.classification == "mutating"
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" not in analysis.reason_codes


def test_wrapped_read_only_stays_unknown() -> None:
    assert analyze_command('bash -lc "ls"').classification == "unknown"
    assert analyze_command('sh -c "cat f"').classification == "unknown"


def test_find_with_write_actions_is_not_read_only() -> None:
    assert analyze_command("find . -exec ls {} +").classification == "mutating"
    assert analyze_command("find . -fprint out.txt").classification == "mutating"
    assert analyze_command("find . -name x").classification == "read_only"


def test_allowed_commands_allow_nested_delete() -> None:
    decision = _decision('sh -c "rm x"', allowed_commands=["rm"])

    assert decision.allowed is True


def test_allowed_commands_do_not_allow_nested_protected_target() -> None:
    decision = _decision('sh -c "rm -rf /"', allowed_commands=["rm"])

    assert decision.allowed is False
    assert "COMMAND_DANGEROUS_PATTERN_BLOCKED" in decision.finding_codes


def test_command_string_values_respect_allowed_commands() -> None:
    """命令字符串值的递归沿用同一 allowlist：ds5 初审变异把递归处的 allowed_commands 丢弃为
    frozenset() 时无用例变红（MUT-D SURVIVED），这里钉住 allowlist 在 command_options 路径的传递。"""
    assert _decision('env -S "rm x"', allowed_commands=["rm"]).allowed is True
    assert _decision("flock /tmp/l -c 'rm x'", allowed_commands=["rm"]).allowed is True
    assert _decision('env --split-string="rm x"', allowed_commands=["rm"]).allowed is True


@pytest.mark.parametrize(
    "command",
    [
        "env MY_VAR=1 rm -rf /tmp/x",
        "env -- MY_VAR=1 rm -rf /tmp/x",
        'sh -c "env MY_VAR=1 rm -rf /tmp/x"',
    ],
)
def test_underscore_assignments_still_block_delete(command: str) -> None:
    """下划线变量名（主流写法）同样被赋值扫描跳过：ds5 初审变异去掉 looks_like_assignment 的
    下划线支持时无用例变红（MUT-E SURVIVED），这里钉住三条路径（直接、`--` 之后、嵌套）。"""
    decision = _decision(command)

    assert decision.allowed is False
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.finding_codes


def test_two_layer_wrapping_is_still_blocked() -> None:
    decision = _decision('sh -c \'bash -c "rm -rf x"\'')

    assert decision.allowed is False
    assert decision.findings[0].evidence["nested_depth"] >= 2


def test_depth_limit_boundary_allows_four_layers() -> None:
    source = "rm -rf x"
    for _ in range(4):
        source = f"sh -c {shlex.quote(source)}"

    decision = _decision(source)

    assert decision.allowed is False
    assert "COMMAND_NESTED_DEPTH_EXCEEDED" not in decision.finding_codes
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.finding_codes


def test_depth_limit_is_fail_closed() -> None:
    source = "rm -rf x"
    for _ in range(8):
        source = f"sh -c {shlex.quote(source)}"

    decision = _decision(source)

    assert decision.allowed is False
    assert "COMMAND_NESTED_DEPTH_EXCEEDED" in decision.finding_codes


def test_oversized_nested_source_is_fail_closed() -> None:
    command = f"sh -c {shlex.quote('echo ' + 'a' * 40_000)}"

    decision = _decision(command)

    assert decision.allowed is False
    assert "COMMAND_NESTED_SOURCE_TOO_LARGE" in decision.finding_codes


def test_long_wrapped_input_is_fast() -> None:
    command = f"sh -c {shlex.quote('echo ' + 'a' * 10_000)}"

    started = time.monotonic()
    decision = _decision(command)
    elapsed = time.monotonic() - started

    assert decision.allowed is True
    assert elapsed < 0.1


class TestCallSiteWiring:
    def test_shell_tool_blocks_wrapped_delete(self, tmp_path) -> None:
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        tool = ShellTool(tmp_path, options=ShellToolOptions(default_timeout=10))
        outcome = tool.execute({"command": 'sh -c "rm -rf x"'})

        assert outcome.ok is False
        assert outcome.error_code == "COMMAND_POLICY_BLOCKED"

    def test_pty_tool_blocks_wrapped_delete(self, tmp_path) -> None:
        from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        tool = TerminalSessionTool(ShellTool(tmp_path, options=ShellToolOptions(default_timeout=10)))
        outcome = tool._start({"command": 'sh -c "rm -rf x"'})

        assert outcome.ok is False
        assert outcome.error_code == "COMMAND_POLICY_BLOCKED"

    def test_shell_gateway_blocks_wrapped_delete(self, tmp_path) -> None:
        from agent_py_agent.agent.subagents.shell_gateway import (
            ShellGatewayRequest,
            plan_shell_command,
        )

        decision = plan_shell_command(
            ShellGatewayRequest(
                command='sh -c "rm -rf x"',
                workspace_root=tmp_path,
                command_allowlist=["sh"],
            )
        )

        assert decision.allowed is False
        assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.blockers

    def test_path_url_command_gate_blocks_wrapped_delete(self) -> None:
        from agent_py_agent.agent.contracts.gates.path_url_command import _command_findings

        findings = _command_findings("command", 'sh -c "rm -rf x"', True)

        assert [finding.code for finding in findings] == ["COMMAND_DESTRUCTIVE_DELETE_BLOCKED"]
