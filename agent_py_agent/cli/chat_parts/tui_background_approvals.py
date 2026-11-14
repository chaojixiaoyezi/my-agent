"""只为已显式进入的后台会话接收原工具审批，列表观察不创建兴趣。"""
from __future__ import annotations

import threading
import time
from uuid import uuid4

from ..chat_client_context import post_gateway_json


def start_background_approval_observation(agent, navigation, stop_event):
    def selected():
        snapshot = navigation.snapshot()
        return (snapshot.active_agent_id, snapshot.active_thread_id)

    def observe():
        current, view, runtime, next_read = ('', ''), None, None, 0.0
        root_generation = navigation.root_runtime.conversation_reset_generation

        def close():
            nonlocal view, runtime
            old, old_runtime = view, runtime
            view, runtime = None, None
            if old is not None:
                post_gateway_json(agent, '/client/agent-approval-view', {
                    'conversation_id': navigation.root_runtime.session_id,
                    'operation': 'close', 'view': old}, timeout=5)
            if old_runtime is not None:
                old_runtime.sync_agent_permission_requests([], decision_writer=lambda *_: {'ok': False},
                                                           interest_id=old['view_id'])

        try:
            while not stop_event.is_set():
                target = selected()
                observed_generation = navigation.root_runtime.conversation_reset_generation
                if observed_generation != root_generation:
                    close()
                    current, root_generation = target, observed_generation
                    # 原会话reset后，须重新进入后台页；滞后显示不能自动续旧兴趣。
                if target != current:
                    close()
                    current, next_read = target, 0.0
                    if all(target):
                        view = {'agent_id': target[0], 'view_id': uuid4().hex}
                        runtime = navigation.root_runtime
                if view is not None and time.monotonic() >= next_read:
                    frozen, target_runtime, target_key = dict(view), runtime, current
                    generation = target_runtime.conversation_reset_generation
                    source_generation = root_generation
                    status, result = post_gateway_json(agent, '/client/agent-approval-view', {
                        'conversation_id': navigation.root_runtime.session_id,
                        'operation': 'observe', 'view': frozen}, timeout=5)
                    next_read = time.monotonic() + 5
                    if (selected() != target_key or generation != target_runtime.conversation_reset_generation
                            or source_generation != navigation.root_runtime.conversation_reset_generation):
                        continue
                    if status != 200 or not isinstance(result, dict) or result.get('ok') is not True:
                        target_runtime.sync_agent_permission_requests([], decision_writer=lambda *_: {'ok': False},
                                                                    interest_id=frozen['view_id'])
                    elif result.get('coverage') == 'complete':
                        root = result.get('root_task_id', '')

                        def decide(run_id, request, decision, *, original=frozen, key=target_key,
                                   root_task_id=root, original_runtime=target_runtime,
                                   original_generation=generation, original_source_generation=source_generation):
                            # 离开/重入同名页也有不同view；迟到按钮不能借新界面复活。
                            if (selected() != key or view != original or runtime is not original_runtime
                                    or original_generation != original_runtime.conversation_reset_generation
                                    or original_source_generation != navigation.root_runtime.conversation_reset_generation):
                                return {'ok': False, 'error_code': 'BACKGROUND_APPROVAL_VIEW_INACTIVE'}
                            _, receipt = post_gateway_json(agent, '/client/agent-permission', {
                                'conversation_id': navigation.root_runtime.session_id,
                                'run_id': run_id, 'request': request.to_dict(), 'decision': decision.to_dict(),
                                'background_view': {**original, 'root_task_id': root_task_id}}, timeout=5)
                            return receipt

                        target_runtime.apply_conversation_snapshot(generation, lambda:
                            target_runtime.sync_agent_permission_requests(
                                result.get('agent_permission_requests', []), decision_writer=decide,
                                interest_id=frozen['view_id']))
                stop_event.wait(.2)
        finally:
            close()

    thread = threading.Thread(target=observe, name='tui-background-approval-view', daemon=True)
    thread.start()
    return thread
