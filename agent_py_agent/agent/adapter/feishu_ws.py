# LLM: WS 只在原 typed 入站回执 accepted/稳定 ignored 后提交 seen；暂时失败向官方 SDK 抛安全异常以生成失败 ACK。
# 模块用途: 共用 webhook 解析和原进程内去重，保护并发 claim，不存正文或建立第二消息队列。
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
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .protocol import FeishuEventDisposition, FeishuRetryableEventError, provider_message_reference

logger = logging.getLogger(__name__)

# WebSocket 建连超时 45 秒：飞书长连接首连/重连需要合理等待窗口，太长会拖住看门狗。
WS_OPEN_TIMEOUT_SECONDS = 45.0
# 进程内重连(借鉴 长期助手 每通道看门狗):lark 客户端整体死掉(内部重连耗尽)后,原地重建重连,
# 指数退避 + jitter;稳定连过(>STABLE 秒)又断则退避归零,避免长连一闪而过却背着大退避。
# 重连初始退避 1 秒：首断快速重试。
_RECONNECT_INITIAL_BACKOFF_SECONDS = 1.0
# 重连最大退避 30 秒：防止反复失败时高频打飞书网关。
_RECONNECT_MAX_BACKOFF_SECONDS = 30.0
# 稳定连接超过 60 秒后再断，退避归零重新从 1 秒起步。
_RECONNECT_STABLE_RESET_SECONDS = 60.0
_ws_connect_patched = False


def _fixed_ws_kwargs(orig_kw: dict[str, Any], has_open_timeout: bool, proxy: str) -> dict[str, Any]:
    """补 open_timeout + 纠正 proxy(移植自 参考实现 真机踩坑;详见 _patch_ws_connect)。
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


class _LarkProcessorNotFoundFilter(logging.Filter):
    """丢弃 lark 的 "processor not found" 噪音:订阅了但我们不注册处理器的事件(如有人进 bot 私聊的
    bot_p2p_chat_entered、bot 被加群等)会被 lark 当 ERROR 刷屏。这些事件本就无需处理,静默丢弃即可
    (比按事件名逐个 register 忽略更省心、对新事件类型也不漏)。只丢这一类,真错误(鉴权/连接)照常出。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "processor not found" not in record.getMessage()
        except Exception:
            return True


_lark_noise_filtered = False


def _suppress_lark_processor_not_found() -> None:
    """给 "Lark" logger 装一次过滤器消 processor-not-found 刷屏(幂等)。"""
    global _lark_noise_filtered
    if _lark_noise_filtered:
        return
    logging.getLogger("Lark").addFilter(_LarkProcessorNotFoundFilter())
    _lark_noise_filtered = True


# LLM: WS 必须保留 provider mention 的 key/open_id，与 webhook 走同一入站和群聊授权门；不按姓名识别机器人。
# 函数用途: 把 SDK 事件转换成共享消息结构，保留话题、发送人和直接提及字段。
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
                    "mentions": [
                        {"key": row["key"], "id": {"open_id": row["open_id"]}}
                        for row in _normalized_event_mentions(msg)
                    ],
                    "msg_type": str(getattr(msg, "message_type", "") or ""),
                    "content": getattr(msg, "content", None) or "{}",
                    "chat_id": str(getattr(msg, "chat_id", "") or ""),
                    "chat_type": str(getattr(msg, "chat_type", "") or ""),
                    "root_id": str(getattr(msg, "root_id", "") or ""),
                    "parent_id": str(getattr(msg, "parent_id", "") or ""),
                    "thread_id": str(getattr(msg, "thread_id", "") or ""),
                    "create_time": str(getattr(msg, "create_time", "") or ""),
                },
                "sender": {"sender_id": {"open_id": open_id}},
            }
        }
    except Exception:
        return None


