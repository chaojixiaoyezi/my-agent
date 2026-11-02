from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


# LLM: 他线程（stop/心跳失败）只终止并唤醒指定准确连接（缺省当前连接），不释放描述符也不清引用；释放归接收线程收尾。
# 函数用途: 唤醒一个 QQ 连接的阻塞接收，让其接收线程按停止位收尾。
def interrupt_ws(adapter, ws=None) -> None:
    ws = adapter._ws if ws is None else ws
    if not ws:
        return
    try:
        ws.interrupt()
    except Exception:
        pass


# LLM: 只关闭指定的准确连接（缺省为当前连接）；当前引用仍指向它时才清空，不误关已换上的新连接。
# 函数用途: 关闭一个 QQ WebSocket 连接并解除当前引用。
def close_ws(adapter, ws=None) -> None:
    ws = adapter._ws if ws is None else ws
    if not ws:
        return
    try:
        ws.close()
    except Exception:
        pass
    if adapter._ws is ws:
        adapter._ws = None


# LLM: 有界等待生命周期/心跳线程；从这些线程内部调用（如消息回调里 stop）时跳过自身，不 join 当前线程。
# 函数用途: 停止时回收 QQ 后台线程。
def join_ws_threads(adapter) -> None:
    current = threading.current_thread()
    ws_thread = adapter._ws_thread
    if ws_thread and ws_thread is not current:
        ws_thread.join(timeout=5)
        adapter._ws_thread = None
    heartbeat = adapter._heartbeat_thread
    if heartbeat and heartbeat is not current:
        heartbeat.join(timeout=5)
        adapter._heartbeat_thread = None


# LLM: 连接收尾只动本连接：置其停止位让心跳退出、关闭准确 socket、有界 join 其心跳（不 join 当前线程），不设全局停止。
# 函数用途: 释放一个 QQ 连接的本地资源，交回生命周期循环决定是否重连。
def end_connection(adapter, ws, connection_stop) -> None:
    connection_stop.set()
    close_ws(adapter, ws)
    thread = adapter._heartbeat_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=5)
        if adapter._heartbeat_thread is thread:
            adapter._heartbeat_thread = None


def handle_ready_event(adapter, data: dict) -> None:
    adapter._session_id = data.get("session_id")
    adapter._maybe_start_heartbeat(data)


# LLM: INVALID_SESSION / op7 只结束当前连接并清会话，交给生命周期循环重连；全局停止只由 stop() 设置。
# 函数用途: 请求 QQ 连接级重连。
def request_reconnect(adapter, message: str) -> None:
    logger.warning(message)
    adapter._session_id = None
    adapter._connection_stop.set()


# LLM: 心跳发到调用方给定的连接（缺省当前连接）；发送失败只回报 False，由心跳线程转为连接级重连。
# 函数用途: 发送一次 QQ 心跳并报告是否已发出。
def send_heartbeat_once(adapter, ws=None) -> bool:
    ws = adapter._ws if ws is None else ws
    try:
        if ws and ws.is_connected:
            ws.send_json({"op": 1, "d": adapter._last_seq})
            return True
    except Exception as exc:
        logger.warning("QQ 心跳发送失败 error_type=%s", type(exc).__name__)
    return False
