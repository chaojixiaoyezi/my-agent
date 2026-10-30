# LLM: 回归只构建解析树或调用纯命令边界，不创建 Agent、不启停真实服务。
# 模块用途: 验证 e905 规范命令树、历史输入解码和退出命令消歧。
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

import pytest

from agent_py_agent.cli.argv_compat_v1 import (
    CLI_V1_PATHS,
    CanonicalArgumentParser,
    CanonicalCommandRegistrar,
    decode_v1_argv,
)
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


# LLM: 普通argparse消费者只使用规范树；解析后核对原handler和预算，不启动服务。
# 函数用途: 防止局部注册Gateway命令时遗漏规范看门狗动作。
@pytest.mark.parametrize("action,handler", [("start", "cmd_supervisor_start"), ("stop", "cmd_supervisor_stop")])
def test_cli_local_gateway_supervisor(action, handler):
    from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands
    parser = argparse.ArgumentParser()
    add_gateway_subcommands(parser.add_subparsers(dest="command"))
    argv = ["gateway", "supervisor", action]
    if action == "stop":
        argv += ["--timeout", "30.5"]
    args = parser.parse_args(argv)
    assert args.func.__name__ == handler
    if action == "stop":
        assert args.timeout == 30.5


# LLM: 真实根解析器应保留显式目录，规范及跨版本旧入口都必须成立。
# 函数用途: 守住看门狗子解析器不能用默认None覆盖顶层目录的合同。
@pytest.mark.parametrize("action", ["start", "stop", "status", "install-service", "uninstall-service"])
@pytest.mark.parametrize("legacy", [False, True])
def test_cli_supervisor_preserves_explicit_workspace(action, legacy):
    route = ["gateway", "supervisor-" + action] if legacy else ["gateway", "supervisor", action]
    args = build_parser().parse_args(["--dir", "/w", *route])
    assert args.workspace_root == "/w"


# LLM: 扩展只注册独立根；碰撞必须在返回内置parser之前拒绝，不执行任何handler。
# 函数用途: 用真实扩展回调验证退休名、规范名和重复扩展名都不能接管Doctor。
@pytest.mark.parametrize("name", ["memory-doctor", "memory", "dev", "subagent-run", "extension-status"])
def test_cli_extension_cannot_reuse_reserved_parser(name):
    # LLM: 仅模拟扩展注册回调，不加载插件或执行处理器。
    # 类用途: 验证冲突在内置Doctor处理器被返回之前拒绝。
    class Registry:
        # LLM: 捕获真实内置处理器并在拒绝后核对身份；允许先登记一个独立扩展做重名对照。
        # 函数用途: 尝试注册保留名或重复名，确认不会接管内置命令。
        def register_cli_commands(self, sub):
            memory = sub.choices["memory"]
            doctor = memory.add_subparsers().choices["doctor"]
            original = doctor.get_default("func")
            if name == "extension-status":
                sub.add_parser(name)
            with pytest.raises(argparse.ArgumentError):
                sub.add_parser(name).set_defaults(func=lambda args: 99)
            assert doctor.get_default("func") is original
    args = build_parser(Registry()).parse_args(["memory", "doctor"])
    assert args.func.__name__ == "cmd_memory_v2_doctor"


# LLM: 检查实际帮助伪动作而非静态目录，不允许规范中间节点只显示自身英文名字。
# 函数用途: 遍历完整CLI帮助树，防止注册顺序吞掉分组说明。
def test_cli_no_placeholder_group_help():
    pending = [build_parser()]
    bad = []
    while pending:
        parser = pending.pop()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                bad.extend((parser.prog, choice.dest) for choice in action._choices_actions if choice.help == choice.dest)
                pending.extend(action.choices.values())
    assert not bad
    memory = build_parser().add_subparsers().choices["memory"]
    assert memory.description and "统一" in memory.description


