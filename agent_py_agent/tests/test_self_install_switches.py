"""learnpack 第 1 步：「能力包自动装」「插件自动装」两个开关。

锁定：默认都关（dataclass 与随包 YAML 一致）；参数中心登记成管理员专用边界项、生效时机如实报"马上生效"；
模型改不了、管理员 /settings 能改（TUI 文本回放、IM 绑定管理员各一条，非管理员被拒）；运行时每次按文件现读，
不吃 agent 上缓存的旧快照；配置读不到按"关"；回执给出的开关命令能被 /settings 解析器原样接受；模型能只读查看。
"""
from __future__ import annotations

import json
import os
import stat
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.capability.runtime_config_reload import (
    bundled_capability_config_path,
    capability_config_version,
    load_capability_config_snapshot,
)
from agent_py_agent.agent.capability.self_install_switches import (
    PACK_SELF_INSTALL_KEY,
    PLUGIN_SELF_INSTALL_KEY,
    SELF_INSTALL_SWITCH_KEYS,
    UNREADABLE_CONFIG_VERSION,
    read_self_install_switches,
    switch_command,
    switch_facts,
)
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import settings_control_service
from agent_py_agent.agent.settings.parameter_changes import (
    ChangeOrigin,
    WritePaths,
    set_parameter,
    user_settings_write_scope,
)
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import (
    BOUNDARY_KEYS,
    EFFECT_IMMEDIATE,
    USER_SETTINGS_BOUNDARY_KEYS,
)
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.cli.chat_parts import control_runtime

_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
_FEISHU_USER = SimpleNamespace(owner_provider="feishu", owner_kind="users", owner_id="ou_x")
_KEYS = (PACK_SELF_INSTALL_KEY, PLUGIN_SELF_INSTALL_KEY)


def _agent(tmp_path, *, home=_ADMIN):
    user_config = tmp_path / "desktop.yaml"
    if not user_config.exists():
        user_config.write_text('agent_name: "myagent"\n', encoding="utf-8")
    capability = tmp_path / "owner" / "config" / "capability_config.yaml"
    return SimpleNamespace(
        config=SimpleNamespace(config_path=str(user_config)), root=tmp_path / "owner",
        capability_config_path=str(capability), home_paths=home,
    )


