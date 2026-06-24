
from __future__ import annotations

"""飞书长连接(WebSocket)入站客户端 —— 免公网 / 不绑端口部署。

webhook 模式需公网回调 + 绑端口(内网机收不到飞书推送);长连模式用 app_id/secret **主动连飞书
网关**,内网/本机/单实例即可收消息(只出站、零入站暴露)。事件经**与 webhook 完全同一条下游**:
lark 事件对象 → 归一化成 webhook 同构 dict → adapter._handle_feishu_event → feishu_to_incoming →
_dispatch(下游对来源无感知)。依赖飞书官方 ``lark-oapi`` 的 ws.Client(自管握手/心跳/重连),未装时
报清晰中文错误、webhook 模式不受影响。
"""

import contextlib
import logging
import threading
from collections import deque
from typing import Any, Callable

logger = logging.getLogger(__name__)

WS_OPEN_TIMEOUT_SECONDS = 45.0
_ws_connect_patched = False


def _fixed_ws_kwargs(orig_kw: dict[str, Any], has_open_timeout: bool, proxy: str) -> dict[str, Any]:
    """补 open_timeout + 纠正 proxy(移植自 claw 真机踩坑;详见 _patch_ws_connect)。
    (1) 慢链路握手 >10s(websockets 默认)会 timed out → 调大到 45s;(2) SDK 硬塞 proxy=None 在
    代理/TUN 环境直连飞书 WS 网关是黑洞 → 去掉它让 websockets 读 HTTPS_PROXY 环境变量(无代理=直连)。"""
    kw = dict(orig_kw)
    if has_open_timeout:
        kw.setdefault("open_timeout", WS_OPEN_TIMEOUT_SECONDS)
    if proxy:
        kw["proxy"] = proxy  # 显式代理(feishu_ws_proxy):覆盖 SDK 的 None
    elif kw.get("proxy", "keep") is None:
        kw.pop("proxy", None)  # 去掉 SDK 的 None → websockets 读 HTTPS_PROXY 等环境变量
    return kw


def _patch_ws_connect(proxy: str = "") -> None:
    """monkeypatch lark-oapi 的 _ws_connect_kwargs(幂等;失败静默退回 SDK 默认)。"""
    global _ws_connect_patched
    if _ws_connect_patched:
        return
    try:
        import inspect

        import lark_oapi.ws.client as _lwc
        import websockets
        has = "open_timeout" in inspect.signature(websockets.connect).parameters
        _orig = _lwc._ws_connect_kwargs
        _lwc._ws_connect_kwargs = lambda: _fixed_ws_kwargs(_orig(), has, proxy)
        _ws_connect_patched = True
    except Exception:
        pass


def lark_event_to_webhook_payload(data: Any) -> dict[str, Any] | None:
    """lark-oapi 的 ``im.message.receive_v1`` 事件对象 → 与 webhook 同构的 payload dict
    (复用 feishu_to_incoming,长连/webhook 同一条下游)。防御式 getattr(兼容真 lark 对象与测试假对象);
    取不到 message_id 视为无效返回 None。"""
    try:
        event = getattr(data, "event", None)
        msg = getattr(event, "message", None) if event is not None else None
        message_id = str(getattr(msg, "message_id", "") or "") if msg is not None else ""
        if not message_id:
            return None
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None) if sender is not None else None
        open_id = str(getattr(sender_id, "open_id", "") or "") if sender_id is not None else ""
        return {
            "event": {
                "message": {
                    "message_id": message_id,
                    "msg_type": str(getattr(msg, "message_type", "") or ""),
                    "content": getattr(msg, "content", None) or "{}",
                    "chat_id": str(getattr(msg, "chat_id", "") or ""),
                },
                "sender": {"sender_id": {"open_id": open_id}},
            }
        }
    except Exception:
        return None


class FeishuWsClient:
    """飞书 WS 长连客户端:收事件 → 归一化成 webhook 同构 dict → on_payload(= adapter._handle_feishu_event,
    与 webhook 同一条下游)。lark-oapi 自管握手/心跳/重连,我们只做"事件→归一化→交下游"。"""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        on_payload: Callable[[dict[str, Any]], None],
        ws_proxy: str = "",
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_payload = on_payload
        self.ws_proxy = ws_proxy or ""
        self._client: Any = None
        self._dedup_cap = 2048
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()

    def _handle_event(self, data: Any) -> None:
        """单条 im.message 事件:去重 → 归一化 → 交下游。任何异常不掀翻长连。"""
        try:
            payload = lark_event_to_webhook_payload(data)
            if payload is None:
                return
            message_id = str(payload["event"]["message"]["message_id"])
            if self._is_duplicate(message_id):
                return  # 飞书长连重连/重投会重发同一 message_id:跳过,绝不重复处理/重复回复
            self.on_payload(payload)
        except Exception as exc:
            logger.error(f"飞书长连事件处理异常(不中断长连): {type(exc).__name__}: {exc}")

    def _is_duplicate(self, message_id: str) -> bool:
        """按 message_id 去重(有界缓存 _dedup_cap):防飞书重投/重连重放同一消息导致重复回复
        (移植参考实现的去重;原 my-agent base adapter 无去重,移植时漏了→真机踩出重复回复)。"""
        if message_id in self._seen_ids:
            return True
        if len(self._seen_order) >= self._dedup_cap:
            self._seen_ids.discard(self._seen_order.popleft())
        self._seen_order.append(message_id)
        self._seen_ids.add(message_id)
        return False

    def start(self) -> None:
        """启动长连(阻塞;lark-oapi 内部自管握手/心跳/重连)。"""
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError(
                "飞书长连接模式需要 lark-oapi:请 `pip install lark-oapi`。webhook 模式不受影响。"
            ) from exc
        _patch_ws_connect(self.ws_proxy)
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_event)
            .build()
        )
        self._client = lark.ws.Client(
            self.app_id, self.app_secret, event_handler=handler, log_level=lark.LogLevel.INFO,
        )
        logger.info("飞书长连接(WebSocket)已启动:主动连飞书网关收消息(免公网/不绑端口)")
        self._client.start()  # 阻塞:内部 wss 长连 + 自动重连

    def close(self) -> None:
        """尽力关闭底层 ws(lark 无统一干净 stop;关不掉就靠 daemon 线程随进程退出)。"""
        inner = self._client
        self._client = None
        closer = getattr(inner, "stop", None) or getattr(inner, "disconnect", None)
        if callable(closer):
            with contextlib.suppress(Exception):
                closer()


def run_ws_client_thread(client: FeishuWsClient, on_dead: Callable[[], None]) -> threading.Thread:
    """daemon 线程里跑长连(client.start 阻塞);异常退出=通道死,调 on_dead(让 adapter 置 _running=False
    供健康探测/supervisor 重启,同 webhook _serve 的处理)。返回线程句柄。"""
    thread = threading.Thread(target=lambda: _run_ws_blocking(client, on_dead), daemon=True)
    thread.start()
    return thread


def _run_ws_blocking(client: FeishuWsClient, on_dead: Callable[[], None]) -> None:
    try:
        client.start()
    except Exception as exc:
        logger.error(f"飞书长连接异常退出(通道已死,需重启): {type(exc).__name__}: {exc}")
        on_dead()


__all__ = ["FeishuWsClient", "lark_event_to_webhook_payload", "run_ws_client_thread"]
