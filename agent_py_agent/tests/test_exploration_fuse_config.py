from __future__ import annotations

from pathlib import Path


def test_load_exploration_fuse_config_from_shared_runtime_guard_file(tmp_path: Path):
    from agent_py_agent.agent.agent_core.exploration_fuse_config import load_exploration_fuse_config

    path = tmp_path / "runtime_guard_config.yaml"
    path.write_text(
        "\n".join(
            [
                "round_threshold: 0",
                "unlimited_hint_rounds: [25, 75, 125]",
                "local_progress_unlimited_hint_interval: 12",
            ]
        ),
        encoding="utf-8",
    )

    config = load_exploration_fuse_config(path)

    assert config.round_threshold == 0
    assert config.unlimited_hint_rounds == (25, 75, 125)
    assert config.local_progress_unlimited_hint_interval == 12


def test_load_exploration_fuse_config_invalid_values_fall_back(tmp_path: Path):
    from agent_py_agent.agent.agent_core.exploration_fuse_config import (
        DEFAULT_EXPLORATION_FUSE_ROUND_THRESHOLD,
        DEFAULT_UNLIMITED_HINT_ROUNDS,
        load_exploration_fuse_config,
    )

    path = tmp_path / "runtime_guard_config.yaml"
    path.write_text(
        "round_threshold: many\nunlimited_hint_rounds: [bad]\n",
        encoding="utf-8",
    )

    config = load_exploration_fuse_config(path)

    assert config.round_threshold == DEFAULT_EXPLORATION_FUSE_ROUND_THRESHOLD
    assert config.unlimited_hint_rounds == DEFAULT_UNLIMITED_HINT_ROUNDS


def test_load_exploration_fuse_config_local_progress_defaults():
    from agent_py_agent.agent.agent_core.exploration_fuse_config import load_exploration_fuse_config

    config = load_exploration_fuse_config(Path("/missing/runtime_guard_config.yaml"))

    assert config.local_progress_unlimited_hint_interval == 10


def test_runtime_guard_policy_records_values_and_sources(tmp_path: Path):
    from agent_py_agent.agent.settings.runtime_guard_config import runtime_guard_policy

    path = tmp_path / "runtime_guard_config.yaml"
    path.write_text(
        "repeat_fail_threshold: 14\nterminal_block_enabled: true\n",
        encoding="utf-8",
    )

    policy = runtime_guard_policy(path, overrides={"repeat_fail_threshold": 18})
    snapshot = policy.snapshot(task_id="task-1", run_id="run-1")

    assert policy.int_value("repeat_fail_threshold", 10) == 18
    assert policy.bool_value("terminal_block_enabled", False) is True
    assert snapshot["task_id"] == "task-1"
    assert snapshot["run_id"] == "run-1"
    assert snapshot["sources"]["terminal_block_enabled"] == str(path)
    assert snapshot["sources"]["repeat_fail_threshold"] == "runtime_override"


def test_runtime_guard_policy_bool_values_require_machine_tokens():
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    policy = RuntimeGuardPolicy(
        values={
            "explicit_true": "true",
            "explicit_false": "false",
            "ordinary_on": "on",
            "ordinary_off": "off",
        },
        sources={},
    )

    assert policy.bool_value("explicit_true", False) is True
    assert policy.bool_value("explicit_false", True) is False
    assert policy.bool_value("ordinary_on", False) is False
    assert policy.bool_value("ordinary_off", True) is True
