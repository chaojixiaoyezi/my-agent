# LLM: PARAM-W0 只验配置权威、参数权限及正式写入，不将配置存在外推为消费者接线或部署成功。
# 模块用途: 锁定15键默认、类型范围、联合约束和角色数量映射事务。
from __future__ import annotations

import ast
import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.config import AgentConfig, load_config, normalize_agent_config
from agent_py_agent.agent.settings.config_io import load_simple_yaml
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import packaged_config_path

DEFAULTS = {
    "gateway_http_listen_backlog": 128,
    "gateway_http_max_request_workers": 32,
    "gateway_http_max_outstanding_requests": 256,
    "gateway_http_socket_timeout_seconds": 5.0,
    "gateway_http_queue_timeout_seconds": 2.0,
    "gateway_http_birth_gate_timeout_seconds": 1.0,
    "curator_daily_quota": 5000,
    "runner_role_limits": {},
    "gateway_submission_timeout_cap_seconds": 30.0,
    "retention_scan_slice_entries": 128,
    "retention_scan_slice_bytes": 8388608,
    "retention_scan_slice_ms": 25,
    "memory_reference_pin_chunk_bytes": 65536,
    "reasoning_probe_max_output_tokens": 40000,
    "reasoning_probe_timeout_seconds": 180.0,
}
STRATEGY_KEYS = {
    "runner_role_limits", "gateway_submission_timeout_cap_seconds",
    "reasoning_probe_max_output_tokens", "reasoning_probe_timeout_seconds",
}
HOST_KEYS = DEFAULTS.keys() - STRATEGY_KEYS
SCALAR_KEYS = [key for key, value in DEFAULTS.items() if type(value) in (int, float)]
INVALID_NUMBERS = [
    ("gateway_http_listen_backlog", 0), ("gateway_http_listen_backlog", 65536),
    ("gateway_http_max_request_workers", 0), ("gateway_http_max_request_workers", 1025),
    ("gateway_http_max_outstanding_requests", 0),
    ("gateway_http_socket_timeout_seconds", 0.09), ("gateway_http_socket_timeout_seconds", 301),
    ("gateway_http_queue_timeout_seconds", 0.001), ("gateway_http_queue_timeout_seconds", 301),
    ("gateway_http_birth_gate_timeout_seconds", 0.001), ("gateway_http_birth_gate_timeout_seconds", 31),
    ("curator_daily_quota", -1),
    ("gateway_submission_timeout_cap_seconds", 0.09), ("gateway_submission_timeout_cap_seconds", 301),
    ("retention_scan_slice_entries", 0), ("retention_scan_slice_bytes", 1023),
    ("retention_scan_slice_ms", 0), ("memory_reference_pin_chunk_bytes", 1023),
    ("reasoning_probe_max_output_tokens", 0), ("reasoning_probe_timeout_seconds", 0),
]
INVALID_ROLE_LIMITS = [
    [], None, "worker=1", {"worker": True}, {"worker": -1}, {"worker": 1.0},
    {"worker": "1"}, {"worker": None}, {1: 2}, {"": 1}, {"bad role": 1},
    {"worker\nrole": 1}, {"Worker": 1, "worker": 2}, {"worker": float("inf")},
]


# LLM: 在构造新字段前先断言 schema 存在，红测必须指向缺失契约而不是导入或拼写异常。
# 函数用途: 报告尚未加入配置类的任务字段。
def _require_schema():
    assert DEFAULTS.keys() <= {item.name for item in fields(AgentConfig)}


# LLM: 随包文件在同一测试版本不变，只加载一次；原始mini-YAML保留小数字符串，正式loader负责转换。
# 函数用途: 冻结同一版本的默认配置观察点，避免每个字段重复读盘。
@pytest.fixture(scope="module")
def defaults_snapshot():
    return load_simple_yaml(packaged_config_path()), AgentConfig(), load_config(packaged_config_path()), parameter_registry()


# LLM: 原始token按mini-YAML契约逐值比对，正式构造/loader/registry严格比较数值类型，不用raw token冒充运行值。
# 函数用途: 核对15键定义、默认类型、中文说明和重载语义。
@pytest.mark.parametrize("key,expected", DEFAULTS.items())
def test_defaults_yaml_and_registry_agree(key, expected, defaults_snapshot):
    _require_schema()
    raw, constructed, loaded, registry = defaults_snapshot
    spec = registry[key]
    raw_expected = str(expected) if type(expected) is float else expected
    assert type(raw[key]) is type(raw_expected) and raw[key] == raw_expected
    for value in (getattr(constructed, key), getattr(loaded, key), spec.default):
        assert type(value) is type(expected)
        assert value == expected
    assert spec.value_type == type(expected).__name__
    assert spec.source == "agent" and spec.description
    assert any("\u4e00" <= char <= "\u9fff" for char in spec.description)
    assert "重启" in spec.description and spec.effect == "restart_gateway"
    if type(expected) in (int, float):
        assert spec.range


