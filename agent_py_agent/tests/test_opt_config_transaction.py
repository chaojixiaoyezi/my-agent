# LLM: 所有写入只在 pytest 临时目录；校验配置目录来源、真实 writer 能力和管理边界，不访问部署数据。
# 模块用途: 验证 e905 参数目录与统一配置事务合同。
import pytest

from agent_py_agent.agent.settings import parameter_registry as registry_module
from agent_py_agent.agent.settings.parameter_changes import (
    ChangeOrigin,
    WritePaths,
    set_parameter,
    user_settings_write_scope,
)


# LLM: 核心展示名单是 v2 封闭产品口径，不把模型档案或工具细项重新塞回默认页面。
# 函数用途: 验证日常参数精确为十四项。
def test_config_common_exact_fourteen():
    assert registry_module.COMMON_KEYS == (
        "max_tokens", "request_timeout", "model_reasoning_effort", "memory_compact_trigger_percent",
        "conversation_history_max_turns", "memory_top_k", "memory_curator_enabled", "enable_self_learning",
        "enable_subagents", "max_subagents", "tool_shell_timeout", "tool_output_externalize_min_chars",
        "timezone", "tui_mouse_capture_default",
    )


# LLM: 故意令旧 YAML 路径解析失败，注册表仍必须从 typed 部署策略取得守卫默认。
# 函数用途: 验证参数目录不再读取已经退役的运行守卫 YAML。
def test_guard_registry_uses_typed_defaults_not_old_yaml(monkeypatch):
    original = registry_module._packaged_extra_config_path

    # LLM: 只拦旧来源；能力配置仍使用自己的合法说明来源，不扩大故障注入范围。
    # 函数用途: 捕获注册表误读旧守卫文件的行为。
    def path_for(source):
        assert source != registry_module.SOURCE_RUNTIME_GUARD
        return original(source)

    registry_module.parameter_registry.cache_clear()
    with monkeypatch.context() as patch:
        patch.setattr(registry_module, "_packaged_extra_config_path", path_for)
        assert registry_module.parameter_registry()["repeat_fail_threshold"].default == 10
    registry_module.parameter_registry.cache_clear()


# LLM: 用户并发额度留在高级展示层，但仅认证管理员控制入口能写；actor 字符串不能伪造该权限。
# 函数用途: 验证模型拒绝而管理员经真实 writer 可以修改同一个参数。
def test_gateway_user_limit_is_admin_editable_not_model_editable(tmp_path):
    path = tmp_path / "desktop.yaml"
    path.write_text("gateway_user_inflight_limit: 5\n")
    spec = registry_module.parameter_registry()["gateway_user_inflight_limit"]
    assert not spec.common and not spec.writable
    denied = set_parameter(spec.key, "8", paths=WritePaths(path), origin=ChangeOrigin("chat"))
    assert denied["code"] == "PARAMETER_BOUNDARY"
    assert path.read_text() == "gateway_user_inflight_limit: 5\n"
    with user_settings_write_scope():
        result = set_parameter(spec.key, "8", paths=WritePaths(path), origin=ChangeOrigin("chat"))
    assert result["ok"] and result["effective"] == "8"


# LLM: Python 调用方也必须使用唯一新键，不留下构造、复制或动态 setter 联动；未知旧构造参数应直接失败。
# 函数用途: 验证旧压缩比例已从运行时 schema 物理移除。
def test_compact_alias_is_not_a_runtime_field():
    from dataclasses import fields, replace

    from agent_py_agent.agent.settings.config import AgentConfig

    assert "memory_compact_auto_trigger_percent" not in {item.name for item in fields(AgentConfig)}
    with pytest.raises(TypeError):
        AgentConfig(memory_compact_auto_trigger_percent=70)
    base = AgentConfig(memory_compact_trigger_percent=80)
    updated = replace(base, memory_compact_trigger_percent=70)
    assert updated.memory_compact_trigger_percent == 70 and base.memory_compact_trigger_percent == 80


