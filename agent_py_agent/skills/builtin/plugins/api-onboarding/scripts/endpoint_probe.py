"""只读接口探路，结构化输出且不打印认证头。"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不追随重定向，防止向其它目标携带认证头或隐式改变请求。"""

    # LLM: 探测只观察原地址的 HTTP 状态，不自动访问 Location。
    # 函数用途: 拒绝 urllib 的重定向请求。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# LLM: 只访问显式 HTTP(S) 地址，拒绝写方法、内嵌凭据与重定向；环境中的认证值绝不进入结果或错误。
# 函数用途: 探测一个已授权的 HTTP 端点。
def probe(url, *, method='HEAD', timeout=10.0, auth_header_env='', header_name='Authorization'):
    started = time.perf_counter()
    result = {'url': '', 'method': method, 'http_status': None, 'latency_ms': 0,
        'content_type': '', 'error_code': ''}
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('invalid URL')
        _ = parsed.port
        result['url'] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    except (ValueError, TypeError):
        result['error_code'] = 'INVALID_URL'
        return result
    if method not in {'GET', 'HEAD'}:
        result['error_code'] = 'METHOD_REFUSED'
        return result
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        result['error_code'] = 'INVALID_TIMEOUT'
        return result
    headers = {'User-Agent': 'my-agent-endpoint-probe'}
    if auth_header_env:
        value = os.environ.get(auth_header_env)
        if not value:
            result['error_code'] = 'AUTH_HEADER_UNAVAILABLE'
            return result
        if not re.fullmatch(r'[A-Za-z0-9-]+', header_name) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            result['error_code'] = 'AUTH_HEADER_INVALID'
            return result
        headers[header_name] = value
    try:
        request = urllib.request.Request(url, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            result['http_status'] = response.status
            result['content_type'] = response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        result['http_status'] = exc.code
        result['content_type'] = exc.headers.get_content_type() if exc.headers else ''
        result['error_code'] = 'HTTP_REDIRECT' if 300 <= exc.code < 400 else 'HTTP_ERROR'
        exc.close()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        result['error_code'] = ('TLS_CERTIFICATE_VERIFY_FAILED' if isinstance(reason, ssl.SSLCertVerificationError)
            else 'TIMEOUT' if isinstance(reason, TimeoutError)
            else 'DNS_ERROR' if isinstance(reason, socket.gaierror) else 'CONNECTION_FAILED')
    result['latency_ms'] = round((time.perf_counter() - started) * 1000, 3)
    return result


# LLM: 入口只负责解析参数和输出 JSON。
# 函数用途: 执行只读探测命令。
def main(argv=None):
    parser = argparse.ArgumentParser(description='只读端点探路；认证值仅从环境读取。')
    parser.add_argument('url')
    parser.add_argument('--method', default='HEAD')
    parser.add_argument('--timeout', type=float, default=10.0)
    parser.add_argument('--auth-header-env', default='')
    parser.add_argument('--header-name', default='Authorization')
    args = parser.parse_args(argv)
    result = probe(args.url, method=args.method, timeout=args.timeout,
        auth_header_env=args.auth_header_env, header_name=args.header_name)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if not result['error_code'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
