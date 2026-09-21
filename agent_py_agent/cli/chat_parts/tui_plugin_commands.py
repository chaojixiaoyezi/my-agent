# LLM: TUI 只保存用户所选目录版本并异步调用共享命令分派；此状态不是 owner、权限或执行权来源。
# 模块用途: 连接输入候选、版本绑定与宿主回执，目录请求不阻塞键盘输入。

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from ...agent.plugin_commands import plugin_namespace
from .plugin_command_client import PluginCommandClient


# LLM: binding 属于一个输入框；编辑参数保留所选版本，清空或更换命名空间即撤销，不自动换成新目录版本。
# 类用途: 将已经接受的补全候选版本保留到 Enter 提交。
@dataclass
class PluginInputBinding:
    client: PluginCommandClient
    namespace: str = ""
    revision: str = ""

    # LLM: 只比较明确插件命名空间，正文参数不是机器身份；该回调不请求宿主或执行命令。
    # 函数用途: 编辑输入时丢弃已离开命名空间的旧候选绑定。
    def edited(self, text: str) -> None:
        namespace = plugin_namespace(text)
        if namespace is None or namespace.prefix != self.namespace:
            self.namespace, self.revision = "", ""

    # LLM: 首次选择绑定候选自身 revision；同命名空间后续参数补全也不能将原业务选择升级到新目录。
    # 函数用途: 在文字替换完成后保存原始选择，清空或换命名空间后才建立新绑定。
    def selected(self, text: str, revision: str) -> None:
        namespace = plugin_namespace(text)
        if namespace is not None and revision:
            if not self.revision or namespace.prefix != self.namespace:
                self.namespace, self.revision = namespace.prefix, revision


# LLM: 回调跟随原 buffer 生命周期；变更只更新易失版本绑定，不创建额外后台轮询或持久状态。
# 函数用途: 把一个会话的插件客户端及候选版本管理连接到输入框。
def bind_plugin_input(buffer, client: PluginCommandClient) -> None:
    binding = PluginInputBinding(client)
    buffer._my_agent_plugin_input = binding

    # LLM: Buffer 负责发送原文本变化事件，回调不能改写输入或重新接受候选。
    # 函数用途: 清空、历史替换或更换命名空间时及时丢弃旧目录版本。
    def changed(sender) -> None:
        binding.edited(str(sender.text))

    buffer.on_text_changed += changed


# LLM: 只处理已识别插件命令，复用共享 dispatcher；线程外执行一次，无业务重试，不影响原控制/普通任务。
# 函数用途: 在后台读取目录或提交命令，将宿主回执交回当前 TUI。
def submit_plugin_command(
    event, params, text: str, binding: PluginInputBinding | None, revision: str
) -> bool:
    if plugin_namespace(text) is None:
        return False
    from .tui import _tui_handle_command
    from .tui_keybindings import _handle_command_params

    command_params = replace(
        _handle_command_params(params, text),
        plugin_client=binding.client if binding else None,
        plugin_revision=revision,
    )
    app = event.app

    # LLM: 原会话参数在发起时冻结；异常或结束不重发命令，重绘只影响本客户端。
    # 函数用途: 让网络或宿主目录故障不会冻结 TUI 编辑和中断按键。
    async def execute() -> None:
        try:
            await asyncio.to_thread(_tui_handle_command, params=command_params)
        finally:
            app.invalidate()

    app.create_background_task(execute())
    return True
