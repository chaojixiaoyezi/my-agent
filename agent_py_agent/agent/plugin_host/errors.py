# LLM: Worker C独立测试的最小错误合同；合并时保留Worker A唯一HostAPIError类型。
# 模块用途: 对外只提供稳定错误码、HTTP状态和副作用结局，不暴露原异常正文。
from __future__ import annotations


# LLM: 领域拒绝保持固定安全消息，outcome不表示规范请求终态。
# 类用途: 传递宿主API的拒绝原因与已观察到的提交状态。
class HostAPIError(ValueError):
    # LLM: 不接受原始异常作公开消息，错误码由各领域固定给出。
    # 函数用途: 构建可被统一HTTP外壳安全投影的错误。
    def __init__(self, code: str, status: int = 400, outcome: str = "not_started"):
        super().__init__(code)
        self.code = code
        self.status = status
        self.outcome = outcome