# LLM: 非法输入必须被正式归一入口识别，告警和有效值都不能悄悄漏掉新字段。
# 函数用途: 验证所有数值的下界、上界及安全回落。
@pytest.mark.parametrize("key,bad", INVALID_NUMBERS)
def test_invalid_ranges_warn_and_fall_back(key, bad):
    out, warnings = normalize_agent_config({key: bad})
    assert key in out
    assert out[key] == DEFAULTS[key]
    assert any(key in warning for warning in warnings)


# LLM: bool 不能冒充整数或超时，非有限数不能进入运行对象或破坏配置加载。
# 函数用途: 对全部新增数值验证坏类型、NaN 和无穷的拒绝与默认回落。
@pytest.mark.parametrize("key", SCALAR_KEYS)
@pytest.mark.parametrize("bad", [True, False, "bad", float("nan"), float("inf"), float("-inf")])
def test_invalid_numeric_types_are_bounded(key, bad):
    out, warnings = normalize_agent_config({key: bad})
    assert type(out[key]) is type(DEFAULTS[key])
    assert out[key] == DEFAULTS[key]
    assert any(key in warning for warning in warnings)


# LLM: YAML 的可解析数字字符串应沿原 coercion 入口归一，不改变字节、字符、token 与时间的单位。
# 函数用途: 验证字符串数字保留正确的数值类型。
@pytest.mark.parametrize("key", SCALAR_KEYS)
def test_numeric_strings_normalize_to_declared_type(key):
    out, warnings = normalize_agent_config({key: str(DEFAULTS[key])})
    assert out[key] == DEFAULTS[key] and type(out[key]) is type(DEFAULTS[key])
    assert not warnings


# LLM: 直接构造和 replace 不能绕过字段或联合约束，错误必须指向实际键名。
# 函数用途: 验证 dataclass 的严格范围校验。
@pytest.mark.parametrize("key,bad", INVALID_NUMBERS)
def test_direct_constructor_and_replace_reject_bad_ranges(key, bad):
    _require_schema()
    for construct in (lambda: AgentConfig(**{key: bad}), lambda: replace(AgentConfig(), **{key: bad})):
        with pytest.raises(ValueError, match=key):
            construct()


# LLM: 字段注解不足以拒绝 bool 和字符串，直接构造同样要走正式校验。
# 函数用途: 验证直接构造不能绕过数值的类型与有限性检查。
@pytest.mark.parametrize("key", SCALAR_KEYS)
@pytest.mark.parametrize("bad", [True, "1", float("nan"), float("inf")])
def test_direct_constructor_rejects_bad_numeric_types(key, bad):
    _require_schema()
    with pytest.raises(ValueError, match=key):
        AgentConfig(**{key: bad})


# LLM: 联合约束只保留请求线程数与总接纳容量的真实关系，不擅自引入其它统一上限。
# 函数用途: 验证非法组合在直接构造被拒绝、在 YAML 归一后保持有效并告警。
@pytest.mark.parametrize("values,marker", [
    ({"gateway_http_max_request_workers": 512, "gateway_http_max_outstanding_requests": 16}, "gateway_http_max_outstanding_requests"),
    ({"gateway_http_max_request_workers": 512}, "gateway_http_max_outstanding_requests"),
])
def test_joint_constraints(values, marker):
    _require_schema()
    with pytest.raises(ValueError, match=marker):
        AgentConfig(**values)
    out, warnings = normalize_agent_config(values)
    assert out["gateway_http_max_outstanding_requests"] >= out["gateway_http_max_request_workers"]
    assert any(marker in warning for warning in warnings)


