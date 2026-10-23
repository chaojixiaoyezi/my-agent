# LLM: 错误只携带协议字段，不回显异常链、参数、凭据或路径；outcome独立于HTTP状态。
# 模块用途: 定义插件宿主v2唯一的结构化错误。
from __future__ import annotations

import re


# LLM: 只能由宿主选择安全message；retryable不意味着允许重放未知副作用。
# 类用途: 在协议、授权及传输之间保持一致的拒绝原因。
class HostAPIError(ValueError):
    # LLM: 四个显式参数保持稳定，不能从异常字符串推断提交事实。
    # 函数用途: 验证并保存公开错误码、HTTP状态与执行结局。
    def __init__(self, code: str, status: int = 400, outcome: str = "not_started", message: str = "请求不符合宿主接口要求。"):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            raise ValueError("错误码无效")
        if type(status) is not int or not 400 <= status <= 599:
            raise ValueError("错误HTTP状态无效")
        if outcome not in {"not_started", "committed", "unknown"} or len(message) > 256:
            raise ValueError("错误结局或消息无效")
        super().__init__(f"{code}: {message}")
        self.code, self.status, self.outcome = code, status, outcome
        self.message = message

    # LLM: 不输出traceback和底层异常文本，未知结果不自动标为可重试。
    # 函数用途: 生成闭合错误投影。
    def to_payload(self) -> dict:
        return {"code": self.code, "message": self.message,
                "retryable": self.status in {429, 503} and self.outcome == "not_started", "outcome": self.outcome}
