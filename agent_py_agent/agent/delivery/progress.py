# LLM: 进度投递回执只承诺 provider 确认的消息 ID，不等同最终答复或任务完成；由原 pending 持久化。
# 模块用途: 定义同一条进度更新的结构化发送结果，供统一投递服务与适配器 worker 共用。
from __future__ import annotations

from dataclasses import dataclass


# LLM: 消息 ID 只能来自 provider 回执或原 pending；failure_kind 是结构化分类，unknown/布尔失败不证明外部副作用未发生。
# 类用途: 保存进度发送或编辑的实际结果及永久/暂时/未知失败事实，错误不阻止最终回复。
@dataclass(frozen=True)
class ProgressDeliveryReceipt:
    ok: bool
    message_id: str = ""
    error_code: str = ""
    failure_kind: str = "unknown"

    # LLM: 兼容 worker 的布尔发送判据，只以明确 ok 为发送成功，不能把非空对象当成功。
    # 函数用途: 在原外发状态机里使用真实发送结果。
    def __bool__(self) -> bool:
        return self.ok
