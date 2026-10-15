# LLM: 运输观测只读取原 SDK 实例的帧头及连接标识；不读取 payload、修改帧、分发事件或保存第二状态账。
# 模块用途: 用固定阶段和身份摘要定位卡片回调停在哪一层，日志失败不能影响原 SDK 的返回、异常或 ACK。
from __future__ import annotations

import logging

from .protocol import provider_message_reference

logger = logging.getLogger(__name__)
_FRAME_STAGES = frozenset({'received', 'returned', 'exception'})
_CARD_STAGES = frozenset({'entered', 'invalid', 'duplicate_cached', 'claim_rejected',
    'handler_missing', 'success', 'warning', 'rejected', 'no_response', 'exception'})


# LLM: 摘要只接受 SDK 的原字符串，不能调用未知对象的 repr/str 或把任意头值带入日志。
# 函数用途: 复用供应商消息摘要生成安全诊断引用，空值或非字符串保持未知。
def _reference(value: object) -> str:
    return provider_message_reference(value) if type(value) is str else ''


# LLM: 连接身份直接读取 SDK 每次握手更新的 _conn_id，不解析 WS URL、不另存连接代次。
# 函数用途: 将当前 SDK 连接与同进程卡片处理阶段关联起来，不输出原连接标识。
def _connection_reference(client: object) -> str:
    return _reference(getattr(client, '_conn_id', None))


# LLM: 只读取固定三类帧头；忽略未知头，类型只能输出 event/card/unknown，完全不访问 payload。
# 函数用途: 提取用于关联 SDK 收帧的安全元数据，保留原帧对象和所有内容。
def _frame_metadata(frame: object) -> dict[str, str]:
    selected = {}
    for header in getattr(frame, 'headers', ()):
        key = getattr(header, 'key', None)
        if key in {'type', 'message_id', 'trace_id'} and key not in selected:
            selected[key] = getattr(header, 'value', None)
    kind = selected.get('type')
    return {'frame_type': kind if kind in {'event', 'card'} else 'unknown',
            'message_ref': _reference(selected.get('message_id')),
            'trace_ref': _reference(selected.get('trace_id'))}


# LLM: 观测错误不进入原运输异常路径；只输出固定阶段与摘要，不包含事件名、异常正文或响应正文。
# 函数用途: 记录原 SDK 数据帧的进入和退出位置，日志不可用时继续原运输。
def _log_frame(client: object, frame: object, stage: str) -> None:
    try:
        facts = _frame_metadata(frame)
        logger.info('feishu_ws_frame stage=%s frame_type=%s message_ref=%s trace_ref=%s connection_ref=%s',
            stage if stage in _FRAME_STAGES else 'unknown', facts['frame_type'], facts['message_ref'],
            facts['trace_ref'], _connection_reference(client))
    except Exception:
        pass


# LLM: 仅原 SDK 实例被包装一次，原 async 方法仍负责解包、CARD 行为、分发、ACK 和重试；不改类或全局 SDK。
# 函数用途: 给本次真实连接安装安全元数据日志，旧 SDK 缺入口时仅报告不可观测，不阻止启动。
def install_ws_observation(client: object, *, card_handler_registered: bool) -> None:
    original = getattr(client, '_handle_data_frame', None)
    if getattr(client, '_my_agent_frame_observed', False):
        return
    installed = False
    if callable(original):
        try:
            client._handle_data_frame = _observed_frame_method(client, original)
            client._my_agent_frame_observed = True
            installed = True
        except Exception:
            pass
    try:
        logger.info('feishu_ws_observation frame_hook=%s card_handler_registered=%s',
                    installed, card_handler_registered is True)
    except Exception:
        pass


# LLM: 收帧与原返回/异常一一对应；同一帧原样交回 SDK，异常必须原样抛给它的既有外层处理。
# 函数用途: 包装一次原调用，不增加分发、ACK、异常吞并或重试。
def _observed_frame_method(client, original):
    # LLM: 包装器不检查原返回对象或异常内容，包含取消在内的异常一律原样传播。
    # 函数用途: 记录原 SDK 调用的进入和退出阶段。
    async def observed(frame):
        _log_frame(client, frame, 'received')
        try:
            result = await original(frame)
        except BaseException:
            _log_frame(client, frame, 'exception')
            raise
        _log_frame(client, frame, 'returned')
        return result

    return observed


# LLM: 业务阶段只接受固定标识；连接来自原 SDK，消息仅取原规范化 message_id，不接受表单或引用字段。
# 函数用途: 记录卡片进入、去重、业务回执和异常阶段，不输出卡片值、正文或凭据。
def log_card_stage(client: object, stage: str, *, message_id: object = '') -> None:
    try:
        logger.info('feishu_ws_card stage=%s message_ref=%s connection_ref=%s',
                    stage if stage in _CARD_STAGES else 'unknown', _reference(message_id), _connection_reference(client))
    except Exception:
        pass


# LLM: 只检查固定 toast.type 的枚举，不读 toast.content、卡片内容或错误字符串；分类不影响业务决定。
# 函数用途: 将已有回执投影成可安全记录的卡片退出阶段。
def card_response_stage(response: object) -> str:
    toast = response.get('toast') if isinstance(response, dict) else None
    kind = toast.get('type') if isinstance(toast, dict) else None
    return {'success': 'success', 'warning': 'warning', 'error': 'rejected'}.get(kind, 'no_response') if type(kind) is str else 'no_response'
