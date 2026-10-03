# LLM: 仓库验收脚本复用原配置解析与 G3 凭据选择，不添加传输入口，不收 token 参数或环境变量。
# 模块用途: 在脚本原身份头位置附加唯一凭据；读取失败按 G2b 开关降级（默认）或抛安全原因码（开关开）。
from __future__ import annotations

from types import SimpleNamespace

from ..agent.gateway_parts.client_credentials import gateway_client_credentials
from ..agent.settings.config import load_config
from .bootstrap import default_config_path


# LLM: 配置路径是原部署入口而不是凭据参数；复用配置 token 优先合同，返回值只在请求构造期间使用。
# 函数用途: 给没有 Agent 的仓库 HTTP 验收脚本读取当前部署的 Gateway 请求头。
def gateway_script_headers(config_path=None) -> dict[str, str]:
    config = load_config(config_path or default_config_path())
    return gateway_client_credentials(SimpleNamespace(config=config)).headers()
