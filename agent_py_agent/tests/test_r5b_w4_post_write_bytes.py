"""计量真实文件描述符读取，约束自动写后检查而保留显式完整验收。"""
from __future__ import annotations

import builtins
import io
import os
from collections import Counter
from pathlib import Path

import pytest

from agent_py_agent.agent.subagents.static_site import validator
from agent_py_agent.agent.tooling import web_project_integrity as web
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool

SMALL_TEXT_LIMIT = 262144
PAGE = b"<!doctype html><html><head></head><body>complete</body></html>"


class _CountedRaw(io.RawIOBase):
    """在缓冲层下面计量实际返回的字节，包括 TextIOWrapper 的预读。"""

    def __init__(self, raw, meter, path):
        self.raw, self.meter, self.path = raw, meter, path

    def read(self, size=-1):
        data = self.raw.read(size)
        self.meter[self.path] += len(data)
        return data

    def readinto(self, buffer):
        size = self.raw.readinto(buffer)
        self.meter[self.path] += size
        return size

    def readable(self):
        return True

    def seekable(self):
        return self.raw.seekable()

    def seek(self, offset, whence=0):
        return self.raw.seek(offset, whence)

    def tell(self):
        return self.raw.tell()

    def fileno(self):
        return self.raw.fileno()

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


