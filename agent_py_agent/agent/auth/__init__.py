# LLM: Auth module; keep user/session/token contracts stable for middleware callers.
# 模块用途: 处理认证用户、会话、权限检查和请求中间件。

"""权限认证模块 — 角色、权限、鉴权中间件。

给人看的解释：
这个包提供多租户权限模型：
- models.py: Role 枚举、Permission 数据类、Action 枚举
- manager.py: AuthManager 权限管理器
- middleware.py: AuthMiddleware HTTP 请求鉴权中间件
"""

from .manager import AuthManager
from .middleware import AuthMiddleware
from .models import Action, Permission, Role

__all__ = ["Role", "Permission", "Action", "AuthManager", "AuthMiddleware"]
