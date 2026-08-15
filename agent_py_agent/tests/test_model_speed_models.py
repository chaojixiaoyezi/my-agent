"""单元测试：model_speed models 速度模型数据结构"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.model_speed.models import SpeedProfile, SpeedSample


class TestSpeedSampleModel:
    """测试 SpeedSample 模型"""

    def test_create_with_all_fields(self):
        """验证创建带所有字段的 SpeedSample"""
        sample = SpeedSample(
            input_tokens=1000,
            output_tokens=500,
            latency_seconds=1.5,
        )
        assert sample.input_tokens == 1000
        assert sample.output_tokens == 500
        assert sample.latency_seconds == 1.5

    def test_create_with_defaults(self):
        """验证创建带默认值的 SpeedSample"""
        sample = SpeedSample()
        assert sample.input_tokens == 0
        assert sample.output_tokens == 0
        assert sample.latency_seconds == 0.0


class TestSpeedProfileModel:
    """测试 SpeedProfile 模型"""

    def test_create_minimal(self):
        """验证创建最小 SpeedProfile"""
        profile = SpeedProfile()
        assert profile.backend == ""
        assert profile.model == ""
        assert profile.samples == []
        assert profile.interpolation_method == "log_linear"

    def test_create_full(self):
        """验证创建完整 SpeedProfile"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=2.0),
        ]
        profile = SpeedProfile(
            backend="openai",
            model="gpt-4",
            tested_at="2026-05-01T12:00:00Z",
            samples=samples,
            interpolation_method="log_linear",
        )
        assert profile.backend == "openai"
        assert profile.model == "gpt-4"
        assert len(profile.samples) == 2
        assert profile.tested_at == "2026-05-01T12:00:00Z"


class TestSpeedSampleMethods:
    """测试 SpeedSample 方法"""

    def test_total_tokens_adds_inputs_and_outputs(self):
        """验证 total_tokens 加法"""
        sample = SpeedSample(input_tokens=5000, output_tokens=3000)
        assert sample.total_tokens() == 8000

    def test_total_tokens_with_zero_inputs(self):
        """验证零输入的 total_tokens"""
        sample = SpeedSample(input_tokens=0, output_tokens=1000)
        assert sample.total_tokens() == 1000

    def test_total_tokens_with_zero_outputs(self):
        """验证零输出的 total_tokens"""
        sample = SpeedSample(input_tokens=1000, output_tokens=0)
        assert sample.total_tokens() == 1000

    def test_tokens_per_second_normal_case(self):
        """验证正常情况的 tps"""
        sample = SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=3.0)
        # 3000 tokens / 3 seconds = 1000 tps
        assert sample.tokens_per_second() == 1000.0

    def test_tokens_per_second_zero_latency(self):
        """验证零延迟的 tps（使用 max 防止除零）"""
        sample = SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=0.0)
        # max(0.001, 0.0) = 0.001, so 1500 / 0.001 = 1,500,000
        tps = sample.tokens_per_second()
        assert tps > 0

    def test_tokens_per_second_very_small_latency(self):
        """验证极小延迟的 tps"""
        sample = SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=0.001)
        tps = sample.tokens_per_second()
        # 1500 tokens / 0.001 second = 1,500,000 tps
        assert tps > 1000000


