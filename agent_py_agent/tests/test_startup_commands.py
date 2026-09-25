"""startup_commands CLI 命令测试。

测试 daemon 命令和 startup recovery 相关功能。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.startup_recovery import (
    ActiveWorkSummary,
    _detect_active_tasks,
    format_active_work_summary,
)


def _daemon_args(tmp_path: Path) -> MagicMock:
    args = MagicMock()
    args.capability_config = str(tmp_path / "capability.yaml")
    args.skill_dir = None
    args.apply = True
    args.start_runners = True
    args.planner = True
    args.interval = 0
    args.max_runners = 2
    args.limit = 7
    args.max_cycles = 1
    args.reviewer = "reviewer"
    args.note = "daemon note"
    args.instruction = "runner note"
    args.max_cards = 3
    args.no_probe = True
    args.take_over_by = "owner"
    args.locked_file = ["a.py"]
    args.force_lock = True
    return args


def _daemon_agent(tmp_path: Path) -> MagicMock:
    mock_report = MagicMock()
    mock_report.summary = {"total": 0}
    mock_agent = MagicMock()
    mock_agent.config.daemon_mutate_state = False
    mock_agent.config.daemon_start_runners = False
    mock_agent.config.daemon_planner = False
    mock_agent.config.daemon_interval = 30
    mock_agent.config.daemon_max_runners = 1
    mock_agent.config.daemon_limit = 20
    mock_agent.config.daemon_max_cycles = 0
    mock_agent.config.daemon_reviewer = "parent-dispatch"
    mock_agent.config.daemon_runner_instruction = ""
    mock_agent.config.daemon_max_cards = 0
    mock_agent.config.daemon_probe = True
    mock_agent.watch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = tmp_path / "subs"
    return mock_agent


def _daemon_numbers(overrides: dict | None = None):
    from agent_py_agent.cli.daemon import DaemonNumberOptions

    data = {
        "interval": 60,
        "max_runners": 2,
        "limit": 10,
        "max_cycles": 5,
        "max_cards": 3,
    }
    data.update(overrides or {})
    return DaemonNumberOptions(**data)


class TestCmdDaemon:
    """测试 cmd_daemon 命令。"""

    def test_daemon_resolves_options(self, tmp_path: Path):
        """测试 daemon 选项解析。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_options, cmd_daemon

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.apply = False
        args.start_runners = False
        args.planner = False
        args.interval = 60
        args.max_runners = 1
        args.limit = 10
        args.max_cycles = 1  # 改为 1 避免持续运行
        args.reviewer = None
        args.note = None
        args.instruction = None
        args.max_cards = 0
        args.no_probe = False
        args.take_over_by = None
        args.locked_file = []
        args.force_lock = False
        args.skill_dir = None

        mock_agent = MagicMock()
        mock_agent.config.daemon_mutate_state = False
        mock_agent.config.daemon_start_runners = False
        mock_agent.config.daemon_planner = False
        mock_agent.config.daemon_interval = 60
        mock_agent.config.daemon_max_runners = 1
        mock_agent.config.daemon_limit = 10
        mock_agent.config.daemon_max_cycles = 1
        mock_agent.config.daemon_reviewer = None
        mock_agent.config.daemon_runner_instruction = None
        mock_agent.config.daemon_max_cards = 0
        mock_agent.config.daemon_probe = True

        # 直接测试 _resolve_daemon_options 函数
        options = _resolve_daemon_options(mock_agent, args)
        assert options.mutate_state is False
        assert options.interval == 60

    def test_cmd_daemon_passes_watch_params_bundle(self, tmp_path: Path):
        from agent_py_agent.agent.agent_core.orchestration.dispatch.params import WatchParams
        from agent_py_agent.cli.daemon import cmd_daemon

        args = _daemon_args(tmp_path)
        mock_agent = _daemon_agent(tmp_path)

        with (
            patch("agent_py_agent.cli.daemon.make_agent", return_value=mock_agent),
            patch("agent_py_agent.cli.daemon.load_capability_config", return_value=MagicMock()),
            patch("agent_py_agent.cli.daemon.make_capability_router", return_value=MagicMock()),
        ):
            assert cmd_daemon(args) == 0

        call_kwargs = mock_agent.watch_subagents.call_args.kwargs
        assert isinstance(call_kwargs["params"], WatchParams)
        assert call_kwargs["params"].start_runners is True
        assert call_kwargs["params"].advance is True
        assert call_kwargs["params"].max_runners == 2
        assert "apply" not in call_kwargs


