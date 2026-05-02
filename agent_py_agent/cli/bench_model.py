from __future__ import annotations

"""LLM: implements the bench-model CLI command.

给人看的解释：
这个文件只管 `my-agent bench-model` 命令的参数解析和结果打印。
实际测速逻辑在 agent.model_speed 模块。
"""

import json

from ..agent.config import load_config
from ..agent.model_speed import load_speed_profile, run_speed_benchmark, save_speed_profile
from .common import DEFAULT_CONFIG, make_agent


def cmd_bench_model(args) -> int:
    """运行模型速度基准测试或查看已有速度模型。"""

    config_path = args.config if args.config else DEFAULT_CONFIG
    config = load_config(config_path)

    speed_profile_path = (
        args.profile if args.profile else config.workspace_root / "data" / "model_speed_profile.json"
    )

    if args.show:
        # 查看已有速度模型
        profile = load_speed_profile(speed_profile_path)
        if profile is None:
            print(f"速度模型文件不存在: {speed_profile_path}")
            print("请先运行 `my-agent bench-model` 生成速度模型。")
            return 1

        print(f"Backend: {profile.backend}")
        print(f"Model: {profile.model}")
        print(f"Tested at: {profile.tested_at}")
        print(f"Interpolation: {profile.interpolation_method}")
        print()
        print("Samples:")
        for sample in profile.samples:
            tps = sample.tokens_per_second()
            print(
                f"  Input {sample.input_tokens:5d},  Output {sample.output_tokens:5d}: "
                f"{sample.latency_seconds:5.1f}s ({tps:3.0f} tok/s)"
            )
        print()
        print(f"Speed profile file: {speed_profile_path}")
        return 0

    # 运行速度测试
    print(f"使用配置: {config_path}")
    print(f"Backend: {config.model_backend}")
    print(f"Model: {config.model_name}")
    print()

    agent = make_agent(args)
    profile = run_speed_benchmark(config)

    if not profile.samples:
        print("速度测试失败，没有生成任何样本。")
        return 1

    print("速度测试结果：")
    for sample in profile.samples:
        tps = sample.tokens_per_second()
        print(
            f"  Input {sample.input_tokens:5d},  Output {sample.output_tokens:5d}: "
            f"{sample.latency_seconds:5.1f}s ({tps:3.0f} tok/s)"
        )
    print()

    # 保存速度模型
    save_speed_profile(profile, speed_profile_path)
    print(f"Speed model saved to: {speed_profile_path}")
    return 0
