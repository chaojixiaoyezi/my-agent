# LLM: 带账号凭据的 HTTP 请求不跟随任何重定向；复用标准异常，既有传输层负责脱敏和错误分类。
# 模块用途: 为 OAuth 模型请求阻止凭据转发。
from urllib.request import HTTPRedirectHandler


# LLM: 不创建跳转后的 Request，调用方按原 3xx 失败处理，不放宽认证目的地。
# 类用途: 保持账号凭据只发送给显式绑定的接口。
class NoAuthRedirect(HTTPRedirectHandler):
    # LLM: 此方法只拒绝新请求，不操作原请求或账号状态。
    # 函数用途: 禁止 OAuth 请求随 301/302/307/308 跳转。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
