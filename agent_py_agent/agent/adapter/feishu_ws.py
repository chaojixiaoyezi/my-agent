# LLM: WS 只在原 typed 入站回执 accepted/稳定 ignored 后提交 seen；连接健康只投影 SDK 原连接，安全观测只记元数据，不改变 SDK 分发/ACK。
# 模块用途: 共用 webhook 解析、安全诊断和原进程内去重，展示真实连接事实，不建立第二消息队列或健康状态账。
from __future__ import annotations

"""飞书长连接(WebSocket)入站客户端 —— 免公网 / 不绑端口部署。

webhook 模式需公网回调 + 绑端口(内网机收不到飞书推送);长连模式用 app_id/secret **主动连飞书
网关**,内网/本机/单实例即可收消息(只出站、零入站暴露)。事件经**与 webhook 完全同一条下游**:
lark 事件对象 → 归一化成 webhook 同构 dict → adapter._handle_feishu_event → feishu_to_incoming →
_dispatch(下游对来源无感知)。依赖飞书官方 ``lark-oapi`` 的 ws.Client(自管握手/心跳/重连),未装时
报清晰中文错误、webhook 模式不受影响。
"""

import contextlib
import hashlib
import json
import logging
import random
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ..common.log_redaction import redact_sensitive_text
from .feishu_ws_observation import card_response_stage, install_ws_observation, log_card_stage
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


# LLM: 空代理保持系统/环境发现，显式 HTTP/SOCKS 原样交上游；SOCKS 运行依赖由 pyproject 提供，不得失败后偷换直连。
# 函数用途: 在原 SDK 参数副本中补握手超时与用户代理，不改进程环境或发送请求。
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


_SDK_WS_LOG_STAGES = {
    'start': ('started', 'connect_failed'), '_try_connect': ('reconnect_attempt', 'connect_failed'),
    '_connect': ('connected', 'connect_failed'), '_disconnect': ('disconnected', 'disconnect_failed'),
    '_ping_loop': ('ping', 'ping_failed'), '_receive_message_loop': ('receiving', 'receive_loop_exited'),
    '_handle_message': ('frame_received', 'frame_failed'), '_handle_data_frame': ('data_frame_received', 'dispatch_failed'),
    '_reconnect': ('reconnecting', 'reconnect_callback_failed'),
    '_fire_on_reconnected': ('reconnected', 'reconnected_callback_failed'),
}
_SDK_SAFE_ERROR_TYPES = frozenset({'ValueError', 'TypeError', 'RuntimeError', 'OSError', 'TimeoutError',
    'ConnectionError', 'SSLError', 'ProxyError', 'InvalidProxyMessage', 'InvalidHandshake', 'InvalidStatus',
    'InvalidStatusCode', 'ConnectionClosed', 'ConnectionClosedError', 'ConnectionClosedOK', 'InvalidMessage',
    'WebSocketException', 'JSONDecodeError', 'UnicodeDecodeError', 'DecodeError', 'EncodeError', 'AssertionError',
    'UnmarshalException', 'EventException', 'ClientException', 'ServerException', 'HeaderNotFoundException',
    'ConnectionClosedException', 'ServerUnreachableException', 'AccessDeniedException', 'InvalidArgsException'})


# LLM: 仅审计过的 SDK 运输及 dispatcher 源文件使用固定日志；其它来源仍走共享脱敏，不按异常正文推断业务状态。
# 函数用途: 在 SDK 自带 handler 输出前去掉任意异常、URL、标识和 traceback，只保留固定阶段、类型和整数错误码。
def _safe_sdk_log_record(record: logging.LogRecord) -> str | None:
    source = record.pathname.replace('\\', '/')
    if source.endswith('/lark_oapi/ws/client.py'):
        stages = _SDK_WS_LOG_STAGES.get(record.funcName, ('transport', 'transport_failed'))
    elif source.endswith('/lark_oapi/event/dispatcher_handler.py'):
        stages = ('event_access', 'event_failed')
    else:
        return None
    stage = stages[1 if record.levelno >= logging.WARNING else 0]
    exception = (record.exc_info or sys.exc_info())[1]
    kind = type(exception).__name__ if exception is not None else 'none'
    kind = kind if kind in _SDK_SAFE_ERROR_TYPES or kind == 'none' else 'other'
    code = getattr(exception, 'code', None)
    code = code if type(code) is int and -(2**31) <= code < 2**31 else None
    record.exc_info = record.exc_text = record.stack_info = None
    return f'feishu_sdk stage={stage} error_type={kind} provider_code={code}'


