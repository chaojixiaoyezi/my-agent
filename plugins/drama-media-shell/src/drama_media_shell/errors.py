# LLM: 所有插件业务失败都通过稳定 code 和有界结构化 extra 返回；不能依赖解析自然语言，也不能携带输入正文。
# 模块用途: 定义检查、作业、权限和夹具流程共用的结构化错误。

from __future__ import annotations


# LLM: code 供模型和测试稳定判断，extra 只允许非敏感结构化事实；server 会统一转换为 isError 工具结果。
# 类用途: 表示一次工具调用可以预期地失败而不终止插件进程。
class DramaShellError(ValueError):
    # LLM: 不保存底层异常或文件内容，避免把本机路径、输入正文和凭据带进工具回执。
    # 函数用途: 保存稳定错误码、中文说明和可选结构化详情。
    def __init__(self, code: str, message: str, **extra: object):
        super().__init__(message)
        self.code = code
        self.extra = extra
