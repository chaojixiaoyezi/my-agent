# LLM: 只放插件内共用的业务错误类型；不依赖 SDK，供纯标准库模块（启动器、CDP）和协议入口共用。
# 模块用途: 定义带稳定错误码的中文业务错误。

from __future__ import annotations

UNAVAILABLE_MESSAGE = "浏览器不可用：未找到 Chrome/Chromium（可在设置 chrome_path 指定）"


# LLM: code 是稳定的机器可读分类，正文是给用户看的中文说明，不含堆栈、本地配置或页面正文以外的私有信息。
#   不继承 ValueError，避免与上下文解析错误混在同一个 except 分支里。
# 类用途: 让协议入口把浏览器失败返回为工具错误结果，不终止插件进程。
class BrowserError(Exception):
    # LLM: extra 只放结构化事实（如匹配个数、被拦地址），协议入口原样并入错误结果。
    # 函数用途: 保存错误码、中文说明和附加结构化字段。
    def __init__(self, code: str, message: str, **extra: object):
        super().__init__(message)
        self.code = code
        self.extra = extra
