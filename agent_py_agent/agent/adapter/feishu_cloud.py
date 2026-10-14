# LLM: 云资源和权限证明共用原 FeishuAdapter 的宿主 token，目标固定官方 API；错误只保留结构化状态，不返回秘密或自动重放未知写入。
# 模块用途: 调用飞书云资源和权限接口，拒绝认证重定向，区分凭据、应用权限、资源权限和限流错误。
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..common.authenticated_http import open_authenticated_request
from ..common.log_redaction import redact_sensitive_value

API_BASE = 'https://open.feishu.cn/open-apis'
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


# LLM: code 是宿主稳定错误码；message 只用本地文案，不包含供应商正文或网络异常中的认证数据。
# 类用途: 区分缺凭据、供应商拒绝和未知写入结果。
class FeishuCloudError(RuntimeError):
    # LLM: unknown 仅表示请求可能到达供应商；HTTP/API 数值和固定 reason 不含供应商正文，原操作账不能自动重放写入。
    # 函数用途: 保存可读原因及公开诊断字段，供工具返回明确下一步。
    def __init__(self, code: str, message: str, *, unknown: bool = False,
                 http_status: int = 0, provider_code: int | None = None, reason: str = ''):
        super().__init__(message)
        self.code = code
        self.unknown = unknown
        self.http_status = http_status
        self.provider_code = provider_code
        self.reason = reason

    # LLM: 公开字段由本模块产生，绝不复制 URL、响应正文、认证头或异常文本。
    # 函数用途: 返回能用于诊断和恢复判断的最小结构化事实。
    def public_facts(self) -> dict:
        return {'http_status': self.http_status, 'provider_code': self.provider_code,
                'reason': self.reason, 'effect_outcome': 'unknown' if self.unknown else 'not_started'}