# LLM: 所有有效边界保留原语义；curator quota 0 不限额，角色0禁止本批接纳。
# 函数用途: 检查有效极值、开放上限以及相互独立的角色映射默认。
def test_valid_boundaries_and_default_factory():
    _require_schema()
    values = {
        "gateway_http_listen_backlog": 65535, "gateway_http_max_request_workers": 1024,
        "gateway_http_max_outstanding_requests": 1024, "gateway_http_socket_timeout_seconds": 0.1,
        "gateway_http_queue_timeout_seconds": 0.01, "gateway_http_birth_gate_timeout_seconds": 0.01,
        "curator_daily_quota": 0, "runner_role_limits": {" Worker ": 0, "*": 3},
        "gateway_submission_timeout_cap_seconds": 300.0,
        "retention_scan_slice_entries": 1, "retention_scan_slice_bytes": 1024, "retention_scan_slice_ms": 1,
        "memory_reference_pin_chunk_bytes": 1024, "reasoning_probe_max_output_tokens": 1,
        "reasoning_probe_timeout_seconds": 0.000001,
    }
    config = AgentConfig(**values)
    expected = {**values, "runner_role_limits": {"worker": 0, "*": 3}}
    out, warnings = normalize_agent_config(values)
    assert not warnings
    for key, value in expected.items():
        assert getattr(config, key) == out[key] == value
    first, second = AgentConfig(), AgentConfig()
    first.runner_role_limits["worker"] = 1
    assert second.runner_role_limits == {}
    assert values["runner_role_limits"] == {" Worker ": 0, "*": 3}


# LLM: 角色数量只允许严格非负整数；整份非法映射回落且警告，不保留其中部分条目。
# 函数用途: 验证 YAML 和 dataclass 对映射类型、角色键、碰撞与值类型的统一校验。
@pytest.mark.parametrize("bad", INVALID_ROLE_LIMITS)
def test_role_limits_reject_invalid_mapping(bad):
    _require_schema()
    out, warnings = normalize_agent_config({"runner_role_limits": bad})
    assert out["runner_role_limits"] == {}
    assert any("runner_role_limits" in warning for warning in warnings)
    with pytest.raises(ValueError, match="runner_role_limits"):
        AgentConfig(runner_role_limits=bad)


# LLM: 参数权限不仅展示，还应在正式 writer 拒绝模型及认证用户局部覆写宿主容量。
# 函数用途: 验证11个容量/配额键模型只读，4个逐任务策略具备真实写路线。
def test_registry_permissions_and_host_write_refusal(tmp_path):
    registry = parameter_registry()
    assert DEFAULTS.keys() <= registry.keys()
    path = tmp_path / "user.yaml"
    path.write_text("agent_name: myagent\n", encoding="utf-8")
    before = path.read_bytes()
    for key in HOST_KEYS:
        spec = registry[key]
        assert not spec.writable and not spec.user_editable and spec.tier == "deployment"
        for authenticated in (False, True):
            if authenticated:
                with changes.user_settings_write_scope():
                    report = changes.set_parameter(key, DEFAULTS[key], paths=changes.WritePaths(path), origin=changes.ChangeOrigin("chat"))
            else:
                report = changes.set_parameter(key, DEFAULTS[key], paths=changes.WritePaths(path), origin=changes.ChangeOrigin("model"))
            assert report["ok"] is False and report["code"] == "PARAMETER_SOURCE_READ_ONLY"
    assert path.read_bytes() == before and not changes.ledger_path(path).exists()
    for key in STRATEGY_KEYS:
        assert registry[key].writable and registry[key].user_editable
    assert registry["runner_role_limits"].writer == "structured"


# LLM: 使用正式事务、加载器与台账验证策略写入；不是只断言字段存在或假回执。
# 函数用途: 验证4个策略的写入、读取和恢复默认。
@pytest.mark.parametrize("key,value", [
    ("runner_role_limits", {"Worker": 0, "reviewer": 3}),
    ("gateway_submission_timeout_cap_seconds", 17.5),
    ("reasoning_probe_max_output_tokens", 2000), ("reasoning_probe_timeout_seconds", 12.5),
])
def test_strategy_writer_round_trip(tmp_path, key, value):
    path = tmp_path / "user.yaml"
    path.write_text("agent_name: myagent\n", encoding="utf-8")
    paths, origin = changes.WritePaths(path), changes.ChangeOrigin("model")
    saved = changes.set_parameter(key, value, paths=paths, origin=origin)
    assert saved["ok"] is True, saved
    expected = {"worker": 0, "reviewer": 3} if key == "runner_role_limits" else value
    assert getattr(load_config(path), key) == expected
    assert saved["effect_when"] == "restart_gateway"
    assert len(changes.parameter_history(user_path=path)) == 1
    reset = changes.reset_parameter(key, paths=paths, origin=origin)
    assert reset["ok"] is True, reset
    assert getattr(load_config(path), key) == DEFAULTS[key]
    assert f"{key}:" not in path.read_text(encoding="utf-8")


