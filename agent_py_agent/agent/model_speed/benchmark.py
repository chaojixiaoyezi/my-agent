from __future__ import annotations

"""LLM: model speed benchmarking.

给人看的解释：
负责运行模型速度基准测试，生成 SpeedProfile。
"""

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .models import SpeedProfile, SpeedSample

if TYPE_CHECKING:
    from ..settings import AgentConfig


def run_speed_benchmark(
    config: AgentConfig,
    **options: Any,
) -> SpeedProfile:
    """运行模型速度基准测试。"""

    input_sizes = options.get("input_sizes")
    output_size = int(options.get("output_size", 500))
    backend = options.get("backend")
    model = options.get("model")
    if input_sizes is None:
        input_sizes = [1000, 5000, 10000, 50000, 100000]

    effective_backend = backend or config.model_backend
    effective_model = model or config.model_name

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