# LLM: 响应净化先于工具结果和归档；精确秘密的 JSON/URL 编码也移除，字典键不能成为泄露旁路。
# 函数用途: 保留云文档数据结构，删除本次宿主认证回显。
def _clean_response(value, secrets):
    from urllib.parse import quote
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                for variant in (secret, json.dumps(secret, ensure_ascii=True)[1:-1], quote(secret, safe='')):
                    value = value.replace(variant, '[private]')
        return value
    if isinstance(value, dict):
        return {_clean_response(key, secrets): _clean_response(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_response(item, secrets) for item in value]
    return value


# LLM: 飞书对象编号和分页游标不是认证凭据；投影到公开 ID 字段后仍走通用脱敏及精确宿主秘密清理，不放宽日志规则。
# 函数用途: 避免非秘密的资源编号和游标被误删，保持 wiki 定位、所有者核验及分页可用。
def _public_identifiers(value):
    names = {'obj_token': 'resource_id', 'doc_token': 'resource_id', 'obj_type': 'resource_type',
             'page_token': 'next_cursor', 'spreadsheet_token': 'resource_id'}
    if isinstance(value, dict):
        projected = {}
        for key, item in value.items():
            public_key = names.get(key, key)
            if public_key in projected:
                raise ValueError('云资源响应字段冲突')
            projected[public_key] = _public_identifiers(item)
        return projected
    if isinstance(value, list):
        return [_public_identifiers(item) for item in value]
    return value


# LLM: 只接收宿主生成的官方路径；read_only 只给元数据 POST 使用，业务写入默认保持 unknown，不接受模型 URL 或自动重试。
# 函数用途: 发一次有界云请求，净化成功数据并为失败保留 HTTP/API 原因事实。
def request_cloud_api(adapter, method: str, path: str, body: dict | None = None, *, read_only: bool = False) -> dict:
    if not path.startswith('/') or path.startswith('//') or '..' in path.split('?')[0].split('/'):
        raise ValueError('云接口路径无效')
    token = adapter._get_tenant_access_token()
    if not token:
        raise FeishuCloudError('FEISHU_CREDENTIAL_UNAVAILABLE', '宿主飞书应用凭据不可用，请管理员检查应用配置。')
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
    request = urllib.request.Request(API_BASE + path, data=data, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json; charset=utf-8'})
    mutating = method != 'GET' and not read_only
    try:
        with open_authenticated_request(request, timeout=20) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError('response too large')
        result = json.loads(raw)
        if not isinstance(result, dict) or type(result.get('code')) is not int:
            raise ValueError('invalid response')
    except urllib.error.HTTPError as exc:
        # 429/5xx 不能据此推断写操作未发生，保留 unknown，不复制供应商正文。
        provider_code = _http_error_code(exc)
        raise _classified_error(exc.code, provider_code, unknown=mutating and (exc.code >= 500 or exc.code == 429)) from None
    except (OSError, ValueError, TypeError, RecursionError):
        raise FeishuCloudError('FEISHU_CLOUD_RESPONSE_UNKNOWN', '飞书云接口未返回可验证结果；写操作请先读回核对，不能直接重复追加。', unknown=mutating,
                               reason='response_unknown') from None
    if result['code'] != 0:
        raise _classified_error(0, result['code'], unknown=mutating and _provider_error_details(result['code']) is None)
    secrets = (token, str(getattr(adapter, 'app_secret', '') or ''))
    try:
        payload = result.get('data')
        if payload is not None and not isinstance(payload, dict):
            raise ValueError('invalid response data')
        return _clean_response(redact_sensitive_value(_public_identifiers(payload or {})), secrets)
    except (ValueError, TypeError, RecursionError):
        raise FeishuCloudError('FEISHU_CLOUD_RESPONSE_UNKNOWN', '云接口响应无法投影；写操作请先读回核对，不能直接重复追加。', unknown=mutating,
                               reason='response_projection_unknown') from None


# LLM: HTTP 错误体只能贡献数值 code；读量有界，其余字段包括 msg、headers 和正文不进入公开错误。
# 函数用途: 在供应商返回 HTTP 错误时保留可分类的 API 错误编号。
def _http_error_code(error: urllib.error.HTTPError) -> int | None:
    try:
        raw = error.read(_MAX_RESPONSE_BYTES + 1)
        payload = json.loads(raw) if len(raw) <= _MAX_RESPONSE_BYTES else None
        value = payload.get('code') if isinstance(payload, dict) else None
        return value if type(value) is int and value != 0 else None
    except (OSError, ValueError, TypeError):
        return None


# LLM: 分类只依官方固定数值或 HTTP 状态，不解析供应商自然语言；429/5xx 未知写入事实由调用方原样传入。
# 函数用途: 区分可修复的凭据、应用 scope、资源协作者、额度和供应商故障。
def _classified_error(http_status: int, provider_code: int | None, *, unknown: bool = False) -> FeishuCloudError:
    details = _provider_error_details(provider_code) or _http_error_details(http_status)
    code, reason, message = details
    return FeishuCloudError(code, message, unknown=unknown, http_status=http_status,
                           provider_code=provider_code, reason=reason)


# LLM: 固定供应商 code 优先于 HTTP 包装；只使用官方编号，不从 msg 内容猜授权或隐式扩大重试范围。
# 函数用途: 将飞书明确返回的应用和资源错误转换为稳定的宿主诊断。
def _provider_error_details(provider_code: int | None) -> tuple[str, str, str] | None:
    if provider_code in {99991672, 99991679}:
        return ('FEISHU_CLOUD_SCOPE_REQUIRED', 'application_scope_required',
            '飞书应用缺少接口权限或用户授权 scope；请管理员在开放平台启用对应权限并发布。')
    if provider_code in {20004, 20006, 99991663, 99991668, 99991677}:
        return ('FEISHU_CLOUD_AUTH_INVALID', 'credential_invalid',
            '飞书认证凭据无效或已过期，请检查宿主应用认证；无需向助手发送密钥。')
    if provider_code in {91403, 91002, 91004}:
        return ('FEISHU_CLOUD_RESOURCE_FORBIDDEN', 'resource_permission_required',
            '飞书拒绝资源访问；请检查应用协作者及本人或当前群的文档权限。')
    if provider_code in {1069604, 91005, 90304, 90305}:
        return ('FEISHU_CLOUD_RESOURCE_NOT_FOUND', 'resource_not_found',
            '飞书资源不存在或已删除，请核对文档编号或知识库链接。')
    if provider_code == 99991403:
        return ('FEISHU_CLOUD_QUOTA_EXCEEDED', 'quota_exceeded',
            '飞书应用本月 API 额度已用完，请管理员检查开放平台额度。')
    return None


# LLM: 缺供应商编号时仅使用 HTTP 标准语义；unknown 由请求副作用和状态共同决定，不暗示已成功或可重放。
# 函数用途: 为认证、授权、限流、故障和拒绝重定向提供明确错误。
def _http_error_details(http_status: int) -> tuple[str, str, str]:
    values = {
        401: ('FEISHU_CLOUD_AUTH_INVALID', 'credential_invalid', '飞书认证凭据无效或已过期，请检查宿主应用认证；无需向助手发送密钥。'),
        403: ('FEISHU_CLOUD_RESOURCE_FORBIDDEN', 'resource_permission_required', '飞书拒绝资源访问；请检查应用协作者及本人或当前群的文档权限。'),
        404: ('FEISHU_CLOUD_RESOURCE_NOT_FOUND', 'resource_not_found', '飞书资源不存在或已删除，请核对文档编号或知识库链接。'),
        429: ('FEISHU_CLOUD_RATE_LIMITED', 'rate_limited', '飞书接口触发频率限制；写入结果可能未知，请先读回核对，勿重复追加。'),
    }
    if http_status in values:
        return values[http_status]
    if http_status >= 500:
        return ('FEISHU_CLOUD_SERVICE_UNAVAILABLE', 'provider_unavailable',
            '飞书服务暂不可用；写入结果可能未知，请先读回核对。')
    if 300 <= http_status < 400:
        return ('FEISHU_CLOUD_REDIRECT_REFUSED', 'redirect_refused',
            '飞书云接口返回重定向，宿主未转发认证信息，请核对官方接口。')
    return ('FEISHU_CLOUD_HTTP_ERROR' if http_status else 'FEISHU_CLOUD_API_DENIED',
            'provider_rejected', '飞书云接口拒绝请求，请按结构化 HTTP 状态和 API 错误编号核查。')
