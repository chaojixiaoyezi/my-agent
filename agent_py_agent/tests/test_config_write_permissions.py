"""配置与账本写回的权限回归（2026-10-02，ae 在 P18 发现生产 desktop.yaml 被参数写回从 600 放宽成 644）。

锁住三件事：写回保留目标原有权限；参数中心新建的配置与账本一律 0600；配置写回不再用固定的 "<name>.tmp"。
"""
from __future__ import annotations

import os
import stat

from agent_py_agent.agent.common.json_io import append_jsonl_capped, write_json_file_atomic
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.config_io import set_simple_yaml_raw, set_simple_yaml_value
from agent_py_agent.agent.settings.parameter_changes import ChangeOrigin

_ORIGIN = ChangeOrigin("test")


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _private_file(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def test_parameter_center_writes_keep_the_user_config_and_ledger_private(tmp_path):
    config = tmp_path / "desktop.yaml"
    _private_file(config, 'agent_name: "myagent"\n')
    paths = changes.WritePaths(user_path=config)

    first = changes.set_parameter("max_tokens", "32768", paths=paths, origin=_ORIGIN)
    assert first["ok"] and _mode(config) == 0o600
    ledger = changes.ledger_path(config)
    assert _mode(ledger) == 0o600
    # 第二次写会整文件重写账本（append_jsonl_capped 的原子替换），权限不能丢
    assert changes.set_parameter("max_tokens", "16384", paths=paths, origin=_ORIGIN)["ok"]
    assert (_mode(config), _mode(ledger)) == (0o600, 0o600)
    reset = changes.reset_parameter("max_tokens", paths=paths, origin=_ORIGIN)
    assert reset["ok"]
    assert changes.revert_change(reset["change_id"], paths=paths, origin=_ORIGIN)["ok"]
    assert (_mode(config), _mode(ledger)) == (0o600, 0o600)


def test_capability_config_created_by_the_parameter_center_is_private(tmp_path):
    config = tmp_path / "desktop.yaml"
    _private_file(config, 'agent_name: "myagent"\n')
    capability = tmp_path / "capability" / "capability_config.yaml"
    paths = changes.WritePaths(user_path=config, capability_path=capability)

    report = changes.set_parameter("subagent_heartbeat_timeout", "120", paths=paths, origin=_ORIGIN)

    assert report["ok"], report
    assert capability.is_file() and _mode(capability) == 0o600


def test_simple_yaml_writes_keep_whatever_mode_the_file_had(tmp_path):
    private = tmp_path / "private.yaml"
    _private_file(private, "a: 1\n")
    set_simple_yaml_value(private, "a", "2")
    set_simple_yaml_raw(private, "b", "3")
    assert _mode(private) == 0o600

    shared = tmp_path / "shared.yaml"
    shared.write_text("a: 1\n", encoding="utf-8")
    shared.chmod(0o640)
    set_simple_yaml_raw(shared, "a", "5")
    assert _mode(shared) == 0o640
    assert "a: 5" in shared.read_text(encoding="utf-8")


def test_config_write_does_not_depend_on_a_fixed_tmp_name(tmp_path):
    config = tmp_path / "desktop.yaml"
    _private_file(config, "a: 1\n")
    # 旧实现固定写 "desktop.yaml.tmp"：这里放一个同名目录，旧写法会直接失败
    (tmp_path / "desktop.yaml.tmp").mkdir()

    set_simple_yaml_raw(config, "a", "2")

    assert "a: 2" in config.read_text(encoding="utf-8")
    leftovers = sorted(item.name for item in tmp_path.iterdir() if item.name != "desktop.yaml.tmp")
    assert leftovers == ["desktop.yaml"]


def test_atomic_json_and_jsonl_rewrites_keep_the_target_mode(tmp_path):
    record = tmp_path / "state.json"
    _private_file(record, "{}\n")
    write_json_file_atomic(record, {"ok": True})
    assert _mode(record) == 0o600

    ledger = tmp_path / "changes.jsonl"
    _private_file(ledger, "")
    for index in range(3):
        append_jsonl_capped(ledger, {"index": index}, max_records=2)
    assert _mode(ledger) == 0o600
    assert ledger.read_text(encoding="utf-8").count("\n") == 2
