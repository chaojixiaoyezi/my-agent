
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def close_ws(adapter) -> None:
    if not adapter._ws:
        return
    try:
        adapter._ws.close()
    except Exception:
        pass
    adapter._ws = None


def join_ws_threads(adapter) -> None:
    if adapter._ws_thread:
        adapter._ws_thread.join(timeout=5)
        adapter._ws_thread = None
    if adapter._heartbeat_thread:
        adapter._heartbeat_thread.join(timeout=5)
        adapter._heartbeat_thread = None


def handle_ready_event(adapter, data: dict) -> None:
    adapter._session_id = data.get("session_id")
    adapter._maybe_start_heartbeat(data)


def request_reconnect(adapter, message: str) -> None:
    logger.warning(message)
    adapter._session_id = None
    adapter._stop_event.set()


def send_heartbeat_once(adapter) -> None:
    try:
        if adapter._ws and adapter._ws.is_connected:
            adapter._ws.send_json({"op": 1, "d": adapter._last_seq})
    except Exception:
        pass
