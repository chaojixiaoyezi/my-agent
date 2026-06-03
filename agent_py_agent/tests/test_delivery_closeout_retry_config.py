from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.agent_core.delivery_closeout.config import load_delivery_closeout_config


def test_delivery_closeout_retry_config_defaults_to_unlimited_rework():
    config = load_delivery_closeout_config(Path("/missing/runtime_guard_config.yaml"))

    assert config.invalid_artifacts_retry_limit == 0
    assert config.missing_artifacts_retry_limit == 0


def test_delivery_closeout_retry_config_accepts_zero_as_unlimited(tmp_path: Path):
    path = tmp_path / "runtime_guard_config.yaml"
    path.write_text(
        "\n".join(
            [
                "invalid_artifacts_retry_limit: 0",
                "missing_artifacts_retry_limit: 0",
            ]
        ),
        encoding="utf-8",
    )

    config = load_delivery_closeout_config(path)

    assert config.invalid_artifacts_retry_limit == 0
    assert config.missing_artifacts_retry_limit == 0


def test_delivery_closeout_retry_config_invalid_values_fall_back(tmp_path: Path):
    path = tmp_path / "runtime_guard_config.yaml"
    path.write_text(
        "invalid_artifacts_retry_limit: many\nmissing_artifacts_retry_limit: []\n",
        encoding="utf-8",
    )

    config = load_delivery_closeout_config(path)

    assert config.invalid_artifacts_retry_limit == 0
    assert config.missing_artifacts_retry_limit == 0