# LLM: 复用只能合并帮助元数据，不重建子树或丢失先注册的命令。
# 函数用途: 验证后注册分组的标题、描述和metavar同步到真实帮助组。
def test_cli_reused_subparser_help_metadata():
    from agent_py_agent.cli.argv_compat_v1 import CanonicalArgumentParser
    parser = CanonicalArgumentParser()
    original = parser.add_subparsers()
    original.add_parser("first", help="原动作")
    reused = parser.add_subparsers(title="业务动作", description="动作说明", metavar="动作", help="选择业务动作")
    assert reused is original
    assert "first" in reused.choices
    assert reused.metavar == "动作"
    assert reused.help == "选择业务动作"
    assert "业务动作" in parser.format_help()
    assert "动作说明" in parser.format_help()


# LLM: 冻结真实安装器/后台argv及参数值，含重复run与双值attempt身份；来源独立于产品转换表，只解析不执行。
# 函数用途: 永久守住跨版本升级、回滚和派工消费者，不能随普通v1别名一起删除。
def test_cli_cross_version_frozen_contracts():
    expected = {
        "gateway supervisor-stop": (["gateway", "supervisor-stop"], "cmd_supervisor_stop", {}),
        "gateway supervisor-start": (["gateway", "supervisor-start"], "cmd_supervisor_start", {}),
        "gateway stop": (["gateway", "stop", "--drain", "--stop-background", "--background-timeout", "30"], "cmd_gateway_stop", {"drain": True, "stop_background": True, "background_timeout": 30.0}),
        "gateway start": (["gateway", "start", "--ready-timeout", "60"], "cmd_gateway_start", {"ready_timeout": 60.0}),
        "adapter stop": (["adapter", "stop"], "cmd_adapter_stop", {"timeout": 10.0}),
        "subagents-dispatch": (
            ["--config", "/frozen/config.yaml", "subagents-dispatch", "--workspace-root", "/frozen/workspace",
             "--apply", "--start-runners", "--max-runners", "2", "--limit", "20",
             "--reviewer", "create-subagents-auto-start", "--note", "auto-start after create_subagents launch_id=launch-1",
             "--background-launch-id", "launch-1", "--run-id", "run-1", "--expected-attempt", "run-1", "attempt-1",
             "--run-id", "run-2", "--expected-attempt", "run-2", "attempt-2"],
            "cmd_subagents_dispatch",
            {"config": "/frozen/config.yaml", "workspace_root": "/frozen/workspace", "apply": True, "start_runners": True,
             "max_runners": 2, "limit": 20, "reviewer": "create-subagents-auto-start",
             "note": "auto-start after create_subagents launch_id=launch-1", "background_launch_id": "launch-1",
             "run_id": ["run-1", "run-2"], "expected_attempt": [["run-1", "attempt-1"], ["run-2", "attempt-2"]]},
        ),
    }
    manifest = json.loads((Path(__file__).resolve().parents[2] / "scripts/optimization_removal_manifest.json").read_text())
    frozen = manifest.get("frozen_cli_cross_version", {})
    assert set(frozen) == set(expected)
    parser = build_parser()
    for name, (argv, handler, values) in expected.items():
        assert frozen[name]["argv"] == argv
        assert frozen[name]["handler"] == handler
        assert frozen[name]["expected"] == values
        args = parser.parse_args(argv)
        assert args.func.__name__ == handler
        assert {key: getattr(args, key) for key in values} == values


@pytest.mark.parametrize("entry", [CanonicalArgumentParser.__init__, CanonicalArgumentParser.add_subparsers, CanonicalCommandRegistrar.add_parser])
def test_cli_registration_signatures_are_explicit_and_typed(entry):
    for name, parameter in inspect.signature(entry).parameters.items():
        if name == "self":
            continue
        assert parameter.kind not in {inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL}
        assert parameter.annotation is not inspect.Parameter.empty


