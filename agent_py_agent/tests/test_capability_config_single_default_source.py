# LLM: 钉住"capability 配置值只有 dataclass 默认值一个权威"：各调用点删掉自带兜底后，缺文件与坏文件都取 CapabilityConfig()
#   的值，文件里的非默认值照常生效。会话互通三个工具文件与 conversation/session_messaging.py 的兜底另行清理，不在这里。
# 模块用途: 对选包判定、子代理包入口开关、流式活动投影、活动提醒、失败自动拆分和看板巡检阈值逐处做缺文件/坏文件/文件值三种断言。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig

DEFAULTS = CapabilityConfig()
# 让统一入口返回 None 的格式错误（非法决策模式）；缺文件时统一入口返回默认实例。
MALFORMED = "decision_subagent_model_mode: bogus\n"
UNREADABLE = pytest.mark.parametrize("content", [None, MALFORMED], ids=["missing", "malformed"])


class _Stop(Exception):
    """替身在“开关放行后第一次取数据”时抛出，用来证明门控放行而不跑后续流程。"""


# 函数用途: 在临时目录放（或不放）一份 capability 配置，返回给 agent 用的路径。
def _config_path(tmp_path, content):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "capability_config.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    return path


@UNREADABLE
def test_package_selection_scope_follows_the_default_switch(tmp_path, content) -> None:
    from agent_py_agent.agent.capability.package_selection_scope import package_selection_scope
    from agent_py_agent.tests.test_capability_selection_scope import _fixture

    fixture = _fixture(tmp_path, enabled=True)
    if content is None:
        fixture.path.unlink()
    else:
        fixture.path.write_text(content, encoding="utf-8")

    assert DEFAULTS.enable_capability_package_selection is False
    assert package_selection_scope(fixture.agent, fixture.params) is None
    assert fixture.calls == []  # 默认关闭：在读工具与 Skill 快照之前就停下


def test_package_selection_scope_honours_the_file_switch(tmp_path) -> None:
    from agent_py_agent.agent.capability.package_selection_scope import package_selection_scope
    from agent_py_agent.tests.test_capability_selection_scope import _fixture

    fixture = _fixture(tmp_path, enabled=True)
    assert package_selection_scope(fixture.agent, fixture.params) is not None


@UNREADABLE
def test_subagent_entries_switch_uses_the_default(tmp_path, content) -> None:
    from agent_py_agent.agent.capability.subagent_package_entries import subagent_entries_enabled

    agent = SimpleNamespace(capability_config_path=_config_path(tmp_path, content))
    assert subagent_entries_enabled(agent) is DEFAULTS.enable_capability_package_selection


def test_subagent_entries_switch_honours_the_file(tmp_path) -> None:
    from agent_py_agent.agent.capability.subagent_package_entries import subagent_entries_enabled

    agent = SimpleNamespace(capability_config_path=_config_path(tmp_path, "enable_capability_package_selection: true\n"))
    assert subagent_entries_enabled(agent) is True


# 函数用途: 按给定的单调时钟连续推送同一流的数据块，返回每次真正发出的活动投影里的累计字符数。
def _stream_emissions(monkeypatch, tmp_path, content, times) -> list[int]:
    from agent_py_agent.agent.agent_core import tool_model_generation as generation

    emitted: list[int] = []
    monkeypatch.setattr(generation, "trace_runner_model_stream_active", lambda request: emitted.append(request.observed_chars))
    clock = iter(times)
    monkeypatch.setattr(generation.time, "monotonic", lambda: next(clock))
    agent = SimpleNamespace(capability_config_path=_config_path(tmp_path, content))
    params = SimpleNamespace(run_id="run-1", live_archive_state={"_current_model_turn_id": "turn-1"})
    request = generation.ModelGenerateParams(agent=agent, params=params, prompt="", tool_rounds=1)
    for _ in times:
        generation._publish_runner_model_stream_activity(request, "chunk", stream_kind="output")
    return emitted


@UNREADABLE
def test_stream_projection_uses_the_default_switch_and_interval(tmp_path, monkeypatch, content) -> None:
    interval = DEFAULTS.subagent_stream_activity_interval_seconds
    assert DEFAULTS.subagent_stream_activity_projection_enabled is True
    # 同一流：第二块早半秒被节流，第三块正好到默认间隔才发。
    times = (100.0, 100.0 + interval - 0.5, 100.0 + interval)
    assert _stream_emissions(monkeypatch, tmp_path, content, times) == [5, 15]


