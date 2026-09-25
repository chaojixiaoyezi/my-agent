"""测试脚本统一 key 加载:model.env 为部署权威(systemd 服务同源),环境变量只兜底。

测试机 上交互 shell 的 .bashrc 会把 AGENT_API_KEY 覆盖成旧 MiniMax key
(真机实测 401 根因),后台/setsid 任务则完全没有 key;正式 key 在
/etc/my-agent/model.env。所有跑真模型(禁模拟判读)的测试脚本在检查/使用
key 之前调用 ensure_model_key()。环境变量优先曾让 .bashrc 的旧 key 在交互
shell 里劫持一切测试——model.env 优先则任何 session 注入都不能改测试所用
key(测试要验证的就是部署配置的模型,必须与常驻服务同源)。

用法:import sys; sys.path.insert(0, <本目录>); from test_env_loader import ensure_model_key
"""

from __future__ import annotations

import os
from pathlib import Path

_MODEL_ENV_PATH = Path("/etc/my-agent/model.env")


def _load_key_from_model_env() -> str:
    if not _MODEL_ENV_PATH.is_file():
        return ""
    try:
        for raw in _MODEL_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            name, _, value = line.partition("=")
            if name.strip() != "AGENT_API_KEY":
                continue
            value = value.strip().strip('"').strip("'")
            if value:
                return value
    except OSError:
        return ""
    return ""


def ensure_model_key() -> str:
    """返回 AGENT_API_KEY:model.env 优先(部署权威),环境变量缺省兜底。

    写入 os.environ 让脚本后续读取保持一致;model.env 不可读/无该 key 时
    才回落环境变量(显式传入的兜底,而不是优先)。"""
    key = _load_key_from_model_env()
    if key:
        os.environ["AGENT_API_KEY"] = key
        return key
    return str(os.environ.get("AGENT_API_KEY") or "").strip()
