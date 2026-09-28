
# LLM: chat 分派读取公共 command_catalog，不再声明命令目录；执行权仍归原 typed parser、权限与显式 handler。
# 模块用途: 渲染公共帮助并处理当前界面的控制、记忆和提示文件命令，错误插件输入在普通聊天前结束。

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ...agent.command_catalog import (
    COMMAND_CATALOG,
    COMMAND_INDEX,
    system_slash_command_name,
    unavailable_command_message,
)
from ...agent.conversation.control_commands import (
    parse_conversation_control,
    parse_conversation_task_command,
)
from ...agent.plugin_commands import plugin_namespace
from .plugin_command_client import PluginCommandClient
from .slash_command_types import SlashCommandContext


# LLM: 帮助投影公共声明及其用法变体，保持 30 列布局；文案不能反向决定解析与执行。
# 函数用途: 为 plain/TUI 生成同一份中文帮助，并准确标示尚未开放的命令。
def _render_chat_help_text() -> str:
    lines = ["可用命令："]
    for spec in COMMAND_CATALOG:
        for usage, summary in ((spec.usage, spec.summary), *spec.help_variants):
            lines.append(f"{usage:<30} {summary}")
    return "\n".join(lines) + "\n"


CHAT_HELP_TEXT = _render_chat_help_text()


SlashHandler = Callable[[str, SlashCommandContext, bool], bool | None]


# LLM: 所有显式命令先经现有 handler；返回已处理后，TUI/plain 不得再把原输入当作聊天或活动插话。
# 函数用途: 顺序分派当前界面支持的命令，执行副作用仍由各原处理器负责。
def handle_common_slash_command(
    user: str,
    *,
    ctx: SlashCommandContext,
    include_plain_help: bool = False,
) -> bool:
    handlers: tuple[SlashHandler, ...] = (
        _handle_help_command,
        _handle_sessions_command,
        _handle_tell_command,
        _handle_permissions_command,
        _handle_plugin_command,
        _handle_control_command,
        _handle_remember_command,
        _handle_memory_command,
        _handle_prompt_file_command,
        _handle_audit_command,
        _handle_unsupported_slash_command,
    )
    for handler in handlers:
        result = handler(user, ctx, include_plain_help)
        if result is not None:
            return result
    return False


# LLM: 目录来自显式模式选定的宿主；原 revision 与命令交互引用分别传递，不借聊天回合或模型执行权限。
# 函数用途: 在输入线程外提交插件管理或业务命令，并展示原宿主结果。
def _handle_plugin_command(user: str, ctx: SlashCommandContext, include_plain_help: bool) -> bool | None:
    del include_plain_help
    if plugin_namespace(user) is None:
        return None
    client = ctx.plugin_client or PluginCommandClient(ctx.agent, ctx.conversation_id, use_gateway=ctx.use_gateway)
    result = client.command(user, revision=ctx.plugin_revision, interaction=ctx.command_interaction)
    ctx.print_line(str(result["message"]))
    return True


# LLM: plain 命令也只消费封闭枚举；Gateway 与本地共用权限配置入口，不进模型任务队列。
# 函数用途: 无菜单终端可以查看模式，或用明确参数切换；rich TUI 的无参数命令打开菜单。
def _handle_permissions_command(user: str, ctx: SlashCommandContext, include_plain_help: bool) -> bool | None:
    del include_plain_help
    if user != "/permissions" and not user.startswith("/permissions "):
        return None
    from .tui_permissions_menu import request_permissions

    mode = user.removeprefix("/permissions").strip()
    result = request_permissions(ctx.agent, ctx.conversation_id, "set" if mode else "get", mode or None)
    ctx.print_line(str(result.get("message") or "权限配置操作失败。"))
    if not mode:
        ctx.print_line("用法：/permissions ask|auto|full-access；Full Access 仅本机管理员可用。")
    return True


# LLM: Local slash parsing delegates to the adapter-neutral typed control protocol.
# 函数用途：即时处理状态、单次纠偏和停止命令，不把它们排进普通聊天任务。
def _handle_control_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    command = parse_conversation_control(user)
    if command is None:
        return None
    if not command.valid:
        ctx.print_line(command.usage)
        return True
    if ctx.control_executor is None:
        ctx.print_line("当前聊天界面没有可用的任务控制入口。")
        return True
    result = ctx.control_executor(command)
    if command.kind == "stop":
        # `/stop` is the text equivalent of the UI stop button.  The control
        # result remains available to programmatic callers, but the chat UI
        # must not turn the button press into a second assistant-style message.
        return True
    ctx.print_line(str(getattr(result, "message", "") or "控制命令没有返回结果。"))
    return True