# LLM: 审计过的 SDK 运输日志只保留固定诊断；其它日志共享脱敏，不让第三方 handler 输出任意异常或 payload。
# 类用途: 收窄 SDK 诊断并保留其它来源的既有未注册处理器噪音过滤。
class _LarkProcessorNotFoundFilter(logging.Filter):
    """原 SDK 的运输/分发错误保留 severity 和固定阶段，不输出自由文本或 traceback。"""

    # LLM: 固定日志先于 getMessage，避免格式化 payload；清空旧 args，异常诊断不改变 SDK 的实际异常、分发或 ACK。
    # 函数用途: 在原 logger 上净化记录后保留错误，未知来源仍按原共享规则脱敏。
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            safe = _safe_sdk_log_record(record)
            message = safe if safe is not None else record.getMessage()
            if safe is None and "processor not found" in message:
                return False
            record.msg = message if safe is not None else redact_sensitive_text(message)
            record.args = ()
            return True
        except Exception:
            record.msg, record.args = "飞书 SDK 日志格式异常", ()
            record.exc_info = record.exc_text = record.stack_info = None
            return True


_lark_noise_filtered = False


# LLM: 原 SDK logger 的过滤器先于它自带的所有 handler 执行，只安装一次，不改变日志级别或运输行为。
# 函数用途: 在启动唯一长连接前安装安全诊断边界。
def _suppress_lark_processor_not_found() -> None:
    """给原 Lark logger 安装一次固定运输诊断及共享脱敏过滤器。"""
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


# LLM: 去重只使用已验证运输身份与 typed 引用，不读取表单秘密；密码提交与未知卡留给原业务幂等入口。
# 函数用途: 给每张审批或凭据卡的准确按钮生成独立缓存键，避免不同卡片撞成 None:None。
def _card_action_key(norm: dict) -> str:
    value = norm.get("value") or {}
    if value.get("session_lock_action") in {"pwd_set", "pwd_unlock", "pwd_change"}:
        return ""
    kind = value.get("kind")
    if kind in {"my_agent_tool_approval", "my_agent_task_credential"}:
        identity = [kind, value.get("reference"), value.get("decision"), norm.get("operator_open_id"),
                    norm.get("message_id"), norm.get("chat_id")]
        if not identity[1] or not all(identity[3:]):
            return ""
    elif not kind and value.get("token") and value.get("choice"):
        identity = ["persona", value["token"], value["choice"], norm.get("operator_open_id")]
    else:
        return ""
    return "card:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


