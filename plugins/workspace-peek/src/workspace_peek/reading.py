# LLM: 路径授权始终读逐次 SDK 上下文；游标仅绑定进度，不授予权限，不替代每次安全打开。
# 模块用途: 为文件预览和目录分页提供共用的路径检查、身份摘要与游标编码。

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path

from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext


# LLM: code 是业务失败分类；正文只提供可操作说明，不含原始异常、配置或被拒绝文件内容。
# 类用途: 让协议入口把读取失败返回为工具结果，不终止整个插件进程。
class ReadError(ValueError):
    # LLM: 本异常不表示宿主操作状态，不能用它重试已发生的调用或修改权限。
    # 函数用途: 保存稳定错误码及中文展示说明。
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# LLM: lexical 路径用于打开，resolve 仅在 SDK 内做权限判断；不能先 resolve 擦掉路径链上的链接。
# 函数用途: 组合当前工作区路径，拒绝上溯和空路径，再按原合同检查读取资格。
def authorized_path(context: WorkspaceReadContext, value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or ".." in Path(value).parts:
        raise ReadError("INVALID_PATH", "路径不能为空或含上溯组件。")
    path = context.cwd / value
    decision = context.check(path)
    if not decision.allowed:
        raise ReadError(decision.code, "目标不在本次允许读取的范围内。")
    return path


# LLM: 使用稳定 stat 字段检查同一描述符前后变化，不从内容推测完整性或跨文件继承进度。
# 函数用途: 提取文件或目录的可比较身份和变化标志。
def identity(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


# LLM: 摘要只绑定结构化事实；JSON 排序保证不同调用的同一上下文可以续页。
# 函数用途: 计算游标需要绑定的权限或目录快照摘要。
def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


# LLM: 字符串有上界，base64 不是签名；每次仍须重新授权、打开和比对身份。
# 函数用途: 将已读偏移与快照绑定为下一页参数。
def encode_cursor(binding: str, offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([1, binding, offset], separators=(",", ":")).encode()).decode()


# LLM: 游标版本、类型、偏移与本次事实全部精确匹配；拒绝 bool 冒充整数或过期对象。
# 函数用途: 读回页起点，坏游标不静默从头重读。
def decode_cursor(token: str | None, binding: str, size: int) -> int:
    if token is None:
        return 0
    try:
        if not isinstance(token, str) or not 0 < len(token) <= 512:
            raise ValueError
        value = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        if (not isinstance(value, list) or len(value) != 3 or type(value[0]) is not int or value[0] != 1
                or value[1] != binding or type(value[2]) is not int or not 0 <= value[2] <= size):
            raise ValueError
        return value[2]
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ReadError("INVALID_CURSOR", "游标无效或读取对象已变化，请从首页重新读取。") from exc
