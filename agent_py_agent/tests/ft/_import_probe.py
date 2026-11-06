"""独立解释器的导入/延迟扩展探针；只能由测试显式执行，无网络请求。"""
from __future__ import annotations

import ctypes.util
import importlib
import importlib.util
import io
import json
import struct
import sys
import zlib
from pathlib import Path

# 仅列声明为可选的外部包；缺内部模块、拼写错误及已安装包的损坏依赖不能被跳过。
OPTIONAL_IMPORT_ROOTS = frozenset({
    "sqlalchemy", "psycopg", "fastapi", "uvicorn", "redis", "boto3", "botocore",
    "opentelemetry", "cryptography", "mcp", "Quartz", "AppKit", "Foundation",
    "ScreenCaptureKit", "ApplicationServices", "objc", "mss", "rapidocr_onnxruntime",
    "Xlib", "onnxruntime",
})


def _gil_enabled():
    return bool(getattr(sys, "_is_gil_enabled", lambda: True)())


def _assert_unchanged(before, stage):
    assert _gil_enabled() == before, f"GIL changed at {stage}: {before} -> {_gil_enabled()}"


def _all_modules():
    before = _gil_enabled()
    import agent_py_agent

    root = Path(agent_py_agent.__file__).resolve().parent
    _assert_unchanged(before, "agent_py_agent")
    imported, skipped = [], []
    files = [root / "__init__.py", root / "__main__.py"]
    for directory in ("agent", "cli"):
        files.extend(sorted((root / directory).rglob("*.py")))
    for file in files:
        parts = file.relative_to(root.parent).with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        # 此模块直接绑定 Linux waitid；无此系统 API 的主机没有可导入的实现。
        if name == "agent_py_agent.agent.gateway_parts.multiprocess_native_linux_reference":
            import os

            if not hasattr(os, "waitid"):
                skipped.append({"module": name, "missing_extra": "platform API os.waitid absent"})
                continue
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            missing = (exc.name or "").split(".")[0]
            if missing not in OPTIONAL_IMPORT_ROOTS or importlib.util.find_spec(missing) is not None:
                raise
            skipped.append({"module": name, "missing_extra": missing})
        else:
            imported.append(name)
        _assert_unchanged(before, name)
    assert imported
    return {"gil": _gil_enabled(), "before": before, "imported": imported, "skipped": skipped}


def _png_bytes():
    def chunk(kind, content):
        return struct.pack("!I", len(content)) + kind + content + struct.pack("!I", zlib.crc32(kind + content))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


def _lazy_extension(kind):
    before = _gil_enabled()
    missing = None
    if kind == "crypto":
        missing = "cryptography"
        if importlib.util.find_spec(missing) is not None:
            from agent_py_agent.agent.adapter.feishu_crypto import (
                decrypt_event,
                encrypt_event_for_test,
            )

            content = b'{"probe": true}'
            encrypted = encrypt_event_for_test("ft-probe-only", content, bytes(16))
            assert decrypt_event("ft-probe-only", encrypted) == content
            missing = None
    elif kind == "image":
        from agent_py_agent.agent.conversation.input_media_images import inspect_input_image

        assert inspect_input_image(_png_bytes()) == "image/png"
    elif kind == "sqlalchemy":
        missing = "sqlalchemy"
        if importlib.util.find_spec(missing) is not None:
            from agent_py_agent.agent.storage_backend import StorageBackend

            backend = StorageBackend.in_memory()
            try:
                assert backend.scalar("SELECT 23") == 23
            finally:
                backend.dispose()
            missing = None
    elif kind == "psycopg":
        missing = "psycopg"
        if importlib.util.find_spec(missing) is not None and ctypes.util.find_library("pq"):
            import psycopg

            assert psycopg.pq.version() > 0
            missing = None
        elif importlib.util.find_spec(missing) is not None:
            missing = "OS libpq absent (pure psycopg)"
    elif kind == "tui":
        importlib.import_module("agent_py_agent.cli.chat_parts.tui")
        importlib.import_module("agent_py_agent.cli.chat_parts.renderer")
        importlib.import_module("prompt_toolkit")
    elif kind == "yaml":
        missing = "yaml"
        if importlib.util.find_spec(missing) is not None:
            import yaml

            assert yaml.safe_load(io.StringIO("probe: 23")) == {"probe": 23}
            missing = None
    elif kind == "lark":
        from Crypto.Cipher import AES

        cipher = AES.new(bytes(16), AES.MODE_ECB)
        assert cipher.decrypt(cipher.encrypt(bytes(16))) == bytes(16)
        importlib.import_module("lark_oapi")
    else:
        raise AssertionError(f"unknown probe {kind}")
    _assert_unchanged(before, kind)
    return {"before": before, "gil": _gil_enabled(), "stage": kind, "missing_extra": missing}


if __name__ == "__main__":
    print(json.dumps(_all_modules() if sys.argv[1] == "all" else _lazy_extension(sys.argv[1])))