# LLM: 迁移只处理显式提供的各来源层，不读默认/环境或写盘；同层新键优先，不改变层间优先级。
# 函数用途: 验证旧 YAML 离线迁移保持来源，并优先采用显式新字段。
def test_compact_offline_migration_preserves_layer_priority():
    from agent_py_agent.agent.settings.config_layers import ConfigLayer, merge_config_layers
    from agent_py_agent.agent.settings.model_credentials import migrate_legacy_compact_layers

    original = [ConfigLayer("admin.yaml", 20, {"memory_compact_trigger_percent": 85}),
                ConfigLayer("owner.yaml", 30, {"memory_compact_auto_trigger_percent": 70}),
                ConfigLayer("task.yaml", 50, {"memory_compact_auto_trigger_percent": 90, "memory_compact_trigger_percent": 75})]
    migrated = migrate_legacy_compact_layers(original)
    assert [(item.source, item.priority) for item in migrated] == [(item.source, item.priority) for item in original]
    assert all("memory_compact_auto_trigger_percent" not in item.values for item in migrated)
    assert migrated[1].values["memory_compact_trigger_percent"] == 70
    assert merge_config_layers(migrated).value("memory_compact_trigger_percent") == 75
    assert original[1].values == {"memory_compact_auto_trigger_percent": 70}


# LLM: 仅开放经过 typed writer 登记的三项结构；输入必须真实类型或严格 JSON，不能把任意映射当标量写入。
# 函数用途: 验证列表和角色期限经过正式读取、记账、恢复及回滚的完整本地链路。
@pytest.mark.parametrize("key,value", [
    ("tool_catalog_categories", ["core", "plugins"]),
    ("tool_catalog_deferred_categories", ["memory", "plugins"]),
    ("runner_timeout_by_role", {"c": 0, "worker": None, "default": "inherit", "*": "off"}),
])
def test_config_structured_writer_round_trip(tmp_path, key, value):
    from agent_py_agent.agent.settings import parameter_changes as changes
    from agent_py_agent.agent.settings.config import load_config

    path = tmp_path / "desktop.yaml"
    path.write_text("agent_name: fixture\n")
    paths, origin = WritePaths(path), ChangeOrigin("test")
    assert registry_module.parameter_registry()[key].writable
    first = changes.set_parameter(key, value, paths=paths, origin=origin)
    assert first["ok"], first
    assert getattr(load_config(path), key) == value
    reset = changes.reset_parameter(key, paths=paths, origin=origin)
    assert reset["ok"], reset
    reverted = changes.revert_change(reset["change_id"], paths=paths, origin=origin)
    assert reverted["ok"] and getattr(load_config(path), key) == value


# LLM: 坏值不得生成成功台账或留下改动；bool 不能伪装秒数，重复 JSON 键与大小写碰撞不能静默取末项。
# 函数用途: 验证结构化写入在磁盘提交前拒绝非法输入。
@pytest.mark.parametrize("key,value", [
    ("tool_catalog_categories", ["core", "core"]),
    ("tool_catalog_categories", [True]),
    ("tool_catalog_categories", "not-json"),
    ("runner_timeout_by_role", {"worker": True}),
    ("runner_timeout_by_role", {"worker": -1}),
    ("runner_timeout_by_role", {"worker": float("inf")}),
    ("runner_timeout_by_role", {"C": 1, "c": 2}),
    ("runner_timeout_by_role", '{"worker":1,"worker":2}'),
])
def test_config_structured_invalid_is_noop(tmp_path, key, value):
    from agent_py_agent.agent.settings.parameter_changes import parameter_history

    path = tmp_path / "desktop.yaml"
    before = b"# original\r\nmax_tokens: 1000\r\n"
    path.write_bytes(before)
    result = set_parameter(key, value, paths=WritePaths(path), origin=ChangeOrigin("test"))
    assert not result["ok"]
    assert path.read_bytes() == before
    assert parameter_history(user_path=path) == []


