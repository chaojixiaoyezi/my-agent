from __future__ import annotations

"""Curator 模型调用超时随输入规模自适应放大(长文灌入需求③的真机缺陷修复)。

真机复现(2026-08-12, MiniMax-M2.7 anthropic 端点):3567 字长文灌入后,curator 固定
90s 超时连续两次 CURATOR_MODEL_TIMEOUT(13:58/14:07),放开到 600s 后真实耗时 164s 成功。
根因 = 固定 timeout_seconds 与输入规模/端点延迟不匹配。本测试锁定:
adaptive_timeout_seconds 的放大/封顶语义、extract_with_retries 使用放大后超时、
_lease_seconds 按 max_input_chars 保守上界覆盖自适应超时(防 lease 先过期)。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store.curator import _lease_seconds
from agent_py_agent.agent.memory_store.curator_backend import adaptive_timeout_seconds
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig


def _config(**overrides) -> MemoryCuratorConfig:
    values = {
        "interval_seconds": 60,
        "turn_threshold": 10,
        "batch_message_limit": 80,
        "max_input_chars": 40000,
        "timeout_seconds": 90,
        "max_retries": 1,
        "daily_finalize_hour": 23,
    }
    values.update(overrides)
    return MemoryCuratorConfig(**values)


def test_adaptive_timeout_short_input_keeps_base() -> None:
    assert adaptive_timeout_seconds(90, 100) == 90
    assert adaptive_timeout_seconds(90, 1999) == 90


def test_adaptive_timeout_scales_with_prompt_length() -> None:
    # 每 2000 字符追加一个基础预算:7K 字符 ≈ 长文灌入实测 164s 场景
    assert adaptive_timeout_seconds(90, 2000) == 180
    assert adaptive_timeout_seconds(90, 7000) == 360
    assert adaptive_timeout_seconds(90, 12000) == 630


def test_adaptive_timeout_caps_at_multiplier() -> None:
    assert adaptive_timeout_seconds(90, 40000, max_multiplier=8) == 720
    assert adaptive_timeout_seconds(90, 100000) == 720
    # 自定义封顶参数生效
    assert adaptive_timeout_seconds(90, 20000, max_multiplier=4) == 360


def test_adaptive_timeout_zero_and_negative_safe() -> None:
    assert adaptive_timeout_seconds(90, 0) == 90
    assert adaptive_timeout_seconds(0, 5000) == 0  # 基础超时关闭则保持关闭


def test_extract_with_retries_uses_adaptive_timeout(monkeypatch) -> None:
    """长 prompt 时模型调用收到放大后的超时,而不是固定配置值。"""
    from agent_py_agent.agent.memory_store import curator_backend
    from agent_py_agent.agent.memory_store.curator_backend import extract_with_retries

    captured: dict[str, object] = {}

    def fake_call(backend, *, prompt, response_schema, timeout_seconds):
        captured["timeout_seconds"] = timeout_seconds
        captured["prompt_len"] = len(prompt)
        return SimpleNamespace(text='{"schema_version": "x"}')

    monkeypatch.setattr(curator_backend, "call_backend_with_timeout", fake_call)
    monkeypatch.setattr(curator_backend, "parse_curator_extraction", lambda _text: object())

    batch = SimpleNamespace(
        messages=(),
        audit_events=(),
        to_model_payload=lambda: {"messages": []},
    )
    monkeypatch.setattr(
        curator_backend,
        "curator_prompt",
        lambda _batch: "长" * 7000,  # 长文灌入量级
    )

    extract_with_retries(object(), _config(), batch)

    assert captured["prompt_len"] == 7000
    assert captured["timeout_seconds"] == 360  # 90 * (1 + 7000//2000)


def test_lease_seconds_covers_adaptive_worst_case() -> None:
    """lease 必须覆盖 max_input_chars 上限下的自适应超时 × 全部重试 + 缓冲。"""
    config = _config(timeout_seconds=90, max_retries=1)
    worst = adaptive_timeout_seconds(90, config.max_input_chars)
    assert _lease_seconds(config) == worst * 2 + 90

    # 短输入配置(现有测试语义)不受影响:max_input_chars 仍是保守上界
    small = _config(timeout_seconds=1, max_retries=0, max_input_chars=2000)
    assert _lease_seconds(small) == adaptive_timeout_seconds(1, 2000) * 1 + 90


def test_lease_seconds_keeps_busy_semantics_for_short_batches(tmp_path: Path) -> None:
    """短输入 run 仍以基础超时执行,lease 只是保守上界——不改变既有 busy/失败语义。"""
    from agent_py_agent.agent.memory_store.curator import MemoryCuratorService  # noqa: F401

    config = _config(timeout_seconds=2, max_retries=0, max_input_chars=40000)
    # 真实 run 中短输入使用 adaptive(2, len(prompt))==2,不会因 lease 变大而改变行为
    assert adaptive_timeout_seconds(2, 500) == 2
    assert _lease_seconds(config) == adaptive_timeout_seconds(2, 40000) * 1 + 90