def _measure_reads(monkeypatch, *paths):
    measured = {path.resolve() for path in paths}
    meter = Counter()
    original = io.open

    def counted_open(file, mode="r", buffering=-1, encoding=None, errors=None,
                     newline=None, closefd=True, opener=None):
        path = Path(file).resolve() if isinstance(file, (str, os.PathLike)) else None
        if path not in measured or not mode.startswith("r") or "+" in mode:
            return original(file, mode, buffering, encoding, errors, newline, closefd, opener)
        raw = _CountedRaw(original(file, "rb", buffering=0, closefd=closefd, opener=opener), meter, path)
        if buffering == 0:
            assert "b" in mode
            return raw
        stream = io.BufferedReader(raw, buffer_size=buffering if buffering > 1 else io.DEFAULT_BUFFER_SIZE)
        return stream if "b" in mode else io.TextIOWrapper(stream, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(io, "open", counted_open)
    monkeypatch.setattr(builtins, "open", counted_open)
    return meter


def _sparse_html(path, size, *, prefix=PAGE, tail=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(prefix)
        stream.seek(size - len(tail) - 1)
        stream.write(b" " + tail)


@pytest.mark.parametrize("method", ["read_text", "open", "parser", "readinto"])
def test_meter_counts_raw_reads_below_all_text_and_parser_entry_points(tmp_path, monkeypatch, method):
    page = tmp_path / "index.html"
    content = PAGE + b" " * 10000
    page.write_bytes(content)
    meter = _measure_reads(monkeypatch, page)
    if method == "read_text":
        assert page.read_text() == content.decode()
        assert meter[page] == len(content)
    elif method == "open":
        with open(page, encoding="utf-8") as stream:
            assert stream.read(1) == "<"
        assert meter[page] > 1  # 证明计量包含实际缓冲预读，而非仅统计返回字符。
    elif method == "parser":
        parser = validator.StaticSiteHTMLParser()
        parser.feed(page.read_text())
        assert meter[page] == len(content)
    else:
        with page.open("rb", buffering=0) as stream:
            assert stream.readinto(bytearray(13)) == 13
        assert meter[page] == 13


@pytest.mark.parametrize("size", [1024 * 1024, 100 * 1024 * 1024], ids=["1MiB", "100MiB"])
def test_real_small_js_write_does_not_read_large_sibling_html(tmp_path, monkeypatch, size):
    html = tmp_path / "site/index.html"
    _sparse_html(html, size)
    meter = _measure_reads(monkeypatch, html)
    result = WriteFileTool(tmp_path).execute({"path": "site/change.js", "content": "console.log('x');"})
    assert result.ok is True
    assert (tmp_path / "site/change.js").read_text() == "console.log('x');"
    assert "STATIC_SITE_SCAN_INCOMPLETE" in result.result_envelope["artifact_integrity"]["warning_codes"]
    assert meter[html] == 8192  # HTML 只读模板头；自动解析按已存在的字节合同拒收整页。


@pytest.mark.parametrize("prefix, expected", [
    ("x" * 8190 + "{{", True),
    ("界" * 8190 + "{{", True),
    ("x\r\n" * 4095 + "{{", True),
    ("x" * 8192 + "{{", False),
])
def test_template_header_keeps_original_character_boundary_with_bounded_bytes(tmp_path, monkeypatch, prefix, expected):
    html = tmp_path / "index.html"
    _sparse_html(html, 1024 * 1024, prefix=prefix.encode())
    meter = _measure_reads(monkeypatch, html)
    assert web._is_server_side_template_project(html, tmp_path) is expected
    assert meter[html] <= 4 * 8192


def test_invalid_utf8_cannot_make_header_ignore_decoder_read_until_eof(tmp_path, monkeypatch):
    html = tmp_path / "index.html"
    html.write_bytes(b"\xff" * 1024 * 1024)
    meter = _measure_reads(monkeypatch, html)
    assert web._is_server_side_template_project(html, tmp_path) is False
    assert meter[html] <= 4 * 8192


@pytest.mark.parametrize("template", [False, True])
def test_template_directory_iteration_is_lazy_and_closes_scandir(tmp_path, monkeypatch, template):
    for index in range(60):
        (tmp_path / f"{index}.html").write_text("{{ title }}" if template else "plain")
    original = os.scandir
    observed = []

    class Observed:
        def __init__(self, path):
            self.iterator, self.entries, self.closed = original(path), 0, False
            observed.append(self)

        def __iter__(self):
            return self

        def __next__(self):
            item = next(self.iterator)
            self.entries += 1
            return item

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.iterator.close()
            self.closed = True

    monkeypatch.setattr(os, "scandir", Observed)
    assert web._is_server_side_template_project(tmp_path / "change.js", tmp_path) is template
    assert sum(item.entries for item in observed) == (1 if template else 20)
    assert observed and all(item.closed for item in observed)


@pytest.mark.parametrize("extra", [0, 1])
def test_auto_html_byte_limit_marks_only_incomplete_coverage(tmp_path, monkeypatch, extra):
    html = tmp_path / "index.html"
    _sparse_html(html, SMALL_TEXT_LIMIT + extra)
    meter = _measure_reads(monkeypatch, html)
    record = validator.run_static_site_check(web._static_site_request(tmp_path, tmp_path), tmp_path)
    assert record.validation_result["scan_incomplete"] is bool(extra)
    assert record.passed is (extra == 0)
    assert ("STATIC_SITE_SCAN_INCOMPLETE" in record.validation_result["warnings"]) is bool(extra)
    assert meter[html] <= SMALL_TEXT_LIMIT + 1


def test_small_text_growth_after_stat_still_bounds_actual_reads_and_reports_partial(tmp_path, monkeypatch):
    script = tmp_path / "change.js"
    script.write_text("console.log(1);")
    meter = _measure_reads(monkeypatch, script)
    original = Path.open
    grew = []

    def grow_before_read(path, mode="r", *args, **kwargs):
        if path == script and mode == "rb" and not grew:
            grew.append(True)
            with original(script, "ab") as stream:
                stream.write(b" " * SMALL_TEXT_LIMIT)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow_before_read)
    result = validator.StaticSiteCheckResult(str(tmp_path))
    assert validator.small_text(script, scan_result=result) is None
    assert result.scan_incomplete is True
    assert result.warnings == ["STATIC_SITE_SCAN_INCOMPLETE"]
    assert meter[script] == SMALL_TEXT_LIMIT + 1


def test_large_external_script_also_marks_auto_scan_incomplete(tmp_path, monkeypatch):
    html = tmp_path / "site/index.html"
    html.parent.mkdir()
    html.write_bytes(PAGE.replace(b"</body>", b'<script src="large.js"></script></body>'))
    script = html.with_name("large.js")
    script.write_bytes(b" " * (SMALL_TEXT_LIMIT + 1))
    meter = _measure_reads(monkeypatch, script)
    result = WriteFileTool(tmp_path).execute({"path": "site/change.js", "content": "console.log(1);"})
    assert result.ok is True
    assert "STATIC_SITE_SCAN_INCOMPLETE" in result.result_envelope["artifact_integrity"]["warning_codes"]
    assert meter[script] == 0


def test_small_page_reference_placeholder_and_dom_warnings_are_preserved(tmp_path):
    html = tmp_path / "site/index.html"
    html.parent.mkdir()
    html.write_bytes(PAGE.replace(b"</body>", b'<a href="absent.html">go</a>${name}<script src="change.js"></script></body>'))
    result = WriteFileTool(tmp_path).execute({
        "path": "site/change.js", "content": "document.getElementById('missing').textContent='changed';",
    })
    assert result.ok is True
    codes = result.result_envelope["artifact_integrity"]["warning_codes"]
    assert {"STATIC_SITE_BROKEN_LOCAL_REFS", "STATIC_SITE_PLACEHOLDER_HITS", "STATIC_SITE_MISSING_DOM_ID_HITS"} <= set(codes)
    assert "STATIC_SITE_SCAN_INCOMPLETE" not in codes


def test_explicit_full_validation_still_reads_tail_and_finds_damage(tmp_path, monkeypatch):
    html = tmp_path / "site/index.html"
    _sparse_html(html, 1024 * 1024, tail=b"<section>damaged tail</section>")
    auto = WriteFileTool(tmp_path).execute({"path": "site/change.js", "content": "console.log(1);"})
    assert auto.ok is True
    assert "STATIC_SITE_SCAN_INCOMPLETE" in auto.result_envelope["artifact_integrity"]["warning_codes"]
    meter = _measure_reads(monkeypatch, html)
    record = validator.run_static_site_check({"site_root": "site", "require_complete_html": True}, tmp_path)
    assert record.passed is False
    assert record.validation_result["scan_incomplete"] is False
    assert "index.html:trailing_markup_after_html_close" in record.validation_result["html_structure_hits"]
    assert meter[html] == html.stat().st_size