# LLM: 原 seen 缓存只是运输优化；inflight claim 必须释放，持久执行去重仍由 GatewayIngressStore 负责。
# 类用途: 把官方 WS 事件交给同一飞书入站入口，按受理事实完成或撤销临时去重。
class FeishuWsClient:
    """飞书 WS 长连客户端:收事件 → 归一化成 webhook 同构 dict → on_payload(= adapter._handle_feishu_event,
    与 webhook 同一条下游)。lark-oapi 自管握手/心跳/重连,我们只做"事件→归一化→交下游"。"""

    # LLM: 仅初始化原有界 seen 和并发 claim 锁；消费者必须返回 typed disposition，不能把 None 猜成接受。
    # 函数用途: 创建未连接的 WS 客户端，不写持久账或发送消息。
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        on_payload: Callable[[dict[str, Any]], FeishuEventDisposition],
        ws_proxy: str = "",
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_payload = on_payload
        self.ws_proxy = ws_proxy or ""
        # 卡片按钮回调(飞书人设确认):由 adapter 构造后按需设置;None=不注册卡片处理器。
        self.on_card_action: Callable[[dict[str, Any]], None] | None = None
        self._client: Any = None
        self._stopped = False  # close() 置 True:区分"主动停"(退出重连循环)vs"断线"(重连)
        self._dedup_cap = 2048
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._inflight_ids: set[str] = set()
        self._dedup_lock = threading.Lock()

    # LLM: claim→共享入口→commit/release；retryable 异常由官方 SDK 转成失败 ACK，不能被这里吞掉或记成 seen。
    # 函数用途: 只让一个并发事件进入消费者，允许原消息编号在暂时失败后重投。
    def _handle_event(self, data: Any) -> None:
        payload = lark_event_to_webhook_payload(data)
        if payload is None:
            return
        message_id = str(payload["event"]["message"]["message_id"])
        claim = self._claim_message(message_id)
        if claim == 'duplicate':
            logger.info('feishu_transport message_ref=%s status=ignored reason_code=FEISHU_EVENT_DUPLICATE', provider_message_reference(message_id))
            return
        if claim == 'inflight':
            logger.info('feishu_transport message_ref=%s status=retryable reason_code=FEISHU_EVENT_INFLIGHT', provider_message_reference(message_id))
            raise FeishuRetryableEventError('FEISHU_EVENT_INFLIGHT')
        try:
            result = self.on_payload(payload)
            if not isinstance(result, FeishuEventDisposition):
                raise FeishuRetryableEventError('FEISHU_INGRESS_UNAVAILABLE')
            if result.status == 'retryable':
                raise FeishuRetryableEventError(result.reason_code)
            self._finish_message(message_id, commit=True)
        except Exception as exc:
            self._finish_message(message_id, commit=False)
            reason = exc.reason_code if isinstance(exc, FeishuRetryableEventError) else 'FEISHU_INGRESS_UNAVAILABLE'
            logger.warning('feishu_transport message_ref=%s status=retryable reason_code=%s error_type=%s',
                           provider_message_reference(message_id), reason, type(exc).__name__)
            raise FeishuRetryableEventError(reason) from None

    # LLM: 锁内只操作现有进程缓存，不能把 inflight 当作完成；锁外才运行消费者。
    # 函数用途: 原子领取一个未处理的消息编号，防两个 WS 回调同时执行。
    def _claim_message(self, message_id: str) -> str:
        with self._dedup_lock:
            if message_id in self._seen_ids:
                return 'duplicate'
            if message_id in self._inflight_ids:
                return 'inflight'
            self._inflight_ids.add(message_id)
            return 'claimed'

    # LLM: commit 仅由 typed accepted/稳定 ignored 触发，异常只释放 claim；不改变原持久入站记录。
    # 函数用途: 完成本次临时领取，只有明确受理或不触发才记入原 seen 缓存。
    def _finish_message(self, message_id: str, *, commit: bool) -> None:
        with self._dedup_lock:
            self._inflight_ids.discard(message_id)
            if commit:
                self._remember_message(message_id)

    # LLM: 调用方必须已持 dedup_lock；容量沿原 dedup_cap，seen 只保留运输完成的编号。
    # 函数用途: 在原有界缓存中登记一个已处理编号，不持久化正文或新增账本。
    def _remember_message(self, message_id: str) -> None:
        if message_id in self._seen_ids:
            return
        if len(self._seen_order) >= self._dedup_cap:
            self._seen_ids.discard(self._seen_order.popleft())
        self._seen_order.append(message_id)
        self._seen_ids.add(message_id)

    def _handle_card_action(self, data: Any) -> Any:
        """卡片按钮回调:归一化 → 去重 → 交 on_card_action。on_card_action 返回 dict 时(密码卡)
        包成 P2CardActionTriggerResponse 同步返回飞书 → 原卡【就地整卡替换】(密码框+密码当场消失,
        不残留);返回 None(persona 卡)则由 on_card_action 侧另发消息。任何异常不掀翻长连。"""
        try:
            from .feishu_card import extract_card_action

            norm = extract_card_action(data)
            if norm is None:
                return None
            value = norm.get("value") or {}
            # 密码卡表单提交不走 token/choice 去重(没这俩键会都撞成 None:None + 合法重试被误杀);
            # 密码操作重复执行本就安全(set 首设不可覆盖/unlock 重验/change 验旧),放它每次都处理。
            is_pwd = str(value.get("session_lock_action") or "") in {"pwd_set", "pwd_unlock", "pwd_change"}
            if not is_pwd:
                key = f"card:{value.get('token')}:{value.get('choice')}"
                if self._is_duplicate(key):
                    return None  # 同一按钮重复回调:不重复处理/不重复回执
            if self.on_card_action is not None:
                resp = self.on_card_action(norm)
                if isinstance(resp, dict):  # 密码卡:同步返回卡片就地替换
                    from lark_oapi.event.callback.model.p2_card_action_trigger import (
                        P2CardActionTriggerResponse,
                    )

                    return P2CardActionTriggerResponse(resp)
        except Exception as exc:
            logger.error(f"飞书卡片回调处理异常(不中断长连): {type(exc).__name__}: {exc}")
        return None

    def _ignore_event(self, data: Any) -> None:
        """订阅了但无需处理的事件(如机器人贴 reaction 后飞书回的 created/deleted 通知)直接忽略,
        避免 lark 报 processor not found 刷屏日志。"""

    # LLM: 卡片点击沿原即记去重合同；普通消息必须使用 claim/finish，不得再调用这个前置提交入口。
    # 函数用途: 保留卡片回调的原有界去重，并用相同锁保护缓存。
    def _is_duplicate(self, message_id: str) -> bool:
        """按 message_id 去重(有界缓存 _dedup_cap):防飞书重投/重连重放同一消息导致重复回复
        (移植参考实现的去重;原 my-agent base adapter 无去重,移植时漏了→真机踩出重复回复)。"""
        with self._dedup_lock:
            if message_id in self._seen_ids:
                return True
            self._remember_message(message_id)
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
        _suppress_lark_processor_not_found()  # 消 lark "processor not found" 刷屏(未注册处理器的订阅事件)
        builder = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_event)
            .register_p2_im_message_reaction_created_v1(self._ignore_event)
            .register_p2_im_message_reaction_deleted_v1(self._ignore_event)
        )
        # 卡片按钮回调(飞书人设确认):仅在接线了 on_card_action 且 SDK 版本支持该注册时挂上(防旧版无此方法)。
        if self.on_card_action is not None and hasattr(builder, "register_p2_card_action_trigger"):
            builder = builder.register_p2_card_action_trigger(self._handle_card_action)
        handler = builder.build()
        self._client = lark.ws.Client(
            self.app_id, self.app_secret, event_handler=handler, log_level=lark.LogLevel.INFO,
        )
        logger.info("飞书长连接(WebSocket)已启动:主动连飞书网关收消息(免公网/不绑端口)")
        self._client.start()  # 阻塞:内部 wss 长连 + 自动重连

    def close(self) -> None:
        """尽力关闭底层 ws(lark 无统一干净 stop;关不掉就靠 daemon 线程随进程退出)。"""
        self._stopped = True  # 让重连循环识别为主动停、退出(而非断线重连)
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
    """跑长连;lark 客户端整体死掉(内部重连耗尽)时,**进程内指数退避重连**——不靠重启整个进程。
    只在 close()(主动停)时退出循环。借鉴 长期助手 每通道断线重连看门狗:补上"WS 彻底死后没人救"的洞
    (旧实现死一次就 on_dead,而 systemd 部署无 supervisor 消费它、进程不退 → 飞书永久断线)。"""
    backoff = _RECONNECT_INITIAL_BACKOFF_SECONDS
    attempt = 0
    while not client._stopped:
        connected_at = time.monotonic()
        try:
            client.start()  # 阻塞:返回/抛出 = 这条 WS 整体结束
            reason = "连接正常结束"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        if client._stopped:
            break  # 主动停:不再重连
        # 稳定连过一段又断 → 退避归零(是"连上后掉线"而非"连不上死循环")
        if time.monotonic() - connected_at >= _RECONNECT_STABLE_RESET_SECONDS:
            backoff, attempt = _RECONNECT_INITIAL_BACKOFF_SECONDS, 0
        attempt += 1
        wait = backoff + random.uniform(0, min(backoff, 1.0))
        safe = reason.replace(client.app_secret, "***") if getattr(client, "app_secret", "") else reason
        logger.warning(f"飞书长连断开(第 {attempt} 次),{wait:.1f}s 后进程内重连: {safe}")
        time.sleep(wait)
        backoff = min(backoff * 2, _RECONNECT_MAX_BACKOFF_SECONDS)
    on_dead()  # 主动停 → 置 _running=False


__all__ = ["FeishuWsClient", "lark_event_to_webhook_payload", "run_ws_client_thread"]


# LLM: SDK 的 mention 不得在 WS 归一过程中丢失，群聊门与 webhook 共用相同结构；只复制 ID 与 key。
# 函数用途: 读取 SDK 事件的 mention 数组，不读或记录聊天正文。
def _normalized_event_mentions(message: object) -> list[dict[str, str]]:
    from .feishu_mentions import normalize_mentions

    return normalize_mentions(getattr(message, "mentions", None))
