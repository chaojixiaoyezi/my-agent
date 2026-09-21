# LLM: 客户端只持会话范围的声明缓存，不拥有安装、权限或执行状态；Gateway 模式失败不能降级为本地目录。
# 模块用途: 为 plain 与 TUI 共用目录读取和显式提交，保留原 revision，拒绝过期重放与乱序缓存回写。

from __future__ import annotations

import threading

from ...agent.plugin_command_catalog import PluginCommandCatalog
from ...agent.plugin_command_service import (
    execute_plugin_command,
    plugin_catalog_unavailable,
    read_plugin_catalog,
)
from ...agent.user_space.owner_resolver import owner_identity_from_config
from ..chat_client_context import post_gateway_json


# LLM: 每个实例固定一个宿主连接与会话；序号只仲裁异步展示回写，不是目录版本或持久安装代次。
# 类用途: 保存一个聊天客户端最近看到的插件声明，并按用户操作读取或提交。
class PluginCommandClient:
    # LLM: 构造不进行网络、磁盘或插件加载；use_gateway 来自 CLI 模式，不能由 agent 是否薄客户端推断。
    # 函数用途: 绑定当前会话的传输方式，未使用插件时不增加后台工作。
    def __init__(self, agent: object, conversation_id: str, *, use_gateway: bool) -> None:
        self.agent = agent
        self.conversation_id = conversation_id
        self.use_gateway = use_gateway
        self._lock = threading.Lock()
        self._sequence = 0
        self._snapshot: PluginCommandCatalog | None = None
        self._snapshot_binding: tuple | None = None

    # LLM: 身份从原客户端或配置解析器读取，传输地址也在请求前固定；正文和目录回执不能修改它。
    # 函数用途: 获取一次宿主调用实际使用的 owner、会话、模式和端口。
    def _binding(self) -> tuple:
        owner = getattr(self.agent, "owner_identity", None) or owner_identity_from_config(
            self.agent.config
        )
        return (
            owner,
            self.conversation_id,
            self.use_gateway,
            int(getattr(self.agent.config, "gateway_port", 0) or 0),
        )

    # LLM: 自动补全只读取不可变对象，不发请求；更新必须经过显式 refresh 或 command。
    # 函数用途: 返回最近成功读取的目录，未读或读取失败返回空。
    def snapshot(self) -> PluginCommandCatalog | None:
        with self._lock:
            return self._snapshot if self._snapshot_binding == self._binding() else None

    # LLM: 读取和提交都在同一模式下解析身份；完整 Agent 的 --gateway 也只走 HTTP，绝不使用本地目录兜底。
    # 函数用途: 执行一次宿主调用，传输或目录解码错误只显示固定失败。
    def _request(self, operation: str, *, text: str = "", revision: str = "") -> dict:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        try:
            binding = self._binding()
            owner, conversation_id, use_gateway, port = binding
            if use_gateway:
                status, result = post_gateway_json(
                    port,
                    owner,
                    "/client/plugins",
                    {
                        "operation": operation,
                        "conversation_id": conversation_id,
                        "command": text,
                        "catalog_revision": revision,
                    },
                    timeout=3.0,
                )
                if status != 200:
                    raise ValueError("目录请求未成功")
            else:
                catalog = read_plugin_catalog(
                    owner, channel="local", conversation_id=conversation_id
                )
                result = (
                    {"ok": True, "catalog": catalog.to_payload()}
                    if operation == "catalog"
                    else execute_plugin_command(catalog, text, revision=revision)
                )
            snapshot = PluginCommandCatalog.from_payload(result["catalog"])
            if binding != self._binding():
                raise ValueError("请求期间客户端作用域变化")
        except Exception:  # noqa: BLE001 只读缓存失败不得变成聊天任务或泄露配置细节
            snapshot, result = None, plugin_catalog_unavailable()
        with self._lock:
            if sequence == self._sequence:
                self._snapshot = snapshot
                self._snapshot_binding = binding if snapshot is not None else None
        return result

    # LLM: 仅显式 Tab/命令调用此入口；乱序旧响应不覆盖新快照，无轮询或自动重试。
    # 函数用途: 从宿主更新一次用于帮助与补全的目录。
    def refresh(self) -> dict:
        return self._request("catalog")

    # LLM: 用户已选择的 revision 原样提交；没有选择时使用已见目录或首次显式读取，失败不重放、不静默替换旧版本。
    # 函数用途: 提交一次插件命令，并把宿主的成功或明确拒绝原样交给界面。
    def command(self, text: str, *, revision: str = "") -> dict:
        snapshot = self.snapshot()
        if not revision and snapshot is None:
            loaded = self.refresh()
            if not loaded.get("ok"):
                return loaded
            snapshot = self.snapshot()
            if snapshot is None:
                return plugin_catalog_unavailable()
        return self._request("command", text=text, revision=revision or snapshot.revision)
