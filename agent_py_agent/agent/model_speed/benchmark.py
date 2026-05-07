# LLM: Model-speed module; keep benchmark samples and interpolation data shapes stable.
# 模块用途: 记录和估算模型速度，辅助超时、调度或容量判断。

from __future__ import annotations

"""model speed benchmarking.

给人看的解释：
负责运行模型速度基准测试，生成 SpeedProfile。
"""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .models import SpeedProfile, SpeedSample

if TYPE_CHECKING:
    from ..settings import AgentConfig


# LLM: SpeedBenchmarkParams is a 模型速度评估 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 保存 SpeedBenchmarkParams 的输入字段，调用方先构造这个对象再进入 模型速度评估，避免继续散传参数。
@dataclass(frozen=True)
class SpeedBenchmarkParams:
    input_sizes: list[int] | None = None
    output_size: int = 500
    backend: str | None = None
    model: str | None = None


# LLM: run_speed_benchmark belongs to 模型速度评估; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 运行模型速度基准测试。。
def run_speed_benchmark(
    config: AgentConfig,
    *,
    params: SpeedBenchmarkParams | None = None,
    input_sizes: list[int] | None = None,
    output_size: int = 500,
    backend: str | None = None,
    model: str | None = None,
) -> SpeedProfile:
    """运行模型速度基准测试。"""

    values = params or SpeedBenchmarkParams(input_sizes, output_size, backend, model)
    output_size = int(values.output_size)
    input_sizes = values.input_sizes or [1000, 5000, 10000, 50000, 100000]

    effective_backend = values.backend or config.model_backend
    effective_model = values.model or config.model_name

    from ..backends import create_backend

    backend_instance = create_backend(effective_backend, config)
    samples = [
        sample
        for input_tokens in input_sizes
        if (sample := _benchmark_single(backend_instance, input_tokens, output_size)) is not None
    ]

    return SpeedProfile(
        backend=effective_backend,
        model=effective_model,
        tested_at=datetime.utcnow().isoformat() + "Z",
        samples=samples,
        interpolation_method="log_linear",
    )


# LLM: _benchmark_single belongs to 模型速度评估; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 运行单次基准测试，失败返回 None。。
def _benchmark_single(
    backend_instance, input_tokens: int, output_size: int
) -> SpeedSample | None:
    """运行单次基准测试，失败返回 None。"""

    test_input = "x" * (input_tokens // 2)
    start_time = time.time()
    try:
        response = backend_instance.call(
            system_prompt="测速测试，返回简短回复。",
            user_prompt=test_input,
            max_tokens=output_size,
        )
    except Exception:
        return None

    return SpeedSample(
        input_tokens=len(test_input),
        output_tokens=len(response) if response else 0,
        latency_seconds=time.time() - start_time,
    )
