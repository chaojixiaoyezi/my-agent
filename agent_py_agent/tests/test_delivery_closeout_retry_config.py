from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.agent_core.delivery_closeout_config import load_delivery_closeout_config


# LLM: closeout retry budgets should be configurable and default to unlimited rework attempts.
# 函数用途: 验证交付验收失败预算不再散落在进展计算代码里，缺产物和坏产物默认都不按次数阻断。
def test_delivery_closeout_retry_config_defaults_to_unlimited_rework():
    config = load_delivery_closeout_config(Path("/missing/runtime_guard_config.yaml"))

    assert config.invalid_artifacts_retry_limit == 0
    assert config.missing_artifacts_retry_limit == 0


# LLM: zero retry budgets mean unlimited rework attempts instead of immediate blocking.
# 函数用途: 验证配置文件里 0 仍然表示不按次数阻断，和其他次数门语义一致。
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


# LLM: invalid config values should fall back to the default unlimited rework behavior.
# 函数用途: 验证配置写错时回退默认 0 次数限制，而不是解释成立即阻断。
def test_delivery_closeout_retry_config_invalid_values_fall_back(tmp_path: Path):
    path = tmp_path / "runtime_guard_config.yaml"
    path.write_text(
        "invalid_artifacts_retry_limit: many\nmissing_artifacts_retry_limit: []\n",
        encoding="utf-8",
    )

    config = load_delivery_closeout_config(path)

    assert config.invalid_artifacts_retry_limit == 0
    assert config.missing_artifacts_retry_limit == 0
