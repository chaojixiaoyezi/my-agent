

from __future__ import annotations

from pathlib import Path

from ..agent.model_speed import load_speed_profile, run_speed_benchmark, save_speed_profile
from ..agent.settings import load_config
from ..agent.settings.model_profiles import selected_model_config
from .common import DEFAULT_CONFIG, make_agent, resolve_workspace_root


def cmd_bench_model(args) -> int:
    config_path = args.config if args.config else DEFAULT_CONFIG
    config = load_config(config_path)
    speed_profile_path = _speed_profile_path(args, config)
    if args.show:
        return _show_speed_profile(speed_profile_path)
    return _run_speed_benchmark(args, config_path, speed_profile_path)


def _speed_profile_path(args, config) -> Path:
    return args.profile if args.profile else resolve_workspace_root(config, args.config or DEFAULT_CONFIG) / "data" / "model_speed_profile.json"


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


# LLM: 测速对象是 owner 当前选定的对话模型（模型目录解析结果），不是部署配置里的占位模型；展示、测速与保存用同一份配置。
# 函数用途: 对当前选定模型跑速度基准并保存速度档案。
def _run_speed_benchmark(args, config_path: str | Path, speed_profile_path: Path) -> int:
    agent = make_agent(args)
    selected = selected_model_config(agent)
    print(f"Config: {config_path}")
    print(f"Backend: {selected.model_backend}")
    print(f"Model: {selected.model_name}")
    print()

    profile = run_speed_benchmark(selected)
    if not profile.samples:
        print("Speed benchmark failed: no samples were generated.")
        return 1

    print("Speed benchmark results:")
    _print_samples(profile.samples)
    print()
    save_speed_profile(profile, speed_profile_path)
    print(f"Speed model saved to: {speed_profile_path}")
    return 0


def _print_samples(samples) -> None:
    print("Samples:")
    for sample in samples:
        tps = sample.tokens_per_second()
        print(
            f"  Input {sample.input_tokens:5d},  Output {sample.output_tokens:5d}: "
            f"{sample.latency_seconds:5.1f}s ({tps:3.0f} tok/s)"
        )
