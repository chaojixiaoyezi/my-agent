# LLM: 原生 decide 独立于生成后端，只经原 HTTP 发送一次；调用者负责准入、完整等待期限、原账本与应用权。
# 实验调用拆成无网络 prepare 与必须携带发送许可的 send；普通 decide 的请求字节与行为保持原样。
# 模块用途: 为正常增强和显式测试共用原决策配置映射与标准库传输，不安装 harness/SDK、不生成聊天或执行工具。
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .base import BackendOptions
from .decision_protocol import (
    MAX_DECISION_RESPONSE_BYTES,
    DecisionInputError,
    DecisionRequest,
    DecisionResponse,
)
from .gateway_helpers import GatewayRequest, gateway_request_body, post_json
from .gateway_request_limits import remaining_deadline_seconds
from .provider_headers import (
    endpoint_parts,
    request_headers,
    validate_headers,
    validate_session_header,
)
from .provider_send_gate import ProviderSendRefused
from .typesafe_decision_wire import (
    parse_typesafe_response,
    typesafe_payload,
    validate_typesafe_request_window,
)


# LLM: body 是按唯一 wire 编码器得到的最终线上字节，上界与许可摘要都以它为准；payload 仍是 send 实际提交的同一对象。
# 类用途: 保存一次已校验、尚未联网的 Jev 请求：原请求、线上载荷与字节、端点、模型和绝对期限。
@dataclass(frozen=True)
class PreparedDecisionCall:
    request: DecisionRequest
    payload: dict = field(repr=False, compare=False)
    body: bytes = field(repr=False)
    endpoint: str
    model: str
    deadline: float


# LLM: 连接复用 BackendOptions，决策不实现 generate；角色解析须由原 model_profiles 在构造前完成。
# 类用途: 保存一份决策连接快照，提供一次结构化判断；创建实例不访问网络。
class TypesafeDecisionBackend:
    name = "typesafe_decision"

    # LLM: 自定义头复制并校验，引用原连接配置不建凭据存储；此类不会替代普通生成后端。
    # 函数用途: 冻结已经过 owner 用途验证的连接参数，不测试模型或启用接入点。
    def __init__(self, options: BackendOptions) -> None:
        self.api_base, self._path = endpoint_parts(options.api_base, "/v1/systemone")
        self._api_key = options.api_key
        self.model_name = options.model_name
        self.context_window_tokens = options.context_window_tokens
        self._custom_headers = validate_headers(options.custom_headers)
        self._session_header = validate_session_header(options.session_header)

    # LLM: deadline 为宿主准备前建立的绝对 monotonic 期限，不得在重试/解码时重置；迟到响应没有采用权。
    # 函数用途: 校验题目后发一次无重试、无重定向的请求，返回逐题结果；超时和取消交给可选调用边界分类。
    def decide(self, request: DecisionRequest, *, deadline: float) -> DecisionResponse:
        return self._post(self.prepare(request, deadline=deadline), None)

    # LLM: 纯本地校验与编码，不构造请求头、不读会话、不联网；窗口门仍是原保守筛选，不是供应商 tokenizer。
    # 函数用途: 生成实验计算输入上界和发送许可所需的最终请求字节及绑定事实。
    def prepare(self, request: DecisionRequest, *, deadline: float) -> PreparedDecisionCall:
        try:
            valid = type(deadline) in (int, float) and math.isfinite(deadline)
        except OverflowError:
            valid = False
        if not valid:
            raise DecisionInputError("决策期限须为有限时间。")
        remaining_deadline_seconds(deadline)
        payload = typesafe_payload(request, self.model_name)
        validate_typesafe_request_window(payload, self.context_window_tokens)
        return PreparedDecisionCall(request, payload, gateway_request_body(payload), self.api_base + self._path,
                                    self.model_name, float(deadline))

    # LLM: 没有可调用 admit 的许可就在构造请求前拒绝；许可在传输层每次物理发送前复核，本方法不自行放行。
    # 函数用途: 以同一份已准备载荷发送一次实验请求，许可、零重试与禁止重定向由传输信封强制。
    def send(self, prepared: PreparedDecisionCall, *, permit: object) -> DecisionResponse:
        if type(prepared) is not PreparedDecisionCall or not callable(getattr(permit, "admit", None)):
            raise ProviderSendRefused("send_permit_missing")
        return self._post(prepared, permit)

    # LLM: 请求头在发送线程内构造，沿原会话头作用域；permit=None 时与原 decide 的信封及字节完全一致。
    # 函数用途: 执行一次原严格 JSON 请求并解析逐题结果，发送前后都复核同一绝对期限。
    def _post(self, prepared: PreparedDecisionCall, permit: object) -> DecisionResponse:
        headers = request_headers({"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"},
                                  self._custom_headers, self._session_header)
        remaining = remaining_deadline_seconds(prepared.deadline)
        raw = post_json(GatewayRequest(api_base=self.api_base, api_key=self._api_key, path=self._path,
            payload=prepared.payload, headers=headers, timeout=remaining, connect_timeout=remaining,
            allow_redirects=False, deadline=prepared.deadline, max_retries=0, max_response_bytes=MAX_DECISION_RESPONSE_BYTES,
            send_permit=permit))
        remaining_deadline_seconds(prepared.deadline)
        result = parse_typesafe_response(prepared.request, self.model_name, raw)
        remaining_deadline_seconds(prepared.deadline)
        return result


# LLM: 接入服务和显式探测共用同一配置映射；profile必须已由原owner目录解析，不在此验证权限或读取配置。
# 函数用途: 从已授权的原决策配置创建无网络后端，避免连接头和窗口字段在两个入口分叉。
def decision_backend_from_profile(config: dict) -> TypesafeDecisionBackend:
    return TypesafeDecisionBackend(BackendOptions(api_base=config["api_base"], api_key=config["api_key"],
        model_name=config["model_name"], context_window_tokens=config["model_context_window_tokens"],
        custom_headers=config["model_custom_headers"], session_header=config["model_session_header"]))
