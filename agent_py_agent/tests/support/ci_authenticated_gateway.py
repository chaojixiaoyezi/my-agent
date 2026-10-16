# LLM: CI 认证夹具只装配临时宿主的真实凭据与中间件；不替换权限门，不读取日常数据根或打印令牌。
# 模块用途: 让 HTTP、薄客户端和命令测试明确提供当前宿主认证事实，负例仍可省略凭据验证拒绝。
from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.gateway_parts.local_client_token import (
    ensure_local_client_credential,
    load_local_client_credential,
)


# LLM: 仅允许调用方给出的隔离测试根；经产品宿主入口创建私有凭据文件，不修改认证或网络策略。
# 函数用途: 模拟测试 Gateway 启动时生成本机凭据，返回值不应出现在日志或断言正文里。
def seed_gateway_credential(root: Path) -> str:
    return ensure_local_client_credential(root)


# LLM: 使用与薄客户端相同的真实读取入口；身份头是已持有宿主凭据的测试适配器声明，不从正文推断权限。
# 函数用途: 为隔离 HTTP 请求构造凭据头，可选附上明确用户与渠道。
def authenticated_headers(root: Path, *, user: str = "", channel: str = "") -> dict[str, str]:
    headers = {"X-Gateway-Token": load_local_client_credential(root)}
    if user:
        headers["X-User-Id"] = user
    if channel:
        headers["X-Channel"] = channel
    return headers


# LLM: 保留产品 check_trusted、角色和权限裁决；只绑定当前临时宿主实际生成的令牌，不 mock 总权限门。
# 函数用途: 为不启动完整 Gateway 的协议测试装配真实认证中间件。
def authenticated_middleware(root: Path, manager: AuthManager | None = None) -> AuthMiddleware:
    middleware = AuthMiddleware(manager or AuthManager())
    middleware.set_local_client_credential(seed_gateway_credential(root))
    return middleware
