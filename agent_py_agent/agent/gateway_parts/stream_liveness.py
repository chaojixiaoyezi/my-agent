# LLM: 本模块是原 BufferedChunkStreamWriter 的等待展示接口；所有事件仍由 writer._write_event 落原 chunk，不拥有任务或调用生命周期。
# 模块用途: 将同职责的工具/模型等待接口拆出流写入器，保持同一时间戳、消息草稿和外发授权。
from __future__ import annotations

from collections.abc import Callable

from ..conversation.channels import supports_transcript_delivery


# LLM: 仅原 writer 组合此 mixin；字段和写事件函数属于 writer，不能在此创建线程、队列、状态或发送渠道消息。
# 类用途: 声明 IM 等待展示能力，并把执行端真实等待事实交回原 chunk 出口。
class StreamLiveness:
    # LLM: rich transcript 不新增心跳；IM off 也给最小等待状态，不从模型正文判断启用。
    # 函数用途: 声明本请求是否需要长工具状态刷新。
    @property
    def tool_liveness_enabled(self) -> bool:
        return not self.rich_transcript and not supports_transcript_delivery(self.delivery_channel)

    # LLM: 执行器只传数值和工具身份，不 flush 模型缓冲，不伪造 commentary 或改变工具状态。
    # 函数用途: 将长工具心跳写入原 chunk，投递端继续编辑同一短窗口。
    def write_tool_liveness(self, event: dict[str, object], *, still_current: Callable[[], bool] | None = None) -> None:
        self._publish_liveness({"kind": "tool_progress", "verbose_level": self._verbose_level,
            "heartbeat": True, "preview_max_chars": self._im_preview_max_chars, "progress": dict(event)}, still_current)

    # LLM: 模型与工具共用原 IM 展示能力；后台静默 sink 不声明此能力，不产生新窗口或传输。
    # 函数用途: 声明本请求是否能在原进度消息中显示模型等待。
    @property
    def model_liveness_enabled(self) -> bool:
        return self.tool_liveness_enabled

    # LLM: 只接受宿主模型请求等待的身份和数值，不 flush 思考/候选，不宣称业务正在前进。
    # 函数用途: 把未返回模型调用的心跳交回原 chunk，继续沿同一草稿更新短状态和时间。
    def write_model_liveness(self, event: dict[str, object], *, still_current: Callable[[], bool] | None = None) -> None:
        self._publish_liveness({"kind": "model_liveness", "verbose_level": self._verbose_level,
            "preview_max_chars": self._im_preview_max_chars, "progress": dict(event)}, still_current)

    # LLM: 模型/工具共用原 writer；纯谓词合并关闭状态和同观察 stop，不保存或序列化，不撤销已进入外部回调。
    # 函数用途: 在下一落盘/投影边界过滤已停止的等待事件。
    def _publish_liveness(self, event: dict[str, object], still_current: Callable[[], bool] | None) -> None:
        if self.tool_liveness_enabled:
            # LLM: writer 关闭与同观察 stop 合成纯谓词，不序列化，不改变执行生命周期。
            # 函数用途: 向写入和后续投影提供一致的发布资格。
            def current() -> bool:
                return not getattr(self, '_liveness_closed', False) and (still_current is None or still_current())
            self._write_event(event, still_current=current)