def _handle_help_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    if user != "/help":
        return None
    suffix = "Ctrl+C                        退出\n其他输入                      发送普通消息\n"
    ctx.print_line(CHAT_HELP_TEXT + (suffix if include_plain_help else ""))
    return True


# LLM: `/sessions` only projects SessionManager's current-owner records and exact CLI resume
# commands. It must not scan another owner, infer a title from conversation text, switch the
# live TUI in place, or mutate session/task state.
# 函数用途: 在当前界面列出本用户最近会话，并明确告诉用户怎样退出后恢复指定会话。
def _handle_sessions_command(
    user: str,
    ctx: SlashCommandContext,
    include_plain_help: bool,
) -> bool | None:
    del include_plain_help
    if user == "/sessions threads":
        return _print_message_targets(ctx)
    if user != "/sessions":
        return None
    from ...agent.session.manager import SessionManager

    manager = SessionManager(ctx.agent.config)
    sessions, load_errors = manager.list_sessions_report()
    recent = sessions[:10]
    if not recent:
        ctx.print_line("当前用户还没有可恢复的历史会话。")
        return True
    lines = ["最近会话（仅当前用户，按更新时间倒序）："]
    for session in recent:
        marker = "（当前）" if session.session_id == ctx.conversation_id else ""
        updated = datetime.fromtimestamp(session.updated_at).astimezone().strftime(
            "%Y-%m-%d %H:%M"
        )
        channel = str(session.last_active_channel or "chat")
        lines.append(f"- {updated}  {session.session_id}{marker}  [{channel}]")
    if len(sessions) > len(recent):
        lines.append(f"… 另有 {len(sessions) - len(recent)} 条较早会话未显示。")
    if load_errors:
        lines.append(f"另有 {len(load_errors)} 条损坏会话记录未展示。")
    lines.extend(
        (
            "恢复旧会话：先输入 /exit 退出当前界面，再运行：",
            "my-agent resume <session_id>",
            "要查看可接收会话消息的目标，输入：/sessions threads",
        )
    )
    ctx.print_line("\n".join(lines))
    return True


# LLM: 会话消息目标是 canonical ConversationThread，与 /sessions 的 CLI 恢复记录（SessionManager）
#   不是同一套；这里只投影本 owner 的 thread 列表，不推断标题、不跨 owner、不改状态。
# 函数用途: 列出可接收会话消息的会话编号，供 /tell 使用。
def _print_message_targets(ctx: SlashCommandContext) -> bool:
    store = getattr(ctx.agent, "conversation_store", None)
    threads = getattr(store, "threads", None)
    if threads is None:
        ctx.print_line("当前运行环境没有会话存储，无法列出会话目标。")
        return True
    items, load_errors = threads.list_report(limit=20)
    if not items:
        ctx.print_line("当前用户还没有可接收消息的会话。")
        return True
    lines = ["可接收会话消息的会话（仅当前用户）："]
    for thread in items:
        thread_id = str(getattr(thread, "thread_id", "") or "")
        marker = "（当前）" if thread_id == ctx.conversation_id else ""
        title = str(getattr(thread, "title", "") or "").strip() or "(无标题)"
        status = str(getattr(thread, "status", "") or "active")
        lines.append(f"- {thread_id}{marker}  [{status}]  {title}")
    if load_errors:
        lines.append(f"另有 {len(load_errors)} 条损坏会话记录未展示。")
    lines.append("用法：/tell <会话编号> <消息>")
    ctx.print_line("\n".join(lines))
    return True


# LLM: `/tell` 是会话间消息的 TUI 入口，与模型工具共用同一权限判定与投递语义（GuidanceStore + WakeStore）。
#   身份只从 agent.home_paths 取；关闭或身份不可用时返回结构化错误文案，不静默失败。
# 函数用途: 给同一用户下的另一个本地会话发一条消息。
def _handle_tell_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if not user.startswith("/tell"):
        return None
    rest = user[len("/tell"):].strip()
    target_thread_id, _, message = rest.partition(" ")
    target_thread_id = target_thread_id.strip()
    message = message.strip()
    if not target_thread_id or not message:
        ctx.print_line("用法：/tell <会话编号> <消息>；会话编号可用 /sessions threads 查看。")
        return True
    ctx.print_line(_tell_message(ctx, target_thread_id, message))
    return True