class TestSpeedProfileSerialization:
    """测试 SpeedProfile 序列化"""

    def test_to_dict_includes_all_fields(self):
        """验证 to_dict 包含所有字段"""
        profile = SpeedProfile(
            backend="anthropic",
            model="claude-3-sonnet",
            tested_at="2026-05-01T10:00:00Z",
            samples=[
                SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            ],
            interpolation_method="log_linear",
        )
        d = profile.to_dict()
        assert "backend" in d
        assert "model" in d
        assert "tested_at" in d
        assert "samples" in d
        assert "interpolation_method" in d

    def test_to_dict_samples_format(self):
        """验证 to_dict 样本格式"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.5),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)
        d = profile.to_dict()

        sample_dict = d["samples"][0]
        assert "input_tokens" in sample_dict
        assert "output_tokens" in sample_dict
        assert "latency_seconds" in sample_dict
        assert sample_dict["input_tokens"] == 1000

    def test_from_dict_creates_valid_profile(self):
        """验证 from_dict 创建有效 profile"""
        data = {
            "backend": "openai",
            "model": "gpt-4-turbo",
            "tested_at": "2026-05-01",
            "samples": [
                {"input_tokens": 5000, "output_tokens": 2000, "latency_seconds": 5.0},
            ],
            "interpolation_method": "log_linear",
        }
        profile = SpeedProfile.from_dict(data)
        assert profile.backend == "openai"
        assert profile.model == "gpt-4-turbo"
        assert len(profile.samples) == 1
        assert profile.samples[0].input_tokens == 5000

    def test_from_dict_with_missing_fields(self):
        """验证 from_dict 处理缺失字段"""
        data = {"backend": "test"}
        profile = SpeedProfile.from_dict(data)
        assert profile.model == ""
        assert profile.samples == []
        assert profile.interpolation_method == "log_linear"

    def test_roundtrip_serialization(self):
        """验证往返序列化"""
        original = SpeedProfile(
            backend="test-backend",
            model="test-model",
            tested_at="2026-05-01T12:00:00Z",
            samples=[
                SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
                SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=2.0),
            ],
            interpolation_method="log_linear",
        )

        d = original.to_dict()
        restored = SpeedProfile.from_dict(d)

        assert restored.backend == original.backend
        assert restored.model == original.model
        assert len(restored.samples) == len(original.samples)


class TestSpeedProfileInterpolationLogic:
    """测试 SpeedProfile 插值逻辑"""

    def test_empty_samples_returns_30_seconds(self):
        """验证空样本返回 30 秒默认值"""
        profile = SpeedProfile(backend="test", model="test", samples=[])
        result = profile.interpolate(input_tokens=1000, output_tokens=500)
        assert result == 30.0

    def test_single_sample_returns_sample_latency(self):
        """验证单样本返回样本延迟"""
        sample = SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=2.5)
        profile = SpeedProfile(backend="test", model="test", samples=[sample])

        # 任何查询都应该返回这个样本的延迟
        assert profile.interpolate(500, 250) == 2.5
        assert profile.interpolate(1500, 750) == 2.5

    def test_finds_surrounding_samples(self):
        """验证找到周围样本"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            SpeedSample(input_tokens=3000, output_tokens=1500, latency_seconds=3.0),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)

        result = profile.interpolate(input_tokens=2000, output_tokens=1000)
        # 应该在 1.0 和 3.0 之间
        assert 1.0 < result < 3.0

    def test_no_lower_sample_returns_upper_latency(self):
        """验证没有下限样本时返回上限延迟"""
        samples = [
            SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=4.0),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)

        result = profile.interpolate(input_tokens=500, output_tokens=250)
        assert result == 4.0

    def test_no_upper_sample_returns_lower_latency(self):
        """验证没有上限样本时返回下限延迟"""
        samples = [
            SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=4.0),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)

        result = profile.interpolate(input_tokens=5000, output_tokens=2500)
        assert result == 4.0

    def test_identical_total_tokens_returns_sample_latency(self):
        """验证总 token 数相同时返回样本延迟"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=2.0),
            SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=4.0),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)

        result = profile.interpolate(input_tokens=3000, output_tokens=1500)
        assert result == 4.0

    def test_log_linear_method(self):
        """验证对数线性插值方法"""
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
        # 在对数空间中线性插值
        assert 1.0 < result < 10.0

    def test_simple_linear_method(self):
        """验证简单线性插值方法"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=2.0),
        ]
        profile = SpeedProfile(
            backend="test",
            model="test",
            samples=samples,
            interpolation_method="simple_linear",
        )

        result = profile.interpolate(input_tokens=1500, output_tokens=750)
        # 线性插值应该得到 1.5
        assert 1.0 < result < 2.0


class TestSpeedSampleEdgeCases:
    """测试 SpeedSample 边界情况"""

    def test_all_zeros(self):
        """验证全零情况"""
        sample = SpeedSample(input_tokens=0, output_tokens=0, latency_seconds=0.0)
        assert sample.total_tokens() == 0
        assert sample.tokens_per_second() == 0.0

    def test_very_large_numbers(self):
        """验证极大数字"""
        sample = SpeedSample(
            input_tokens=1_000_000,
            output_tokens=500_000,
            latency_seconds=3600.0,
        )
        tps = sample.tokens_per_second()
        # 1,500,000 / 3600 ≈ 416.67
        assert tps > 400 and tps < 500

    def test_fractional_latency(self):
        """验证分数延迟"""
        sample = SpeedSample(input_tokens=100, output_tokens=50, latency_seconds=0.25)
        tps = sample.tokens_per_second()
        # 150 / 0.25 = 600
        assert tps == 600.0


class TestSpeedProfileEdgeCases:
    """测试 SpeedProfile 边界情况"""

    def test_all_samples_have_same_tokens(self):
        """验证所有样本 token 数相同"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=2.0),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)

        result = profile.interpolate(input_tokens=1000, output_tokens=500)
        # 应该返回第一个匹配的样本延迟
        assert result == 1.0

    def test_samples_sorted_by_tokens(self):
        """验证样本按 token 数排序的情况"""
        samples = [
            SpeedSample(input_tokens=5000, output_tokens=2500, latency_seconds=5.0),
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            SpeedSample(input_tokens=3000, output_tokens=1500, latency_seconds=3.0),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)

        # 应该仍然正确插值
        result = profile.interpolate(input_tokens=2000, output_tokens=1000)
        assert 1.0 < result < 5.0

    def test_interpolation_with_zero_total_tokens(self):
        """验证零总 token 插值"""
        samples = [
            SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=1.0),
            SpeedSample(input_tokens=2000, output_tokens=1000, latency_seconds=2.0),
        ]
        profile = SpeedProfile(backend="test", model="test", samples=samples)

        result = profile.interpolate(input_tokens=0, output_tokens=0)
        # 应该使用最小样本的延迟（因为查询值 <= 所有样本）
        assert result == 1.0