
from __future__ import annotations

"""model speed profiling data structures.

这里定义速度基准测试的数据结构。
SpeedProfile 包含后端、模型、测试样本和插值方法。
SpeedSample 是单个测点，记录输入 token、输出 token 和延迟。
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class SpeedSample:
    """单个速度测点。"""

    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0

    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def tokens_per_second(self) -> float:
        total = self.total_tokens()
        if total == 0:
            return 0.0
        return total / max(0.001, self.latency_seconds)


@dataclass
class SpeedProfile:
    """模型速度配置文件。"""

    backend: str = ""
    model: str = ""
    tested_at: str = ""
    samples: list[SpeedSample] = field(default_factory=list)
    interpolation_method: str = "log_linear"

    def to_dict(self) -> dict:
        """转换成字典，用于 JSON 序列化。"""
        return {
            "backend": self.backend,
            "model": self.model,
            "tested_at": self.tested_at,
            "samples": [
                {
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "latency_seconds": s.latency_seconds,
                }
                for s in self.samples
            ],
            "interpolation_method": self.interpolation_method,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SpeedProfile:
        """从字典创建实例。"""
        samples_data = data.get("samples", [])
        samples = [SpeedSample(**s) for s in samples_data]
        return cls(
            backend=data.get("backend", ""),
            model=data.get("model", ""),
            tested_at=data.get("tested_at", ""),
            samples=samples,
            interpolation_method=data.get("interpolation_method", "log_linear"),
        )

    def interpolate(self, input_tokens: int, output_tokens: int) -> float:
        """根据输入大小插值预估耗时（秒）。"""

        if not self.samples:
            return 30.0

        total_tokens = input_tokens + output_tokens

        lower, upper = _bounding_samples(self.samples, total_tokens)

        # 如果只有一个方向，使用最近的样本
        if lower is None:
            return upper.latency_seconds if upper else 30.0
        if upper is None:
            return lower.latency_seconds

        # 如果找到完全匹配的样本
        if lower.total_tokens() == upper.total_tokens():
            return lower.latency_seconds

        # 对数线性插值
        if self.interpolation_method == "log_linear":
            lower_log = max(1.0, lower.total_tokens())
            upper_log = max(1.0, upper.total_tokens())
            target_log = max(1.0, total_tokens)

            # 对数空间线性插值
            weight = (target_log - lower_log) / (upper_log - lower_log)
            return lower.latency_seconds * (1 - weight) + upper.latency_seconds * weight

        # 简单线性插值
        lower_total = max(1, lower.total_tokens())
        upper_total = max(1, upper.total_tokens())
        weight = (total_tokens - lower_total) / (upper_total - lower_total)
        return lower.latency_seconds * (1 - weight) + upper.latency_seconds * weight


def _bounding_samples(
    samples: list[SpeedSample],
    total_tokens: int,
) -> tuple[SpeedSample | None, SpeedSample | None]:
    lower = None
    upper = None
    for sample in samples:
        lower = _choose_lower_sample(lower, sample, total_tokens)
        upper = _choose_upper_sample(upper, sample, total_tokens)
    return lower, upper


def _choose_lower_sample(
    current: SpeedSample | None,
    sample: SpeedSample,
    total_tokens: int,
) -> SpeedSample | None:
    sample_total = sample.total_tokens()
    if sample_total <= total_tokens and (current is None or sample_total > current.total_tokens()):
        return sample
    return current


def _choose_upper_sample(
    current: SpeedSample | None,
    sample: SpeedSample,
    total_tokens: int,
) -> SpeedSample | None:
    sample_total = sample.total_tokens()
    if sample_total >= total_tokens and (current is None or sample_total < current.total_tokens()):
        return sample
    return current