# LLM: 投递语义与模型工具一致：先判权限（读结构化身份与开关），通过后 append_once 幂等入队，
#   目标 active 时再唤醒。返回给用户的是人的可读文案；失败带稳定错误码。
# 函数用途: 执行一次会话间发消息，返回给 TUI 显示的文案。
def _tell_message(ctx: SlashCommandContext, target_thread_id: str, message: str) -> str:
    from ...agent.capability.runtime_config_reload import capability_config_for_agent
    from ...agent.conversation.session_messaging import (
        SESSION_KIND_MESSAGE,
        SessionMessagingRequest,
        decide_session_messaging,
        session_messaging_tool_visible,
    )
    from ...agent.user_space.owner_resolver import OwnerIdentity

    agent = ctx.agent
    home = getattr(agent, "home_paths", None)
    provider = str(getattr(home, "owner_provider", "") or "").strip()
    owner_kind = str(getattr(home, "owner_kind", "") or "").strip()
    owner_id = str(getattr(home, "owner_id", "") or "").strip()
    if not (provider and owner_kind and owner_id):
        return "无法确定当前会话的 owner 身份；消息没有投递。（SESSION_IDENTITY_UNAVAILABLE）"
    identity = OwnerIdentity(provider=provider, owner_kind=owner_kind, owner_id=owner_id)
    config = capability_config_for_agent(agent)
    if not session_messaging_tool_visible(home, config):
        return "会话间消息当前对这类身份关闭。（SESSION_MESSAGING_DISABLED）"

    store = getattr(agent, "conversation_store", None)
    threads = getattr(store, "threads", None)
    if threads is None:
        return "当前运行环境没有会话存储，不能发送会话消息。"
    try:
        thread, load_error = threads.load_report(target_thread_id)
    except Exception:
        return "读取目标会话失败；消息没有投递。（TOOL_EXECUTION_FAILED）"
    target_owner = identity if (load_error is None and thread is not None) else None
    target_channel = _thread_channel(thread) if thread is not None else ""
    decision = decide_session_messaging(
        SessionMessagingRequest(
            sender_identity=identity,
            sender_thread_id=ctx.conversation_id,
            target_thread_id=target_thread_id,
            target_owner_identity=target_owner,
            kind=SESSION_KIND_MESSAGE,
            messaging_admin_enabled=bool(getattr(config, "session_messaging_admin_enabled", True)),
            messaging_user_enabled=bool(getattr(config, "session_messaging_user_enabled", False)),
            task_admin_enabled=bool(getattr(config, "session_task_admin_enabled", True)),
            target_channel=target_channel,
        )
    )
    if not decision.allowed:
        return f"消息没有投递。（{decision.error_code}）"
    try:
        entry = store.guidance.append_once(
            {
                "target_type": "thread",
                "target_id": target_thread_id,
                "message": message,
                "sender": ctx.conversation_id or "session",
                "priority": "normal",
                "delivery": "next_turn",
                "metadata": {"origin_kind": "session_message", "origin_thread_id": ctx.conversation_id},
            },
            dedupe_key=f"session_message:{ctx.conversation_id}->{target_thread_id}",
        )
        wake_id = ""
        if str(getattr(thread, "status", "") or "active").strip() == "active":
            wakes = getattr(store, "wakes", None)
            if wakes is not None:
                signal = wakes.raise_signal(
                    {
                        "thread_id": target_thread_id,
                        "urgency": "normal",
                        "reason": "session_message",
                        "summary": "收到来自另一个会话的消息",
                        "metadata": {"origin_kind": "session_message", "origin_thread_id": ctx.conversation_id},
                    }
                )
                wake_id = str(getattr(signal, "wake_signal_id", "") or "")
    except Exception:
        return "写入目标会话消息箱失败；消息没有投递。（TOOL_EXECUTION_FAILED）"
    suffix = f"，已唤醒 {wake_id}" if wake_id else ""
    return f"消息已耐久排队到 {target_thread_id}（{entry.guidance_id}）{suffix}。目标会在回合边界读取；这不代表已执行。"


# LLM: 从 canonical thread 的 channel_bindings 取渠道；无绑定返回空串（本机会话）。
# 函数用途: 判断目标会话渠道，供权限判定。
def _thread_channel(thread: object) -> str:
    bindings = getattr(thread, "channel_bindings", ()) or ()
    for binding in bindings:
        channel = str(getattr(binding, "channel", "") or "").strip()
        if channel:
            return channel
    return ""


