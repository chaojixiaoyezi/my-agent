
"""权限认证模块 — 角色、权限、鉴权中间件。

这个包提供多租户权限模型：
- models.py: Role 枚举、Permission 数据类、Action 枚举
- manager.py: AuthManager 权限管理器
- middleware.py: AuthMiddleware HTTP 请求鉴权中间件
"""

from .manager import AuthManager
from .middleware import AuthMiddleware
from .models import Action, Permission, Role

__all__ = ["Role", "Permission", "Action", "AuthManager", "AuthMiddleware"]
