# LLM: 无通道线程仍写 ThreadStore 的原线程文件并持同一 JSON 锁；不建绑定索引，不复用子代理身份。
# 模块用途: 为宿主明确创建的独立工作物化精确线程，重试保持已有模型选择、Compact 和消息身份。
from __future__ import annotations

from ..gateway_parts.io import update_json_file_atomic
from ..runtime_errors import DataCorruptionError
from .models import ConversationThread
from .store_io import safe_file_stem


# LLM: seed 必须由宿主构造；冲突拒绝，不覆盖已有线程。只核对持久身份与 owner，不重写可变 cwd/模型/Compact。
# 函数用途: 在原线程事务中创建无绑定会话，或返回完全相同归属的既有会话。
def ensure_unbound_thread(store, seed: ConversationThread, *, identity: dict) -> ConversationThread:
    if not seed.thread_id or safe_file_stem(seed.thread_id) != seed.thread_id or seed.channel_bindings:
        raise ValueError("独立线程必须具有精确安全 ID 且不带通道绑定")

    # LLM: 原子回调不发网络、不写其他账；identity 仅宿主声明的不可变来源字段。
    # 函数用途: 拒绝线程碰撞，保留并发设置和已有工作进度。
    def apply(payload):
        if not payload:
            return seed.to_dict()
        current = ConversationThread.from_dict(payload)
        if (current.thread_id != seed.thread_id or current.owner_id != seed.owner_id
                or current.owner_home != seed.owner_home or current.channel_bindings
                or any(current.metadata.get(key) != value for key, value in identity.items())):
            raise DataCorruptionError("独立线程归属冲突")
        return payload

    payload = update_json_file_atomic(store.storage.thread_path(seed.thread_id), apply)
    return ConversationThread.from_dict(payload)
