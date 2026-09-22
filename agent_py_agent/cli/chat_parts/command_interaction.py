# LLM: 交互引用只属于一次明确命令，不拥有执行状态；GatewayPaths 必须由现有本地连接传入，不能来自网络正文。
# 模块用途: 在 TUI、命令分派和客户端之间传递同一编号、审批回调和取消令牌。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...agent.common.cancellation import CancellationToken
from ...agent.gateway_parts.paths import GatewayPaths


# LLM: 请求编号在输入提交时固定；回调只接原 ToolApproval 合同，关闭由创建方负责，不缓存用户授权。
# 类用途: 保存单次命令的界面交互依赖，避免并发命令共用可变回调。
@dataclass(frozen=True)
class CommandInteraction:
    request_id: str
    request_permission: Callable
    cancellation_token: CancellationToken
    gateway_paths: GatewayPaths | None = None
