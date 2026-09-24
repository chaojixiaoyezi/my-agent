"""image-text 真实标准包、独立 Python 与原 MCP 客户端的组件验收；不代替 TUI、审批或模型验收。

测试图片由本文件的极简 PNG 编码器用 5x7 点阵字体现场生成（标准库 zlib/struct），不依赖 Pillow 或截图工具。
真实识别只在本机装有 tesseract 时运行；超时与语言包缺失另用假 tesseract 脚本经 tesseract_path 注入，CI 也能跑。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.agent.workspace_read_context import WORKSPACE_READ_EXTENSION
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from agent_py_agent.tests.test_workspace_read_context import read_context
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package

TESSERACT = shutil.which("tesseract")
UNAVAILABLE = "OCR 不可用：未找到 tesseract（可在设置 tesseract_path 指定）"

# 5x7 点阵大写字母，1 为黑点；只收录测试文字用到的字符
FONT = {
    "A": "01110 10001 10001 11111 10001 10001 10001", "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111", "G": "01110 10001 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001", "I": "01110 00100 00100 00100 00100 00100 01110",
    "L": "10000 10000 10000 10000 10000 10000 11111", "M": "10001 11011 10101 10101 10001 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "R": "11110 10001 10001 11110 10100 10010 10001",
    "T": "11111 00100 00100 00100 00100 00100 00100", "W": "10001 10001 10001 10101 10101 10101 01010",
    "X": "10001 10001 01010 00100 01010 10001 10001", " ": "00000 00000 00000 00000 00000 00000 00000",
}


# LLM: 测试专用极简 PNG 编码器：8 位灰度、无滤波、白底黑字，每个点阵像素放大 scale 倍并留边距；只用标准库。
# 函数用途: 把几行大写英文画成 tesseract 能识别的 PNG 字节。
def text_png(lines: list[str], scale: int = 6) -> bytes:
    cols, rows = max(len(line) for line in lines) * 6 + 4, len(lines) * 10 + 4
    grid = [[255] * cols for _ in range(rows)]
    for li, text in enumerate(lines):
        for ci, char in enumerate(text):
            for y, bits in enumerate(FONT[char].split()):
                for x, bit in enumerate(bits):
                    if bit == "1":
                        grid[2 + li * 10 + y][2 + ci * 6 + x] = 0
    raw = b"".join(b"\x00" + bytes(v for v in row for _ in range(scale)) for row in grid for _ in range(scale))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", cols * scale, rows * scale, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


# LLM: SDK/插件均从实际源码经标准后端构建；临时环境只安装包内入口 wheel，不注入源目录。
# 函数用途: 为组件测试安装真正的 image-text 发行包，并返回原 manifest。
@pytest.fixture(scope="module")
def installed_image_text(request, tmp_path_factory):
    python, sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("image-text-package")
    bundle = build_plugin_package(ROOT / "plugins/image-text", "image_text/declaration.json", (sdk,), root / "it.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    with ZipFile(bundle) as archive:
        name = package.manifest.entry_wheel
        wheel = root / name.rsplit("/", 1)[1]
        wheel.write_bytes(archive.read(name))
    result = subprocess.run([sys.executable, "-m", "pip", "--isolated", "--python", str(python),
        "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return python, package.manifest


@pytest.fixture
def dirs(tmp_path):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    return workspace, data


# LLM: 按宿主相同方式传入数据目录和设置环境变量；进程由原 MCP 客户端启动与回收。
# 函数用途: 启动一个安装后的 image-text MCP 进程。
def start(installed, data, settings=None):
    python, _ = installed
    env = {"MY_AGENT_PLUGIN_DATA_DIR": str(data)}
    if settings is not None:
        env["MY_AGENT_PLUGIN_SETTINGS"] = json.dumps(settings)
    client = MCPStdioClient(_config("", name="image-text", command=str(python),
                                   args=["-I", "-m", "image_text"], env=env))
    client.start()
    return client


# 函数用途: 带逐次读取上下文调用 read 工具并解析 JSON 正文。
def invoke(client, workspace, **arguments):
    meta = {WORKSPACE_READ_EXTENSION: read_context(workspace).to_payload()}
    result = client.call_tool("read", arguments, request_meta=meta)
    return result["isError"], json.loads(result["content"])


# 函数用途: 生成一个可执行的假 tesseract；--list-langs 立即列出 eng，识别调用记录 pid/参数后按 mode 行事。
def fake_tesseract(tmp_path, mode: str):
    record = tmp_path / "fake-record.json"
    script = tmp_path / "fake-tesseract"
    script.write_text(f"""#!{sys.executable}
