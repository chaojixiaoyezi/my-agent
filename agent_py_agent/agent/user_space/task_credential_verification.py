# LLM: 认证结果只按结构状态码记录，网络异常/403不猜密码错误；不发新卡、不重试、不变更其它凭据。
# 模块用途: 保存准确代次的认证拒绝并提供无正文的安全错误事实；不把网络或权限故障当作密码错误。
from __future__ import annotations

import time
from dataclasses import replace

from ..tooling.models import ToolHandlerOutcome
from ..tooling.web_fetch_runtime import RawResponseParts


# LLM: 状态码只从两种原宿主响应类型读取；不从正文、任意dict或布尔/浮点值猜认证状态。
# 函数用途: 给登录刷新和验证记录共用同一个准确 HTTP 状态投影。
def credential_http_status(response):
    status = (response.status if isinstance(response, RawResponseParts) else
              response.result_envelope.get('http_status') if isinstance(response, ToolHandlerOutcome) else None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


# LLM: 只对明确401生成事实；同引用同代首次拒绝才携opaque ref/generation并要求处理，迟到/重复不要求；登录错误只留code/status不传body。
# 函数用途: 把原认证拒绝交给工具和模型安全解释，供原通知链精确去重；不冒称密码具体哪里错误或自动发卡。
def credential_response_outcome(agent, path, row, response, *, login_response=False):
    recorded = record_credential_verification(agent, path, row, response)
    if credential_http_status(response) != 401:
        if login_response and isinstance(response, ToolHandlerOutcome):
            status = credential_http_status(response)
            return replace(response, output='登录请求未完成。' + (f'目标服务返回 HTTP {status}。' if status else ''),
                result_envelope={'http_status': status} if status is not None else {})
        return response
    text = ('目标服务拒绝了本次凭据认证（HTTP 401）。请用户决定更换凭据或停用；未自动发卡。' if recorded
            else '目标服务拒绝了本次凭据认证（HTTP 401）；未新增当前凭据拒绝记录。')
    facts = {'status': 'rejected', 'http_status': 401, 'record_updated': recorded, 'user_action_required': recorded}
    if recorded:
        facts.update(credential_ref=row['credential_ref'], generation=row['generation'])
    fields = {'output': text,
        'ok': False, 'error_code': 'PERMISSION_BLOCKED', 'result_envelope': {'http_status': 401,
            'credential_authentication': facts}}
    return replace(response, **fields) if isinstance(response, ToolHandlerOutcome) else ToolHandlerOutcome('web_fetch', **fields)


# LLM: 原配额/记录锁内比较引用、正整数generation、密文及非rejected状态；只有首次转换写入，迟到/并发/重复不重写时间或污染新代。
# 函数用途: 在实际 HTTP 401 后一次记录准确凭据代次的认证拒绝；返回本调用是否新增事实，不改监控或传出秘密。
def record_credential_verification(agent, path, row, response):
    from ..common.json_io import read_json_object_report
    from .task_credentials import _credential_mutation, _save_credential_row, credential_path
    if credential_http_status(response) != 401:
        return False
    if type(row.get('generation')) is not int or row['generation'] <= 0:
        return False
    if credential_path(agent.home_paths.owner_home_dir, row.get('credential_ref')) != path:
        return False
    with _credential_mutation(agent, path) as admission:
        latest = read_json_object_report(path, context='credentials.verification').payload
        if (type(latest.get('generation')) is not int or latest.get('generation') != row['generation']
                or latest.get('credential_ref') != row['credential_ref'] or latest.get('ciphertext') != row['ciphertext']
                or latest.get('verification_status') == 'rejected'):
            return False
        latest.update(verification_status='rejected', authentication_error='credentials_rejected', verified_at=time.time())
        _save_credential_row(admission, path, latest)
    return True