def _write(agent, text: str) -> None:
    path = os.fspath(agent.capability_config_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def test_both_switches_default_off_in_dataclass_and_packaged_yaml():
    packaged = load_capability_config(bundled_capability_config_path())
    for key in _KEYS:
        assert getattr(CapabilityConfig(), key) is False
        assert getattr(packaged, key) is False
    assert frozenset(_KEYS) == SELF_INSTALL_SWITCH_KEYS


@pytest.mark.parametrize("key", _KEYS)
def test_registry_marks_switch_admin_only_and_immediate(key):
    spec = parameter_registry()[key]
    assert (spec.source, spec.default, spec.writable, spec.effect) == ("capability", False, False, EFFECT_IMMEDIATE)
    assert key in USER_SETTINGS_BOUNDARY_KEYS and "模型不能改" in BOUNDARY_KEYS[key]
    assert "马上生效" in spec.description and "/settings" in spec.description


@pytest.mark.parametrize("key", _KEYS)
def test_model_is_refused_but_admin_settings_write_takes_effect_now(tmp_path, key):
    agent = _agent(tmp_path)
    paths = WritePaths(user_path=agent.config.config_path, capability_path=agent.capability_config_path)
    refused = set_parameter(key, True, paths=paths, origin=ChangeOrigin("model"))
    assert refused["ok"] is False and refused["code"] == "PARAMETER_BOUNDARY"
    assert not os.path.exists(agent.capability_config_path)
    with user_settings_write_scope():
        report = set_parameter(key, "开", paths=paths, origin=ChangeOrigin("chat"))
    assert report["ok"] is True and report["effect_when"] == EFFECT_IMMEDIATE and "已经生效" in report["note"]
    assert stat.S_IMODE(os.stat(agent.capability_config_path).st_mode) == 0o600
    switches = read_self_install_switches(agent)
    assert (switches.capability_pack, switches.plugin) == (key == PACK_SELF_INSTALL_KEY, key == PLUGIN_SELF_INSTALL_KEY)


def test_switch_is_read_fresh_even_with_a_cached_snapshot(tmp_path):
    agent = _agent(tmp_path)
    _write(agent, "capability_pack_self_install_enabled: false\n")
    # Gateway 启动时缓存的旧快照：开关读取不能吃这份缓存，否则 /settings 改完要等重启。
    agent._capability_config_runtime_snapshot = load_capability_config_snapshot(agent.capability_config_path)
    _write(agent, "capability_pack_self_install_enabled: true\nplugin_self_install_enabled: true\n")
    switches = read_self_install_switches(agent)
    assert (switches.capability_pack, switches.plugin) == (True, True)
    assert switches.config_version == capability_config_version(agent.capability_config_path)
    _write(agent, "capability_pack_self_install_enabled: false\nplugin_self_install_enabled: true\n")
    assert (read_self_install_switches(agent).capability_pack, read_self_install_switches(agent).plugin) == (False, True)
    assert agent._capability_config_runtime_snapshot.config.capability_pack_self_install_enabled is False


def test_missing_user_file_reads_packaged_default_and_unreadable_reads_off(tmp_path):
    agent = _agent(tmp_path)
    missing = read_self_install_switches(agent)
    assert (missing.capability_pack, missing.plugin) == (False, False)
    assert missing.config_version == capability_config_version(bundled_capability_config_path())
    os.makedirs(agent.capability_config_path)  # 路径被目录占住：读不出来就按关处理
    broken = read_self_install_switches(agent)
    assert (broken.capability_pack, broken.plugin, broken.config_version) == (False, False, UNREADABLE_CONFIG_VERSION)


def test_switch_facts_commands_round_trip_through_the_settings_parser():
    facts = switch_facts(read_self_install_switches(SimpleNamespace(root="/nonexistent", capability_config_path="")))
    commands = facts["switch_commands"]
    assert set(commands) == {"capability_pack_on", "capability_pack_off", "plugin_on", "plugin_off"}
    for text in commands.values():
        command = parse_conversation_control(text, reject_unknown_slash=True)
        assert (command.kind, command.operation, command.valid) == ("settings", "set", True)
        assert control_runtime._command_text(command) == text  # TUI 发给 Gateway 的文字原样还原
    assert commands["plugin_off"] == switch_command(PLUGIN_SELF_INSTALL_KEY, False) == (
        "/settings set plugin_self_install_enabled false")
    assert facts[PACK_SELF_INSTALL_KEY] is False and facts[PLUGIN_SELF_INSTALL_KEY] is False


def _settings_run(monkeypatch, agent, text, *, home):
    monkeypatch.setattr(settings_control_service, "_scoped_home", lambda _agent, _scope: home)
    command = parse_conversation_control(text, reject_unknown_slash=True)
    return settings_control_service.execute_settings_control(agent, command, None)


# TUI（本机管理员）与 IM（已用 /admin 绑定的管理员私聊，解析出的也是本机管理员身份）走同一个 Gateway /settings 服务。
@pytest.mark.parametrize("channel_home", [_ADMIN, SimpleNamespace(owner_provider="local", owner_kind="main",
                                                                   owner_id="main", channel="feishu")])
def test_admin_turns_switch_on_and_off_through_gateway_settings(monkeypatch, tmp_path, channel_home):
    agent = _agent(tmp_path)
    turned_on = _settings_run(monkeypatch, agent, "/settings set plugin_self_install_enabled true", home=channel_home)
    assert turned_on.ok and "马上生效" in turned_on.message and "/restart" not in turned_on.message
    assert read_self_install_switches(agent).plugin is True
    turned_off = _settings_run(monkeypatch, agent, "/settings set plugin_self_install_enabled false", home=channel_home)
    assert turned_off.ok and read_self_install_switches(agent).plugin is False


def test_feishu_non_admin_cannot_turn_switch_on(monkeypatch, tmp_path):
    agent = _agent(tmp_path)
    refused = _settings_run(monkeypatch, agent, "/settings set capability_pack_self_install_enabled true",
                            home=_FEISHU_USER)
    assert refused.ok is False and "管理员" in refused.message
    assert not os.path.exists(agent.capability_config_path)


def test_model_can_view_switch_value_read_only(tmp_path):
    agent = _agent(tmp_path)
    _write(agent, "plugin_self_install_enabled: true\n")
    outcome = UserConfigTool(agent).execute({"action": "view", "key": PLUGIN_SELF_INSTALL_KEY})
    parameter = json.loads(outcome.output)["parameter"]
    assert outcome.ok and parameter["running_value"] == "true" and parameter["writable"] is False
    assert parameter["effect_when"] == EFFECT_IMMEDIATE


# LLM: spy仅观察原标准库YAML入口，真实临时文件驱动配置变化，不以墙钟阈值判断缓存有效。
# 函数用途: 验证未变文件只解析一次，磁盘改动立即生效，返回告警及其它列表互不污染。
def test_capability_stat_cache_is_fresh_and_returns_isolated_values(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import config as module
    path = tmp_path / 'capability.yaml'
    path.write_text('plugin_self_install_enabled: false\nobsolete_key: 1\nadmin_message_channels: ["feishu"]\n')
    parse = Mock(wraps=module.load_simple_yaml)
    monkeypatch.setattr(module, 'load_simple_yaml', parse)
    first = module.load_capability_config(path)
    second = module.load_capability_config(path)
    assert first == second and first is not second and parse.call_count == 1
    first.config_warnings.append('x')
    second.admin_message_channels.append('untrusted')
    third = module.load_capability_config(path)
    assert 'x' not in third.config_warnings and third.admin_message_channels == ['feishu']
    path.write_text('plugin_self_install_enabled: true\n')
    assert module.load_capability_config(path).plugin_self_install_enabled is True
    assert parse.call_count == 2


# LLM: 不让缓存跨过显式文件删除/非法值的失败关闭，也核对恢复同尺寸mtime时ctime/替换身份参与失效。
# 函数用途: 验证配置安全开关缓存不能变成旧值兜底。
def test_capability_cache_invalidates_replacement_and_keeps_failures(tmp_path):
    path = tmp_path / 'capability.yaml'
    path.write_text('session_pair_hourly_limit: 11\n')
    before = path.stat()
    assert load_capability_config(path).session_pair_hourly_limit == 11
    replacement = tmp_path / 'replacement'
    replacement.write_text('session_pair_hourly_limit: 22\n')
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert load_capability_config(path).session_pair_hourly_limit == 22
    path.write_text('owner_default_permission_mode: invalid\n')
    with pytest.raises(ValueError):
        load_capability_config(path)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        load_capability_config(path)


# LLM: 锁内的miss装载须防并发重复解析；仅线程并发读取同一临时文件，不启动Gateway。
# 函数用途: 验证配置缓存冷启动并发读与每个返回实例隔离。
def test_capability_cache_serializes_concurrent_misses(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import config as module
    path = tmp_path / 'threaded.yaml'
    path.write_text('plugin_self_install_enabled: true\n')
    parse = Mock(wraps=module.load_simple_yaml)
    monkeypatch.setattr(module, 'load_simple_yaml', parse)
    with ThreadPoolExecutor(max_workers=6) as workers:
        configs = list(workers.map(module.load_capability_config, [path] * 24))
    assert all(cfg.plugin_self_install_enabled for cfg in configs)
    assert len({id(cfg) for cfg in configs}) == 24 and parse.call_count == 1


# LLM: cache替身只隔开其它测试的全局条目，装载与stat仍是真实实现；不新增产品清理API。
# 函数用途: 验证两个配置缓存均有64项上限，命中会刷新LRU顺序。
@pytest.mark.parametrize('kind', ['capability', 'guard'])
def test_config_stat_caches_are_bounded_lru(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.capability import config as capability
    from agent_py_agent.agent.settings import runtime_guard_config as guard
    module, name, load = (capability, '_CAPABILITY_CONFIG_CACHE', capability.load_capability_config) if kind == 'capability' else (
        guard, '_RUNTIME_GUARD_RAW_CACHE', guard.runtime_guard_policy)
    cache = OrderedDict()
    monkeypatch.setattr(module, name, cache, raising=False)
    paths = [tmp_path / f'config-{index}.yaml' for index in range(65)]
    for path in paths:
        path.write_text('session_pair_hourly_limit: 7\n')
    for path in paths[:64]:
        load(path)
    load(paths[0])
    load(paths[64])
    assert len(cache) == 64
    assert str(paths[0]) in {key[0] for key in cache} and str(paths[1]) not in {key[0] for key in cache}


# LLM: 缓存只能保存原始配置，不得把一次调用的overrides或返回列表带给下一次调用。
# 函数用途: 核验守卫原始配置复用、覆盖层隔离、修改失效及OSError原合同。
def test_runtime_guard_raw_cache_is_fresh_and_isolated(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import runtime_guard_config as module
    path = tmp_path / 'guard.yaml'
    path.write_text('limit: 7\ndelays: [1, 2]\n')
    parse = Mock(wraps=module.load_simple_yaml)
    monkeypatch.setattr(module, 'load_simple_yaml', parse)
    first = module.runtime_guard_policy(path, overrides={'limit': 9})
    first.values['delays'].append(99)
    second = module.runtime_guard_policy(path)
    assert second.values == {'limit': 7, 'delays': [1, 2]} and parse.call_count == 1
    assert second.sources['limit'] == str(path) and first.sources['limit'] == 'runtime_override'
    path.write_text('limit: 8\n')
    assert module.runtime_guard_policy(path).values == {'limit': 8} and parse.call_count == 2
    path.unlink()
    assert module.runtime_guard_policy(path, overrides={'limit': 3}).values == {'limit': 3}


# LLM: 没有旧文件时不探测随包源；随包路径函数本身也不再反复realpath。
# 函数用途: 守住两个无需额外路径解析的热路径。
def test_legacy_missing_fast_path_and_bundled_path_constant(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import runtime_config_reload as module
    bundled = module.bundled_capability_config_path()
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'resolve', Mock(side_effect=AssertionError('must not resolve on hot path')))
        assert module.bundled_capability_config_path() == bundled
    monkeypatch.setattr(module, 'bundled_capability_config_path', Mock(side_effect=AssertionError('missing legacy must return early')))
    assert module._legacy_location_warning(tmp_path) is None


# LLM: 使用现行旧位置而不是任务旧稿里的另一条路径；相同/不同内容及两边修改均须及时反映。
# 函数用途: 证明旧位置告警按两份文件的stat复用比较结果且立即失效。
def test_legacy_warning_cache_avoids_repeated_content_reads(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import runtime_config_reload as module
    legacy = tmp_path / 'agent_py_agent/config/capability_config.yaml'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('old\n')
    bundled = tmp_path / 'bundled.yaml'
    bundled.write_text('new\n')
    monkeypatch.setattr(module, 'bundled_capability_config_path', lambda: bundled)
    original = Path.read_bytes
    reads = []
    # LLM: 保留真实字节比较，不缓存或伪造测试输入内容。
    # 函数用途: 计数两份文件实际读取的次数。
    def read(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', read)
    warning = module._legacy_location_warning(tmp_path)
    assert warning and module._legacy_location_warning(tmp_path) == warning
    assert reads == [legacy, bundled]
    legacy.write_text('new\n')
    assert module._legacy_location_warning(tmp_path) is None
    reads.clear()
    assert module._legacy_location_warning(tmp_path) is None and not reads
    bundled.write_text('changed\n')
    assert module._legacy_location_warning(tmp_path)


# LLM: 用真实软链接验证同inode早返；当前主机硬链接夹具报EPERM，不以该环境错误当产品分支失败。
# 函数用途: 验证旧文件与随包目标同一实例时无需读两遍正文。
def test_legacy_warning_same_inode_skips_reads(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import runtime_config_reload as module
    legacy = tmp_path / 'agent_py_agent/config/capability_config.yaml'
    legacy.parent.mkdir(parents=True)
    bundled = tmp_path / 'bundled.yaml'
    bundled.write_text('same\n')
    legacy.symlink_to(bundled)
    monkeypatch.setattr(Path, 'resolve', Mock(side_effect=AssertionError('same inode must not resolve')))
    monkeypatch.setattr(module, 'bundled_capability_config_path', lambda: bundled)
    monkeypatch.setattr(Path, 'read_bytes', Mock(side_effect=AssertionError('same inode must not read')))
    assert module._legacy_location_warning(tmp_path) is None


# LLM: 两个生产入口都要在任何线程/Agent构造前设置间隔，替身在首个业务动作停止，绝不启动服务。
# 函数用途: 核验standalone、普通线程及协调线程路径的真实入口调用顺序。
@pytest.mark.parametrize('entry', ['standalone', 'threads', 'coordinated'])
def test_gateway_entry_tunes_gil_before_work(monkeypatch, entry):
    from agent_py_agent.cli import gateway_coordinated_run
    from agent_py_agent.cli import gateway_process as module
    events = []
    monkeypatch.setattr(module.sys, 'setswitchinterval', lambda value: events.append(('gil', value)))
    # LLM: 在入口第一个业务动作处结束测试，不允许碰真实Gateway或创建线程。
    # 函数用途: 留下GIL设置后的入口观察点。
    def stop(*args, **kwargs):
        events.append(('work', None))
        raise ValueError('stop-before-start')
    if entry == 'standalone':
        monkeypatch.setattr(module, 'make_agent', stop)
        invoke = partial(module._run_standalone_gateway, SimpleNamespace(), None)
    else:
        context = SimpleNamespace(control_plane=object() if entry == 'coordinated' else None, paths=None, agent=None)
        monkeypatch.setattr(gateway_coordinated_run, 'start_coordinated_loops', stop)
        monkeypatch.setattr(module.threading, 'Event', stop)
        invoke = partial(module._cmd_gateway_run_threads, SimpleNamespace(context=context))
    with pytest.raises(ValueError, match='stop-before-start'):
        invoke()
    assert events == [('gil', .001), ('work', None)]


# LLM: 只调整子进程启动路径中的解释器调度提示，不因不支持该API而阻断Gateway。
# 函数用途: 核验任务规定的两个兼容异常均被吞掉。
@pytest.mark.parametrize('error', [AttributeError, ValueError])
def test_gateway_gil_tuning_tolerates_unsupported_api(monkeypatch, error):
    from agent_py_agent.cli import gateway_process as module
    tune = getattr(module, '_tune_gateway_gil_switch_interval', None)
    assert callable(tune)
    monkeypatch.setattr(module.sys, 'setswitchinterval', Mock(side_effect=error('unsupported')))
    assert tune() is None


# LLM: 只在本测试解释器中真实设置并finally恢复，不触碰运行中Gateway。
# 函数用途: 验证解释器实际采纳1ms值，入口顺序另由替身测试守住。
def test_gateway_gil_tuning_sets_real_interval():
    from agent_py_agent.cli import gateway_process as module
    previous = module.sys.getswitchinterval()
    try:
        module._tune_gateway_gil_switch_interval()
        assert module.sys.getswitchinterval() == pytest.approx(.001)
    finally:
        module.sys.setswitchinterval(previous)


# LLM: 只替换本测试的内存cache，真实旧位置文件与目标仍按产品stat/比较逻辑读取。
# 函数用途: 验证第三个缓存也受64项LRU上限约束。
def test_legacy_warning_cache_is_bounded_lru(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import runtime_config_reload as module
    cache = OrderedDict()
    monkeypatch.setattr(module, '_LEGACY_WARNING_CACHE', cache)
    bundled = tmp_path / 'bundled.yaml'
    bundled.write_text('new\n')
    monkeypatch.setattr(module, 'bundled_capability_config_path', lambda: bundled)
    roots = [tmp_path / str(index) for index in range(65)]
    for root in roots:
        path = root / 'agent_py_agent/config/capability_config.yaml'
        path.parent.mkdir(parents=True)
        path.write_text('old\n')
    for root in roots[:64]:
        assert module._legacy_location_warning(root)
    module._legacy_location_warning(roots[0])
    module._legacy_location_warning(roots[64])
    assert len(cache) == 64
    keys = {key[0] for key in cache}
    assert str(roots[0] / 'agent_py_agent/config/capability_config.yaml') in keys
    assert str(roots[1] / 'agent_py_agent/config/capability_config.yaml') not in keys


# LLM: 恢复mtime且保留inode/size后仍须重新解析；两个配置入口必须依赖ctime而不是仅mtime缓存。
# 函数用途: 验证同尺寸原位更新不会隐身于stat缓存。
@pytest.mark.parametrize('kind', ['capability', 'guard'])
def test_config_cache_detects_same_size_edit_with_restored_mtime(tmp_path, kind):
    from agent_py_agent.agent.settings.runtime_guard_config import runtime_guard_policy
    path = tmp_path / 'config.yaml'
    path.write_text('session_pair_hourly_limit: 11\n')
    before = path.stat()
    if kind == 'capability':
        assert load_capability_config(path).session_pair_hourly_limit == 11
    else:
        assert runtime_guard_policy(path).values['session_pair_hourly_limit'] == 11
    path.write_text('session_pair_hourly_limit: 22\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    if kind == 'capability':
        assert load_capability_config(path).session_pair_hourly_limit == 22
    else:
        assert runtime_guard_policy(path).values['session_pair_hourly_limit'] == 22


def test_capability_config_without_path_uses_original_bundled_resolver():
    assert load_capability_config() == load_capability_config(bundled_capability_config_path())