def _handle_remember_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if not user.startswith("/remember "):
        return None
    if getattr(ctx.agent, "gateway_client_only", False) is True:
        result = ctx.agent.request_memory(
            operation="remember",
            session_id=ctx.conversation_id,
            content=user[len("/remember "):],
            limit=1,
        )
        records = result.get("records") if isinstance(result, dict) else []
        if result.get("ok") and isinstance(records, list) and records:
            ctx.print_line(f"已记住：{str(records[0].get('content') or '')}")
        elif result.get("outcome") == "unknown":
            ctx.print_line(
                "记忆保存结果暂时无法确认；Gateway 可能已经写入，系统没有自动重复保存。"
                "可用 /memory <关键词> 查询。"
            )
        else:
            ctx.print_line(
                "记忆保存失败："
                + str(result.get("error_code") or "GATEWAY_UNAVAILABLE")
            )
        return True
    rec = ctx.agent.remember(user[len("/remember "):], kind="fact")
    ctx.print_line(f"已记住：{rec.content}")
    return True


def _handle_memory_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if not user.startswith("/memory"):
        return None
    query = user[len("/memory"):].strip()
    if getattr(ctx.agent, "gateway_client_only", False) is True:
        result = ctx.agent.request_memory(
            operation="search" if query else "recent",
            session_id=ctx.conversation_id,
            query=query,
            limit=ctx.memory_limit,
        )
        if not result.get("ok"):
            ctx.print_line(
                "记忆读取失败："
                + str(result.get("error_code") or "GATEWAY_UNAVAILABLE")
            )
            return True
        records = result.get("records")
        _print_memory_records(ctx, records if isinstance(records, list) else [])
        return True
    records = ctx.agent.recall(query, ctx.memory_limit) if query else _recent_memory(ctx)
    _print_memory_records(ctx, records)
    return True


def _recent_memory(ctx: SlashCommandContext):
    return ctx.agent.memory.all()[-ctx.memory_limit:]


def _print_memory_records(ctx: SlashCommandContext, records) -> None:
    if not records:
        ctx.print_line("没有找到记忆记录。")
        return
    for rec in records:
        if isinstance(rec, dict):
            kind = str(rec.get("kind") or "")
            role = str(rec.get("role") or "")
            content = str(rec.get("content") or "")
        else:
            kind = str(getattr(rec, "kind", "") or "")
            role = str(getattr(rec, "role", "") or "")
            content = str(getattr(rec, "content", "") or "")
        ctx.print_line(f"- [{kind}] {role}: {content}")


def _handle_prompt_file_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if not user.startswith("/prompt-file "):
        return None
    ctx.prompt_files.append(user[len("/prompt-file "):].strip())
    ctx.print_line(f"已添加提示文件；当前共 {len(ctx.prompt_files)} 个。")
    return True


def _handle_audit_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    command = parse_conversation_task_command(user)
    if command is None or command.valid:
        return None
    ctx.print_line(command.usage)
    return True


# LLM: 公共命名空间判据也捕获无效插件 ID；只展示不可用结果，不转给控制执行器、模型或 Shell。
# 函数用途: 消费当前界面不能执行的明确系统命令，保留 Audit 任务和 show-prompt 的原交接。
def _handle_unsupported_slash_command(
    user: str, ctx: SlashCommandContext, include_plain_help: bool
) -> bool | None:
    del include_plain_help
    if parse_conversation_task_command(user) is not None:
        return None
    name = system_slash_command_name(user)
    if not name:
        return None
    if name == "show-prompt":
        return None
    ctx.print_line(unavailable_command_message(name))
    return True


# LLM: slash 别名读取唯一目录；无斜杠退出词保持现有范围，不因别名归一扩展自然语言控制。
# 函数用途: 识别退出当前聊天界面的显式输入，不停止 Gateway 或其它会话。
def is_exit_command(user: str) -> bool:
    spec = COMMAND_INDEX["exit"]
    return user.lower() in {"exit", "logout", "退出", *("/" + name for name in (spec.name, *spec.aliases))}


# LLM: `/expand` 的帮助目录、plain TUI 与 rich TUI 必须共享这一解析器；显式 last
# 和省略参数语义相同，未知文本必须返回 None 而不能猜编号。
# 函数用途: 解析要展开最后一条还是指定编号的助手回复。
def parse_expand_target(raw: str) -> str | None:
    if raw == "/expand":
        return "last"
    target = raw[len("/expand "):].strip()
    if not target or target == "last":
        return "last"
    if target.isdigit():
        return target
    return None


def is_show_prompt_command(user: str) -> tuple[bool, str]:
    if user.startswith("/show-prompt "):
        return True, user[len("/show-prompt "):]
    return False, user


__all__ = [
    "CHAT_HELP_TEXT",
    "handle_common_slash_command",
    "is_exit_command",
    "is_show_prompt_command",
    "parse_expand_target",
]
