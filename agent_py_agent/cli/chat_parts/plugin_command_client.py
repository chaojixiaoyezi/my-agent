# LLM: 客户端只持会话范围的声明缓存，不拥有安装、权限或执行状态；Gateway 模式失败不能降级为本地目录。
# 模块用途: 为 plain 与 TUI 共用目录和管理提交，保留原 revision 与请求编号，断连后查询原结果。

from __future__ import annotations

import threading
import uuid
from dataclasses import replace

from ...agent.plugin_command_catalog import PluginCommandCatalog
from ...agent.plugin_command_service import (
    plugin_catalog_unavailable,
    plugin_command_unknown,
)
from ...agent.user_space.owner_access import is_local_admin_owner
from ...agent.user_space.owner_resolver import owner_identity_from_config, resolve_owner_home
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
    # 函数用途: 提交一次带稳定编号的命令；传输失败保留未知，目录坏响应不覆盖已知操作结果。
    def _request(self, operation: str, *, text: str = "", revision: str = "") -> dict:
        request_id = uuid.uuid4().hex if operation == "command" else ""
        binding = None
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        try:
            binding = self._binding()
            owner, conversation_id, use_gateway, port = binding
            if use_gateway:
                payload = {"operation": operation, "conversation_id": conversation_id,
                           "command": text, "catalog_revision": revision, "plugin_request_id": request_id}
                from .gateway_client import _gateway_submit_workspace

                value = _gateway_submit_workspace(self.agent, workspace_root=None, workspace_roots=None)
                if value.get("cwd"):
                    payload["workspace"] = value
                status, result = post_gateway_json(
                    port,
                    owner,
                    "/client/plugins",
                    payload,
                    timeout=3.0,
                )
                if status != 200:
                    raise ValueError("目录请求未成功")
            else:
                manager = self._direct_manager(owner, conversation_id)
                result = (
                    {"ok": True, "catalog": manager.catalog().to_payload()}
                    if operation == "catalog"
                    else manager.command(text, revision=revision, request_id=request_id)
                )
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("插件响应无效")
            try:
                snapshot = PluginCommandCatalog.from_payload(result["catalog"])
            except (KeyError, TypeError, ValueError):
                if operation == "catalog":
                    raise
                snapshot = None
            if binding != self._binding():
                raise ValueError("请求期间客户端作用域变化")
        except Exception:  # noqa: BLE001 不重试命令、不改本地执行，不从传输错误猜未发生
            snapshot = None
            result = plugin_command_unknown(request_id) if request_id else plugin_catalog_unavailable()
        with self._lock:
            if sequence == self._sequence:
                self._snapshot = snapshot
                self._snapshot_binding = binding if snapshot is not None else None
        return result

    # LLM: 只有 direct 模式才加载执行服务；Gateway 薄客户端不能因导入管理模块而加载本地执行器。
    # 函数用途: 给本地入口组装同一个管理服务，保留原 owner 权限和工作目录。
    def _direct_manager(self, owner, conversation_id):
        from ...agent.plugin_management import PluginManagement, plugin_management_context

        context = plugin_management_context(
            resolve_owner_home(self.agent.home_paths.root, owner), self.agent.home_paths,
            self.agent.config, self.agent.conversation_store.threads,
            actor_id="local-agent", channel="chat", conversation_id=conversation_id,
            is_admin=is_local_admin_owner(self.agent.home_paths),
        )
        return PluginManagement(replace(context, workspace=self.agent.effective_workspace_root))

    # LLM: 仅显式 Tab/命令调用此入口；乱序旧响应不覆盖新快照，无轮询或自动重试。
    # 函数用途: 从宿主更新一次用于帮助与补全的目录。
    def refresh(self) -> dict:
        return self._request("catalog")

    # LLM: 用户已选择的 revision 原样提交；没有选择时使用已见目录或首次显式读取，失败不重放、不静默替换旧版本。
    # 函数用途: 提交一次插件命令；状态查询无需目录可用，其他新命令使用原选中版本。
    def command(self, text: str, *, revision: str = "") -> dict:
        from ...agent.command_arguments import CommandArgumentError
        from ...agent.plugin_commands import parse_plugin_command

        try:
            parsed = parse_plugin_command(text)
            if parsed is not None and parsed.action and parsed.action.name == "status" and not parsed.help_requested:
                result = self._request("command", text=text, revision=revision)
                if result.get("state") == "outcome_unknown":
                    return plugin_command_unknown(parsed.arguments.values["request"])
                return result
        except CommandArgumentError:
            pass
        snapshot = self.snapshot()
        if not revision and snapshot is None:
            loaded = self.refresh()
            if not loaded.get("ok"):
                return loaded
            snapshot = self.snapshot()
            if snapshot is None:
                return plugin_catalog_unavailable()
        return self._request("command", text=text, revision=revision or snapshot.revision)
