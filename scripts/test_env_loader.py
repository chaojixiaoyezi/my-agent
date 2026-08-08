"""测试脚本统一 key 加载:优先已有环境变量,缺省回落到 /etc/my-agent/model.env。

testbox 上交互 shell 的 .bashrc 会把 AGENT_API_KEY 覆盖成旧 MiniMax key
(真机实测 401 根因),后台/setsid 任务则完全没有 key;正式 key 在
/etc/my-agent/model.env(systemd 服务同源)。所有跑真模型(禁模拟判读)的
测试脚本在检查/使用 key 之前调用 ensure_model_key(),统一两处来源,
不再让 .bashrc 的旧 key 把后台测试打成 401。

用法:import sys; sys.path.insert(0, <本目录>); from test_env_loader import ensure_model_key
"""

from __future__ import annotations

import os
from pathlib import Path

_MODEL_ENV_PATH = Path("/etc/my-agent/model.env")


def ensure_model_key() -> str:
    """返回可用的 AGENT_API_KEY;环境变量已有则不覆盖(显式传入优先)。"""
    key = str(os.environ.get("AGENT_API_KEY") or "").strip()
    if key:
        return key
    if _MODEL_ENV_PATH.is_file():
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
                    os.environ["AGENT_API_KEY"] = value
                    return value
        except OSError:
            pass
    return str(os.environ.get("AGENT_API_KEY") or "").strip()
