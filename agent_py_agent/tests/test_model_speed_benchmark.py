"""单元测试：model_speed benchmark 基准测试逻辑"""

from __future__ import annotations

from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.model_speed.benchmark import run_speed_benchmark
from agent_py_agent.agent.model_speed.models import SpeedProfile, SpeedSample


class TestSpeedSample:
    """测试 SpeedSample 数据类"""

    def test_basic_creation(self):
        """验证基本创建"""
        sample = SpeedSample(
            input_tokens=1000,
            output_tokens=500,
            latency_seconds=1.5,
        )
        assert sample.input_tokens == 1000
        assert sample.output_tokens == 500
        assert sample.latency_seconds == 1.5

    def test_total_tokens(self):
        """验证总 token 数计算"""
        sample = SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0)
        assert sample.total_tokens() == 1500

    def test_tokens_per_second(self):
        """验证每秒 token 数计算"""
        sample = SpeedSample(input_tokens=600, output_tokens=600, latency_seconds=1.2)
        tps = sample.tokens_per_second()
        assert tps == 1000.0  # 1200 / 1.2

    def test_zero_latency_gives_high_tps(self):
        """验证零延迟返回极高 tps（max 防止除零）"""
        sample = SpeedSample(input_tokens=100, output_tokens=100, latency_seconds=0.0)
        # max(0.001, 0.0) = 0.001, so 200 / 0.001 = 200000
        assert sample.tokens_per_second() > 0

    def test_zero_tokens_gives_zero_tps(self):
        """验证零 token 返回零 tps"""
        sample = SpeedSample(input_tokens=0, output_tokens=0, latency_seconds=1.0)
        assert sample.tokens_per_second() == 0.0


class TestSpeedProfile:
    """测试 SpeedProfile 数据类"""

    def test_basic_creation(self):
        """验证基本创建"""
        profile = SpeedProfile(
            backend="openai",
            model="gpt-4",
            tested_at="2026-05-01T10:00:00Z",
            samples=[],
        )
        assert profile.backend == "openai"
        assert profile.model == "gpt-4"

    def test_default_interpolation_method(self):
        """验证默认插值方法"""
        profile = SpeedProfile()
        assert profile.interpolation_method == "log_linear"

    def test_to_dict(self):
        """验证字典序列化"""
        profile = SpeedProfile(
            backend="anthropic",
            model="claude-3",
            tested_at="2026-05-01",
            samples=[
                SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            ],
            interpolation_method="log_linear",
        )
        d = profile.to_dict()
        assert d["backend"] == "anthropic"
        assert d["model"] == "claude-3"
        assert len(d["samples"]) == 1

    def test_from_dict(self):
        """验证从字典创建"""
        data = {
            "backend": "openai",
            "model": "gpt-3.5",
            "tested_at": "2026-05-01",
            "samples": [
                {"input_tokens": 2000, "output_tokens": 1000, "latency_seconds": 2.0},
            ],
            "interpolation_method": "log_linear",
        }
        profile = SpeedProfile.from_dict(data)
        assert profile.backend == "openai"
        assert profile.model == "gpt-3.5"
        assert len(profile.samples) == 1

    def test_from_dict_with_defaults(self):
        """验证从字典创建使用默认值"""
        data = {"backend": "test"}
        profile = SpeedProfile.from_dict(data)
        assert profile.model == ""
        assert profile.interpolation_method == "log_linear"

    def test_from_dict_empty_samples(self):
        """验证空 samples 处理"""
        data = {"backend": "test", "samples": []}
        profile = SpeedProfile.from_dict(data)
        assert profile.samples == []


class TestSpeedProfileInterpolation:
    """测试 SpeedProfile 插值计算"""

    def test_empty_samples_returns_default(self):
        """验证空样本返回默认值 30.0"""
        profile = SpeedProfile(backend="test", model="test", samples=[])
        result = profile.interpolate(input_tokens=1000, output_tokens=500)
        assert result == 30.0

    def test_exact_match_returns_sample_latency(self):
        """验证精确匹配返回样本延迟"""
        sample = SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=2.0)
        profile = SpeedProfile(backend="test", model="test", samples=[sample])

        result = profile.interpolate(input_tokens=1000, output_tokens=500)
        assert result == 2.0

    def test_interpolation_between_samples(self):
        """验证样本间插值"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=3.0),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)

        result = profile.interpolate(input_tokens=1500, output_tokens=750)
        # 应该介于 1.0 和 3.0 之间
        assert 1.0 < result < 3.0

    def test_below_lowest_sample_returns_lowest(self):
        """验证低于最小样本时返回最小样本延迟"""
        sample = SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=4.0)
        profile = SpeedProfile(backend="test", model="test", samples=[sample])

        result = profile.interpolate(input_tokens=500, output_tokens=250)
        assert result == 4.0

    def test_above_highest_sample_returns_highest(self):
        """验证高于最大样本时返回最大样本延迟"""
        sample = SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=2.0)
        profile = SpeedProfile(backend="test", model="test", samples=[sample])

        result = profile.interpolate(input_tokens=5000, output_tokens=2000)
        assert result == 2.0

    def test_log_linear_interpolation(self):
        """验证对数线性插值"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            SpeedSample(input_tokens=10000, output_tokens=5000, latency_seconds=10.0),
        ]
        profile = SpeedProfile(
            backend="test",
            model="test",
            samples=samples,
            interpolation_method="log_linear",
        )

        result = profile.interpolate(input_tokens=5500, output_tokens=2750)
        # 应该在 1.0 和 10.0 之间
        assert 1.0 < result < 10.0

    def test_simple_linear_interpolation(self):
        """验证简单线性插值"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=2.0),
        ]
        profile = SpeedProfile(
            backend="test",
            model="test",
            samples=samples,
            interpolation_method="linear",
        )

        result = profile.interpolate(input_tokens=1500, output_tokens=750)
        # 应该在 1.0 和 2.0 之间
        assert 1.0 < result < 2.0


class TestSpeedProfileEdgeCases:
    """测试 SpeedProfile 边界情况"""

    def test_single_sample(self):
        """验证单个样本"""
        sample = SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=2.0)
        profile = SpeedProfile(backend="test", model="test", samples=[sample])

        # 任何查询都应该返回这个样本的延迟
        assert profile.interpolate(500, 250) == 2.0
        assert profile.interpolate(2000, 1000) == 2.0

    def test_identical_samples(self):
        """验证相同样本"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=2.0),
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=2.0),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)

        result = profile.interpolate(1000, 500)
        assert result == 2.0

    def test_zero_latency_samples(self):
        """验证零延迟样本"""
        sample = SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=0.0)
        profile = SpeedProfile(backend="test", model="test", samples=[sample])

        result = profile.interpolate(1000, 500)
        assert result == 0.0


class TestSpeedSampleEdgeCases:
    """测试 SpeedSample 边界情况"""

    def test_large_token_counts(self):
        """验证大 token 计数"""
        sample = SpeedSample(input_tokens=100000, output_tokens=50000, latency_seconds=30.0)
        assert sample.total_tokens() == 150000
        assert sample.tokens_per_second() > 0

    def test_small_latency(self):
        """验证小延迟"""
        sample = SpeedSample(input_tokens=100, output_tokens=50, latency_seconds=0.001)
        tps = sample.tokens_per_second()
        assert tps > 0

    def test_very_large_latency(self):
        """验证大延迟"""
        sample = SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=3600.0)
        tps = sample.tokens_per_second()
        assert tps < 1.0