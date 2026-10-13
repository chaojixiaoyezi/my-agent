# LLM: 本入口仅绑定当前物理模型调用的展示观察；身份取宿主 call_id，不读正文，不改重试、模型参数、执行权或计量。
# 模块用途: 沿现有 120 秒开关为 IM 模型等待组装同消息心跳，后台静默与本地 TUI 不创建窗口。
from __future__ import annotations

import math

from ...tooling.liveness import ActivityHeartbeat


# LLM: 只消费 typed IM sink 与原有效配置；0 禁用，未声明能力不观察，不给 scheduled 增加外发路径。
# 函数用途: 构建尚未启动的模型等待观察者，真正 generate 前才由原调用活性守卫启动。
def model_wait_activity(state: object) -> ActivityHeartbeat | None:
    params = getattr(state, 'params', None)
    callback = getattr(params, 'effective_on_chunk', None)
    sink = getattr(callback, '__self__', None) or callback
    publish = getattr(sink, 'write_model_liveness', None)
    if not getattr(sink, 'model_liveness_enabled', False) or not callable(publish):
        return None
    config = getattr(getattr(state, 'agent', None), 'config', None)
    call_id = getattr(state, 'call_id', None)
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    try:
        interval = float(getattr(config, 'im_stream_heartbeat_seconds', 120.0))
    except (TypeError, ValueError):
        interval = 120.0
    if not math.isfinite(interval):
        interval = 120.0
    if interval <= 0:
        return None
    observer = ActivityHeartbeat({'model_call_id': call_id,
        'phase': 'waiting', 'wait_state': 'model_response'},
        interval_seconds=min(120.0, max(1.0, interval)), cancellation_token=getattr(params, 'cancellation_token', None),
        guarded_sink=publish)
    return observer
