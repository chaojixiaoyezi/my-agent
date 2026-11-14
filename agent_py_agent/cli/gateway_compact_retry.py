"""后台预检容量隔离的只读变化指纹；不发模型、不消费wake、不从失败时间推断新输入。"""
from __future__ import annotations

from ..agent.conversation.compact_threshold_policy import resolve_thread_compact_policy
from ..agent.conversation.decision_policy import connection_revision
from ..agent.settings.model_profiles import selected_model_config
from ..agent.settings.thread_model_selection import thread_model_fingerprint


def compact_retry_fingerprint(scheduler, thread_id):
    agent, store = scheduler.runtime.agent, scheduler.store
    thread = store.threads.require(thread_id)
    config = selected_model_config(agent, profile_id=thread.model_profile_id or None)
    events, errors, coverage = store.observations.recent_window_report(
        thread_id, limit=1, max_bytes=65536, max_segments=1)
    if errors or not coverage['requested_window_complete']:
        raise ValueError('COMPACT_RETRY_INPUT_SOURCE_UNKNOWN')
    goal, error = store.goals.load_report(thread_id)
    if error:
        raise ValueError('COMPACT_RETRY_GOAL_SOURCE_UNKNOWN')
    metadata = thread.metadata or {}
    # 不纳入claim、心跳、失败次数/时间、用量及普通revision；这些自变化不能解除同输入隔离。
    return connection_revision({
        'model': thread_model_fingerprint(agent, thread_id),
        'capacity': {key: getattr(config, key, None) for key in (
            'model_context_window_tokens', 'max_tokens', 'memory_compact_trigger_percent',
            'memory_compact_auto_trigger_max_tokens', 'memory_compact_recovery_target_percent',
            'memory_compact_summary_max_output_tokens', 'memory_compact_keep_recent_turns',
            'memory_compact_max_history_tokens', 'enable_tools')},
        'policy': resolve_thread_compact_policy(agent, thread, use_frozen=False),
        'thread': {key: getattr(thread, key) for key in (
            'last_user_message_id', 'last_user_request_id', 'compact_generation', 'compact_checkpoint_id',
            'compacted_through_message_id', 'compacted_through_byte_offset', 'workspace_task_id',
            'active_task_ids', 'loaded_tool_names', 'conversation_methods')},
        'settings': metadata.get('settings', {}),
        'goal': ({key: getattr(goal, key) for key in
            ('goal_id', 'objective', 'task_id', 'status', 'token_budget', 'duration_seconds')} if goal else None),
        'observation': events[-1].observation_id if events else None,
    })
