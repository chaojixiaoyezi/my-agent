# LLM: Model-speed module; keep benchmark samples and interpolation data shapes stable.
# 模块用途: 记录和估算模型速度，辅助超时、调度或容量判断。

from __future__ import annotations

"""model speed profiling.

给人看的解释：
这个模块负责模型速度基准测试、配置文件保存/加载和动态超时计算。
"""

from .benchmark import run_speed_benchmark
from .models import SpeedProfile, SpeedSample
from .storage import load_speed_profile, save_speed_profile

__all__ = ["SpeedProfile", "SpeedSample", "run_speed_benchmark", "load_speed_profile", "save_speed_profile"]