def test_stream_projection_honours_the_file_values(tmp_path, monkeypatch) -> None:
    slow = _stream_emissions(monkeypatch, tmp_path / "slow", "subagent_stream_activity_interval_seconds: 30\n",
                             (100.0, 115.0, 130.0))
    assert slow == [5, 15]
    off = _stream_emissions(monkeypatch, tmp_path / "off", "subagent_stream_activity_projection_enabled: false\n", (100.0,))
    assert off == []


@UNREADABLE
def test_activity_notices_use_the_default_switch(tmp_path, content) -> None:
    from agent_py_agent.agent.agent_core.runner.activity_diagnostics import observe_runner_activity

    loads: list[str] = []

    def load(run_id):
        loads.append(run_id)
        raise _Stop()

    worker = SimpleNamespace(capability_config_path=_config_path(tmp_path, content), subagents=SimpleNamespace(load=load))
    assert DEFAULTS.subagent_activity_notices_enabled is True
    with pytest.raises(_Stop):
        observe_runner_activity(worker, "run-1")
    assert loads == ["run-1"]


def test_activity_notices_honour_the_file_switch(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runner.activity_diagnostics import observe_runner_activity

    def load(_run_id):
        raise AssertionError("关闭时不应读取任务")

    path = _config_path(tmp_path, "subagent_activity_notices_enabled: false\n")
    worker = SimpleNamespace(capability_config_path=path, subagents=SimpleNamespace(load=load))
    assert observe_runner_activity(worker, "run-1") is None


@pytest.mark.parametrize("phase,field", [
    ("first_token_wait", "subagent_first_token_notice_seconds"),
    ("stream_idle", "subagent_stream_idle_notice_seconds"),
    ("tool_wait", "subagent_tool_wait_notice_seconds"),
    ("between_steps", "subagent_stream_idle_notice_seconds"),
    ("provider_retry", "subagent_first_token_notice_seconds"),
    ("unknown-phase", "subagent_stream_idle_notice_seconds"),
])
def test_activity_thresholds_come_only_from_capability_config(tmp_path, phase, field) -> None:
    from agent_py_agent.agent.agent_core.runner.activity_diagnostics import (
        activity_notice_threshold,
    )
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent

    missing = capability_config_for_agent(SimpleNamespace(capability_config_path=tmp_path / "missing.yaml"))
    assert activity_notice_threshold(missing, phase) == float(getattr(DEFAULTS, field))
    assert activity_notice_threshold(None, phase) == float(getattr(DEFAULTS, field))
    assert activity_notice_threshold(CapabilityConfig(**{field: 7}), phase) == 7.0


@UNREADABLE
def test_failure_auto_split_settings_use_the_defaults(tmp_path, content) -> None:
    from agent_py_agent.agent.agent_core.orchestration.dispatch.mixin import (
        _failure_auto_split_settings,
    )

    agent = SimpleNamespace(capability_config_path=_config_path(tmp_path, content))
    expected = (DEFAULTS.subagent_failure_auto_split_enabled, DEFAULTS.subagent_failure_split_max_depth)
    assert _failure_auto_split_settings(agent) == expected


def test_failure_auto_split_settings_honour_the_file(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.orchestration.dispatch.mixin import (
        _failure_auto_split_settings,
    )

    content = "subagent_failure_auto_split_enabled: true\nsubagent_failure_split_max_depth: 5\n"
    agent = SimpleNamespace(capability_config_path=_config_path(tmp_path, content))
    assert _failure_auto_split_settings(agent) == (True, 5)


def test_due_check_settings_use_the_defaults_without_a_config() -> None:
    from agent_py_agent.agent.subagents.services.board.service import due_check_settings

    settings = due_check_settings(None, 12.0)
    assert settings.now == 12.0
    assert (settings.heartbeat_timeout, settings.run_timeout, settings.no_progress_attempt_limit) == (
        DEFAULTS.subagent_heartbeat_timeout, DEFAULTS.subagent_run_timeout, DEFAULTS.subagent_no_progress_attempt_limit)


def test_due_check_settings_honour_the_given_config() -> None:
    from agent_py_agent.agent.subagents.services.board.service import due_check_settings

    config = CapabilityConfig(subagent_heartbeat_timeout=5, subagent_run_timeout=6, subagent_no_progress_attempt_limit=7)
    settings = due_check_settings(config, 1.0)
    assert (settings.heartbeat_timeout, settings.run_timeout, settings.no_progress_attempt_limit) == (5, 6, 7)