def test_cli_typed_constructor_preserves_argparse_options(tmp_path):
    parent = argparse.ArgumentParser(add_help=False, prefix_chars="+")
    parent.add_argument("+shared", default="继承值")
    parent.set_defaults(parent_marker=True)
    parser = CanonicalArgumentParser(
        "选项测试", usage="自定用法", description="第一行\n第二行", epilog="末尾说明",
        parents=[parent], formatter_class=argparse.RawTextHelpFormatter,
        prefix_chars="+", fromfile_prefix_chars="@", argument_default=argparse.SUPPRESS,
        conflict_handler="resolve", add_help=False, allow_abbrev=False, exit_on_error=False,
    )
    assert parser.legacy_input_boundary is False
    assert parser.parse_args([]).parent_marker is True
    assert parser.parse_args([]).shared == "继承值"
    parser.add_argument("+shared", default="覆盖值")
    parser.add_argument("++optional")
    parser.add_argument("++mode", choices=["safe"])
    args = parser.parse_args([])
    assert args.shared == "覆盖值" and not hasattr(args, "optional")
    assert all("++help" not in a.option_strings for a in parser._actions)
    argfile = tmp_path / "argv.txt"
    argfile.write_text("++mode\nsafe\n", encoding="utf-8")
    assert parser.parse_args(["@" + str(argfile)]).mode == "safe"
    with pytest.raises(argparse.ArgumentError):
        parser.parse_args(["++mode", "unsafe"])
    _, remaining = parser.parse_known_args(["++opt", "value"])
    assert remaining == ["++opt", "value"]
    help_text = parser.format_help()
    assert "自定用法" in help_text and "第一行\n第二行" in help_text and "末尾说明" in help_text


@pytest.mark.parametrize("option", ["color", "suggest_on_error"])
@pytest.mark.parametrize("value", [None, False, True])
def test_cli_constructor_versioned_options_use_native_contract(option, value):
    # 不模拟新版本：当前stdlib支持时核实际属性，否则核原生TypeError。
    if option in inspect.signature(argparse.ArgumentParser).parameters:
        parser = CanonicalArgumentParser(**{option: value})
        assert getattr(parser, option) is value
    else:
        with pytest.raises(TypeError, match=option):
            CanonicalArgumentParser(**{option: value})
    assert CanonicalArgumentParser().parse_args([]) == argparse.Namespace()


def test_cli_subparser_explicit_options_and_custom_action():
    class Subcommands(argparse._SubParsersAction):
        pass

    parser = CanonicalArgumentParser(prog="root")
    parser.add_argument("prefix")
    sub = parser.add_subparsers(
        title="领域", description="领域说明", prog="explicit-prefix", parser_class=argparse.ArgumentParser,
        action=Subcommands, dest="operation", required=True, help="动作帮助", metavar="动作",
    )
    child = sub.add_parser("inspect", aliases=["i"], help="检查")
    assert isinstance(sub, Subcommands) and type(child) is argparse.ArgumentParser
    assert child.prog == "explicit-prefix inspect"
    assert parser.parse_args(["p", "i"]).operation == "i"
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["p"])
    assert exc.value.code == 2
    assert all(text in parser.format_help() for text in ["领域", "领域说明", "动作帮助", "检查"])


@pytest.mark.parametrize("metadata", [{}, {"title": None}, {"description": None}, {"title": "", "description": ""}])
def test_cli_subparser_group_omission_matches_stdlib(metadata):
    ordinary = argparse.ArgumentParser(prog="root")
    canonical = CanonicalArgumentParser(prog="root")
    for parser in (ordinary, canonical):
        parser.add_subparsers(**metadata).add_parser("child", help=None)
    assert canonical.format_help() == ordinary.format_help()


def test_cli_reused_subparser_preserves_omitted_and_applies_explicit_falsy_metadata():
    parser = CanonicalArgumentParser(prog="root")
    original = parser.add_subparsers(dest="operation", required=True, metavar="ACTION", help="先前帮助")
    child = original.add_parser("child")
    child.set_defaults(marker="原子树")
    assert parser.add_subparsers() is original
    assert (original.dest, original.required, original.metavar, original.help) == ("operation", True, "ACTION", "先前帮助")
    assert parser.add_subparsers(title="动作组", description="先前说明") is original
    parser.add_subparsers(title=None, description=None, required=False, metavar=None, help=None)
    group = next(g for g in parser._action_groups if original in g._group_actions)
    assert group.title is None and group.description is None
    assert original.required is False and original.metavar is None and original.help is None
    parser.add_subparsers()
    assert original.choices["child"] is child
    assert parser.parse_args(["child"]).marker == "原子树"
    assert parser.parse_args([]).operation is None