def test_startup_recovery_preserves_active_task_detection_errors() -> None:
    agent = MagicMock()
    agent.subagents.board.build_board.side_effect = TypeError("bad board payload")
    summary = ActiveWorkSummary()

    _detect_active_tasks(agent, summary)
    text = format_active_work_summary(summary)

    assert summary.active_task_count == 0
    assert summary.detection_errors[0]["context"] == "startup_recovery.active_tasks"
    assert "状态读取有 1 个错误" in text
    assert "startup_recovery.active_tasks" in text


def test_gateway_startup_owns_stale_attempt_recovery() -> None:
    from agent_py_agent.cli.gateway_process import _recover_gateway_stale_attempts

    recover = MagicMock(return_value=["run-1", "run-2"])
    agent = MagicMock()
    agent.subagents.runtime_db.recover_stale_attempts = recover
    agent.subagents.runtime_db.unidentified_stale_attempts = MagicMock(return_value=[{"run_id": "run-legacy", "agent_run_id": "agentrun-x"}])

    result = _recover_gateway_stale_attempts(agent)

    assert result == {"run_ids": ["run-1", "run-2"], "count": 2, "error": None, "unidentified": ["run-legacy"]}
    recover.assert_called_once_with()


def test_gateway_stale_attempt_recovery_error_is_structured() -> None:
    from agent_py_agent.cli.gateway_process import _recover_gateway_stale_attempts

    agent = MagicMock()
    agent.subagents.runtime_db.recover_stale_attempts.side_effect = RuntimeError("broken")

    result = _recover_gateway_stale_attempts(agent)

    assert result["count"] == 0 and result["unidentified"] == []
    assert result["error"]["context"] == "gateway.startup.stale_attempts"


class TestResolveDaemonMaxRunners:
    """测试 _resolve_daemon_max_runners 辅助函数。"""

    def test_resolve_daemon_max_runners_auto(self):
        """测试 auto 值转换为 1。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_max_runners

        result = _resolve_daemon_max_runners("auto")
        assert result == 1

    def test_resolve_daemon_max_runners_empty_string(self):
        """测试空字符串转换为 1。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_max_runners

        result = _resolve_daemon_max_runners("")
        assert result == 1

    def test_resolve_daemon_max_runners_integer(self):
        """测试整数直接返回。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_max_runners

        result = _resolve_daemon_max_runners(5)
        assert result == 5

    def test_resolve_daemon_max_runners_invalid_string(self):
        """测试无效字符串抛出异常。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_max_runners

        with pytest.raises(ValueError):
            _resolve_daemon_max_runners("invalid")


class TestValidateDaemonNumbers:
    """测试 _validate_daemon_numbers 辅助函数。"""

    def test_validate_daemon_numbers_valid(self):
        """测试有效参数。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(_daemon_numbers())
        assert result == ""

    def test_validate_daemon_numbers_negative_interval(self):
        """测试负数 interval。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(_daemon_numbers({"interval": -1}))
        assert "不能小于 0" in result

    def test_validate_daemon_numbers_negative_max_runners(self):
        """测试负数 max_runners。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(_daemon_numbers({"max_runners": -1}))
        assert "不能小于 0" in result

    def test_validate_daemon_numbers_negative_limit(self):
        """测试负数 limit。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(_daemon_numbers({"limit": -1}))
        assert "不能小于 0" in result

    def test_validate_daemon_numbers_negative_max_cycles(self):
        """测试负数 max_cycles。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(_daemon_numbers({"max_cycles": -1}))
        assert "不能小于 0" in result

    def test_validate_daemon_numbers_negative_max_cards(self):
        """测试负数 max_cards。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(_daemon_numbers({"max_cards": -1}))
        assert "不能小于 0" in result


class TestResolveDaemonOptions:
    """测试 _resolve_daemon_options 函数。"""

    def test_resolve_daemon_options_start_runners_without_apply(self, tmp_path: Path):
        """测试 start_runners 必须和状态写回一起使用。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_options

        mock_agent = MagicMock()
        mock_agent.config.daemon_mutate_state = False
        mock_agent.config.daemon_start_runners = True
        mock_agent.config.daemon_planner = False
        mock_agent.config.daemon_interval = 60
        mock_agent.config.daemon_max_runners = 1
        mock_agent.config.daemon_limit = 10
        mock_agent.config.daemon_max_cycles = 0
        mock_agent.config.daemon_reviewer = None
        mock_agent.config.daemon_runner_instruction = None
        mock_agent.config.daemon_max_cards = 0
        mock_agent.config.daemon_probe = True

        args = MagicMock()
        args.apply = False
        args.start_runners = True
        args.planner = None
        args.interval = None
        args.max_runners = None
        args.limit = None
        args.max_cycles = None
        args.max_cards = None
        args.reviewer = None
        args.instruction = None
        args.no_probe = False

        with pytest.raises(ValueError, match="必须和.*一起使用"):
            _resolve_daemon_options(mock_agent, args)
