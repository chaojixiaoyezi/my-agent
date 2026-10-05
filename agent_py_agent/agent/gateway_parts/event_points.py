# LLM: B4 只复制宿主已有身份和队列/回执事实；关闭先返回，不读安装表、不等待插件，发布统一走 B3。
# 模块用途: 把 Gateway 的提示、回合和控制收口接到六事件投影，观察失败不改变业务。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ..plugin_events.points import EventPointContext, emit_event, warn_event_assembly_failure


# LLM: 复用宿主已冻结的路径，提供 B3 安装读取与连接需要的两个字段；这里不解析或检查文件系统。
# 类用途: 给后台事件中心传当前用户的既有插件地址，不创建目录或读取安装表。
@dataclass(frozen=True)
class _EventOwner:
    home_dir: Path
    plugins_dir: Path


# LLM: 安全读取可选观察开关，严格 True 才继续；缺配置或 getter 异常只关闭观察，不能包住权限或业务执行。
# 函数用途: 给全部 Gateway 事件入口同源的关闭短路，不读路由、回执、正文、安装表或磁盘。
def _enabled_event_config(agent):
    try:
        config = getattr(agent, 'config', None)
        return config if getattr(config, 'plugin_events_enabled', False) is True else None
    except Exception:  # noqa: BLE001 配置读取故障只影响可选观察
        return None


# LLM: server 是唯一 HTTP 宿主；开关在路由/路径装配前检查，启用后的异常仅一次固定原因诊断并返回 None，不改 owner/权限链。
# 函数用途: 从内存宿主事实构造发布上下文，关闭、缺失或装配失败时零发布、零磁盘读取。
def gateway_event_context(agent, routing: dict, *, server=None) -> EventPointContext | None:
    enabled = False
    try:
        config = _enabled_event_config(agent)
        if config is None:
            return None
        enabled = True
        from . import http_service, plugin_panels_http

        server = server if server is not None else http_service._server_instance
        if server is None or server.agent.home_paths.root != agent.home_paths.root:
            return None
        owner = _EventOwner(agent.home_paths.owner_home_dir, agent.home_paths.owner_plugins_dir)
        return EventPointContext(config, partial(plugin_panels_http.publish_plugin_event, server, owner),
                                 str(routing.get('channel') or ''), str(routing.get('thread_id') or ''),
                                 str(routing.get('actor') or 'main'),
                                 str(routing.get('channel_conversation_id') or ''))
    except Exception:  # noqa: BLE001 观察装配故障不能中止入队或模型执行
        if enabled:
            warn_event_assembly_failure('PLUGIN_EVENT_GATEWAY_CONTEXT_FAILED')
        return None


# LLM: 复用请求 worker 的唯一结构化 owner 解析；解析异常按未知处理，只在确认启用时记固定原因，不记录身份或错误正文。
# 函数用途: 确认提示属于基础 owner，防止 local/tui 的其它用户被错投到主用户插件。
def _prompt_has_base_owner(agent, request: dict) -> bool:
    try:
        from ..user_space.owner_resolver import owner_identity_from_config
        from .request_worker import _resolve_request_owner_identity

        return _resolve_request_owner_identity(agent, request) == owner_identity_from_config(agent.config)
    except Exception:  # noqa: BLE001 无法证明归属时不发观察，入队和 worker 仍沿原权限链处理
        if _enabled_event_config(agent) is not None:
            warn_event_assembly_failure('PLUGIN_EVENT_PROMPT_OWNER_UNAVAILABLE')
        return False


# LLM: 开关先于 owner/路由读取；观察装配异常隔离，确认启用后只记一次固定原因，不影响已成功入队事实。
#   v2（tref2）：入队时点线程可能还没解析（新会话第一次必然没有），thread_ref 留空（A2）；
#   渠道会话号此时已有，进 channel_conversation_ref 供插件在提交时点归组。
# 函数用途: 首次排队后仅向基础 owner 发提示观察；缺配置、字段或装配故障不发，关闭不读取请求。
def prompt_queued(server, request: dict) -> None:
    enabled = False
    try:
        agent = server.agent
        if _enabled_event_config(agent) is None:
            return
        enabled = True
        if not _prompt_has_base_owner(agent, request):
            return
        metadata = request.get('metadata') or {}
        channel = str(metadata.get('channel') or '')
        conversation = request.get('conversation') or {}
        context = gateway_event_context(agent, {'channel': channel,
            'thread_id': '',
            'channel_conversation_id': conversation.get('channel_conversation_id') or ''}, server=server)
        if context is None:
            return
        emit_event(context, 'prompt_submitted', {'request_id': request.get('id') or '',
            'prompt': request.get('prompt') or request.get('goal') or '',
            'has_attachments': bool(request.get('input_media'))})
    except Exception:  # noqa: BLE001 提示观察不能改变 HTTP 入队结果
        if enabled:
            warn_event_assembly_failure('PLUGIN_EVENT_PROMPT_ASSEMBLY_FAILED')
        return