def test_cli_registrar_forwards_parser_options_and_alias_help():
    parent = argparse.ArgumentParser(add_help=False, prefix_chars="+")
    parent.add_argument("+parent", default="继承参数")
    parent.set_defaults(parent_marker=True)
    root = CanonicalArgumentParser(prog="root")
    registrar = CanonicalCommandRegistrar(root)
    child = registrar.add_parser(
        "extension", aliases=["ext"], help=None, prog="explicit-child", usage="扩展用法",
        description="扩展\n说明", epilog="扩展结尾", parents=[parent],
        formatter_class=argparse.RawTextHelpFormatter, prefix_chars="+", fromfile_prefix_chars="@",
        argument_default=argparse.SUPPRESS, conflict_handler="resolve", add_help=False,
        allow_abbrev=False, exit_on_error=False,
    )
    assert child is registrar.choices["ext"] and child.prog == "explicit-child"
    args = root.parse_args(["ext", "+parent", "输入值"])
    assert (args.command, args.parent, args.parent_marker) == ("extension", "输入值", True)
    assert (child.prefix_chars, child.fromfile_prefix_chars, child.argument_default) == ("+", "@", argparse.SUPPRESS)
    assert (child.conflict_handler, child.add_help, child.allow_abbrev, child.exit_on_error) == ("resolve", False, False, False)
    assert all(text in child.format_help() for text in ["扩展用法", "扩展\n说明", "扩展结尾"])
    choices = root.add_subparsers()._choices_actions
    assert len(choices) == 1 and choices[0].help is None
    registrar.add_parser("unlisted")
    assert len(choices) == 1  # 未提供help不创建帮助伪动作；显式None则创建。


@pytest.mark.parametrize("metadata", [{}, {"description": None, "epilog": None, "usage": None, "help": None}])
def test_cli_registrar_namespace_merge_only_changes_supplied_metadata(metadata):
    root = CanonicalArgumentParser(prog="root")
    registrar = CanonicalCommandRegistrar(root)
    leaf = registrar.add_parser("memory-list")
    memory = registrar.choices["memory"]
    memory.description, memory.epilog, memory.usage = "已有说明", "已有结尾", "已有用法"
    memory.formatter_class = argparse.RawTextHelpFormatter
    original_handler = memory.get_default("func")
    choice = next(c for c in root.add_subparsers()._choices_actions if c.dest == "memory")
    old_help = choice.help
    assert registrar.add_parser("memory", **metadata) is memory
    if metadata:
        assert memory.description is None and memory.epilog is None and memory.usage is None
        assert choice.help is None
    else:
        assert (memory.description, memory.epilog, memory.usage, choice.help) == ("已有说明", "已有结尾", "已有用法", old_help)
    assert memory.formatter_class is argparse.RawTextHelpFormatter
    assert memory.get_default("func") is original_handler
    assert memory.add_subparsers().choices["list"] is leaf


@pytest.mark.parametrize("option", ["color", "suggest_on_error", "deprecated"])
@pytest.mark.parametrize("value", [None, False, True])
def test_cli_registrar_versioned_options_use_native_contract(option, value):
    root = CanonicalArgumentParser(prog="root")
    registrar = CanonicalCommandRegistrar(root)
    contract = argparse._SubParsersAction.add_parser if option == "deprecated" else argparse.ArgumentParser
    if option in inspect.signature(contract).parameters:
        child = registrar.add_parser("extension", **{option: value})
        if option == "deprecated":
            assert ("extension" in root.add_subparsers()._deprecated) is bool(value)
        else:
            expected = root.color if option == "color" and value is None else value
            assert getattr(child, option) is expected
        assert root.parse_args(["extension"]).command == "extension"
    else:
        with pytest.raises(TypeError, match=option):
            registrar.add_parser("extension", **{option: value})


def test_cli_registrar_alias_conflict_rejected_before_tree_mutation():
    root = CanonicalArgumentParser()
    registrar = CanonicalCommandRegistrar(root)
    original = registrar.add_parser("extension", aliases=["short"])
    registrar.begin_extensions()
    with pytest.raises(argparse.ArgumentError):
        registrar.add_parser("other", aliases=["short"])
    assert "other" not in registrar.choices
    assert registrar.choices["short"] is original
