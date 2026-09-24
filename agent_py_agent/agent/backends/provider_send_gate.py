# LLM: 发送许可是传输层唯一的实验发送硬门接口：只描述最终请求事实并定义拒绝异常，不读取设置、账本或自然语言。
# 模块用途: 让 gateway_helpers 在任何 DNS/连接/遥测之前把最终请求交给许可复核；普通无许可请求不经过这里。
from __future__ import annotations

import hashlib
from dataclasses import dataclass


# LLM: 刻意直接继承 RuntimeError，不属于 post_json 会重新包装的 HTTP/网络异常族，也不是可恢复或配置类 provider 错误；
# 调用方必须按 code 显式映射，不能触发连接退避、重试或把拒绝当成供应商故障。
# 类用途: 表示发送许可在连接前拒绝了本次请求，code 是固定的结构化原因。
class ProviderSendRefused(RuntimeError):
    # LLM: code 由许可实现的固定分支给出；异常文本不含请求正文、端点或凭据。
    # 函数用途: 保存拒绝代码并生成可读但不泄露材料的异常说明。
    def __init__(self, code: str) -> None:
        self.code = str(code or "send_refused")
        super().__init__(f"发送许可拒绝了本次请求：{self.code}")


# LLM: 事实全部来自最终 urllib.Request：req.data 的摘要就是线上正文；attempt 是本次物理发送序号，从 0 开始。
# 类用途: 把一次即将发出的 HTTP 请求打包成许可可以逐项比较的不可变事实。
@dataclass(frozen=True)
class ProviderSendAttempt:
    method: str
    url: str
    body_sha256: str
    model: str
    attempt: int


# LLM: 只做摘要与字段读取，不发网络；payload 缺模型时给空字符串，由许可拒绝，不从正文猜模型。
# 函数用途: 从最终请求对象及原载荷生成许可需要的发送事实。
def provider_send_attempt(req, payload: object, attempt: int) -> ProviderSendAttempt:
    model = payload.get("model") if isinstance(payload, dict) else ""
    return ProviderSendAttempt(str(req.get_method()), str(req.full_url), hashlib.sha256(req.data or b"").hexdigest(),
                               model if type(model) is str else "", attempt)


__all__ = ["ProviderSendAttempt", "ProviderSendRefused", "provider_send_attempt"]
