"""审计 #23(low/i18n)真测:文件编码探测 + 换行保留——读写非 UTF-8 企业文件不失败/不改坏。

覆盖几十国企业代码库含大量非 UTF-8 文件(日 Shift-JIS、带 BOM、Latin-1、CRLF)。真造各编码字节
落盘,真走 ReadFileTool/WriteFileTool:非 UTF-8 不再硬失败、写回保持原编码+原换行不静默改坏、
二进制仍拒读不返回乱码。BOM/CRLF 用确定性断言;多字节统计判别用较长样本(短样本本就歧义)。
学 终端交互 detectEncoding/detectLineEndings。
"""

from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from agent_py_agent.agent.common.encoding_detect import (
    decode_bytes,
    detect_encoding,
    detect_line_ending,
    encode_text,
)
from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool

_JP = "お得意様各位、平素は格別のご高配を賜り厚く御礼申し上げます。"  # 较长样本,统计判别可靠


# ---------- 模块单元 ----------

def test_bom_detection_is_deterministic() -> None:
    assert decode_bytes(codecs.BOM_UTF8 + "héllo".encode()) == ("héllo", "utf-8-sig")  # BOM 被剥
    assert decode_bytes("データ".encode("utf-16"))[1] == "utf-16"
    assert decode_bytes(b"abc") == ("abc", "utf-8")


def test_shift_jis_decodes_not_hard_fail() -> None:
    text, enc = decode_bytes(_JP.encode("shift_jis"))
    assert text == _JP and enc == "cp932"  # 日文 Shift-JIS 正确解码(cp932 是其超集)


def test_binary_with_nul_raises() -> None:
    png = bytes([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x01, 0x02])
    with pytest.raises(UnicodeDecodeError):
        decode_bytes(png)  # 含 NUL 的二进制不强行读出乱码


def test_line_ending_detection_and_apply() -> None:
    assert detect_line_ending(b"a\r\nb\r\nc") == "\r\n"
    assert detect_line_ending(b"a\rb\rc") == "\r"
    assert detect_line_ending(b"a\nb\nc") == "\n"
    assert encode_text("x\ny\nz", "utf-8", "\r\n") == b"x\r\ny\r\nz"  # 换行风格应用


def test_encode_falls_back_to_utf8_for_unrepresentable() -> None:
    # 往 Shift-JIS 加 emoji(原编码无法表示)→ 退 utf-8,不丢字符不报错
    out = encode_text("emoji \U0001f600 test", "shift_jis", "\n")
    assert out.decode("utf-8") == "emoji \U0001f600 test"


# ---------- ReadFileTool 集成 ----------

def test_read_file_decodes_shift_jis(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "jp.txt").write_bytes(_JP.encode("shift_jis"))
    result = ReadFileTool(workspace, max_chars=10000).execute({"path": "jp.txt"})
    assert result.ok is True  # 非 UTF-8 不再硬失败
    assert "御礼申し上げます" in result.output  # 正确解码出日文


def test_read_file_binary_still_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "blob.bin").write_bytes(bytes([0x89, 0x50, 0x00, 0x00, 0x01, 0x02, 0x03, 0x88]))
    result = ReadFileTool(workspace, max_chars=10000).execute({"path": "blob.bin"})
    assert result.ok is False  # 二进制仍拒读,不返回乱码


# ---------- WriteFileTool 写回保留原格式 ----------

def test_write_preserves_bom_and_crlf(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "config.txt"
    target.write_bytes(codecs.BOM_UTF8 + "原内容\r\n第二行\r\n".encode())  # utf-8-sig + CRLF
    result = WriteFileTool(workspace).execute({"path": "config.txt", "content": "新内容\n新第二行"})
    assert result.ok is True
    raw = target.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8)  # BOM 保留(没被剥)
    assert b"\r\n" in raw  # CRLF 保留(没被静默改 LF)
    assert raw.decode("utf-8-sig").replace("\r\n", "\n") == "新内容\n新第二行"


def test_write_preserves_shift_jis_encoding(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "jp.txt"
    target.write_bytes(_JP.encode("shift_jis"))  # 既有文件是 Shift-JIS
    new_text = "改定版のお知らせ。今後ともよろしくお願い申し上げます。"
    result = WriteFileTool(workspace).execute({"path": "jp.txt", "content": new_text})
    assert result.ok is True
    raw = target.read_bytes()
    assert raw.decode("cp932") == new_text  # 仍是 Shift-JIS 可解码(原编码保留)
    assert raw != new_text.encode("utf-8")  # 确实没被静默改成 utf-8 字节


def test_write_new_file_defaults_utf8(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = WriteFileTool(workspace).execute({"path": "new.txt", "content": "新文件 hello"})
    assert result.ok is True
    assert (workspace / "new.txt").read_bytes() == "新文件 hello".encode()  # 新文件默认 utf-8
    assert detect_encoding((workspace / "new.txt").read_bytes()) == "utf-8"