# LLM: 仅包装原控制处理器公开回执，不加入原事件或表单；重复成功回调返回同一业务回执。
# 函数用途: 将安全回执转换成飞书 SDK 响应。
def _card_response(resp: dict):
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    return P2CardActionTriggerResponse(resp)


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

    # LLM: 只读取 SDK 的原 _conn 和 websockets 协议状态；启动线程/SDK 存在都不等于已连接，未知事实不得报健康。
    # 函数用途: 返回无凭据的实时连接健康投影，不建连接、不写状态、不调用网络。
    def transport_health(self) -> dict[str, str]:
        from websockets.protocol import State

        if self._stopped:
            return {"status": "stopped", "reason_code": "FEISHU_WS_STOPPED"}
        connection = getattr(self._client, "_conn", None)
        if connection is None:
            return {"status": "degraded", "reason_code": "FEISHU_WS_CONNECTING"}
        state = getattr(connection, "state", None)
        if isinstance(state, State):
            if state is State.OPEN:
                return {"status": "healthy", "reason_code": "FEISHU_WS_CONNECTED"}
            reason = "FEISHU_WS_CONNECTING" if state is State.CONNECTING else "FEISHU_WS_DISCONNECTED"
            return {"status": "degraded", "reason_code": reason}
        closed = getattr(connection, "closed", None) if state is None else None
        if closed is False:
            return {"status": "healthy", "reason_code": "FEISHU_WS_CONNECTED"}
        reason = "FEISHU_WS_DISCONNECTED" if closed is True else "FEISHU_WS_STATE_UNKNOWN"
        return {"status": "degraded", "reason_code": reason}

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

    # LLM: 原卡身份/密码门/重复控制保持原样；阶段观测只读固定结果及消息摘要，未知异常不回显表单或认证头。
    # 函数用途: 沿原真实点击身份处理卡片并就地替换，失败不掀翻长连或把秘密带进诊断。
    def _handle_card_action(self, data: Any) -> Any:
        """卡片按钮回调:归一化 → 去重 → 交 on_card_action。on_card_action 返回 dict 时(密码卡)
        包成 P2CardActionTriggerResponse 同步返回飞书 → 原卡【就地整卡替换】(密码框+密码当场消失,
        不残留);返回 None(persona 卡)则由 on_card_action 侧另发消息。任何异常不掀翻长连。"""
        log_card_stage(self._client, 'entered')
        try:
            from .feishu_card import extract_card_action

            norm = extract_card_action(data)
            if norm is None:
                log_card_stage(self._client, 'invalid')
                return None
            value = norm.get("value") or {}
            key = _card_action_key(norm)
            if key:
                with self._dedup_lock:
                    cached = getattr(self, "_card_responses", {}).get(key)
                if cached is not None:
                    log_card_stage(self._client, 'duplicate_cached', message_id=norm.get('message_id'))
                    return _card_response(cached)
                if self._claim_message(key) != "claimed":
                    log_card_stage(self._client, 'claim_rejected', message_id=norm.get('message_id'))
                    return None
            try:
                resp = self.on_card_action(norm) if self.on_card_action is not None else None
                stage = card_response_stage(resp) if self.on_card_action is not None else 'handler_missing'
                log_card_stage(self._client, stage, message_id=norm.get('message_id'))
                # 只有成功的 typed 回执才消费按键；未解锁、运输错误和无效来源可在原卡重试。
                typed = bool(value.get("kind"))
                committed = (isinstance(resp, dict) and (resp.get("toast") or {}).get("type") == "success") if typed else True
                if key and committed and isinstance(resp, dict):
                    self._cache_card_response(key, resp)
                if key:
                    self._finish_message(key, commit=committed)
                return _card_response(resp) if isinstance(resp, dict) else None
            except Exception:
                if key:
                    self._finish_message(key, commit=False)
                raise
        except Exception as exc:
            log_card_stage(self._client, 'exception')
            logger.error('飞书卡片回调处理异常(不中断长连) error_type=%s', type(exc).__name__)
        return None

    # LLM: 仅缓存控制处理器公开成功回执；原事件、表单值与身份令牌不加入缓存，容量沿原运输 dedup_cap。
    # 函数用途: 让重复成功回调得到同一回执，不再执行业务决定。
    def _cache_card_response(self, key: str, response: dict) -> None:
        with self._dedup_lock:
            responses = getattr(self, "_card_responses", {})
            if len(responses) >= self._dedup_cap:
                responses.pop(next(iter(responses)))
            responses[key] = response
            self._card_responses = responses

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

    # LLM: 原 SDK 仍独占分发与 ACK；观测只包装本次实例的元数据入口，不开启 DEBUG 或增加连接。
    # 函数用途: 注册原事件及卡片处理器，安装安全观测后启动唯一长连接。
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
        install_ws_observation(self._client, card_handler_registered=self.on_card_action is not None
                               and hasattr(builder, 'register_p2_card_action_trigger'))
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


# LLM: 原 SDK 重连和显式 close 是唯一控制；外层异常只记录类型，不能回显代理 URL/密钥或误判健康。
# 函数用途: 在原线程内退避重试并在停止后通知适配器，不创建第二连接或持久状态。
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
            error_type = "none"
        except Exception as exc:
            error_type = type(exc).__name__
        if client._stopped:
            break  # 主动停:不再重连
        # 稳定连过一段又断 → 退避归零(是"连上后掉线"而非"连不上死循环")
        if time.monotonic() - connected_at >= _RECONNECT_STABLE_RESET_SECONDS:
            backoff, attempt = _RECONNECT_INITIAL_BACKOFF_SECONDS, 0
        attempt += 1
        wait = backoff + random.uniform(0, min(backoff, 1.0))
        logger.warning("飞书长连断开(第 %s 次),%.1fs 后进程内重连 error_type=%s", attempt, wait, error_type)
        time.sleep(wait)
        backoff = min(backoff * 2, _RECONNECT_MAX_BACKOFF_SECONDS)
    on_dead()  # 主动停 → 置 _running=False


__all__ = ["FeishuWsClient", "lark_event_to_webhook_payload", "run_ws_client_thread"]


# LLM: SDK 的 mention 不得在 WS 归一过程中丢失，群聊门与 webhook 共用相同结构；只复制 ID 与 key。
# 函数用途: 读取 SDK 事件的 mention 数组，不读或记录聊天正文。
def _normalized_event_mentions(message: object) -> list[dict[str, str]]:
    from .feishu_mentions import normalize_mentions

    return normalize_mentions(getattr(message, "mentions", None))
