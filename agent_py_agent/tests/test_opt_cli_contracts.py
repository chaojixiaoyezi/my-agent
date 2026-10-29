# LLM: 回归只构建解析树或调用纯命令边界，不创建 Agent、不启停真实服务。
# 模块用途: 验证 e905 规范命令树、历史输入解码和退出命令消歧。
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent_py_agent.cli.argv_compat_v1 import CLI_V1_PATHS, decode_v1_argv
from agent_py_agent.cli.parser import build_parser

ROOTS = {"adapter", "admin", "audit", "background", "chat", "collaboration", "config", "context", "dev", "feishu", "gateway", "guidance", "home", "local-store", "memory", "resume", "run", "runtime", "skills", "status", "subagents", "task", "timeline", "update"}


# LLM: 直接读取真实 argparse 注册结果，不能用隐藏帮助或虚构清单替代删除。
# 函数用途: 保证内置根只剩实施包指定的二十四项。
def test_cli_has_only_canonical_roots():
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == ROOTS


# LLM: 只解析参数，func 的身份是原业务入口，不运行或模拟后台进程。
# 函数用途: 冻结持久化的守护进程命令与参数语义。
@pytest.mark.parametrize("argv,handler", [
    (["gateway", "supervisor", "--heartbeat-timeout", "42"], "cmd_supervisor_run"),
    (["gateway", "run", "--after-pid", "7"], "cmd_gateway_run"),
    (["gateway", "maintenance-lock", "--ttl", "10"], "cmd_maintenance_lock"),
    (["gateway", "maintenance-unlock"], "cmd_maintenance_unlock"),
    (["adapter", "start", "--daemon"], "cmd_adapter_start"),
])
def test_cli_persisted_daemon_contract(argv, handler):
    assert build_parser().parse_args(argv).func.__name__ == handler


# LLM: 新路径必须进入同一实际处理器，不能重新拼旧命令调用第二解析器。
# 函数用途: 验证按领域收敛后的代表性规范命令可解析。
@pytest.mark.parametrize("argv,handler", [
    (["memory", "route", "query"], "cmd_memory_route"),
    (["home", "status"], "cmd_home_status"),
    (["task", "show", "task-1"], "cmd_task_show"),
    (["subagents", "list"], "cmd_subagents"),
    (["context", "bundle", "latest"], "cmd_context_bundle"),
])
def test_cli_canonical_routes(argv, handler):
    assert build_parser().parse_args(argv).func.__name__ == handler


# LLM: 撤销管理员认证与退出界面是不同权限动作；这里拒绝的词不能触发任一动作。
# 函数用途: 保证裸 logout 和 /logout 不再关闭界面。
@pytest.mark.parametrize("value", ["logout", "/logout", "LOGOUT", "/LOGOUT"])
def test_cli_logout_is_not_exit(value):
    from agent_py_agent.cli.chat_parts.slash_commands import is_exit_command
    assert not is_exit_command(value)


# LLM: 保留明确退出词和已承诺的 quit 别名；不能用消歧顺手删除有效功能。
# 函数用途: 保证正常退出输入仍然有效。
@pytest.mark.parametrize("value", ["exit", "/exit", "/quit", "退出"])
def test_cli_explicit_exit_still_works(value):
    from agent_py_agent.cli.chat_parts.slash_commands import is_exit_command
    assert is_exit_command(value)


