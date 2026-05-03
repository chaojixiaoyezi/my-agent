# Channel Adapter Framework — HANDOFF

## 做了什么

给 my-agent 项目添加了外部通道适配器框架，支持飞书和 QQ 两个平台接入 agent。

## 新增文件

### 核心框架
| 文件 | 说明 |
|------|------|
| `agent_py_agent/agent/adapter/__init__.py` | 包导出 |
| `agent_py_agent/agent/adapter/protocol.py` | 统一消息格式（IncomingMessage / OutgoingMessage）+ 各平台转换函数 |
| `agent_py_agent/agent/adapter/base.py` | BaseChannelAdapter 抽象基类 |
| `agent_py_agent/agent/adapter/manager.py` | ChannelManager 通道管理器 |
| `agent_py_agent/agent/adapter/feishu.py` | FeishuAdapter 飞书适配器 |
| `agent_py_agent/agent/adapter/qq.py` | QQAdapter QQ 适配器 |

### CLI
| 文件 | 说明 |
|------|------|
| `agent_py_agent/cli/adapter.py` | 新增 `cmd_adapter_start/stop/status` 函数 |

### 测试
| 文件 | 说明 |
|------|------|
| `agent_py_agent/tests/test_adapter_base.py` | 基类和协议测试（16 项） |
| `agent_py_agent/tests/test_adapter_feishu.py` | 飞书适配器测试（9 项） |
| `agent_py_agent/tests/test_adapter_qq.py` | QQ 适配器测试（7 项） |
| `agent_py_agent/tests/test_adapter_manager.py` | 通道管理器测试（9 项） |

## 修改文件

| 文件 | 改动 |
|------|------|
| `agent_py_agent/cli/parser.py` | 新增 `adapter start/status/stop` 子命令 |
| `agent_py_agent/agent/settings/config.py` | 新增 feishu_* 和 qq_* 配置字段 |
| `agent_py_agent/config/agent_config.yaml` | 新增飞书/QQ 配置项 |

## 架构说明

```
外部消息 (飞书/QQ)
    → 适配器收到
    → ChannelManager.route_message()
    → gateway HTTP POST /ask
    → 轮询 GET /result/<id>
    → 适配器.send_message() 回复用户
```

### 飞书适配器
- 同时启动本地 HTTP 回调服务（接收飞书事件推送）和使用飞书 API 发送消息
- 回调端点：`POST /feishu/callback`（支持 URL 验证握手）
- 安全：可选 `verification_token` 校验 + `encrypt_key` 签名校验
- Token：自动获取 tenant_access_token，带缓存

### QQ 适配器
- 轮询方式获取消息（定期调用 `/channels/{id}/messages`）
- 消息去重（基于 message_id，保留最近 1000 条）
- Token：client_credentials 模式自动刷新

## CLI 命令

```bash
my-agent adapter start --channel feishu   # 启动飞书适配器
my-agent adapter start --channel qq       # 启动 QQ 适配器
my-agent adapter start --channel all       # 启动所有
my-agent adapter status                    # 查看运行状态
my-agent adapter stop                      # 停止所有
```

## 配置项（agent_config.yaml）

```yaml
# 飞书
feishu_app_id: ""           # 飞书应用 ID
feishu_app_secret: ""        # 飞书应用密钥
feishu_verification_token: "" # 飞书回调验证 token
feishu_encrypt_key: ""       # 可选：飞书消息加密 key
feishu_callback_port: 8421   # 本地 HTTP 回调端口

# QQ
qq_app_id: ""               # QQ 应用 ID
qq_app_secret: ""            # QQ 应用密钥
qq_token: ""                 # QQ access_token
qq_guild_id: ""              # 默认公会 ID
qq_channel_id: ""            # 默认频道 ID
```

## 测试结果

```
adapter tests: 41 passed
full suite: 654 passed, 2 failed (pre-existing, 与本次改动无关)
```

## 约束遵循

- 只用标准库（http.server、json、urllib.request、threading、hmac、hashlib）
- 无第三方依赖
- 飞书/QQ SDK 均未使用，直接调用 HTTP API
- 敏感信息从配置读取，不硬编码
- 遵循项目代码风格（中文注释、类型标注）
