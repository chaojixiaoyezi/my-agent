# LLM: OAuth 是已有三种后端的认证层，不另建模型/工具循环；每次请求核对 owner 私有引用并刷新令牌。
# 模块用途: 将登录账号接入原 HTTP、流式、取消及用量链路，订阅接口的显式差异保持局部。
from __future__ import annotations

from .base import AnthropicCompatibleBackend, OpenAICompatibleBackend
from .gateway_helpers import GatewayRequest
from .provider_headers import endpoint_parts, request_headers
from .responses import OpenAIResponsesBackend


# LLM: 每个实例持有无秘密的冻结引用；令牌不会缓存在工作片中，也不能修改共享 Agent 配置。
# 类用途: 为已有协议附加 owner 私有 OAuth 认证。
class _OAuthMixin:
    # LLM: 引用由模型配置解析器生成，必须完整；旧登录代次失效时不能自行读取新账号。
    # 函数用途: 初始化认证引用并复用原后端初始化。
    def __init__(self, options, *, auth_ref: dict, **kwargs):
        self.auth_ref = dict(auth_ref)
        if not all(self.auth_ref.get(field) for field in ("path", "provider_id", "generation", "binding", "mode")):
            raise ValueError("OAuth 运行引用不完整，请重新选择已登录的模型。")
        super().__init__(options, **kwargs)
        if self.auth_ref["mode"] == "chatgpt":
            self.stream_enabled = True
            self.stream_timeout_is_idle = True

    # LLM: 兼容原后端生成/探针的凭据检查；仅在真实调用访问，菜单读取不会触发刷新。
    # 函数用途: 获取当前有效访问令牌，不将其写入模型输入或日志。
    @property
    def api_key(self):
        from ..settings.model_oauth import request_credentials

        return request_credentials(self.auth_ref, self.api_base)[0]

    # LLM: 原后端初始化会赋 api_key；OAuth 不保存该占位值，唯一秘密源仍是 owner provider。
    # 函数用途: 保持原构造协议但拒绝混入 API Key。
    @api_key.setter
    def api_key(self, value):
        if value:
            raise ValueError("OAuth 登录不能与 API Key 混用。")

    # LLM: 每次物理请求使用同一份刷新结果，认证头覆盖生成期旧值；禁止重定向投递 token。
    # 函数用途: 用原传输封装发送带账号信息的请求，不增加自动重试或新后台线程。
    def _gateway_request(self, path, payload, headers, *, first_event_timeout_seconds=None):
        from ..settings.model_oauth import request_credentials

        key, account_headers = request_credentials(self.auth_ref, self.api_base)
        clean = {name: value for name, value in headers.items() if name.lower() not in {"authorization", "x-api-key", "chatgpt-account-id"}}
        clean.update(Authorization="Bearer " + key, **account_headers)
        base, suffix = endpoint_parts(self.api_base, path)
        return GatewayRequest(api_base=base, path=suffix, api_key=key, payload=payload,
            headers=request_headers(clean, self.custom_headers, self.session_header), timeout=self.request_timeout,
            connect_timeout=self.connect_timeout, first_event_timeout=first_event_timeout_seconds, allow_redirects=False)

    # LLM: OAuth 用户必须显式填写容量，不能以 metadata 探针向另一条 API 路径附送订阅凭据。
    # 函数用途: 保持容量来自用户配置；目录发现仍是独立显式菜单动作。
    def provider_context_window_tokens(self):
        return 0


# LLM: 保持原 Chat 工具、用量与原生回放合同，仅改变认证来源。
# 类用途: 使用通用 OAuth Bearer 登录的 Chat 接口。
class OAuthChatBackend(_OAuthMixin, OpenAICompatibleBackend):
    pass


# LLM: 保持原 Messages 工具与取消合同；通用服务商需支持 Bearer，不自动转为其他认证协议。
# 类用途: 使用通用 OAuth Bearer 登录的 Messages 接口。
class OAuthMessagesBackend(_OAuthMixin, AnthropicCompatibleBackend):
    pass


# LLM: ChatGPT 订阅使用 Responses 原循环；协议差异通过显式 mode，不按模型名猜测。
# 类用途: 使用账号登录的 Responses 接口，支持主代理、子代理及 Compact。
class OAuthResponsesBackend(_OAuthMixin, OpenAIResponsesBackend):
    pass
