"""飞书/Lark 应用「扫码自助建应用」设备码 OAuth 流程。

借鉴 通道运行时 extensions/feishu/src/app-registration.ts:用户拿飞书/Lark App 扫码,
飞书侧自动创建一个应用并回传 app_id(client_id)/app_secret(client_secret),
彻底免去「去开放平台手工建应用、复制密钥」。

纯 stdlib(urllib)实现,无新增硬依赖;二维码渲染优先用可选的 qrcode 库,
没装则回退打印 verification_uri_complete 链接(用户可手机打开/粘进二维码工具)。

三步:init(查环境支持 client_secret)→ begin(拿 device_code + 二维码)→ poll(轮询拿密钥)。
poll 会按 tenant_brand 自动在 feishu/lark 域名间切换。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Literal

FeishuDomain = Literal["feishu", "lark"]

_ACCOUNTS_URL = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
_REGISTRATION_PATH = "/oauth/v1/app/registration"

_REQUEST_TIMEOUT_S = 12
_DEFAULT_POLL_INTERVAL_S = 5
_DEFAULT_EXPIRE_S = 600


class FeishuRegistrationError(Exception):
    """建应用流程不可恢复的错误(环境不支持/被拒/过期/异常)。"""


@dataclass
class AppRegistrationResult:
    app_id: str
    app_secret: str
    domain: FeishuDomain
    open_id: str | None = None


@dataclass
class BeginResult:
    device_code: str
    qr_url: str
    user_code: str
    interval: int
    expire_in: int


def _accounts_base_url(domain: FeishuDomain) -> str:
    return _ACCOUNTS_URL.get(domain, _ACCOUNTS_URL["feishu"])


def _post_registration(domain: FeishuDomain, body: dict[str, str]) -> dict:
    """POST 表单到注册端点,返回解析后的 JSON(注册 poll 在 pending/error 时也返回 JSON 体)。"""
    url = _accounts_base_url(domain) + _REGISTRATION_PATH
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 4xx 也带 JSON 体(pending/error 状态),读出来交给上层判定。
        raw = exc.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def init_app_registration(domain: FeishuDomain = "feishu") -> None:
    """第 1 步:初始化并校验环境支持 client_secret 鉴权。不支持则抛错。"""
    res = _post_registration(domain, {"action": "init"})
    methods = res.get("supported_auth_methods") or []
    if "client_secret" not in methods:
        raise FeishuRegistrationError(
            f"当前环境不支持 client_secret 鉴权(supported={methods})"
        )


def begin_app_registration(domain: FeishuDomain = "feishu") -> BeginResult:
    """第 2 步:启动设备码流程,拿到 device_code 和给用户扫的二维码 URL。"""
    res = _post_registration(
        domain,
        {
            "action": "begin",
            "archetype": "PersonalAgent",
            "auth_method": "client_secret",
            "request_user_info": "open_id",
        },
    )
    complete = res.get("verification_uri_complete")
    device_code = res.get("device_code")
    if not complete or not device_code:
        raise FeishuRegistrationError(f"begin 响应缺字段:{list(res.keys())}")

    # 给二维码 URL 加来源标记(对齐 通道运行时 的 ob_cli_app 流)。
    parsed = urllib.parse.urlparse(complete)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    query.update({"from": "my_agent_onboard", "tp": "ob_cli_app"})
    qr_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))

    # 实测字段是 expires_in;通道运行时 接口写的 expire_in 不准,两个都兜。
    expire_in = res.get("expires_in") or res.get("expire_in") or _DEFAULT_EXPIRE_S
    interval = res.get("interval") or _DEFAULT_POLL_INTERVAL_S
    return BeginResult(
        device_code=device_code,
        qr_url=qr_url,
        user_code=res.get("user_code", ""),
        interval=int(interval),
        expire_in=int(expire_in),
    )


def poll_app_registration(
    begin: BeginResult,
    *,
    initial_domain: FeishuDomain = "feishu",
    should_abort: Callable[[], bool] | None = None,
) -> AppRegistrationResult:
    """第 3 步:轮询直到成功/被拒/过期/超时;按 tenant_brand 自动切 feishu/lark 域名。"""
    interval = begin.interval
    domain: FeishuDomain = initial_domain
    domain_switched = False
    deadline = time.monotonic() + max(begin.expire_in, _DEFAULT_POLL_INTERVAL_S)

    while time.monotonic() < deadline:
        if should_abort and should_abort():
            raise FeishuRegistrationError("用户取消")

        body = {"action": "poll", "device_code": begin.device_code, "tp": "ob_cli_app"}
        try:
            res = _post_registration(domain, body)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            time.sleep(interval)  # 瞬时网络错误,继续轮询
            continue

        user_info = res.get("user_info") or {}
        # 域名自动判定:tenant_brand=lark 则切到 lark 域名重试。
        if not domain_switched and user_info.get("tenant_brand") == "lark":
            domain = "lark"
            domain_switched = True
            continue

        if res.get("client_id") and res.get("client_secret"):
            return AppRegistrationResult(
                app_id=res["client_id"],
                app_secret=res["client_secret"],
                domain=domain,
                open_id=user_info.get("open_id"),
            )

        error = res.get("error")
        if error == "authorization_pending":
            pass  # 还没扫/没批,继续等
        elif error == "slow_down":
            interval += 5
        elif error == "access_denied":
            raise FeishuRegistrationError("用户拒绝了授权(access_denied)")
        elif error == "expired_token":
            raise FeishuRegistrationError("二维码已过期(expired_token),请重来")
        elif error:
            raise FeishuRegistrationError(f"{error}: {res.get('error_description', '未知')}")

        time.sleep(interval)

    raise FeishuRegistrationError("等待扫码超时")


def render_qr_terminal(url: str) -> str:
    """把 URL 渲染成终端可扫的二维码;没装 qrcode 库则返回空串(上层回退打印链接)。"""
    try:
        import qrcode  # 可选依赖
    except ImportError:
        return ""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    import io

    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def register_feishu_app_by_scan(
    *,
    domain: FeishuDomain = "feishu",
    on_qr: Callable[[BeginResult, str], None],
    should_abort: Callable[[], bool] | None = None,
) -> AppRegistrationResult:
    """完整扫码建应用:init→begin→(回调展示二维码)→poll。返回 app_id/app_secret/domain/open_id。

    on_qr(begin, qr_ascii): 拿到二维码后回调,由调用方负责展示(渲染/打印链接)。
    """
    init_app_registration(domain)
    begin = begin_app_registration(domain)
    qr_ascii = render_qr_terminal(begin.qr_url)
    on_qr(begin, qr_ascii)
    return poll_app_registration(begin, initial_domain=domain, should_abort=should_abort)
