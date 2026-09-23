# LLM: 请求宿主只能在原渲染与发送安全点提供回调；ContextVar 不授予权限，不保存身份或模型目录，默认路径零额外 I/O。
# 模块用途: 让宿主选模和完整压缩恢复共用主生成安全点，原身份和提交仍由各宿主负责。
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_HOST: ContextVar[object | None] = ContextVar("model_request_selection_host", default=None)


# LLM: 仅宿主确认尚未进入 provider 时可抛此类型；普通 HTTP/模型错误不得转换成它，避免重复发送。
# 类用途: 通知原调用者撤销临时候选并且只恢复一次原模型。
class ModelRequestSelectionRejected(RuntimeError):
    pass


# LLM: 绑定对象来自内部入口；finally 清理，复制上下文的原生成 worker 可读取同一对象但不能关闭 caller 的绑定。
# 函数用途: 在一个原请求的完整生命周期内安装可选采用回调。
@contextmanager
def model_request_selection_scope(host: object):
    token = _HOST.set(host)
    try:
        yield
    finally:
        _HOST.reset(token)


# LLM: 宿主返回 None 表示沿原 renderer；不额外 gather，子代理仍使用自己的首次请求资格。
# 函数用途: 在原准备时点允许宿主保留实际冻结提示材料。
def render_selected_request(agent: object, params: object, request: object) -> str:
    from .agent_core.subagent.model_selection import render_first_request_prompt

    host = _HOST.get()
    rendered = host.render(agent, params, request) if host is not None else None
    return rendered if rendered is not None else render_first_request_prompt(agent, params, request)


# LLM: 只把原完整模型参数交给请求宿主；无作用域时返回同一对象和同一字节，不改变子代理采用策略。
# 函数用途: 在首请求完整准备后选定本工作片依赖，工具后续轮次保持已采用配置。
def select_request_model(agent: object, params: object, prompt: str) -> tuple[object, str]:
    host = _HOST.get()
    return host.select(agent, params, prompt) if host is not None else (params, prompt)


# LLM: 回调位于原 provider observer/准入内、实际网络前；宿主不得在这里生成模型回复或重新调用 Jev。
# 恢复专用宿主没有发送前事务，不能为适配接口另造空事务。
# 函数用途: 对提供此回调的宿主执行最终发送检查。
def before_model_request_send(backend: object, prompt: str, state: object) -> None:
    host = _HOST.get()
    callback = getattr(host, "before_send", None)
    if callback is not None:
        callback(backend, prompt, state)


# LLM: 只在 caller 处理明确的发送前拒绝，ContextVar token 在创建它的线程退出；不得吞取消或 HTTP 错误。
# 函数用途: 撤销尚未提交的临时候选，随后原生成入口可沿原模型执行一次。
def reject_request_selection(agent: object, params: object) -> None:
    host = _HOST.get()
    if host is None:
        raise RuntimeError("缺少模型请求宿主。")
    host.reject(agent, params)


# LLM: 仅内部恢复宿主可领取本次Compact时点；没有该方法的正常选模宿主不改变原自动回收。
# 函数用途: 在完整恢复捕获前暂缓自动压缩，避免同来源先被另一路提交。
def request_owns_compact(agent: object, params: object) -> bool:
    host = _HOST.get()
    callback = getattr(host, "owns_compact", None)
    return callback is not None and callback(agent, params) is True
