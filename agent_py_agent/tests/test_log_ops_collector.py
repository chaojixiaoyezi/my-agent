
from __future__ import annotations

"""log_ops 三种源采集器"增量不丢"单测 —— 文件 offset / 文件夹已处理集合 / API cursor。

全部用临时目录 + 本地 http server,自包含、CI 友好、不依赖外部模拟器。
重点验证:断点续采不重不丢、轮转/截断处理、半行不误采、API cursor 不漏。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from agent_py_agent.agent.tooling.log_ops.collector import (
    collect_api,
    collect_file,
    collect_folder,
    collect_source,
)

# ----------------------- 文件源 -----------------------


def test_file_incremental_resume_no_dup_no_loss(tmp_path: Path) -> None:
    src = tmp_path / "a.log"
    src.write_text("l1\nl2\nl3\n", encoding="utf-8")

    first = collect_file(str(src), {})
    assert first.new_lines == ["l1", "l2", "l3"]
    state = first.state

    # 断点续采:没有新内容 → 0 行,offset 不变。
    again = collect_file(str(src), state)
    assert again.new_lines == []
    assert again.state["offset"] == state["offset"]

    # append 新行 → 只采到新行(不重复给老行)。
    with src.open("a", encoding="utf-8") as handle:
        handle.write("l4\nl5\n")
    third = collect_file(str(src), again.state)
    assert third.new_lines == ["l4", "l5"]


def test_file_partial_last_line_not_emitted_until_complete(tmp_path: Path) -> None:
    src = tmp_path / "p.log"
    src.write_text("done1\npartial-no-newline", encoding="utf-8")
    res = collect_file(str(src), {})
    # 只采完整行;没有换行结尾的残行这轮不采。
    assert res.new_lines == ["done1"]

    # 残行被补完整后,下次采到它(不丢)。
    with src.open("a", encoding="utf-8") as handle:
        handle.write("-now-complete\n")
    res2 = collect_file(str(src), res.state)
    assert res2.new_lines == ["partial-no-newline-now-complete"]


def test_file_rotation_truncate_resets_offset(tmp_path: Path) -> None:
    src = tmp_path / "r.log"
    src.write_text("a\nb\nc\n", encoding="utf-8")
    res = collect_file(str(src), {})
    assert res.new_lines == ["a", "b", "c"]

    # 轮转:文件被截断换成更短的新内容(size 变小)→ 从头读,不漏新内容。
    src.write_text("new1\n", encoding="utf-8")
    res2 = collect_file(str(src), res.state)
    assert res2.new_lines == ["new1"]


def test_file_missing_then_appears(tmp_path: Path) -> None:
    src = tmp_path / "later.log"
    # 文件还不存在 → 空结果不报错(daemon 可能起在源之前)。
    res = collect_file(str(src), {})
    assert res.new_lines == []
    assert res.error == ""
    src.write_text("x1\nx2\n", encoding="utf-8")
    res2 = collect_file(str(src), res.state)
    assert res2.new_lines == ["x1", "x2"]


# ----------------------- 文件夹源 -----------------------


def test_folder_new_files_all_read_no_miss(tmp_path: Path) -> None:
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "1.log").write_text("a1\na2\n", encoding="utf-8")
    res = collect_folder(str(folder), {})
    assert sorted(res.new_lines) == ["a1", "a2"]

    # 新文件出现 → 全读;老文件不重复。
    (folder / "2.log").write_text("b1\n", encoding="utf-8")
    res2 = collect_folder(str(folder), res.state)
    assert res2.new_lines == ["b1"]

    # 没有新文件、老文件没动 → 0 行。
    res3 = collect_folder(str(folder), res2.state)
    assert res3.new_lines == []


def test_folder_existing_file_appended_incremental(tmp_path: Path) -> None:
    folder = tmp_path / "logs"
    folder.mkdir()
    f = folder / "1.log"
    f.write_text("a1\n", encoding="utf-8")
    res = collect_folder(str(folder), {})
    assert res.new_lines == ["a1"]
    # 已处理文件继续 append → 增量补读,不重复给 a1。
    with f.open("a", encoding="utf-8") as handle:
        handle.write("a2\na3\n")
    res2 = collect_folder(str(folder), res.state)
    assert res2.new_lines == ["a2", "a3"]


def test_folder_missing_dir_no_error(tmp_path: Path) -> None:
    res = collect_folder(str(tmp_path / "nope"), {})
    assert res.new_lines == []
    assert res.error == ""
    assert res.state == {"processed": {}}


# ----------------------- API 源(本地 http server) -----------------------


class _PollHandler(BaseHTTPRequestHandler):
    """模拟器协议:GET /poll?since=N → {"next": M, "lines": [...]}。

    内存里维护一个递增的行列表;since=N 返回 [N:] 的行,next=当前总数。
    """

    lines: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        since = int((params.get("since", ["0"])[0]) or 0)
        new = type(self).lines[since:]
        body = json.dumps({"next": len(type(self).lines), "lines": new}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # 静音,别污染测试输出。
        return


@pytest.fixture
def poll_server():
    handler = type("H", (_PollHandler,), {"lines": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    url = f"http://{host}:{port}/poll"
    try:
        yield url, handler
    finally:
        server.shutdown()
        server.server_close()


def test_api_cursor_incremental_no_loss(poll_server) -> None:
    url, handler = poll_server
    handler.lines = ["a1", "a2", "a3"]
    res = collect_api(url, {})
    assert res.new_lines == ["a1", "a2", "a3"]
    assert res.state["cursor"] == 3

    # 没新数据 → 0 行,cursor 不变。
    res2 = collect_api(url, res.state)
    assert res2.new_lines == []
    assert res2.state["cursor"] == 3

    # 上游追加 → 只拿新行(cursor 推进,不丢不重)。
    handler.lines.extend(["a4", "a5"])
    res3 = collect_api(url, res2.state)
    assert res3.new_lines == ["a4", "a5"]
    assert res3.state["cursor"] == 5


def test_api_network_error_preserves_cursor(tmp_path: Path) -> None:
    # 指向一个没人监听的端口 → 网络错误,cursor 保持,不丢(下轮重试同 cursor)。
    res = collect_api("http://127.0.0.1:9/poll", {"cursor": 7})
    assert res.new_lines == []
    assert res.state["cursor"] == 7
    assert "api_request_failed" in res.error


def test_api_url_with_existing_query_uses_ampersand(poll_server) -> None:
    url, handler = poll_server
    handler.lines = ["z1"]
    res = collect_api(url + "?tenant=acme", {})
    assert res.new_lines == ["z1"]


# ----------------------- 分发 -----------------------


def test_collect_source_dispatch_unknown_kind(tmp_path: Path) -> None:
    res = collect_source("weird", "x", {"k": 1})
    assert res.new_lines == []
    assert "unknown_source_kind" in res.error
