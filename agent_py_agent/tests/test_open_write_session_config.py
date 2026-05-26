from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.agent_core.open_write_session_config import (
    load_open_write_session_config,
)


# LLM: open write-session reminder config should default to a small non-blocking cadence.
# 函数用途: 验证未配置时默认每 3 个未处理 open session 的模型回合提醒一次。
def test_open_write_session_config_defaults_to_three_turn_reminders():
    config = load_open_write_session_config(Path("/missing/runtime_guard_config.yaml"))

    assert config.reminder_interval == 3


# LLM: invalid reminder values should not disable staged-write reminders by accident.
# 函数用途: 验证 0、负数、非数字都回退默认 3；这个门没有“按次数阻断”的 0 语义。
def test_open_write_session_config_invalid_values_fall_back(tmp_path: Path):
    path = tmp_path / "runtime_guard_config.yaml"
    path.write_text("open_write_session_reminder_interval: 0\n", encoding="utf-8")
    assert load_open_write_session_config(path).reminder_interval == 3

    path.write_text("open_write_session_reminder_interval: -2\n", encoding="utf-8")
    assert load_open_write_session_config(path).reminder_interval == 3

    path.write_text("open_write_session_reminder_interval: many\n", encoding="utf-8")
    assert load_open_write_session_config(path).reminder_interval == 3


# LLM: the old short key remains accepted for local overrides while the shared file uses a clear name.
# 函数用途: 验证旧 reminder_interval 字段仍可用于显式测试/部署覆盖，不破坏已有本地配置。
def test_open_write_session_config_accepts_legacy_override_key(tmp_path: Path):
    path = tmp_path / "runtime_guard_config.yaml"
    path.write_text("reminder_interval: 5\n", encoding="utf-8")

    assert load_open_write_session_config(path).reminder_interval == 5
