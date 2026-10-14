# LLM: /status 只读同源上下文与匿名计时及可信 owner 权限；不读正文，不生成模型请求或写计量。
# 模块用途: 将同源会话领域事实投影为 Gateway/本地状态页，展示可信角色、权限和上下文，未知不补零。
from __future__ import annotations

from ..conversation.context_usage import context_usage_from_thread
from ..conversation.im_task_status import im_task_status_facts
from ..tooling.self_context import permission_self_context


# LLM: 身份只认可信 home；上下文和最近请求计时均取原线程投影，未计时不补零或冒充回合墙钟。
# 函数用途: 从原会话投影和本人审批事实生成状态，与 list_tools 同源区分 ask/auto 及来源，不重算历史或写执行状态。
def status_display_facts(agent: object, thread: object = None, *, scope: object = None) -> dict[str, object]:
    if scope is not None:
        try:
            thread = agent.conversation_store.threads.resolve(
                channel=scope.channel, channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id
            )
        except (AttributeError, OSError, TypeError, ValueError):
            thread = None
    home = getattr(agent, "home_paths", None)
    key = tuple(str(getattr(home, name, "") or "") for name in ("owner_provider", "owner_kind", "owner_id"))
    role = "管理员" if key == ("local", "main", "local/main") else "普通用户" if all(key) else "未知"
    config = getattr(agent, "config", None)
    permissions = permission_self_context(agent)
    return {"user_role": role,
            "permissions": permissions,
            "permission_mode": f"审批 {permissions['mode']}（来源 {permissions['source']}）；文件 {getattr(config, 'path_access_mode', '未知')}；Shell {getattr(config, 'access_mode', '未知')}",
            "context_usage": context_usage_from_thread(thread), "im_task_status": im_task_status_facts(agent, thread, scope),
            "model_timing": public_model_timing(getattr(thread, 'model_metrics', None))}


# LLM: 只允许原 model_metrics 的当前 request 数字；未知旧字段不转成假观测，不包含模型名/正文或凭据。
# 函数用途: 为 TUI、IM 和状态传输保留最近请求的模型耗时及覆盖次数。
def public_model_timing(metrics: object) -> dict[str, int]:
    if not isinstance(metrics, dict):
        return {}
    names = ('model_latency_ms', 'first_token_wait_ms', 'post_first_token_ms', 'timed_model_call_count',
             'first_token_timed_call_count', 'split_latency_call_count', 'physical_model_attempt_count',
             'logical_model_turn_count', 'main_latency_ms', 'auxiliary_latency_ms', 'decision_latency_ms')
    return {name: value for name in names if type(value := metrics.get('request_' + name)) is int and value >= 0}


# LLM: 时长为物理调用之和，用途仍是原账分区；首字未知不显示零，并行不把累加当墙钟或工具耗时。
# 函数用途: 以简短中文说明最近请求的模型等待，帮助用户判断慢在模型还是多轮调用。
def render_model_timing(facts: dict[str, int]) -> str:
    count = facts.get('timed_model_call_count', 0)
    attempts = facts.get('physical_model_attempt_count')
    if not count or 'model_latency_ms' not in facts:
        return '最近请求模型耗时：暂无完整计时。'
    coverage = f'{count}/{attempts}' if attempts is not None and attempts >= count else f'{count}，总次数未知'
    text = f"最近请求模型累计：{facts['model_latency_ms'] / 1000:.1f}s（计时 {coverage} 次调用）"
    if facts.get('first_token_timed_call_count') and 'first_token_wait_ms' in facts:
        text += f"；首包等待累计 {facts['first_token_wait_ms'] / 1000:.1f}s"
    if facts.get('split_latency_call_count') and 'post_first_token_ms' in facts:
        text += f"；首包后累计 {facts['post_first_token_ms'] / 1000:.1f}s"
    return text + '。累计值不含工具时间，并行时不等于实际等待时长。'
