# LLM: 设置回归只使用临时宿主与真实策略读口；冻结值、持久值和未知运行值不得混淆，不访问外部模型。
# 模块用途: 锁定部署守卫来源、运行快照、参数分类与压缩旧键退役合同。
from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.compact_threshold_policy import resolve_thread_compact_policy
from agent_py_agent.agent.gateway_parts.settings_control_service import _all_line, _search, _show
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.deployment_policy import (
    RuntimeGuardPolicy,
    load_deployment_policy,
)
from agent_py_agent.agent.settings.parameter_registry import (
    COMMON_KEYS,
    DEPLOYMENT_ONLY_KEYS,
    listed_parameters,
    parameter_registry,
    running_value,
)
from agent_py_agent.agent.settings.runtime_guard_config import runtime_guard_policy
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool, _full_config_receipt
from agent_py_agent.agent.user_space.home_layout import home_paths


# LLM: 所有磁盘事实仅写 pytest 临时目录；显式 revision 用来确认测试没有把内存赋值误作持久修改。
# 函数用途: 保存一份稀疏宿主策略，返回其真实标准路径。
def _save_policy(root, revision, threshold):
    path = root / "config" / "deployment_policy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "deployment-policy.v1", "revision": revision,
                                "runtime_guards": {"repeat_fail_threshold": threshold}}), encoding="utf-8")
    return path


# LLM: 宿主根与用户配置位置显式分开，防止测试不经意依赖进程默认 Home；此 helper 只写临时用户文件。
# 函数用途: 构造可供真实工具和聊天展示入口读取的管理员运行上下文。
def _agent(tmp_path):
    root = tmp_path / "host"
    user_path = tmp_path / "desktop.yaml"
    user_path.write_text("agent_name: test\n", encoding="utf-8")
    config = AgentConfig(my_agent_home=str(root), config_path=str(user_path))
    paths = replace(home_paths(root), owner_provider="local", owner_kind="main", owner_id="main")
    return SimpleNamespace(config=config, home_paths=paths, root=tmp_path)


