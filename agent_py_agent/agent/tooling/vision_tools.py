
from __future__ import annotations

"""视觉理解工具 —— analyze_image(看图:分析图片内容)。

给模型一个精确 schema 的看图工具,补 my-agent 只能处理文本的短板(短板6 最后一个子项)。
输入图片(本地路径 或 http/https URL)+ 可选问题,返回图片内容描述/分析。

对标 长期助手 tools/vision_tools.py 的两条路径,适配 my-agent:
  - 长期助手 走 async httpx + 中心化 auxiliary 视觉路由(OpenRouter/Nous/Anthropic...);
    my-agent 工具是同步(execute(params)->ToolExecutionResult),所以这里用 urllib 同步实现,
    和 web_fetch_runtime/gateway_helpers 同一套 HTTP 风格。
  - 长期助手 判 supports_vision 决定"直接附图给主模型"还是"走辅助视觉模型";my-agent 主模型
    (minimax)不一定支持视觉,所以**主路径是辅助视觉模型**:config 配一个独立的 anthropic_compatible
    视觉端点(endpoint/key/model_name),把图片转成 anthropic image block 调它返回分析。
  - 长期助手 的 _detect_image_mime_type(magic bytes)/_image_to_base64_data_url/大小上限/SSRF 重定向
    防护,这里照搬其精神:magic-byte 探测 media type、base64、大小硬顶;URL 取图复用 my-agent
    现有 network_safety gate(和 web_fetch/browser 完全同一把锁),默认拒私网/loopback/云 metadata。

可选加法,零默认影响:没配视觉模型(vision_api_base/vision_model_name 为空)时,工具的
check 返回不可用 → analyze_image 给出 TOOL_UNAVAILABLE 带配置指引,绝不崩、不影响任何现有工具。

异常兜底(都转结构化 error_code,不让异常冒泡崩主流程):
  图片源缺失/非法            → TOOL_INVALID_ARGUMENTS
  本地文件不存在            → PATH_NOT_FOUND
  URL 命中 SSRF/私网         → 复用 gate 的 NETWORK_* 码
  URL 下载失败/超时          → NETWORK_REQUEST_FAILED / TOOL_TIMEOUT
  不是真实图片/格式不支持     → TOOL_INVALID_ARGUMENTS
  图片过大(超上限)          → ARTIFACT_TOO_LARGE
  视觉模型未配               → TOOL_UNAVAILABLE
  视觉模型调用/解析失败       → MODEL_UPSTREAM_FAILED
"""

import base64
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import BaseTool, ToolAvailability, ToolExecutionResult, ToolSpec
from .web import _default_network_resolver, _network_safety_error, _normalize_url

# 下载/编码上限:防超大图或解压炸弹把内存撑爆(对标 长期助手 的 _VISION_MAX_DOWNLOAD_BYTES,
# my-agent 单机场景取更保守的 10 MB 原图上限)。base64 会膨胀约 4/3,仍远低于各家视觉 API 上限。
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_URL_CHARS = 4096
# 默认视觉调用超时(秒)。视觉模型常比纯文本慢,给宽一点;config 可覆盖。
_DEFAULT_VISION_TIMEOUT = 120

# magic-byte → anthropic media_type。只认真实图片二进制头(不靠扩展名),
# 对齐 长期助手 _detect_image_mime_type:防把任意文件/HTML 错当图片喂给视觉 API。
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_GIF_SIGS = (b"GIF87a", b"GIF89a")
_SUPPORTED_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def _looks_like_uri_scheme(source: str) -> bool:
    """判断 source 是否带一个 URI scheme 前缀(如 file:/ftp:/data:),用于挡非 http(s) 的 URL。

    只认 "<scheme>://" 或 "<scheme>:" 形式且 scheme 是字母数字+.-(避免把 Windows 盘符
    'C:\\path' 误判成 scheme:盘符是单字母,这里要求 scheme 至少 2 个字符)。
    """
    head = source.split(":", 1)[0]
    if len(head) < 2 or ":" not in source:
        return False
    return all(ch.isalnum() or ch in "+.-" for ch in head)


