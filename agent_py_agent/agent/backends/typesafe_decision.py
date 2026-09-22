# LLM: 原生 decide 独立于生成后端，只经原 HTTP 发送一次；调用者负责准入、完整等待期限、原账本与应用权。
# 模块用途: 用标准库传输接入 TypeSafe Jev，不安装 harness/SDK，不生成聊天正文或执行工具。
from __future__ import annotations

import math

from .base import BackendOptions
from .decision_protocol import (
    MAX_DECISION_RESPONSE_BYTES,
    DecisionInputError,
    DecisionRequest,
    DecisionResponse,
)
from .gateway_helpers import GatewayRequest, post_json
from .gateway_request_limits import remaining_deadline_seconds
from .provider_headers import (
    endpoint_parts,
    request_headers,
    validate_headers,
    validate_session_header,
)
from .typesafe_decision_wire import parse_typesafe_response, typesafe_payload


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
        self._custom_headers = validate_headers(options.custom_headers)
        self._session_header = validate_session_header(options.session_header)

    # LLM: deadline 为宿主准备前建立的绝对 monotonic 期限，不得在重试/解码时重置；迟到响应没有采用权。
    # 函数用途: 校验题目后发一次无重试、无重定向的请求，返回逐题结果；超时和取消交给可选调用边界分类。
    def decide(self, request: DecisionRequest, *, deadline: float) -> DecisionResponse:
        try:
            valid = type(deadline) in (int, float) and math.isfinite(deadline)
        except OverflowError:
            valid = False
        if not valid:
            raise DecisionInputError("决策期限须为有限时间。")
        remaining_deadline_seconds(deadline)
        payload = typesafe_payload(request, self.model_name)
        headers = request_headers({"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"},
                                  self._custom_headers, self._session_header)
        remaining = remaining_deadline_seconds(deadline)
        raw = post_json(GatewayRequest(api_base=self.api_base, api_key=self._api_key, path=self._path,
            payload=payload, headers=headers, timeout=remaining, connect_timeout=remaining,
            allow_redirects=False, deadline=deadline, max_retries=0, max_response_bytes=MAX_DECISION_RESPONSE_BYTES))
        remaining_deadline_seconds(deadline)
        result = parse_typesafe_response(request, self.model_name, raw)
        remaining_deadline_seconds(deadline)
        return result