# LLM: 控制回执由唯一执行服务冻结 owner；关闭零回执读取，启用后字段/装配异常仅一次固定原因日志，不改变控制结果。
#   v2（tref2）：thread_ref 用回执里执行时解析的 conversation_thread_id（权威会话线程），
#   渠道会话号另进 channel_conversation_ref。
# 函数用途: 将新 completed 回执减量为命令名和状态；缺上下文不发，不带参数或错误正文。
def command_completed(agent, receipt) -> None:
    enabled = False
    try:
        if _enabled_event_config(agent) is None:
            return
        enabled = True
        context = gateway_event_context(agent, {'channel': receipt.channel,
            'thread_id': receipt.conversation_thread_id,
            'channel_conversation_id': receipt.conversation_id})
        if context is None:
            return
        emit_event(context, 'command_executed', {'command': '/' + receipt.command_kind,
            'operation_id': receipt.operation_id, 'state': 'ok' if receipt.result.get('ok') is True else 'failed'})
    except Exception:  # noqa: BLE001 命令观察不能改变已落定的业务回执
        if enabled:
            warn_event_assembly_failure('PLUGIN_EVENT_COMMAND_ASSEMBLY_FAILED')
        return


# LLM: 开关先于请求字段读取；装配异常返回 None，启用后的固定原因诊断不包含通道/会话/模型或错误正文。
#   v2（tref2）：会话线程由调用方给的 thread_id_of 在确认开启后才解析（与工具事件同源；关闭时一次都不调，
#   不为事件多查会话库）；渠道会话号从请求里读，进 channel_conversation_ref。两者都不从正文推断。
# 函数用途: 请求真正开始前发最小回合观察；缺失或关闭时不读路由、不解析线程，不带服务商或密钥。
def turn_started(agent, request: dict, *, thread_id_of: Callable[[], str] | None = None) -> EventPointContext | None:
    enabled = False
    try:
        config = _enabled_event_config(agent)
        if config is None:
            return None
        enabled = True
        thread_id = str(thread_id_of() or "") if thread_id_of is not None else ""
        conversation = request.get('conversation') or {}
        metadata = request.get('metadata') or {}
        context = gateway_event_context(agent, {'channel': conversation.get('channel') or metadata.get('channel') or '',
            'thread_id': thread_id,
            'channel_conversation_id': conversation.get('channel_conversation_id') or ''})
        if context is None:
            return None
        emit_event(context, 'turn_started', {'request_id': request.get('id') or '',
            'model_name': getattr(config, 'model_name', '')})
        return context
    except Exception:  # noqa: BLE001 回合观察装配失败不能阻止模型执行
        if enabled:
            warn_event_assembly_failure('PLUGIN_EVENT_TURN_START_ASSEMBLY_FAILED')
        return None


# LLM: 缺上下文/关闭先返回，零响应读取；启用后的字段故障仅一次固定原因诊断，不能翻转真实回合终态。
# 函数用途: 回合收口时发布最小终态，保留失败和中断事实；观察不可用则不发。
def turn_ended(context, response: dict, duration_ms: float) -> None:
    enabled = False
    try:
        if _enabled_event_config(context) is None:
            return
        enabled = True
        status = 'interrupted' if response.get('restart_resume') else str(response.get('status') or 'failed')
        emit_event(context, 'turn_ended', {'request_id': response.get('id') or '',
            'status': status if status in {'done', 'failed', 'stopped', 'interrupted'} else 'failed',
            'duration_ms': duration_ms, 'tool_calls': response.get('tool_calls', 0),
            'error_code': response.get('error_code') or ''})
    except Exception:  # noqa: BLE001 回合收口观察故障不能反噬业务终态
        if enabled:
            warn_event_assembly_failure('PLUGIN_EVENT_TURN_END_ASSEMBLY_FAILED')
        return
