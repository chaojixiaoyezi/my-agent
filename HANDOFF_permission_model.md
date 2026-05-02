# 权限模型和多租户能力 — HANDOFF

## 做了什么

为 my-agent 项目添加了完整的权限模型和多租户鉴权体系，确保外部通道（飞书/QQ）用户之间的数据隔离。

## 新增文件

| 文件 | 说明 |
|------|------|
| `agent_py_agent/agent/auth/__init__.py` | 包导出 |
| `agent_py_agent/agent/auth/models.py` | Role 枚举、Permission 数据类、Action 枚举、角色推断策略 |
| `agent_py_agent/agent/auth/manager.py` | AuthManager 权限管理器 |
| `agent_py_agent/agent/auth/middleware.py` | AuthMiddleware HTTP 鉴权中间件 + handler helper 函数 |
| `agent_py_agent/tests/test_auth.py` | 权限测试（33 项） |

## 修改文件

| 文件 | 改动 |
|------|------|
| `agent_py_agent/agent/gateway_parts/http_service.py` | 集成 AuthMiddleware；`_handle_ask` 写入 user_id；`_handle_result` 校验 user_id 归属；`/admin/*` 等管理端点强制 admin 权限 |
| `agent_py_agent/agent/settings/config.py` | 新增 `auth_enabled: bool = True` 和 `admin_user_id: str = "admin"` 字段及 coerce 规则 |
| `agent_py_agent/config/agent_config.yaml` | 新增 `auth_enabled: true` 和 `admin_user_id: "admin"` 配置项 |

## 架构说明

### 角色推断策略

| 通道 | user_id | 推断角色 |
|------|----------|---------|
| chat / cli / terminal（终端）| 任意 | ADMIN |
| feishu / qq 等外部通道 | 非 admin | USER |
| feishu / qq 等外部通道 | admin | ADMIN |

### Action 权限矩阵

| Action | ADMIN | USER |
|--------|-------|------|
| READ_TASK | ✓ | ✓（仅自己） |
| WRITE_TASK | ✓ | ✗ |
| READ_USER_DATA | ✓ | ✗ |
| ADMIN_QUERY | ✓ | ✗ |

### HTTP 鉴权规则

- `/ask`：任何人都可提交（user_id 写入 metadata）
- `/result/<id>`：非 admin 只能查自己 user_id 的请求
- `/admin/*`：仅 ADMIN 可访问
- `/sessions/*`：仅 ADMIN 可访问
- 无 `X-User-Id` header → 视为终端，channel=chat

### 配置项

```yaml
auth_enabled: true   # 开启鉴权；关闭后所有人都有 ADMIN 权限（本地开发用）
admin_user_id: "admin"  # 管理员用户 ID
```

## 测试结果

```
test_auth.py: 33 passed
full suite: 688 passed, 1 failed (pre-existing)
```

## 约束遵循

- 只用标准库
- 遵循项目代码风格（中文注释、类型标注）
- auth_enabled=false 关闭鉴权，方便本地开发