# LLM: 失败可以发生在正式加载或记账，三条写路线都不能把失败后的数据留给下轮；保留 BOM/CRLF 原字节。
# 函数用途: 验证 reset/revert 的异常回滚不是只有 set 才有。
@pytest.mark.parametrize("action", ["reset", "revert"])
def test_config_transaction_reader_failure_preserves_bytes(tmp_path, monkeypatch, action):
    from agent_py_agent.agent.settings import parameter_changes as changes

    path = tmp_path / "desktop.yaml"
    path.write_bytes(b'\xef\xbb\xbf# keep\r\nmax_tokens: 1000\r\n')
    paths, origin = WritePaths(path), ChangeOrigin("test")
    saved = set_parameter("max_tokens", 2000, paths=paths, origin=origin)
    assert saved["ok"]
    before, ledger = path.read_bytes(), changes.ledger_path(path).read_bytes()

    # LLM: 仅注入正式读取故障，不伪造文件变化或越过实际 writer。
    # 函数用途: 模拟目标配置不能正确加载。
    def broken(*args):
        raise ValueError("synthetic reader failure")

    monkeypatch.setattr(changes, "_effective", broken)
    result = (changes.reset_parameter("max_tokens", paths=paths, origin=origin) if action == "reset"
              else changes.revert_change(saved["change_id"], paths=paths, origin=origin))
    assert not result["ok"]
    assert path.read_bytes() == before and changes.ledger_path(path).read_bytes() == ledger


# LLM: 同一 revision 的两个提交只能一个成功；原始 actor 文本不授予写权，冲突不落成功台账。
# 函数用途: 验证唯一事务的显式 CAS 合同。
def test_config_transaction_stale_revision_does_not_overwrite(tmp_path):
    from hashlib import sha256

    path = tmp_path / "desktop.yaml"
    path.write_text("max_tokens: 1000\n")
    revision = sha256(path.read_bytes()).hexdigest()
    paths = WritePaths(path, expected_revision=revision)
    first = set_parameter("max_tokens", 2000, paths=paths, origin=ChangeOrigin("test"))
    assert first["ok"]
    second = set_parameter("max_tokens", 3000, paths=paths, origin=ChangeOrigin("test"))
    assert second["code"] == "CONFIG_REVISION_CONFLICT"
    assert path.read_text() == "max_tokens: 2000\n"


# LLM: 真实宿主 JSON 与伪 owner 同名文件分开放；事实展示须标出显式覆盖及 Python 默认，不回旧 YAML。
# 函数用途: 验证读详情来源与真实运行 guard 一致。
def test_config_guard_facts_use_host_json_not_neighbor_yaml(tmp_path):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.settings.user_config_capability import read_config_fact

    (tmp_path / "config").mkdir()
    (tmp_path / "config/deployment_policy.json").write_text(json.dumps({"schema_version": "deployment-policy.v1",
                                             "runtime_guards": {"repeat_fail_threshold": 7}}))
    neighbor = tmp_path / "owner/desktop.yaml"
    neighbor.parent.mkdir()
    neighbor.write_text("repeat_fail_threshold: 99\n")
    neighbor.with_name("runtime_guard_config.yaml").write_text("repeat_fail_threshold: 99\n")
    config = SimpleNamespace(my_agent_home=str(tmp_path))
    fact = read_config_fact("repeat_fail_threshold", user_path=neighbor, default_path=neighbor, config=config)
    assert fact["effective"] == "7" and fact["source"] == "deployment_policy"
    assert fact["user_config_path"] == "" and fact["source_readonly"]
    default = read_config_fact("readonly_no_progress_threshold", user_path=neighbor, default_path=neighbor, config=config)
    assert default["effective"] == "3" and default["source"] == "python_default"
    unknown = read_config_fact("repeat_fail_threshold", user_path=neighbor, default_path=neighbor)
    assert unknown["source"] == "unknown"


# LLM: 经正式 settings 控制分派查看 guard，不得把邻接 owner YAML 冒充宿主部署覆盖。
# 函数用途: 验证管理员详情回执的值、来源和写入口说明同真实 JSON 一致。
def test_config_settings_guard_show_uses_deployment_source(tmp_path):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.settings_control_service import run_settings_control

    (tmp_path / "config").mkdir()
    (tmp_path / "config/deployment_policy.json").write_text(json.dumps({"schema_version": "deployment-policy.v1",
                                            "runtime_guards": {"repeat_fail_threshold": 7}}))
    neighbor = tmp_path / "owner.yaml"
    neighbor.write_text("repeat_fail_threshold: 99\n")
    config = SimpleNamespace(my_agent_home=str(tmp_path), config_path=str(neighbor))
    command = SimpleNamespace(operation="show", value="show repeat_fail_threshold")
    text = run_settings_control(config, command)
    assert "当前运行值：7" in text and "99" not in text
    assert "deployment_policy.json" in text and "runtime_guard_config.yaml" not in text
    assert "随包默认、不可覆盖" not in text and "部署" in text