# LLM: 严格 JSON 重复键、布尔数量、碰撞等失败不能生成半截 YAML 或台账。
# 函数用途: 检查角色映射的全部拒绝路径不写文件。
@pytest.mark.parametrize("bad", [*INVALID_ROLE_LIMITS, '{"worker":1,"worker":2}', '{"Worker":1,"worker":2}'])
def test_role_limit_writer_invalid_is_noop(tmp_path, bad):
    path = tmp_path / "user.yaml"
    path.write_text("agent_name: myagent\n", encoding="utf-8")
    before = path.read_bytes()
    report = changes.set_parameter("runner_role_limits", bad, paths=changes.WritePaths(path), origin=changes.ChangeOrigin("model"))
    assert report["ok"] is False and report["code"] == "PARAMETER_INVALID", report
    assert path.read_bytes() == before
    assert not changes.ledger_path(path).exists()


# LLM: JSON 文本入口与直接映射应同源，回滚按真实历史恢复上次值，不扩大写权限。
# 函数用途: 验证角色数量映射的 JSON 输入和回滚。
def test_role_limit_json_writer_and_revert(tmp_path):
    path = tmp_path / "user.yaml"
    path.write_text("runner_role_limits: {'worker': 1}\n", encoding="utf-8")
    paths, origin = changes.WritePaths(path), changes.ChangeOrigin("model")
    saved = changes.set_parameter("runner_role_limits", json.dumps({"Worker": 2, "*": 0}), paths=paths, origin=origin)
    assert saved["ok"] is True, saved
    assert load_config(path).runner_role_limits == {"worker": 2, "*": 0}
    reverted = changes.revert_change(saved["change_id"], paths=paths, origin=origin)
    assert reverted["ok"] is True, reverted
    assert load_config(path).runner_role_limits == {"worker": 1}


# LLM: 越界策略走正式预检拒绝，不依赖注册表文字范围或静默夹取。
# 函数用途: 验证标量策略无效候选不能发布或记账。
@pytest.mark.parametrize("key,value", [(key, value) for key, value in INVALID_NUMBERS if key in STRATEGY_KEYS])
def test_strategy_invalid_bounds_never_commit(tmp_path, key, value):
    path = tmp_path / "user.yaml"
    path.write_text("agent_name: myagent\n", encoding="utf-8")
    before = path.read_bytes()
    report = changes.set_parameter(key, value, paths=changes.WritePaths(path), origin=changes.ChangeOrigin("model"))
    assert report["ok"] is False, report
    assert path.read_bytes() == before and not changes.ledger_path(path).exists()


# LLM: 开关门禁只接受可静态解析的宿主集合；元数据相等性不能替代字面量约束，集合不能随新增字段自动扩权。
# 函数用途: 同时锁定宿主键的静态声明、十一键名单和配置元数据一致性。
def test_host_capacity_keys_are_static_and_match_schema():
    from agent_py_agent.agent.settings.parameter_registry import _HOST_CAPACITY_KEYS

    source = Path(__file__).resolve().parents[1] / "agent" / "settings" / "parameter_registry.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    declaration = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "_HOST_CAPACITY_KEYS"
                               for target in node.targets))
    assert isinstance(declaration, ast.Call)
    assert isinstance(declaration.func, ast.Name) and declaration.func.id == "frozenset"
    assert len(declaration.args) == 1 and not declaration.keywords
    assert isinstance(declaration.args[0], ast.Set)
    assert all(isinstance(item, ast.Constant) and isinstance(item.value, str)
               for item in declaration.args[0].elts)
    expected = {
        "gateway_http_listen_backlog", "gateway_http_max_request_workers",
        "gateway_http_max_outstanding_requests", "gateway_http_socket_timeout_seconds",
        "gateway_http_queue_timeout_seconds", "gateway_http_birth_gate_timeout_seconds",
        "curator_daily_quota", "retention_scan_slice_entries", "retention_scan_slice_bytes",
        "retention_scan_slice_ms", "memory_reference_pin_chunk_bytes",
    }
    assert expected == _HOST_CAPACITY_KEYS
    assert {f.name for f in fields(AgentConfig) if f.metadata.get("model_read_only")} == _HOST_CAPACITY_KEYS
