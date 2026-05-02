from __future__ import annotations

"""LLM: model speed benchmarking.

给人看的解释：
负责运行模型速度基准测试，生成 SpeedProfile。
"""

import time
from datetime import datetime
from typing import TYPE_CHECKING

from .models import SpeedProfile, SpeedSample

if TYPE_CHECKING:
    from ..settings import AgentConfig


def run_speed_benchmark(
    config: AgentConfig,
    input_sizes: list[int] | None = None,
    output_size: int = 500,
    backend: str | None = None,
    model: str | None = None,
) -> SpeedProfile:
    """运行模型速度基准测试。"""

    if input_sizes is None:
        input_sizes = [1000, 5000, 10000, 50000, 100000]

    effective_backend = backend or config.model_backend
    effective_model = model or config.model_name

    from ..backends import create_backend

    samples = []

    for input_tokens in input_sizes:
        # 构造测试输入，估算 token 数
        test_input = "x" * (input_tokens // 2)

        # 构造测试输出
        test_output = "y" * (output_size // 2)

        # 实际调用模型
        backend_instance = create_backend(effective_backend, config)

        start_time = time.time()
        try:
            response = backend_instance.call(
                system_prompt="测速测试，返回简短回复。",
                user_prompt=test_input,
                max_tokens=output_size,
            )
        except Exception as e:
            # 如果调用失败，跳过此点
            continue

        end_time = time.time()
        latency_seconds = end_time - start_time

        # 估算实际 token 数（简化版，实际应该用 tokenizer）
        actual_input_tokens = len(test_input)
        actual_output_tokens = len(response) if response else 0

        sample = SpeedSample(
            input_tokens=actual_input_tokens,
            output_tokens=actual_output_tokens,
            latency_seconds=latency_seconds,
        )
        samples.append(sample)

    profile = SpeedProfile(
        backend=effective_backend,
        model=effective_model,
        tested_at=datetime.utcnow().isoformat() + "Z",
        samples=samples,
        interpolation_method="log_linear",
    )

    return profile