# LLM: 规范分组帮助必须可发现其子命令，不能被旧裸命令的默认list行为抢走。
# 函数用途: 重现独审发现的help兼容冲突。
@pytest.mark.parametrize("argv,expected", [
    (["subagents", "--help"], "route-capabilities"),
    (["skills", "learned", "proposals", "--help"], "confirm"),
])
def test_cli_canonical_group_help_precedes_legacy_default(argv, expected, capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 0
    assert expected in capsys.readouterr().out


# LLM: 真正注册的规范扩展路径优先于旧输入前缀，不能靠往历史映射表补新命令维持可达性。
# 函数用途: 验证新规范子命令不会被转换成旧list参数。
def test_cli_canonical_extension_is_not_rewritten():
    parser = build_parser()
    root = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    root.choices["subagents"].add_subparsers().add_parser("stats").set_defaults(extension_marker=True)
    assert parser.parse_args(["subagents", "stats"]).extension_marker is True


# LLM: 第三方扩展可注册独立根，但别名不能占用任何规范根或历史路由名。
# 函数用途: 验证别名污染规范命名空间会在注册前被拒绝。
def test_cli_registrar_rejects_reserved_root_alias():
    from agent_py_agent.cli.argv_compat_v1 import CanonicalArgumentParser, CanonicalCommandRegistrar
    parser = CanonicalArgumentParser()
    registrar = CanonicalCommandRegistrar(parser)
    with pytest.raises(argparse.ArgumentError):
        registrar.add_parser("gateway", aliases=["memory"])
    assert not registrar.choices


# LLM: 原扩展接口允许额外根；24是默认内置根合同，不把修复碰撞变成删除有效扩展。
# 函数用途: 保留不冲突的第三方CLI命令。
def test_cli_noncolliding_extension_root_is_preserved():
    # LLM: 不接触真实扩展或宿主状态，只提供原登记接口的测试实现。
    # 类用途: 向当前解析树加入无冲突的示例根。
    class Registry:
        # LLM: 测试只添加解析入口，不执行第三方处理器或加载配置。
        # 函数用途: 模拟原扩展登记接口。
        def register_cli_commands(self, sub):
            sub.add_parser("extension-status").set_defaults(extension_marker=True)
    assert build_parser(Registry()).parse_args(["extension-status"]).extension_marker is True


# LLM: 迁移表逐项与独立基线manifest对账，不用同一产品常量证明自己的完整性。
# 函数用途: 固定原144条v1路径及其规范目标。
def test_cli_v1_mapping_matches_independent_baseline_manifest():
    manifest = json.loads((Path(__file__).resolve().parents[2] / "scripts/optimization_removal_manifest.json").read_text())
    expected = {tuple(k.split()): tuple(v.split()) for k, v in manifest["cli_mapping"].items()}
    assert len(expected) == 144
    assert expected == CLI_V1_PATHS


# LLM: 仅匹配命令前缀，后续任意业务词保持原字节；v1 Doctor只附加显式版本投影标记。
# 函数用途: 遍历全部144个旧输入的纯转换合同。
@pytest.mark.parametrize("old,target", list(CLI_V1_PATHS.items()))
def test_cli_all_v1_inputs_decode_without_touching_payload(old, target):
    marker = "memory-doctor payload value"
    expected = [*target, marker]
    if old == ("memory-doctor",):
        expected += ["--compat-view", "v1"]
    assert decode_v1_argv([*old, marker], {}) == expected


# LLM: 配置值、工作目录和业务正文即使等于旧命令名也不是命令入口。
# 函数用途: 防止全局选项及其值被兼容器误写。
def test_cli_decoder_preserves_option_values_and_separator():
    argv = ["--config", "memory-doctor", "--workspace", "memory-resume", "memory-list", "memory-doctor"]
    assert decode_v1_argv(argv, {"--config": True, "--workspace": True}) == [*argv[:4], "memory", "list", "memory-doctor"]
    assert decode_v1_argv(["--", "memory-doctor"], {}) == ["--", "memory-doctor"]


# LLM: 目前根选项都是零/单值，未来多值扩展须先实现明确解码，不能静默改写第二个值。
# 函数用途: 固定未支持消费规则的fail-closed边界。
def test_cli_decoder_rejects_unsupported_global_nargs():
    from agent_py_agent.cli.argv_compat_v1 import CanonicalArgumentParser
    parser = CanonicalArgumentParser()
    parser.legacy_input_boundary = True
    parser.add_argument("--pair", nargs=2)
    with pytest.raises(ValueError, match="多值选项"):
        parser.parse_args(["--pair", "x", "memory-doctor"])
