# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。


from __future__ import annotations

from pathlib import Path

from ..agent.config import load_config
from ..agent.model_speed import load_speed_profile, run_speed_benchmark, save_speed_profile
from .common import DEFAULT_CONFIG, make_agent, resolve_workspace_root


# LLM: cmd_bench_model 属于benchmark CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_bench_model(args) -> int:
    config_path = args.config if args.config else DEFAULT_CONFIG
    config = load_config(config_path)
    speed_profile_path = _speed_profile_path(args, config)
    if args.show:
        return _show_speed_profile(speed_profile_path)
    return _run_speed_benchmark(args, config, config_path, speed_profile_path)


# LLM: _speed_profile_path 属于benchmark CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _speed_profile_path(args, config) -> Path:
    return args.profile if args.profile else resolve_workspace_root(config, args.config or DEFAULT_CONFIG) / "data" / "model_speed_profile.json"


# LLM: _show_speed_profile 属于benchmark CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _show_speed_profile(speed_profile_path: Path) -> int:
    profile = load_speed_profile(speed_profile_path)
    if profile is None:
        print(f"Speed profile file does not exist: {speed_profile_path}")
        print("Run `my-agent bench-model` first to generate a speed profile.")
        return 1

    print(f"Backend: {profile.backend}")
    print(f"Model: {profile.model}")
    print(f"Tested at: {profile.tested_at}")
    print(f"Interpolation: {profile.interpolation_method}")
    print()
    _print_samples(profile.samples)
    print()
    print(f"Speed profile file: {speed_profile_path}")
    return 0


# LLM: _run_speed_benchmark 属于benchmark CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def _run_speed_benchmark(args, config, config_path: str | Path, speed_profile_path: Path) -> int:
    print(f"Config: {config_path}")
    print(f"Backend: {config.model_backend}")
    print(f"Model: {config.model_name}")
    print()

    make_agent(args)
    profile = run_speed_benchmark(config)
    if not profile.samples:
        print("Speed benchmark failed: no samples were generated.")
        return 1

    print("Speed benchmark results:")
    _print_samples(profile.samples)
    print()
    save_speed_profile(profile, speed_profile_path)
    print(f"Speed model saved to: {speed_profile_path}")
    return 0


# LLM: _print_samples 属于benchmark CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_samples(samples) -> None:
    print("Samples:")
    for sample in samples:
        tps = sample.tokens_per_second()
        print(
            f"  Input {sample.input_tokens:5d},  Output {sample.output_tokens:5d}: "
            f"{sample.latency_seconds:5.1f}s ({tps:3.0f} tok/s)"
        )
