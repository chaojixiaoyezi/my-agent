"""原TUI显式后台会话的审批兴趣运输；决定仍只进入原审批引擎。"""
from __future__ import annotations

import time
from contextlib import ExitStack
from uuid import uuid4

from ..common.opaque_id import validate_opaque_id
from ..conversation import background_approval_views as views
from ..conversation.agent_tool_approval import (
    list_pending_agent_tool_approvals_report,
    resolve_agent_tool_approval,
    tool_approval_scope,
)
from ..conversation.background_agents import BackgroundAgentService
from ..conversation.business_access import BusinessError
from ..conversation.host_actor import HostChannelActor


def _binding(agent, scope, value):
    if not isinstance(value, dict) or set(value) - {'agent_id', 'view_id', 'root_task_id'}:
        raise ValueError('BACKGROUND_APPROVAL_VIEW_INVALID')
    agent_id = validate_opaque_id(value.get('agent_id'), kind='agent_id')
    view_id = validate_opaque_id(value.get('view_id'), kind='approval_view_id')
    if scope.channel != 'chat':
        raise ValueError('BACKGROUND_APPROVAL_PRIVATE_TUI_REQUIRED')
    store = agent.conversation_store
    source, error = store.threads.resolve_report(channel=scope.channel,
        channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id)
    if error is not None or source is None or source.owner_id != agent.home_paths.owner_id:
        raise ValueError('BACKGROUND_APPROVAL_SOURCE_NOT_FOUND')
    actor = HostChannelActor(agent.home_paths.owner_id, source.thread_id, 'approval-view-' + uuid4().hex,
                             scope.channel, scope.conversation_id, scope.user_id)
    # 原业务读口先核空间、Owner和当前Actor；名字或列表投影不能代替目标授权。
    target = store.threads.load(agent_id)
    if target is None or target.owner_id != agent.home_paths.owner_id:
        raise ValueError('BACKGROUND_APPROVAL_TARGET_NOT_FOUND')
    BackgroundAgentService(agent).get(agent_id, actor=actor)
    return target, {'view_id': view_id, 'source_thread_id': source.thread_id,
                    'actor_id': scope.user_id, 'conversation_id': scope.conversation_id}


def _current_root(agent, target):
    target = agent.conversation_store.threads.require(target.thread_id)
    if target.owner_id != agent.home_paths.owner_id:
        return ''
    root = str(target.workspace_task_id or '')
    if not root:
        return ''
    link, error = agent.conversation_store.tasks.load_report(root)
    if error is not None or link is None or link.thread_id != target.thread_id or link.status != 'active':
        return ''
    return root


def execute_view(agent, scope, body):
    target, binding = _binding(agent, scope, body.get('view'))
    action = body.get('operation')
    if action == 'close':
        views.close(agent, target.thread_id, binding)
        return {'ok': True, 'closed': True, 'agent_permission_requests': []}
    if action != 'observe':
        raise ValueError('BACKGROUND_APPROVAL_OPERATION_INVALID')
    from ..conversation.background_team_lifecycle import background_identity
    if background_identity(target).identity_state == 'retired':
        return {'ok': True, 'root_task_id': '', 'coverage': 'complete', 'agent_permission_requests': []}
    views.renew(agent, target.thread_id, binding)
    root = _current_root(agent, target)
    rows, complete = ([], True) if not root else list_pending_agent_tool_approvals_report(
        agent, root_task_id=root, deadline=time.monotonic() + .05)
    return {'ok': True, 'root_task_id': root, 'coverage': 'complete' if complete else 'partial',
            'agent_permission_requests': rows}


def resolve_view_permission(agent, scope, body):
    """已选兴趣只选择原root；精确request/attempt/permission与一次消费全部复用原引擎。"""
    value = body.get('background_view')
    target, binding = _binding(agent, scope, value)
    root = _current_root(agent, target)
    if not root or value.get('root_task_id') != root:
        raise ValueError('BACKGROUND_APPROVAL_ROOT_STALE')
    with views.decision_scope(agent, target.thread_id, binding):
        selected = tool_approval_scope(agent, body.get('run_id', ''))
        if not selected.active or selected.root_task_id != root or _current_root(agent, target) != root:
            raise ValueError('BACKGROUND_APPROVAL_ATTEMPT_STALE')
        from ..conversation.agent_control import AgentControlError
        try:
            return resolve_agent_tool_approval(agent, run_id=body.get('run_id', ''),
                request_value=body.get('request'), decision_value=body.get('decision'))
        except FileNotFoundError as error:
            raise AgentControlError(409, 'AGENT_APPROVAL_STALE',
                '原审批已经结束，决定未写入；请读取当前审批，不得复用旧票。') from error
        except (ValueError, TypeError) as error:
            raise AgentControlError(409, 'AGENT_APPROVAL_MISMATCH',
                '原请求、attempt或permission不匹配，决定未写入；请重新进入原后台会话核对当前请求。') from error
        except OSError as error:
            raise AgentControlError(503, 'AGENT_APPROVAL_UNAVAILABLE',
                '原审批存储不可用，决定写入未确认；核对原票状态，禁止盲目重发。') from error


def handle_background_approval_view(handler, server):
    from ..plugin_host.errors import HostAPIError
    from .business_api import owner_complete_host
    from .control_service import resolve_gateway_scope_owner
    from .http_handlers import _gateway_control_scope, _read_agent_control_body, _request_channel

    body = _read_agent_control_body(handler, server)
    if body is None:
        return
    try:
        user_id, channel = _request_channel(handler)
        scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
        owner = resolve_gateway_scope_owner(server.agent, scope)
        with ExitStack() as stack:
            agent = owner_complete_host(server, owner, stack)
            result = execute_view(agent, scope, body)
    except HostAPIError as error:
        handler._send_json(error.status, {'ok': False, 'error_code': error.code, 'effect_outcome': 'not_started',
            'next_action': '在原已授权TUI会话核对本人身份、后台会话读取权和宿主状态后重新进入。'})
        return
    except PermissionError as error:
        handler._send_json(403, {'ok': False, 'error_code': str(error), 'effect_outcome': 'not_started',
            'next_action': '当前Actor不能读取该后台会话；回到本人已授权的会话，不得更换目标或复用旧审批引用。'})
        return
    except (ValueError, BusinessError) as error:
        code = str(getattr(error, 'code', '') or error)
        handler._send_json(409, {'ok': False, 'error_code': code, 'effect_outcome': 'not_started',
            'next_action': '在原TUI重新进入已授权后台会话；旧界面引用不得重试审批。'})
        return
    except OSError as error:
        handler._send_json(503, {'ok': False, 'error_code': 'BACKGROUND_APPROVAL_VIEW_IO_ERROR',
            'error_type': type(error).__name__, 'errno': error.errno, 'effect_outcome': 'unknown',
            'side_effect_state': 'interest_update_unconfirmed',
            'next_action': '核对原后台会话审批接收兴趣与存储可读性；没有批准工具，禁止重放原工具调用。'})
        return
    handler._send_json(200, result)