def _detect_media_type(data: bytes) -> str:
    """从图片二进制头探测 anthropic media_type;不是支持的图片格式返回 ""。

    只认 png/jpeg/webp/gif(anthropic image block 支持的四种)。靠 magic bytes 而非扩展名,
    防把伪装成图片的任意文件喂给视觉 API。
    """
    if data.startswith(_PNG_SIG):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(_GIF_SIGS):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


@dataclass(frozen=True)
class VisionModelConfig:
    """辅助视觉模型连接配置(从 AgentConfig 的 vision_* 字段抽出,经 registry 传入)。

    全为空 = 未配视觉模型 → analyze_image 返回 TOOL_UNAVAILABLE(可选加法,零默认影响)。
    api_base/model_name 必填才算"已配";api_key 优先用本字段,空则回退主模型 api_key
    (常见:视觉模型和主模型同一家、同一把 key)。
    """

    api_base: str = ""
    api_key: str = ""
    model_name: str = ""
    anthropic_version: str = "2023-06-01"
    timeout: int = _DEFAULT_VISION_TIMEOUT
    max_tokens: int = 1024

    @property
    def configured(self) -> bool:
        return bool(self.api_base.strip() and self.model_name.strip())


def vision_config_from_agent_config(config: Any) -> VisionModelConfig:
    """从 AgentConfig 读 vision_* 字段构造 VisionModelConfig;缺字段全部安全回退。

    api_key 空时回退主模型 api_key(视觉模型与主模型常同源同 key);
    anthropic_version 空时回退主模型 anthropic_version。读不到 config 任何字段都不抛。
    """
    def _get(name: str, default: Any) -> Any:
        return getattr(config, name, default)

    api_key = str(_get("vision_api_key", "") or "").strip()
    if not api_key:
        api_key = str(_get("api_key", "") or "").strip()
    version = str(_get("vision_anthropic_version", "") or "").strip()
    if not version:
        version = str(_get("anthropic_version", "2023-06-01") or "2023-06-01").strip()
    return VisionModelConfig(
        api_base=str(_get("vision_api_base", "") or "").strip(),
        api_key=api_key,
        model_name=str(_get("vision_model_name", "") or "").strip(),
        anthropic_version=version or "2023-06-01",
        timeout=_coerce_positive_int(_get("vision_request_timeout", _DEFAULT_VISION_TIMEOUT), _DEFAULT_VISION_TIMEOUT),
        max_tokens=_coerce_positive_int(_get("vision_max_tokens", 1024), 1024),
    )


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass(frozen=True)
class _LoadedImage:
    data: bytes
    media_type: str


class _RedirectBlocked(Exception):
    """重定向目标未过 SSRF 网关时抛出,携带网关原始拒绝结果供 _download 透出具体 error_code。"""

    def __init__(self, blocked: ToolExecutionResult) -> None:
        super().__init__("redirect blocked by SSRF gate")
        self.blocked = blocked


class _SSRFGuardingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跟随重定向前对每个 3xx 目标重新过 SSRF 网关 —— 堵"首跳公网、跳转到内网/云 metadata"的绕过。

    原 _download 用默认 opener 盲目跟随重定向:首 URL 过了网关,但 3xx 跳转目标不再校验,
    可被引到 169.254.169.254 等内网/云 metadata(凭据外泄)。这里对齐 web_fetch 的逐跳校验。
    """

    def __init__(self, tool_name: str, resolver: Any) -> None:
        self._tool_name = tool_name
        self._resolver = resolver

    def redirect_request(self, *args):  # type: ignore[override]
        # stdlib 固定签名 (req, fp, code, msg, headers, newurl);用 *args 透传,newurl 是末位
        newurl = args[5]
        blocked = self._gate_target(newurl)
        if blocked is not None:
            raise _RedirectBlocked(blocked)
        return super().redirect_request(*args)

    def _gate_target(self, newurl: str) -> ToolExecutionResult | None:
        """对重定向目标过网关:非 http(s) 直接拒;否则复用 _network_safety_error 逐跳校验。"""
        if not str(newurl).lower().startswith(("http://", "https://")):
            return ToolExecutionResult(
                self._tool_name, False, "重定向到非 http(s) 目标被拒", error_code="NETWORK_REQUEST_FAILED"
            )
        return _network_safety_error(self._tool_name, newurl, self._resolver)


# LLM: 视觉工具的 Schema 只在辅助模型配置完整时暴露，执行仍保留同一结构化错误兜底。
# 类用途: 安全读取本地/公网图片并交给配置好的辅助视觉模型分析。
class AnalyzeImageTool(BaseTool):
    """看图工具:本地路径/URL 图片 → base64 → 辅助视觉模型分析,返回内容描述。"""

    def __init__(self, vision_config: VisionModelConfig, *, resolver: Any = None):
        self.vision_config = vision_config
        self._resolver = resolver or _default_network_resolver
        self.spec = _analyze_image_spec()

    # LLM: 只检查静态连接配置，不探测网络或发送图片；真实调用前统一入口还会再次检查。
    # 函数用途: 未配置辅助视觉模型时让 analyze_image 从本轮工具面消失。
    def availability(self) -> ToolAvailability:
        if self.vision_config.configured:
            return ToolAvailability.ready()
        return ToolAvailability.unavailable(
            "辅助视觉模型尚未配置 vision_api_base 与 vision_model_name"
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        if not self.vision_config.configured:
            return self._unavailable()
        source = str(params.get("image", "") or "").strip()
        if not source:
            return self._invalid("analyze_image 需要 image 参数(本地文件路径 或 http/https URL)。")
        question = str(params.get("question", "") or "").strip()

        loaded = self._load_image(source)
        if isinstance(loaded, ToolExecutionResult):
            return loaded
        return self._analyze(loaded, question)

    # ---- 图片获取 + base64 + media type --------------------------------

    def _load_image(self, source: str) -> _LoadedImage | ToolExecutionResult:
        """解析图片源:http/https → 走 SSRF gate 下载;否则当本地文件读取。返回字节+media type。

        显式挡住其它 URL scheme(file://、ftp://、data: 等):它们既不是公网图片,也不应被
        当本地路径去读(避免 file:///etc/passwd 这类被静默当本地文件)。只放行 http/https URL
        和真实本地文件路径两类。
        """
        lowered = source.lower()
        if lowered.startswith(("http://", "https://")):
            return self._load_remote(source)
        if _looks_like_uri_scheme(source):
            return self._invalid(
                "不支持的图片来源 scheme。只接受 http/https 图片 URL 或本地文件路径。"
            )
        return self._load_local(source)

    def _load_local(self, source: str) -> _LoadedImage | ToolExecutionResult:
        path = Path(source).expanduser()
        if not path.is_file():
            return ToolExecutionResult(
                self.spec.name,
                False,
                f"本地图片不存在或不是文件: {source}",
                error_code="PATH_NOT_FOUND",
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            return ToolExecutionResult(
                self.spec.name,
                False,
                f"读取本地图片失败: {type(exc).__name__}",
                error_code="ARTIFACT_UNREADABLE",
            )
        return self._finalize_image(data)

    def _load_remote(self, source: str) -> _LoadedImage | ToolExecutionResult:
        try:
            url = _normalize_url(source)
        except ValueError as exc:
            return self._invalid(str(exc))
        # SSRF:复用 web_fetch/browser 同一把锁,默认拒私网/loopback/云 metadata。
        network_error = _network_safety_error(self.spec.name, url, self._resolver)
        if network_error is not None:
            return network_error
        body = self._download(url)
        if isinstance(body, ToolExecutionResult):
            return body
        return self._finalize_image(body)

    def _download(self, url: str) -> bytes | ToolExecutionResult:
        req = urllib.request.Request(url, headers={"User-Agent": "MyAgent-Vision/1.0", "Accept": "image/*,*/*;q=0.8"})
        # 带 SSRF 守卫的 opener:每个重定向目标都重新过网关,堵"首跳公网→跳转内网/云 metadata"的绕过
        opener = urllib.request.build_opener(_SSRFGuardingRedirectHandler(self.spec.name, self._resolver))
        try:
            with opener.open(req, timeout=self.vision_config.timeout) as resp:
                body = resp.read(_MAX_IMAGE_BYTES + 1)
        except _RedirectBlocked as exc:
            return exc.blocked  # 透出 SSRF 网关对该重定向目标的具体拒绝码
        except urllib.error.HTTPError as exc:
            return ToolExecutionResult(
                self.spec.name, False, f"下载图片失败: HTTP {exc.code}", error_code="NETWORK_REQUEST_FAILED"
            )
        except TimeoutError as exc:
            return ToolExecutionResult(
                self.spec.name, False, f"下载图片超时: {type(exc).__name__}", error_code="TOOL_TIMEOUT"
            )
        except (urllib.error.URLError, OSError) as exc:
            return ToolExecutionResult(
                self.spec.name, False, f"下载图片失败: {type(exc).__name__}", error_code="NETWORK_REQUEST_FAILED"
            )
        if len(body) > _MAX_IMAGE_BYTES:
            return self._too_large()
        return body

    def _finalize_image(self, data: bytes) -> _LoadedImage | ToolExecutionResult:
        if len(data) > _MAX_IMAGE_BYTES:
            return self._too_large()
        media_type = _detect_media_type(data)
        if not media_type:
            return self._invalid(
                "图片格式不支持或不是真实图片。仅支持 PNG/JPEG/WebP/GIF(按文件内容判定,与扩展名无关)。"
            )
        return _LoadedImage(data=data, media_type=media_type)

    # ---- 视觉模型调用(anthropic image block) --------------------------

    def _analyze(self, image: _LoadedImage, question: str) -> ToolExecutionResult:
        encoded = base64.b64encode(image.data).decode("ascii")
        prompt = question or "请详细描述这张图片的内容。"
        payload = _vision_request_payload(self.vision_config, image.media_type, encoded, prompt)
        analysis = self._call_vision_model(payload)
        if isinstance(analysis, ToolExecutionResult):
            return analysis
        result = {
            "ok": True,
            "media_type": image.media_type,
            "size_bytes": len(image.data),
            "question": question,
            "analysis": analysis,
        }
        return ToolExecutionResult(
            self.spec.name,
            True,
            json.dumps(result, ensure_ascii=False),
            result_envelope=result,
        )

    def _call_vision_model(self, payload: dict[str, Any]) -> str | ToolExecutionResult:
        url = self.vision_config.api_base.rstrip("/") + "/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.vision_config.api_key}",
            "anthropic-version": self.vision_config.anthropic_version,
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self.vision_config.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            return self._model_error(f"视觉模型返回 HTTP {exc.code}")
        except TimeoutError:
            return ToolExecutionResult(
                self.spec.name, False, "视觉模型请求超时", error_code="TOOL_TIMEOUT"
            )
        except (urllib.error.URLError, OSError) as exc:
            return self._model_error(f"无法连接视觉模型: {type(exc).__name__}")
        return self._parse_vision_response(raw)

    def _parse_vision_response(self, raw: bytes) -> str | ToolExecutionResult:
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            return self._model_error("视觉模型返回了无法解析的响应体")
        text = _anthropic_text(obj)
        if not text:
            return self._model_error("视觉模型返回空内容")
        return text

    # ---- 结构化错误工厂 ------------------------------------------------

    def _unavailable(self) -> ToolExecutionResult:
        payload = {
            "ok": False,
            "error": "vision_model_not_configured",
            "message": (
                "未配置视觉模型,analyze_image 不可用。请在配置文件填写 vision_api_base 与 "
                "vision_model_name(可选 vision_api_key,留空则复用主模型 api_key),指向一个 "
                "anthropic_compatible 的视觉模型端点。"
            ),
        }
        return ToolExecutionResult(
            self.spec.name, False, json.dumps(payload, ensure_ascii=False), error_code="TOOL_UNAVAILABLE"
        )

    def _invalid(self, message: str) -> ToolExecutionResult:
        payload = {"ok": False, "error": "invalid_arguments", "message": message}
        return ToolExecutionResult(
            self.spec.name, False, json.dumps(payload, ensure_ascii=False), error_code="TOOL_INVALID_ARGUMENTS"
        )

    def _too_large(self) -> ToolExecutionResult:
        payload = {
            "ok": False,
            "error": "image_too_large",
            "message": f"图片超过大小上限({_MAX_IMAGE_BYTES // (1024 * 1024)} MB);请压缩后重试。",
        }
        return ToolExecutionResult(
            self.spec.name, False, json.dumps(payload, ensure_ascii=False), error_code="ARTIFACT_TOO_LARGE"
        )

    def _model_error(self, message: str) -> ToolExecutionResult:
        payload = {"ok": False, "error": "vision_model_failed", "message": message}
        return ToolExecutionResult(
            self.spec.name, False, json.dumps(payload, ensure_ascii=False), error_code="MODEL_UPSTREAM_FAILED"
        )


def _vision_request_payload(
    config: VisionModelConfig, media_type: str, encoded: str, prompt: str
) -> dict[str, Any]:
    """构造 anthropic_compatible 的带图请求体(image block 用 base64 source)。"""
    return {
        "model": config.model_name,
        "max_tokens": config.max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": encoded},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }


def _anthropic_text(obj: dict[str, Any]) -> str:
    """从 anthropic messages 响应里抽出文本(拼所有 text block);兼容 completion 老字段。"""
    parts = obj.get("content", []) if isinstance(obj, dict) else []
    if isinstance(parts, list):
        text = "".join(
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
        if text:
            return text
    if isinstance(obj, dict) and isinstance(obj.get("completion"), str):
        return obj["completion"]
    return ""


def _analyze_image_spec() -> ToolSpec:
    return ToolSpec(
        name="analyze_image",
        category="vision",
        effect="read_only",
        output_trust="external_data",
        description=(
            "看图:分析一张图片的内容。输入本地图片路径或 http/https 图片 URL,加可选问题,"
            "返回图片内容的描述/分析。用于需要理解图片(截图、照片、图表、UI)时。"
        ),
        use_cases=[
            "用户给了一张图片(本地路径或 URL),需要知道图里有什么、读图中文字、看懂图表/界面",
            "网页/工具输出里出现图片 URL,需要理解其内容再决定下一步",
            "对一张图片提具体问题(这是什么牌子/有几个人/报错信息是什么)",
        ],
        avoid_when=[
            "目标是纯文本文件时用 read_file,不要当图片分析",
            "要抓网页正文/下载文件时用 web_fetch",
        ],
        keywords=[
            "看图", "图片", "图像", "视觉", "vision", "image", "看懂", "识图", "截图",
            "图表", "照片", "OCR", "图里", "analyze_image", "多模态",
        ],
        parameters={
            "image": "图片来源:本地文件路径,或 http/https 图片 URL。",
            "question": "可选。对图片的具体问题或分析要求;不传则返回整体内容描述。",
        },
        parameter_details={
            "image": (
                "本地绝对/相对路径,或 http/https URL。URL 取图前会走 SSRF 安全检查,默认拒绝"
                "私网/内网/云 metadata 地址。仅支持 PNG/JPEG/WebP/GIF(按文件内容判定)。"
            ),
            "question": "可选字符串。比如'图里有几个人''读出图中的报错文字''这是什么图表'。",
        },
        parameter_schema={
            "image": {"type": "string"},
            "question": {"type": "string"},
        },
        required_parameters=["image"],
        examples=[
            '{"tool": "analyze_image", "image": "/path/to/screenshot.png", "question": "图里的报错信息是什么?"}',
            '{"tool": "analyze_image", "image": "https://example.com/chart.png"}',
        ],
    )


__all__ = [
    "AnalyzeImageTool",
    "VisionModelConfig",
    "vision_config_from_agent_config",
]