import json, os, sys, time
if sys.argv[1:] == ["--list-langs"]:
    print('List of available languages in "/fake/" (1):')
    print("eng")
    sys.exit(0)
with open({str(record)!r}, "w") as handle:
    json.dump({{"pid": os.getpid(), "argv": sys.argv[1:], "exists": os.path.exists(sys.argv[1])}}, handle)
if {mode!r} == "sleep":
    time.sleep(60)
sys.exit(1)
""")
    script.chmod(0o755)
    return script, record


def test_tool_matches_manifest_and_is_read_only(installed_image_text, dirs):
    _, manifest = installed_image_text
    client = start(installed_image_text, dirs[1])
    try:
        assert {tool.name: tool.input_schema for tool in client.list_tools()} == {
            tool.name: tool.input_schema for tool in manifest.tools}
        assert {tool.name: tool.requested_effect for tool in manifest.tools} == {"read": "read_only"}
        result = client.call_tool("read", {"path": "a.png"})
        assert result["isError"] and json.loads(result["content"])["code"] == "MISSING_CONTEXT"
    finally:
        client.stop()


@pytest.mark.skipif(TESSERACT is None, reason="本机没有 tesseract，跳过真实识别")
def test_real_tesseract_recognizes_text_and_reports_missing_language(installed_image_text, dirs):
    workspace, data = dirs
    image = text_png(["HELLO WORLD", "IMAGE TEXT"])
    (workspace / "sample.png").write_bytes(image)
    client = start(installed_image_text, data, {"tesseract_path": TESSERACT})
    try:
        error, result = invoke(client, workspace, path="sample.png")
        assert not error, result
        assert result["format"] == "png" and result["bytes"] == len(image)
        assert (result["width"], result["height"]) == (70 * 6, 24 * 6)
        assert result["sha256"] == hashlib.sha256(image).hexdigest() and result["lang"] == "eng"
        assert "HELLO" in result["text"] and "IMAGE TEXT" in result["text"]
        assert len(result["lines"]) == 2 and result["word_count"] == 4
        first = result["lines"][0]
        assert first["text"].startswith("HELLO") and first["width"] > 0 and first["height"] > 0
        assert 0 < result["mean_confidence"] <= 100 and "/attach" in result["hint"]
        error, result = invoke(client, workspace, path="sample.png", lang="xyz")
        assert error and result["code"] == "LANG_UNAVAILABLE" and "eng" in result["available_langs"]
        assert "xyz" in result["message"] and result["width"] == 70 * 6
    finally:
        client.stop()
    assert list((data / "tmp").iterdir()) == []
    assert sorted(path.name for path in workspace.iterdir()) == ["sample.png"]


def gif_bytes():
    return b"GIF89a" + struct.pack("<HH", 3, 4) + b"\x00" * 10


def jpeg_bytes():
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, 40, 30, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp_bytes():
    return b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8X" + struct.pack("<I", 10) + b"\x00" * 4 \
        + (99).to_bytes(3, "little") + (49).to_bytes(3, "little")


@pytest.mark.parametrize("name,data,expected", [
    ("disguised.txt", text_png(["HI"]), ("png", 16 * 6, 14 * 6)),
    ("pic.gif", gif_bytes(), ("gif", 3, 4)),
    ("photo.jpg", jpeg_bytes(), ("jpeg", 30, 40)),
    ("anim.webp", webp_bytes(), ("webp", 100, 50)),
])
def test_missing_tesseract_still_returns_metadata(installed_image_text, dirs, name, data, expected):
    workspace, data_dir = dirs
    (workspace / name).write_bytes(data)
    client = start(installed_image_text, data_dir, {"tesseract_path": str(workspace / "no/such/tesseract")})
    try:
        error, result = invoke(client, workspace, path=name)
    finally:
        client.stop()
    assert error and result["code"] == "OCR_UNAVAILABLE" and result["message"] == UNAVAILABLE
    assert (result["format"], result["width"], result["height"]) == expected
    assert result["bytes"] == len(data) and result["sha256"] == hashlib.sha256(data).hexdigest()


def test_rejects_non_images_limits_bad_paths_and_bad_lang(installed_image_text, dirs, tmp_path):
    workspace, data = dirs
    (workspace / "fake.png").write_bytes(b"not really a png file")
    (workspace / "cut.png").write_bytes(text_png(["HI"])[:14])
    (workspace / "big.png").write_bytes(text_png(["HI"]) + b"\x00" * 300)
    (workspace / "real").mkdir()
    (workspace / "real/ok.png").write_bytes(text_png(["HI"]))
    (workspace / "link.png").symlink_to(workspace / "real/ok.png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(text_png(["HI"]))
    client = start(installed_image_text, data, {"max_image_bytes": 200, "tesseract_path": "/no/such/tesseract"})
    try:
        cases = [({"path": "fake.png"}, "UNSUPPORTED_FORMAT"), ({"path": "cut.png"}, "INVALID_IMAGE"),
                 ({"path": "big.png"}, "FILE_TOO_LARGE"), ({"path": "none.png"}, "FILE_NOT_FOUND"),
                 ({"path": "link.png"}, "UNSAFE_PATH"), ({"path": "real/../real/ok.png"}, "INVALID_PATH"),
                 ({"path": str(outside)}, "PATH_READ_SCOPE_BLOCKED")]
        cases += [({"path": "real/ok.png", "lang": lang}, "INVALID_ARGUMENTS")
                  for lang in ("ENG", "eng;rm -rf", "eng+", "../eng", "eng\n", "")]
        cases.append(({"path": "real/ok.png", "extra": 1}, "INVALID_ARGUMENTS"))
        for arguments, code in cases:
            error, result = invoke(client, workspace, **arguments)
            assert error and result["code"] == code, (arguments, result)
    finally:
        client.stop()


def test_missing_language_pack_is_reported_from_list_langs(installed_image_text, dirs, tmp_path):
    workspace, data = dirs
    (workspace / "a.png").write_bytes(text_png(["HI"]))
    script, record = fake_tesseract(tmp_path, "fail")
    client = start(installed_image_text, data, {"tesseract_path": str(script), "default_lang": "chi_sim+eng"})
    try:
        error, result = invoke(client, workspace, path="a.png")
    finally:
        client.stop()
    assert error and result["code"] == "LANG_UNAVAILABLE" and result["available_langs"] == ["eng"]
    assert "chi_sim" in result["message"] and result["format"] == "png"
    argv = json.loads(record.read_text())["argv"]
    assert argv[1:] == ["stdout", "--psm", "3", "-l", "chi_sim+eng", "tsv"]
    assert list((data / "tmp").iterdir()) == []


def test_timeout_kills_tesseract_and_removes_temp_file(installed_image_text, dirs, tmp_path):
    workspace, data = dirs
    (workspace / "a.png").write_bytes(text_png(["HI"]))
    script, record = fake_tesseract(tmp_path, "sleep")
    client = start(installed_image_text, data, {"tesseract_path": str(script), "ocr_timeout_seconds": 1})
    try:
        error, result = invoke(client, workspace, path="a.png")
    finally:
        client.stop()
    assert error and result["code"] == "OCR_TIMEOUT" and result["timeout_seconds"] == 1 and result["width"] > 0
    seen = json.loads(record.read_text())
    assert seen["exists"] and seen["argv"][0].startswith(str(data / "tmp"))
    assert not os.path.exists(seen["argv"][0]) and list((data / "tmp").iterdir()) == []
    with pytest.raises(ProcessLookupError):
        os.kill(seen["pid"], 0)


def test_invalid_settings_fail_startup(installed_image_text):
    python, _ = installed_image_text
    for settings in ({"default_lang": "eng; rm"}, {"ocr_timeout_seconds": 0}, {"max_image_bytes": True}):
        environment = dict(os.environ, MY_AGENT_PLUGIN_SETTINGS=json.dumps(settings))
        result = subprocess.run([str(python), "-I", "-m", "image_text"], env=environment,
                                input="", capture_output=True, text=True, timeout=15)
        assert result.returncode == 2 and "插件设置无效" in result.stderr, settings
