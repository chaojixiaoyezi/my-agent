# LLM: 配置接口沿用可信 owner 与共享 owner_conversation_store；冷用户不能触发 Agent/后端/工具初始化，配置读写仍独立。
# 文字 /model（IM 与 TUI 带编号形式）只做查看、会话选择和默认值，复用同一 owner/线程解析与同一写入口。
# 模块用途: 为 TUI 菜单和聊天里的 /model 提供模型列表、保存和选择，不把密钥写入聊天队列或启动后台服务。

from __future__ import annotations

import json
from types import SimpleNamespace

from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..settings.model_profiles import ModelProfileError, execute_model_profile_operation
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .control_service import resolve_gateway_scope_owner
from .owner_conversation_store import owner_conversation_store


# LLM: 列表永不回传密钥；只从可信 owner/channel binding 取得会话，冷菜单可创建线程但不进入 owner pool。
# owner/线程解析与文字 /model 共用 _scoped_model_host，两处不能各自推导身份。
# 函数用途: 为已认证用户管理共享于本人各会话的模型目录，选择仅保存当前会话，不被完整 Agent 初始化拖慢。
def handle_client_models(handler, server) -> None:
    from .http_handlers import (
        _gateway_control_scope,
        _http_conversation_id,
        _request_channel,
        require_trusted_source,
    )

    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(503, {"ok": False, "message": "Gateway 尚未就绪。"})
        return
    try:
        body = handler._read_json()
        if not isinstance(body, dict) or not _http_conversation_id(body):
            raise ModelProfileError("缺少会话编号。")
        user_id, channel = _request_channel(handler)
        scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
        config_host, thread = _scoped_model_host(server.agent, scope)
        result = execute_model_profile_operation(
            config_host, str(body.get("operation") or ""), body, thread_id=thread.thread_id,
        )
    except json.JSONDecodeError:
        handler._send_json(400, {"ok": False, "message": "模型配置请求格式错误。"})
        return
    except ModelProfileError as exc:
        handler._send_json(400, {"ok": False, "message": str(exc)})
        return
    except Exception:  # noqa: BLE001 配置错误不得泄露凭证或内部路径
        handler._send_json(500, {"ok": False, "message": "无法读取或保存模型配置，原配置未被主动清除。"})
        return
    handler._send_json(200, result)


# LLM: 菜单与文字 /model 共用：只按已认证 scope 解析 owner 和会话线程，冷用户不物化完整 Agent；线程缺失时按原方式创建。
# 函数用途: 构造只带 home/config/会话库的轻量宿主和当前会话线程，供模型配置读写使用；可能新建“会话设置”线程。
def _scoped_model_host(base_agent, scope) -> tuple[SimpleNamespace, object]:
    owner = resolve_gateway_scope_owner(base_agent, scope)
    base_home = base_agent.home_paths
    scoped_home = home_paths_with_owner(base_home, resolve_owner_home(base_home.root, owner))
    config_host = SimpleNamespace(home_paths=scoped_home, config=base_agent.config)
    store = owner_conversation_store(base_agent, scoped_home)
    config_host.conversation_store = store
    thread = store.threads.get_or_create({
        "canonical_user_id": scope.user_id, "owner_id": scoped_home.owner_id,
        "owner_home": str(scoped_home.owner_home_dir), "channel": scope.channel,
        "channel_conversation_id": scope.conversation_id, "channel_user_id": scope.user_id,
        "title": "会话设置",
    })
    return config_host, thread


