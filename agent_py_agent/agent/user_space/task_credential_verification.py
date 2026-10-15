# LLM: 认证结果只按结构状态码记录，网络异常/403不猜密码错误；不发新卡、不重试、不变更其它凭据。
# 模块用途: 保存准确代次的认证事实，错误候选不会覆盖原有效引用。
from __future__ import annotations

import time


# LLM: 原凭据锁内比较 generation 与密文，迟到请求不能污染重填版本；响应正文和秘密不写入记录。
# 函数用途: 在实际 HTTP 使用后记录明确拒绝或成功 HTTP 事实，不把业务成功等同密码验证成功。
def record_credential_verification(agent, path, row, response):
    from ..common.json_io import read_json_object_report
    from ..tooling.models import ToolHandlerOutcome
    from ..tooling.web_fetch_runtime import RawResponseParts
    from .task_credentials import _credential_mutation, _save_credential_row
    status = (response.status if isinstance(response, RawResponseParts) else
              response.result_envelope.get('http_status') if isinstance(response, ToolHandlerOutcome) else None)
    if status != 401:
        return
    with _credential_mutation(agent, path) as admission:
        latest = read_json_object_report(path, context='credentials.verification').payload
        if latest.get('generation') != row['generation'] or latest.get('ciphertext') != row['ciphertext']:
            return
        latest.update(verification_status='rejected', authentication_error='credentials_rejected', verified_at=time.time())
        _save_credential_row(admission, path, latest)
