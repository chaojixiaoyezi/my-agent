# LLM: 宿主固定接口携带认证信息时不得把 Authorization/body 交给任何重定向第二跳；不提供不安全回落。
# 模块用途: 为原 stdlib HTTP 调用统一拒绝重定向，不解析响应或改变调用方超时、重试和副作用合同。
from __future__ import annotations

import urllib.request


# LLM: 返回 None 令 urllib 抛原 HTTPError，保留数值状态给原调用方裁决，不发送第二个请求。
# 类用途: 拒绝固定认证接口的 HTTP 跳转，避免跨 origin 或降级泄露凭据。
class NoAuthenticatedRedirect(urllib.request.HTTPRedirectHandler):
    # LLM: 不从 URL/正文猜供应商可信关系；携带认证的固定接口所有重定向均拒绝。
    # 函数用途: 停止 urllib 自动构造带原认证的跳转请求。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# LLM: 只替换原运输 opener；响应生命周期仍由 caller with/HTTPError.close 管理，无缓存、日志或新认证状态。
# 函数用途: 以原超时发一次不可重定向的认证请求。
def open_authenticated_request(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(NoAuthenticatedRedirect()).open(request, timeout=timeout)
