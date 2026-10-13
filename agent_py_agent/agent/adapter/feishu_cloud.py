# LLM: 云资源请求复用原 FeishuAdapter 的宿主 token，目标固定为官方 API；不返回密钥、不重试未知写入、不接受模型 URL。
# 模块用途: 调用飞书云文档接口，阻止带认证头的重定向并返回净化后的数据或稳定错误。
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..common.log_redaction import redact_sensitive_value

API_BASE = 'https://open.feishu.cn/open-apis'
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


# LLM: code 是宿主稳定错误码；message 只用本地文案，不包含供应商正文或网络异常中的认证数据。
# 类用途: 区分缺凭据、供应商拒绝和未知写入结果。
class FeishuCloudError(RuntimeError):
    # LLM: unknown 仅表示请求可能到达供应商，原操作账不能自动重放写入。
    # 函数用途: 保存可读拒绝原因及副作用状态。
    def __init__(self, code: str, message: str, *, unknown: bool = False):
        super().__init__(message)
        self.code = code
        self.unknown = unknown


# LLM: urllib 默认会跟随重定向；携带宿主 token 的云调用必须拒绝跳转，不能把租户权限送到另一个 origin。
# 类用途: 为固定官方接口关闭 HTTP 重定向。
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # LLM: 返回 None 令原 urllib 抛 HTTPError，不发起第二跳。
    # 函数用途: 拒绝供应商跳转请求。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
# 函数用途: 避免非秘密的 obj_token/page_token 被字符串脱敏误删，保持 wiki 定位和文档分页可用。
def _public_identifiers(value):
    names = {'obj_token': 'resource_id', 'obj_type': 'resource_type', 'page_token': 'next_cursor', 'spreadsheet_token': 'resource_id'}
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


# LLM: 只接收工具自身生成的准确 API 路径；宿主 token 只在 Authorization 内，所有异常采用稳定本地错误且不自动重试。
# 函数用途: 用现有飞书应用凭据请求一次云接口，读取有界 JSON 并清理秘密。
def request_cloud_api(adapter, method: str, path: str, body: dict | None = None) -> dict:
    if not path.startswith('/') or path.startswith('//') or '..' in path.split('?')[0].split('/'):
        raise ValueError('云接口路径无效')
    token = adapter._get_tenant_access_token()
    if not token:
        raise FeishuCloudError('FEISHU_CREDENTIAL_UNAVAILABLE', '宿主飞书应用凭据不可用，请管理员检查应用配置。')
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
    request = urllib.request.Request(API_BASE + path, data=data, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json; charset=utf-8'})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError('response too large')
        result = json.loads(raw)
        if not isinstance(result, dict) or type(result.get('code')) is not int:
            raise ValueError('invalid response')
    except urllib.error.HTTPError as exc:
        # 429/5xx 不能据此推断写操作未发生，保留 unknown，不复制供应商正文。
        unknown = method != 'GET' and (exc.code >= 500 or exc.code == 429)
        raise FeishuCloudError('FEISHU_CLOUD_HTTP_ERROR', '飞书云接口拒绝请求；请检查应用权限、文档协作者与接口额度。', unknown=unknown) from None
    except (OSError, ValueError, TypeError):
        raise FeishuCloudError('FEISHU_CLOUD_RESPONSE_UNKNOWN', '飞书云接口未返回可验证结果；写操作请先读回核对，不能直接重复追加。', unknown=method != 'GET') from None
    if result['code'] != 0:
        raise FeishuCloudError('FEISHU_CLOUD_API_DENIED', f'飞书云接口拒绝请求（code={result["code"]}）；请管理员检查应用权限和文档协作者。')
    secrets = (token, str(getattr(adapter, 'app_secret', '') or ''))
    try:
        return _clean_response(redact_sensitive_value(_public_identifiers(result.get('data') or {})), secrets)
    except (ValueError, TypeError):
        raise FeishuCloudError('FEISHU_CLOUD_RESPONSE_UNKNOWN', '云接口响应无法投影；写操作请先读回核对，不能直接重复追加。', unknown=method != 'GET') from None
