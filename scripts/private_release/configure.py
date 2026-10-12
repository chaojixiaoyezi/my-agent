# LLM: 安装辅助的显式用户配置入口；复用原 YAML setter，只写用户指定配置，密钥不回显。
# 模块用途: 内网用户在终端填入自己的 OpenAI 兼容接口（含百炼）、模型和密钥，不改发行包。
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from agent_py_agent.agent.settings.config_io import set_simple_yaml_value


# LLM: 仅在用户调用 --configure-model 时执行；不发网络请求、不保存输入历史或打印 API key。
# 函数用途: 用隐藏输入读取密钥，沿现有配置机制保存接口，配置后由用户启动 TUI。
def main() -> int:
    path = Path(sys.argv[1])
    print("输入你获准访问的 OpenAI 兼容接口。百炼或公司代理请使用实际提供的地址。")
    base = input("接口 Base URL（通常以 /v1 结尾）：").strip().rstrip("/")
    if urlparse(base).scheme not in {"http", "https"} or not urlparse(base).netloc:
        raise ValueError("接口地址必须是有效的 http/https URL")
    model = input("模型 ID（按接口提供方给出的名称填写）：").strip()
    if not model:
        raise ValueError("模型 ID 不能为空")
    key = getpass.getpass("API Key（不回显）：").strip()
    if not key:
        raise ValueError("API Key 不能为空")
    os.chmod(path, 0o600)
    for name, value in (("model_backend", "openai_compatible"), ("api_base", base), ("model_name", model), ("api_key", key)):
        set_simple_yaml_value(path, name, value)
    print("模型配置已保存，密钥未显示。修改已有会话选模请使用 TUI 的模型管理入口。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
