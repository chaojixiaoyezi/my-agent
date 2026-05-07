# LLM: External adapter module; keep platform payload and runtime boundary contracts stable.
# 模块用途: 对接 QQ、飞书等外部渠道，把平台事件转换成内部请求。

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# LLM: close_ws 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理closews相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def close_ws(adapter) -> None:
    if not adapter._ws:
        return
    try:
        adapter._ws.close()
    except Exception:
        pass
    adapter._ws = None


# LLM: join_ws_threads 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理joinwsthreads相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def join_ws_threads(adapter) -> None:
    if adapter._ws_thread:
        adapter._ws_thread.join(timeout=5)
        adapter._ws_thread = None
    if adapter._heartbeat_thread:
        adapter._heartbeat_thread.join(timeout=5)
        adapter._heartbeat_thread = None


# LLM: handle_ready_event 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 推进readyevent的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
def handle_ready_event(adapter, data: dict) -> None:
    adapter._session_id = data.get("session_id")
    adapter._maybe_start_heartbeat(data)


# LLM: request_reconnect 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 发送reconnect请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def request_reconnect(adapter, message: str) -> None:
    logger.warning(message)
    adapter._session_id = None
    adapter._stop_event.set()


# LLM: send_heartbeat_once 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 发送heartbeatonce请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def send_heartbeat_once(adapter) -> None:
    try:
        if adapter._ws and adapter._ws.is_connected:
            adapter._ws.send_json({"op": 1, "d": adapter._last_seq})
    except Exception:
        pass