# LLM: 快照来自真实 loader，改盘后不重建；各入口必须继续消费绑定快照而不是磁盘最新 revision。
# 函数用途: 验证参数读口、聊天 show 和模型工具共同保持冻结阈值。
def test_frozen_guard_snapshot_survives_disk_revision_and_environment_change(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    root = agent.home_paths.root
    _save_policy(root, 1, 7)
    frozen = runtime_guard_policy(trusted_host_paths=agent.home_paths)
    agent.config.runtime_guard_policy = frozen
    _save_policy(root, 2, 13)
    other = tmp_path / "other-host"
    _save_policy(other, 3, 19)
    monkeypatch.setenv("MY_AGENT_HOME", str(other))
    assert load_deployment_policy(trusted_host_paths=agent.home_paths).revision == 2
    assert running_value(parameter_registry()["repeat_fail_threshold"], agent.config) == 7
    shown = _show(agent.config, "repeat_fail_threshold")
    assert "当前运行值：7" in shown and "当前运行值：13" not in shown
    assert "持久部署策略值：13" in shown and "重载" in shown
    result = UserConfigTool(agent).execute({"action": "view", "key": "repeat_fail_threshold"})
    assert result.ok
    payload = json.loads(result.output)
    assert payload["parameter"]["running_value"] == "7"
    assert payload["fact"]["effective"] == "13"


# LLM: 显式传入的已校验策略优先于 config 绑定；非法对象不转为磁盘读取，也不伪装成 typed 默认。
# 函数用途: 验证直接调用方可以传快照且保持类型边界。
def test_running_value_accepts_explicit_frozen_policy(tmp_path):
    config = _agent(tmp_path).config
    config.runtime_guard_policy = RuntimeGuardPolicy(repeat_fail_threshold=7)
    spec = parameter_registry()["repeat_fail_threshold"]
    assert running_value(spec, config, policy=RuntimeGuardPolicy(repeat_fail_threshold=11)) == 11
    assert running_value(spec, policy=RuntimeGuardPolicy(repeat_fail_threshold=0)) == 0
    assert running_value(spec, config, policy=SimpleNamespace(repeat_fail_threshold=17)) == "unknown"


# LLM: 没有冻结实例时即使磁盘合法也只能展示持久值；默认值单独可见，不代表当前进程已经采用它。
# 函数用途: 验证静态配置下 show、搜索、全量列表和工具都不把磁盘阈值称作运行值。
@pytest.mark.parametrize("threshold", [0, 13])
def test_without_snapshot_running_is_unknown_and_persistent_value_is_labelled(tmp_path, threshold):
    agent = _agent(tmp_path)
    _save_policy(agent.home_paths.root, 2, threshold)
    spec = parameter_registry()["repeat_fail_threshold"]
    assert running_value(spec, agent.config) == "unknown"
    assert running_value(spec) == "unknown"
    shown = _show(agent.config, spec.key)
    assert f"持久部署策略值：{threshold}" in shown and "待重载" in shown
    assert "当前运行值：unknown" in shown and "快照" in shown
    assert f"当前运行值：{threshold}" not in shown
    assert "unknown" in _all_line(spec, agent.config, set())
    assert "unknown" in _search(agent.config, spec.key)
    result = UserConfigTool(agent).execute({"action": "view", "key": spec.key})
    assert result.ok
    parameter = json.loads(result.output)["parameter"]
    assert parameter["running_value"] == "unknown"
    assert parameter["running_value_status"] == "unknown"


# LLM: 已冻结的合法快照不因后来的坏盘或文件删除失效；缺快照时则不能吞错声称运行默认。
# 函数用途: 验证策略后续损坏或消失时运行事实仍保持准确。
@pytest.mark.parametrize("disk_state", ["corrupt", "missing"])
def test_frozen_value_remains_known_when_persistent_source_is_unavailable(tmp_path, disk_state):
    agent = _agent(tmp_path)
    path = _save_policy(agent.home_paths.root, 1, 7)
    agent.config.runtime_guard_policy = runtime_guard_policy(trusted_host_paths=agent.home_paths)
    if disk_state == "corrupt":
        path.write_text("{broken", encoding="utf-8")
    else:
        path.unlink()
    assert running_value(parameter_registry()["repeat_fail_threshold"], agent.config) == 7
    assert "当前运行值：7" in _show(agent.config, "repeat_fail_threshold")
    del agent.config.runtime_guard_policy
    assert running_value(parameter_registry()["repeat_fail_threshold"], agent.config) == "unknown"


# LLM: 只读是参数中心的权限边界，不代表宿主无策略覆盖能力；实际工具与聊天必须说明同一权威来源。
# 函数用途: 验证来源文案准确并保持真实写入拒绝及零台账副作用。
def test_guard_source_is_deployment_policy_and_parameter_writer_stays_readonly(tmp_path):
    agent = _agent(tmp_path)
    _save_policy(agent.home_paths.root, 1, 7)
    result = UserConfigTool(agent).execute({"action": "view", "key": "repeat_fail_threshold"})
    assert result.ok
    parameter = json.loads(result.output)["parameter"]
    assert "deployment_policy.json" in parameter["source_readonly"]
    assert "Python 默认" in parameter["source_readonly"]
    assert "随包默认" not in parameter["source_readonly"]
    assert parameter["source_readonly"] in _show(agent.config, "repeat_fail_threshold")
    report = changes.set_parameter("repeat_fail_threshold", "13",
                                   paths=changes.WritePaths(user_path=agent.config.config_path),
                                   origin=changes.ChangeOrigin("test"))
    assert (report["ok"], report["code"]) == (False, "PARAMETER_SOURCE_READ_ONLY")
    assert "部署" in report["error"] and "随包" not in report["error"]
    assert changes.parameter_history(user_path=agent.config.config_path) == []


# LLM: 常用名单是产品显式决策，只校验两个错位键的归属和原有常用总量，不以新增键凑数。
# 函数用途: 验证 HTTP 超时归常用而输出外置阈值归高级。
def test_http_timeout_and_externalization_have_the_correct_tiers():
    registry = parameter_registry()
    assert registry["tool_http_timeout"].tier == "common"
    assert registry["tool_output_externalize_min_chars"].tier == "advanced"
    assert len(COMMON_KEYS) == 14


# LLM: 现存目录包含两项高级池容量及宿主拓扑；只恢复拓扑部署归属，不搬无关键凑精确计数。
# 函数用途: 核证当前目录的精确分层、新池与拓扑边界、部署守卫拆分和后台身份默认上限。
def test_listed_parameter_classification_has_exact_counts():
    registry = listed_parameters()
    # GAP158 高级参数及 CAT1/CAT3 部署参数已登记；精确计数须包含这些正式目录项。
    assert Counter(spec.tier for spec in registry.values()) == {"common": 14, "advanced": 208, "deployment": 146}
    assert sum(spec.source == "runtime_guard" for spec in registry.values()) == 23
    assert len(DEPLOYMENT_ONLY_KEYS) == 123
    assert registry.keys() >= DEPLOYMENT_ONLY_KEYS
    assert len(registry) == 368
    for key in ("gateway_http_control_lane_workers", "gateway_native_stream_max"):
        assert registry[key].tier == "advanced" and registry[key].writable and registry[key].user_editable
    topology = registry["gateway_worker_process_count"]
    assert topology.tier == "deployment" and topology.default == 0
    assert not topology.writable and not topology.user_editable
    assert registry["max_background_agents"].default == AgentConfig().max_background_agents == 8
    assert registry["max_background_agents"].tier == "deployment"
    assert not registry["max_background_agents"].writable and not registry["max_background_agents"].user_editable
    assert registry["im_topic_aggregation_enabled"].tier == "advanced"
    assert registry["im_progress_cards_enabled"].tier == "advanced"
    assert len(COMMON_KEYS) == len(set(COMMON_KEYS))


# LLM: 完整回执会重新加载已保存主配置，但守卫必须仍来自原运行快照；无快照不能把重读得到的默认对象冒充运行。
# 函数用途: 验证完整脱敏回执不丢失原冻结守卫，也不借重读磁盘生成假运行值。
def test_full_config_receipt_preserves_original_guard_snapshot(tmp_path):
    agent = _agent(tmp_path)
    _save_policy(agent.home_paths.root, 1, 7)
    agent.config.runtime_guard_policy = runtime_guard_policy(trusted_host_paths=agent.home_paths)
    _save_policy(agent.home_paths.root, 2, 13)
    receipt = _full_config_receipt(agent.config.config_path, None, agent_config=agent.config)
    assert receipt["repeat_fail_threshold"] == "7"
    del agent.config.runtime_guard_policy
    assert _full_config_receipt(agent.config.config_path, None, agent_config=agent.config)["repeat_fail_threshold"] == "unknown"


# LLM: 旧压缩触发键已经退役，属性连读取都不允许；新键缺失只采用正式默认值80。
# 类用途: 捕获旧 fallback 被 eager getattr 意外求值的测试配置。
class _NoLegacyCompactConfig:
    memory_compact_trigger_percent = 85

    # LLM: 仅退役键访问立即失败，其余属性照普通缺失处理，避免测试桩替代真实解析逻辑。
    # 函数用途: 将任何对已删除压缩别名的访问变成可定位的测试失败。
    def __getattr__(self, name):
        if name == "memory_compact_auto_trigger_percent":
            raise AssertionError("不得读取旧压缩触发键")
        raise AttributeError(name)


# LLM: 既覆盖新旧属性同时存在时的 eager fallback，也覆盖只剩旧属性时不能继续采用旧值。
# 函数用途: 验证线程压缩比例不再读取被删除的旧键。
def test_compact_threshold_never_reads_the_deleted_alias():
    result = resolve_thread_compact_policy(SimpleNamespace(config=_NoLegacyCompactConfig()), use_frozen=False)
    assert result["effective_trigger_percent"] == 85
    legacy = SimpleNamespace(memory_compact_auto_trigger_percent=93)
    result = resolve_thread_compact_policy(SimpleNamespace(config=legacy), use_frozen=False)
    assert result["effective_trigger_percent"] == 80
