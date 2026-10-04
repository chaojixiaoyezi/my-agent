# LLM: B4 只复制宿主已有身份和队列/回执事实；关闭先返回，不读安装表、不等待插件，发布统一走 B3。
# 模块用途: 把 Gateway 的提示、回合和控制收口接到六事件投影，观察失败不改变业务。
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ..plugin_events.points import EventPointContext, emit_event


# LLM: 复用宿主已冻结的路径，提供 B3 安装读取与连接需要的两个字段；这里不解析或检查文件系统。
# 类用途: 给后台事件中心传当前用户的既有插件地址，不创建目录或读取安装表。
@dataclass(frozen=True)
class _EventOwner:
    home_dir: Path
    plugins_dir: Path


# LLM: server 是 HTTP 宿主唯一实例；Agent 已由请求工作者按 owner 装配，事件不从工具参数或正文选择 owner。
# 函数用途: 从内存宿主事实构造发布上下文，关闭或宿主缺失时零发布、零磁盘读取。
def gateway_event_context(agent, routing: dict, *, server=None) -> EventPointContext | None:
    try:
        if getattr(agent.config, 'plugin_events_enabled', False) is not True:
            return None
        from . import http_service, plugin_panels_http

        server = server if server is not None else http_service._server_instance
        if server is None or server.agent.home_paths.root != agent.home_paths.root:
            return None
        owner = _EventOwner(agent.home_paths.owner_home_dir, agent.home_paths.owner_plugins_dir)
        return EventPointContext(agent.config, partial(plugin_panels_http.publish_plugin_event, server, owner),
                                 str(routing.get('channel') or ''), str(routing.get('thread_id') or ''),
                                 str(routing.get('actor') or 'main'))
    except Exception:  # noqa: BLE001 观察装配故障不能中止入队或模型执行
        return None


# LLM: 复用请求 worker 的唯一结构化 owner 解析（含管理员绑定），不创建 scoped Agent；解析失败按未知处理。
# 函数用途: 确认提示属于基础 owner，防止 local/tui 的其它用户被错投到主用户插件。
def _prompt_has_base_owner(agent, request: dict) -> bool:
    try:
        from ..user_space.owner_resolver import owner_identity_from_config
        from .request_worker import _resolve_request_owner_identity

        return _resolve_request_owner_identity(agent, request) == owner_identity_from_config(agent.config)
    except Exception:  # noqa: BLE001 无法证明归属时不发观察，入队和 worker 仍沿原权限链处理
        return False


# LLM: 先检查总开关，再复用 worker owner 解析；频道名不证明基础 owner，不扩大发送范围，不初始化用户 Agent。
# 函数用途: 在提示首次排队成功后仅向基础 owner 发观察；重放、插话和排队失败不进入此函数。
def prompt_queued(server, request: dict) -> None:
    if getattr(server.agent.config, 'plugin_events_enabled', False) is not True:
        return
    if not _prompt_has_base_owner(server.agent, request):
        return
    metadata = request.get('metadata') or {}
    channel = str(metadata.get('channel') or '')
    conversation = request.get('conversation') or {}
    context = gateway_event_context(server.agent, {'channel': channel,
        'thread_id': conversation.get('channel_conversation_id') or ''}, server=server)
    emit_event(context, 'prompt_submitted', {'request_id': request.get('id') or '',
        'prompt': request.get('prompt') or request.get('goal') or '',
        'has_attachments': bool(request.get('input_media'))})


# LLM: 控制回执由唯一执行服务冻结 owner；只在新的 completed 持久提交后调用，既有回执重放不发。
# 函数用途: 将已落定控制结果减量为命令名和操作状态，不带参数或错误正文。
def command_completed(agent, receipt) -> None:
    context = gateway_event_context(agent, {'channel': receipt.channel, 'thread_id': receipt.conversation_id})
    emit_event(context, 'command_executed', {'command': '/' + receipt.command_kind,
        'operation_id': receipt.operation_id, 'state': 'ok' if receipt.result.get('ok') is True else 'failed'})


# LLM: 回合从已解析队列记录取得通道和会话，不读正文判定身份；只复制配置中的模型名，不带服务商或密钥。
# 函数用途: 在请求真正开始执行前发布回合开始。
def turn_started(agent, request: dict) -> EventPointContext | None:
    conversation = request.get('conversation') or {}
    metadata = request.get('metadata') or {}
    context = gateway_event_context(agent, {'channel': conversation.get('channel') or metadata.get('channel') or '',
        'thread_id': conversation.get('channel_conversation_id') or ''})
    emit_event(context, 'turn_started', {'request_id': request.get('id') or '',
        'model_name': getattr(agent.config, 'model_name', '')})
    return context


# LLM: 四种状态只读终态响应或明确的重启接续标记；耗时由调用方单调时钟计算，工具数量不拿轮数代替。
# 函数用途: 在回合收口时发布最小终态，保留失败和中断事实。
def turn_ended(context, response: dict, duration_ms: float) -> None:
    status = 'interrupted' if response.get('restart_resume') else str(response.get('status') or 'failed')
    emit_event(context, 'turn_ended', {'request_id': response.get('id') or '',
        'status': status if status in {'done', 'failed', 'stopped', 'interrupted'} else 'failed',
        'duration_ms': duration_ms, 'tool_calls': response.get('tool_calls', 0),
        'error_code': response.get('error_code') or ''})