# LLM: 目标只认当前列表里的编号或精确配置编号；select 只改本会话，set_default 只改未来默认，都经原写入口。
# 列表与回执不含密钥和接口地址；新增、改密钥、共享开关仍只在 TUI 菜单里做。
# 函数用途: 执行聊天里的 /model 文字命令并返回可直接发给用户的中文回执；select/set_default 会写模型配置。
def execute_model_text_control(base_agent, command: ConversationControlCommand, scope) -> ConversationControlResult:
    try:
        host, thread = _scoped_model_host(base_agent, scope)
        listing = execute_model_profile_operation(host, "list", {}, thread_id=thread.thread_id)
        if command.operation == "view":
            return ConversationControlResult("model", True, render_model_choices(listing))
        row = _choice_by_reference(listing, command.value)
        if row is None:
            return ConversationControlResult(
                "model", False, f"没有编号为 {command.value} 的可选模型。\n" + render_model_choices(listing),
            )
        execute_model_profile_operation(host, command.operation, {"profile_id": row["id"]}, thread_id=thread.thread_id)
    except ModelProfileError as exc:
        return ConversationControlResult("model", False, str(exc))
    except Exception:  # noqa: BLE001 配置错误不得泄露凭证或内部路径
        return ConversationControlResult("model", False, "无法读取或保存模型配置，原配置未被主动清除。")
    if command.operation == "set_default":
        return ConversationControlResult(
            "model", True,
            f"新会话默认使用 {row['model_name']}；已打开的会话保持原模型，当前会话要换请发送 /model <编号>。",
        )
    return ConversationControlResult(
        "model", True,
        f"本会话已选择 {row['model_name']}，上下文 {row.get('model_context_window_tokens', '?')} tokens，下一条消息生效。",
    )


# LLM: 与 TUI 菜单同一过滤：部署默认行或 agentic 可用行；顺序取 list 投影原顺序，编号从 1 开始。
# 函数用途: 返回可供选择的模型行，保证查看与选择两次命令用同一套编号。
def _selectable_choices(listing: dict) -> list[dict]:
    return [row for row in listing.get("profiles", []) if row.get("id") == "default" or row.get("available", True)]


# LLM: 只接受列表编号或精确配置编号，不按模型名模糊匹配，避免私有与共享重名时选错。
# 函数用途: 把用户给的编号解析成列表里的一行，找不到返回 None。
def _choice_by_reference(listing: dict, reference: str) -> dict | None:
    rows = _selectable_choices(listing)
    text = str(reference or "").strip()
    if text.isdigit() and 1 <= int(text) <= len(rows):
        return rows[int(text) - 1]
    return next((row for row in rows if row.get("id") == text), None)


# LLM: 只渲染公开字段（模型名、服务商名、上下文窗口、共享/默认标记），不渲染接口地址或密钥状态以外的内容。
# 函数用途: 把模型列表整理成聊天文字：当前会话模型、新会话默认、编号列表和用法。
def render_model_choices(listing: dict) -> str:
    rows = _selectable_choices(listing)
    names = {row.get("id"): row.get("model_name") for row in listing.get("profiles", [])}
    current = listing.get("selected", "default")
    lines = [
        f"当前会话模型：{names.get(current) or '未选择'}",
        f"新会话默认：{names.get(listing.get('default_selected', 'default')) or '未设置'}",
    ]
    if not rows:
        lines.append("还没有可选模型。请让管理员在终端 TUI 的 /model 里用“管理员共享模型”开放一个模型，再发送 /model 查看。")
    else:
        lines.append("可选模型：")
        for index, row in enumerate(rows, start=1):
            mark = "● " if row.get("id") == current else ""
            label = "（部署默认）" if row.get("id") == "default" else ("（管理员共享）" if row.get("shared") else "")
            provider_name = str(row.get("provider_name") or "")
            provider = f" · {provider_name}" if provider_name and provider_name != row.get("model_name") else ""
            lines.append(f"{index}. {mark}{row.get('model_name')}{provider} · {row.get('model_context_window_tokens', '?')} tokens{label}")
        lines.append("用法：/model <编号> 选为本会话模型；/model default <编号> 设为新会话默认。")
    # 未选模型时投影给的是 TUI 菜单用语（“新增并选择”），聊天里改由上面的“未选择”和用法引导，不重复显示。
    if listing.get("warning") and not (current == "default" and not listing.get("selection_available")):
        lines.append(str(listing["warning"]))
    lines.append("新增或修改模型、密钥请在终端 TUI 的 /model 里操作，聊天里不收发密钥。")
    return "\n".join(lines)
