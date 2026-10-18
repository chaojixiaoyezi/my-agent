# LLM: CLI bootstrap 与 Gateway worker 共用这一依赖轻的默认配置路径实现；只在调用时读环境，不导入 settings/Agent、读配置或创建目录。
# 模块用途: 统一随包默认配置位置和 MY_AGENT_CONFIG 选择，让早期 CLI 与工作进程保持相同的路径优先级。
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "agent_config.yaml"


# LLM: MY_AGENT_CONFIG 只提供调用时默认值，显式 --config/worker config_path 仍由原调用方优先；保留 strip/expanduser，不把相对路径提前 resolve。
# 函数用途: 返回本次默认配置文件路径，不读取文件、不缓存环境，也不创建运行状态。
def default_config_path() -> Path:
    configured = str(os.environ.get("MY_AGENT_CONFIG") or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG
