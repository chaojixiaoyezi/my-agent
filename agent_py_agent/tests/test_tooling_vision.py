"""视觉理解工具 analyze_image（短板6 视觉理解）—— 工具边界测试。

钉死契约（覆盖整条 AnalyzeImageTool.execute 链路 + media-type 探测 + 配置 + 注册）：
1. media type 靠 magic bytes 探测：PNG/JPEG/WebP/GIF 命中，非图片/截断头返回 ""（被拒）。
2. 本地图片 → 读字节 + base64 + anthropic image block，走（mock 的）视觉模型返回分析。
3. URL 图片取数前走 SSRF gate：解析到私网/内网被拒，绝不下载。
4. 非 http(s) scheme（file://、data:、ftp://）被拒为 TOOL_INVALID_ARGUMENTS，绝不当本地文件读。
5. 未配视觉模型 → TOOL_UNAVAILABLE（带配置指引），不崩、不影响其它工具。
6. 图片不存在 → PATH_NOT_FOUND；格式不支持 → TOOL_INVALID_ARGUMENTS；超大 → ARTIFACT_TOO_LARGE。
7. anthropic image block 构造正确：{"type":"image","source":{"type":"base64","media_type":..,"data":..}}。
8. 精确 parameter_schema + required_parameters；analyze_image 注册进 registry 且在模型可见目录。
9. 视觉模型调用失败（HTTP 错/空响应/坏 JSON）→ MODEL_UPSTREAM_FAILED，不崩。

标杆：长期助手 tools/vision_tools.py（async + auxiliary 视觉路由）；这里适配 my-agent 同步工具，
用 urllib 同步实现，URL 取图复用 my-agent network_safety gate。
用一个极小的真实 1x1 PNG（70 字节）做 base64/media-type 测试。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.tooling.vision_tools import (
    AnalyzeImageTool,
    VisionModelConfig,
    _detect_media_type,
    _looks_like_uri_scheme,
    _vision_request_payload,
    vision_config_from_agent_config,
)

pytestmark = pytest.mark.integration

# 极小真实 1x1 红点 PNG（70 字节）——足够覆盖 magic-byte 探测 + base64 编码 + 端到端。
_PNG_1x1_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
_GIF_HEADER = b"GIF89a\x01\x00\x01\x00"
_WEBP_HEADER = b"RIFF\x24\x00\x00\x00WEBPVP8 "


def _configured() -> VisionModelConfig:
    return VisionModelConfig(
        api_base="https://vision.example.com/anthropic",
        api_key="vkey",
        model_name="vis-1",
        max_tokens=512,
    )


def _resolver_to(*ips: str):
    def resolver(_host: str) -> tuple[str, ...]:
        return tuple(ips)

    return resolver


def _vision_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps({"content": [{"type": "text", "text": text}]}).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# --- 1. media type magic-byte 探测 -----------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    [
        (_PNG_1x1_BYTES, "image/png"),
        (_JPEG_HEADER, "image/jpeg"),
        (_GIF_HEADER, "image/gif"),
        (_WEBP_HEADER, "image/webp"),
        (b"<html>not an image</html>", ""),
        (b"PK\x03\x04zipfile", ""),
        (b"", ""),
        (b"\x89PNG", ""),  # 截断的 PNG 头（不足 8 字节签名）
    ],
)
def test_detect_media_type(data: bytes, expected: str) -> None:
    assert _detect_media_type(data) == expected


# --- 2. 本地图片 → base64 → mock 视觉模型 ----------------------------------


def test_local_png_base64_and_vision_call(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_1x1_BYTES)
    tool = AnalyzeImageTool(_configured())
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _vision_response("A tiny red pixel.")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = tool.execute({"image": str(img), "question": "what color?"})

    assert result.ok is True
    payload = json.loads(result.output)
    assert payload["analysis"] == "A tiny red pixel."
    assert payload["media_type"] == "image/png"
    assert payload["size_bytes"] == len(_PNG_1x1_BYTES)
    # 端点 + 请求体里 image block 携带的 base64 等于源文件 base64。
    assert str(captured["url"]).endswith("/v1/messages")
    sent_block = captured["body"]["messages"][0]["content"][0]
    assert sent_block["source"]["data"] == base64.b64encode(_PNG_1x1_BYTES).decode("ascii")


# --- 3. URL 图片走 SSRF gate ------------------------------------------------


def test_url_private_ip_blocked_before_download() -> None:
    tool = AnalyzeImageTool(_configured(), resolver=_resolver_to("10.0.0.7"))
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = tool.execute({"image": "http://internal.example.test/secret.png"})
    assert result.ok is False
    assert result.error_code in {"NETWORK_PRIVATE_IP_BLOCKED", "NETWORK_ALWAYS_BLOCKED_IP"}
    mock_urlopen.assert_not_called()


def test_url_cloud_metadata_blocked() -> None:
    tool = AnalyzeImageTool(_configured(), resolver=_resolver_to("169.254.169.254"))
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = tool.execute({"image": "http://metadata.example.test/latest/img.png"})
    assert result.ok is False
    assert result.error_code in {"NETWORK_ALWAYS_BLOCKED_IP", "NETWORK_PRIVATE_IP_BLOCKED"}
    mock_urlopen.assert_not_called()


def _download_opener(body: bytes):
    """造一个假 opener:其 .open 返回带 body 的响应(下载路径走 build_opener().open,非 urlopen)。"""

    def build(*handlers):
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        opener = MagicMock()
        opener.open.return_value = resp
        return opener

    return build


def test_public_url_downloads_and_analyzes() -> None:
    tool = AnalyzeImageTool(_configured(), resolver=_resolver_to("93.184.216.34"))
    # 下载走带 SSRF 守卫的 opener(build_opener),视觉模型调用走 urlopen —— 分别 mock
    with patch("urllib.request.build_opener", side_effect=_download_opener(_PNG_1x1_BYTES)):
        with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: _vision_response("Public image described.")):
            result = tool.execute({"image": "https://example.com/pic.png", "question": "?"})
    assert result.ok is True
    assert json.loads(result.output)["analysis"] == "Public image described."


# --- 4. 非 http(s) scheme 被拒 ---------------------------------------------


@pytest.mark.parametrize(
    "source", ["file:///etc/passwd", "data:image/png;base64,AAAA", "ftp://internal/x.png", "gopher://x/"]
)
def test_non_http_scheme_rejected(source: str) -> None:
    tool = AnalyzeImageTool(_configured())
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = tool.execute({"image": source})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    mock_urlopen.assert_not_called()


@pytest.mark.parametrize(
    "source,is_scheme",
    [
        ("file:///x", True),
        ("data:image/png;base64,x", True),
        ("/abs/path.png", False),
        ("rel/path.png", False),
        ("C:\\Users\\x.png", False),  # Windows 盘符不能被误判成 scheme
        ("./x.png", False),
    ],
)
def test_looks_like_uri_scheme(source: str, is_scheme: bool) -> None:
    assert _looks_like_uri_scheme(source) is is_scheme


# --- 5. 未配视觉模型 → TOOL_UNAVAILABLE ------------------------------------


def test_unconfigured_returns_tool_unavailable() -> None:
    tool = AnalyzeImageTool(VisionModelConfig())  # 全空 = 未配
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = tool.execute({"image": "/any/path.png"})
    assert result.ok is False
    assert result.error_code == "TOOL_UNAVAILABLE"
    mock_urlopen.assert_not_called()
    # 错误体带配置指引，便于模型/用户排障。
    assert "vision_api_base" in result.output


def test_partial_config_not_configured() -> None:
    # 只有 base、缺 model_name → 仍算未配。
    tool = AnalyzeImageTool(VisionModelConfig(api_base="https://v/anthropic"))
    result = tool.execute({"image": "/x.png"})
    assert result.error_code == "TOOL_UNAVAILABLE"


# --- 6. 优雅报错：不存在 / 格式错 / 超大 / 缺参 ----------------------------


def test_missing_image_arg() -> None:
    tool = AnalyzeImageTool(_configured())
    result = tool.execute({})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_local_file_not_found() -> None:
    tool = AnalyzeImageTool(_configured())
    result = tool.execute({"image": "/nonexistent/zzz_no_such_image.png"})
    assert result.ok is False
    assert result.error_code == "PATH_NOT_FOUND"


def test_local_file_not_an_image(tmp_path: Path) -> None:
    bad = tmp_path / "notimg.png"  # 扩展名 .png 但内容是文本
    bad.write_text("this is not an image", encoding="utf-8")
    tool = AnalyzeImageTool(_configured())
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = tool.execute({"image": str(bad)})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    mock_urlopen.assert_not_called()  # 格式不对时不调用视觉模型


def test_local_image_too_large(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling import vision_tools as vt

    big = tmp_path / "big.png"
    big.write_bytes(_PNG_1x1_BYTES)
    tool = AnalyzeImageTool(_configured())
    # 把上限临时调到极小，让这张 70 字节图触发"超大"分支（不必真写大文件）。
    with patch.object(vt, "_MAX_IMAGE_BYTES", 10):
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = tool.execute({"image": str(big)})
    assert result.ok is False
    assert result.error_code == "ARTIFACT_TOO_LARGE"
    mock_urlopen.assert_not_called()


def test_url_download_too_large_rejected() -> None:
    from agent_py_agent.agent.tooling import vision_tools as vt

    tool = AnalyzeImageTool(_configured(), resolver=_resolver_to("93.184.216.34"))
    # 返回超过上限的字节（读 max+1 暴露超限）；下载走 build_opener().open。
    oversized = b"\x89PNG\r\n\x1a\n" + b"X" * 50
    with patch.object(vt, "_MAX_IMAGE_BYTES", 16):
        with patch("urllib.request.build_opener", side_effect=_download_opener(oversized)):
            result = tool.execute({"image": "https://example.com/big.png"})
    assert result.ok is False
    assert result.error_code == "ARTIFACT_TOO_LARGE"


# --- 7. anthropic image block 构造 -----------------------------------------


def test_vision_request_payload_image_block() -> None:
    cfg = _configured()
    payload = _vision_request_payload(cfg, "image/png", "BASE64DATA", "describe this")
    assert payload["model"] == "vis-1"
    assert payload["max_tokens"] == 512
    content = payload["messages"][0]["content"]
    image_block, text_block = content[0], content[1]
    assert image_block == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "BASE64DATA"},
    }
    assert text_block == {"type": "text", "text": "describe this"}


def test_no_question_uses_default_prompt(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(_PNG_1x1_BYTES)
    tool = AnalyzeImageTool(_configured())
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _vision_response("ok")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        tool.execute({"image": str(img)})  # 不传 question
    text_block = captured["body"]["messages"][0]["content"][1]
    assert text_block["text"]  # 有默认描述 prompt，非空


# --- 8. 精确 schema + 注册 -------------------------------------------------


def test_precise_schema_and_required() -> None:
    spec = AnalyzeImageTool(VisionModelConfig()).spec
    assert spec.name == "analyze_image"
    assert spec.category == "vision"
    assert spec.effect == "read_only"
    assert spec.parameter_schema == {
        "image": {"type": "string"},
        "question": {"type": "string"},
    }
    assert spec.required_parameters == ["image"]


def test_registered_in_registry(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    params = ToolRegistryParams(
        workspace_root=tmp_path,
        max_chars=1000,
        max_entries=10,
        max_matches=10,
        web_max_chars=1000,
        http_timeout=10,
        catalog_limit=50,
        retrieval_limit=3,
        vector_search_enabled=False,
        vision_config=_configured(),
    )
    registry = ToolRegistry(params)
    assert "analyze_image" in registry.tools
    # 在模型可见的默认目录里（不是隐藏工具）。
    assert "analyze_image" in {spec.name for spec in registry.specs()}


def test_registered_even_without_vision_config(tmp_path: Path) -> None:
    # vision_config=None（默认）也要注册，只是调用返回 TOOL_UNAVAILABLE。
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    params = ToolRegistryParams(
        workspace_root=tmp_path,
        max_chars=1000,
        max_entries=10,
        max_matches=10,
        web_max_chars=1000,
        http_timeout=10,
        catalog_limit=50,
        retrieval_limit=3,
        vector_search_enabled=False,
    )
    registry = ToolRegistry(params)
    assert "analyze_image" in registry.tools
    result = registry.tools["analyze_image"].execute({"image": "/x.png"})
    assert result.error_code == "TOOL_UNAVAILABLE"


# --- 9. 视觉模型调用失败 → MODEL_UPSTREAM_FAILED ---------------------------


def test_vision_model_http_error(tmp_path: Path) -> None:
    import urllib.error

    img = tmp_path / "x.png"
    img.write_bytes(_PNG_1x1_BYTES)
    tool = AnalyzeImageTool(_configured())

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = tool.execute({"image": str(img)})
    assert result.ok is False
    assert result.error_code == "MODEL_UPSTREAM_FAILED"


def test_vision_model_empty_content(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(_PNG_1x1_BYTES)
    tool = AnalyzeImageTool(_configured())

    def fake_urlopen(req, timeout=None):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"content": []}).encode("utf-8")
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = tool.execute({"image": str(img)})
    assert result.ok is False
    assert result.error_code == "MODEL_UPSTREAM_FAILED"


def test_vision_model_bad_json(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(_PNG_1x1_BYTES)
    tool = AnalyzeImageTool(_configured())

    def fake_urlopen(req, timeout=None):
        resp = MagicMock()
        resp.read.return_value = b"<html>gateway error</html>"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = tool.execute({"image": str(img)})
    assert result.ok is False
    assert result.error_code == "MODEL_UPSTREAM_FAILED"


def test_vision_model_timeout(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(_PNG_1x1_BYTES)
    tool = AnalyzeImageTool(_configured())

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("slow")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = tool.execute({"image": str(img)})
    assert result.ok is False
    assert result.error_code == "TOOL_TIMEOUT"


# --- 10. config 读取 -------------------------------------------------------


def test_vision_config_from_agent_config_key_fallback() -> None:
    class Cfg:
        vision_api_base = "https://v/anthropic"
        vision_api_key = ""  # 空 → 回退主 key
        vision_model_name = "m"
        api_key = "MAIN_KEY"
        anthropic_version = "2023-06-01"

    vc = vision_config_from_agent_config(Cfg())
    assert vc.configured is True
    assert vc.api_key == "MAIN_KEY"
    assert vc.anthropic_version == "2023-06-01"


def test_vision_config_empty_not_configured() -> None:
    class Cfg:
        pass

    vc = vision_config_from_agent_config(Cfg())
    assert vc.configured is False


# --- 11. 重定向 SSRF 防护(此前缺口:默认 opener 盲跟随重定向到内网/云 metadata)-------------

import email.message  # noqa: E402
import urllib.request  # noqa: E402

from agent_py_agent.agent.tooling.models import ToolExecutionResult  # noqa: E402
from agent_py_agent.agent.tooling.vision_tools import (  # noqa: E402
    _RedirectBlocked,
    _SSRFGuardingRedirectHandler,
)


def _redirect(handler, newurl: str):
    req = urllib.request.Request("http://public.example/start")
    return handler.redirect_request(req, MagicMock(), 302, "Found", email.message.Message(), newurl)


def test_redirect_to_cloud_metadata_is_blocked() -> None:
    """首跳公网、302 跳转到云 metadata IP → 被网关拦,绝不跟随(堵重定向 SSRF 凭据外泄)。"""
    handler = _SSRFGuardingRedirectHandler("analyze_image", _resolver_to("169.254.169.254"))
    with pytest.raises(_RedirectBlocked) as exc:
        _redirect(handler, "http://metadata.internal.test/latest/meta-data/iam/creds")
    assert exc.value.blocked.error_code in {"NETWORK_ALWAYS_BLOCKED_IP", "NETWORK_PRIVATE_IP_BLOCKED"}


def test_redirect_to_private_ip_is_blocked() -> None:
    handler = _SSRFGuardingRedirectHandler("analyze_image", _resolver_to("10.0.0.5"))
    with pytest.raises(_RedirectBlocked):
        _redirect(handler, "http://intranet.test/secret.png")


def test_redirect_to_non_http_scheme_is_blocked() -> None:
    handler = _SSRFGuardingRedirectHandler("analyze_image", _resolver_to("93.184.216.34"))
    with pytest.raises(_RedirectBlocked):
        _redirect(handler, "ftp://host.test/x.png")  # 跳转换协议也拒


def test_redirect_to_public_ip_is_allowed() -> None:
    handler = _SSRFGuardingRedirectHandler("analyze_image", _resolver_to("93.184.216.34"))
    new_req = _redirect(handler, "http://cdn.public.test/img.png")
    assert isinstance(new_req, urllib.request.Request)  # 公网跳转放行(不过度拦)


def test_download_builds_ssrf_guarding_opener() -> None:
    """_download 真的用带 SSRF 守卫重定向 handler 的 opener(接线正确,不是默认盲跟随)。"""
    tool = AnalyzeImageTool(_configured(), resolver=_resolver_to("93.184.216.34"))
    captured: dict = {}

    def spy_build_opener(*handlers):
        captured["handlers"] = handlers
        opener = MagicMock()
        resp = MagicMock()
        resp.read.return_value = _PNG_1x1_BYTES
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        opener.open.return_value = resp
        return opener

    with patch("urllib.request.build_opener", side_effect=spy_build_opener):
        body = tool._download("https://example.com/img.png")
    assert isinstance(body, bytes)
    assert any(isinstance(h, _SSRFGuardingRedirectHandler) for h in captured["handlers"])


def test_download_surfaces_redirect_ssrf_rejection() -> None:
    """重定向被网关拦时,_download 把网关的具体拒绝码原样透出(不吞成泛化错误)。"""
    tool = AnalyzeImageTool(_configured(), resolver=_resolver_to("93.184.216.34"))
    blocked = ToolExecutionResult("analyze_image", False, "blocked", error_code="NETWORK_PRIVATE_IP_BLOCKED")

    def spy_build_opener(*handlers):
        opener = MagicMock()
        opener.open.side_effect = _RedirectBlocked(blocked)
        return opener

    with patch("urllib.request.build_opener", side_effect=spy_build_opener):
        result = tool._download("https://example.com/img.png")
    assert result is blocked