# LLM: 统计记录容量最少为1；新策略不能接受0后又在消费者处暗中改为1。
# 函数用途: 验证部署记录容量的公开值与实际值不分叉。
def test_config_deployment_record_budget_zero_is_rejected():
    from agent_py_agent.agent.settings.deployment_policy import (
        DeploymentPolicyError,
        deployment_policy_from_dict,
    )

    with pytest.raises(DeploymentPolicyError):
        deployment_policy_from_dict({"schema_version": "deployment-policy.v1", "runtime_guards": {"tool_rate_max_records": 0}})


# LLM: 记账完成后失败再注入恢复失败，必须分别尝试两份恢复；不可把未恢复包装成普通已回滚失败。
# 函数用途: 验证配置与台账的恢复互不短路且回执诚实。
@pytest.mark.parametrize("failed_targets", [{"config"}, {"ledger"}, {"config", "ledger"}])
def test_config_transaction_restore_failure_does_not_skip_other_target(tmp_path, monkeypatch, failed_targets):
    from agent_py_agent.agent.settings import parameter_changes as changes

    path = tmp_path / "desktop.yaml"
    path.write_text("max_tokens: 1000\n")
    paths, origin = WritePaths(path), ChangeOrigin("test")
    assert set_parameter("max_tokens", 2000, paths=paths, origin=origin)["ok"]
    ledger = changes.ledger_path(path)
    before = {"config": path.read_bytes(), "ledger": ledger.read_bytes()}
    restore, attempted = changes._restore, []

    # LLM: 故障发生在两份数据均提交之后，确保不是只测写前拒绝路径。
    # 函数用途: 注入返回回执时的原始事务故障。
    def fail_receipt(*args):
        raise ValueError("fixture receipt failure")

    # LLM: 只拦指定对象的恢复，另一份仍通过真实磁盘 writer 完成恢复。
    # 函数用途: 注入可区分对象的回滚 I/O 故障。
    def failing_restore(target, content, existed):
        name = "config" if target == path.resolve() else "ledger"
        attempted.append(name)
        if name in failed_targets:
            raise OSError("fixture restore failure")
        return restore(target, content, existed)

    monkeypatch.setattr(changes, "_receipt", fail_receipt)
    monkeypatch.setattr(changes, "_restore", failing_restore)
    result = set_parameter("max_tokens", 3000, paths=paths, origin=origin)
    assert attempted == ["config", "ledger"]
    assert result["code"] == "CONFIG_ROLLBACK_INCOMPLETE" and "未能恢复" in result["error"]
    for name, target in (("config", path), ("ledger", ledger)):
        assert (target.read_bytes() == before[name]) is (name not in failed_targets)


# LLM: 台账别名与实际目标必须保持同一锁和快照；真实提交后失败也不能替换链接位置留下成功记录。
# 函数用途: 验证路径别名下配置与台账仍能精确回滚。
def test_config_transaction_ledger_alias_uses_locked_target(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import parameter_changes as changes

    path = tmp_path / "desktop.yaml"
    path.write_text("max_tokens: 1000\n")
    paths, origin = WritePaths(path), ChangeOrigin("test")
    assert set_parameter("max_tokens", 2000, paths=paths, origin=origin)["ok"]
    ledger = changes.ledger_path(path)
    target = tmp_path / "actual-ledger.jsonl"
    target.write_bytes(ledger.read_bytes())
    ledger.unlink()
    ledger.symlink_to(target)
    before, ledger_before, reached = path.read_bytes(), target.read_bytes(), []

    # LLM: 注入点在真实记账之后，不用伪成功替代磁盘事务。
    # 函数用途: 模拟已提交事务的回执故障。
    def fail_after_ledger(*args):
        reached.append(True)
        raise ValueError("fixture receipt failure")

    monkeypatch.setattr(changes, "_receipt", fail_after_ledger)
    result = set_parameter("max_tokens", 3000, paths=paths, origin=origin)
    assert not result["ok"] and reached == [True]
    assert ledger.is_symlink() and ledger.resolve() == target
    assert path.read_bytes() == before and target.read_bytes() == ledger_before
