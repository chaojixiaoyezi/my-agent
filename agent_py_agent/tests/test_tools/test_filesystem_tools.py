"""LLM: tests for filesystem tools — write boundary, symlink, write/append, replace, search, params.

给人看的解释：
这个文件放所有和文件系统工具相关的测试：写入边界保护、符号链接逃逸拦截、
写文件/追加文件、替换内容、搜索文本、参数校验和绝对路径隐藏。
"""

import tempfile
from pathlib import Path

from agent_py_agent.agent.tools import (
    AppendFileTool,
    FetchUrlTool,
    HttpRequestTool,
    ListFilesTool,
    ReadFileTool,
    ReplaceInFileTool,
    SearchTextTool,
    WriteFileTool,
)

from .backends import make_tool_registry, start_test_server


def test_write_boundary_blocks_subagent_writes_outside_allowed_roots():
    """LLM: verify that write_boundary restricts writes to allowed roots and blocks forbidden/locked paths.

    新手说明:
    子代理写文件必须在 allowed_write_roots 内，不能写 forbidden_write_roots 和 locked_files，
    非 string path 也应被拒绝。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        registry = make_tool_registry(workspace)
        task_dir = workspace / "subs" / "run-1"
        task_dir.mkdir(parents=True)
        boundary = {
            "allowed_write_roots": [str(task_dir)],
            "forbidden_write_roots": [str(task_dir / "private")],
            "locked_files": ["subs/run-1/LOCKED.md"],
        }

        ok = registry.execute_call(
            {"tool": "write_file", "path": "subs/run-1/output.md", "content": "ok"},
            allowed_tools=["write_file"],
            write_boundary=boundary,
        )
        outside = registry.execute_call(
            {"tool": "write_file", "path": "README.md", "content": "bad"},
            allowed_tools=["write_file"],
            write_boundary=boundary,
        )
        forbidden = registry.execute_call(
            {"tool": "write_file", "path": "subs/run-1/private/secret.md", "content": "bad"},
            allowed_tools=["write_file"],
            write_boundary=boundary,
        )
        locked = registry.execute_call(
            {"tool": "write_file", "path": "subs/run-1/LOCKED.md", "content": "bad"},
            allowed_tools=["write_file"],
            write_boundary=boundary,
        )
        locked_child = registry.execute_call(
            {"tool": "write_file", "path": "subs/run-1/LOCKED.md/child.txt", "content": "bad"},
            allowed_tools=["write_file"],
            write_boundary=boundary,
        )
        weird_path = registry.execute_call(
            {"tool": "write_file", "path": {"unexpected": "object"}, "content": "bad"},
            allowed_tools=["write_file"],
            write_boundary=boundary,
        )

        assert ok.ok
        assert (task_dir / "output.md").read_text(encoding="utf-8") == "ok"
        assert not outside.ok
        assert "allowed_write_roots" in outside.output
        assert not (workspace / "README.md").exists()
        assert not forbidden.ok
        assert "forbidden_write_roots" in forbidden.output
        assert not locked.ok
        assert "locked_files" in locked.output
        assert not locked_child.ok
        assert "locked_files" in locked_child.output
        assert not (task_dir / "LOCKED.md").exists()
        assert not weird_path.ok
        assert "path 参数必须是字符串路径" in weird_path.output


def test_write_boundary_blocks_symlink_escape_under_allowed_root():
    """LLM: verify that a symlink inside an allowed root cannot escape to write outside.

    新手说明:
    在允许的目录下创建指向外部的符号链接，写文件仍应被拦截。
    """
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
        workspace = Path(td)
        outside = Path(outside_td)
        registry = make_tool_registry(workspace)
        task_dir = workspace / "subs" / "run-1"
        task_dir.mkdir(parents=True)
        link = task_dir / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            return

        result = registry.execute_call(
            {"tool": "write_file", "path": "subs/run-1/outside-link/escape.txt", "content": "bad"},
            allowed_tools=["write_file"],
            write_boundary={"allowed_write_roots": [str(task_dir)]},
        )

        assert not result.ok
        assert "路径超出允许的工作区范围" in result.output
        assert not (outside / "escape.txt").exists()


def test_write_and_append_file_tools():
    """LLM: verify that WriteFileTool creates files and AppendFileTool appends to existing files.

    新手说明:
    先写再追加，确认内容顺序和文件内容正确。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        write_tool = WriteFileTool(workspace)
        append_tool = AppendFileTool(workspace)

        write_result = write_tool.execute({"path": "src/demo.py", "content": "print('a')\n"})
        append_result = append_tool.execute({"path": "src/demo.py", "content": "print('b')\n"})

        assert write_result.ok
        assert append_result.ok
        assert (workspace / "src" / "demo.py").read_text(encoding="utf-8") == "print('a')\nprint('b')\n"


def test_filesystem_tools_reject_bad_parameters_and_hide_absolute_outside_paths():
    """LLM: verify that filesystem tools reject bad params and never expose absolute paths outside workspace.

    新手说明:
    读工作区外文件应被拒绝且不泄露绝对路径；start_line 传字符串应报错；
    path 传非字符串应报错；recursive="false" 应只列一层。
    """
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
        workspace = Path(td)
        outside_file = Path(outside_td) / "secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        (workspace / "notes.txt").write_text("line one\nline two\n", encoding="utf-8")
        read_tool = ReadFileTool(workspace, max_chars=2000)
        write_tool = WriteFileTool(workspace)
        list_tool = ListFilesTool(workspace, max_entries=20)
        (workspace / "src").mkdir()
        (workspace / "src" / "nested.txt").write_text("nested", encoding="utf-8")

        outside = read_tool.execute({"path": str(outside_file)})
        bad_line = read_tool.execute({"path": "notes.txt", "start_line": "abc"})
        bad_path_type = write_tool.execute({"path": {"bad": "type"}, "content": "x"})
        non_recursive = list_tool.execute({"path": ".", "recursive": "false"})

        assert not outside.ok
        assert "工作区" in outside.output
        assert str(outside_file) not in outside.output
        assert not bad_line.ok
        assert "start_line 必须是整数" in bad_line.output
        assert not bad_path_type.ok
        assert "path 参数必须是字符串路径" in bad_path_type.output
        assert non_recursive.ok
        assert "src/" in non_recursive.output
        assert "src/nested.txt" not in non_recursive.output


def test_search_text_does_not_follow_symlink_to_outside_workspace():
    """LLM: verify that SearchTextTool does not follow symlinks pointing outside the workspace.

    新手说明:
    在工作区内创建指向外部的符号链接，搜索和读取都不应泄露外部内容。
    """
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
        workspace = Path(td)
        outside_file = Path(outside_td) / "outside.txt"
        outside_file.write_text("needle outside workspace", encoding="utf-8")
        link = workspace / "linked-outside.txt"
        try:
            link.symlink_to(outside_file)
        except OSError:
            return
        search_tool = SearchTextTool(workspace, max_matches=10)
        read_tool = ReadFileTool(workspace, max_chars=2000)

        search_result = search_tool.execute({"query": "needle", "path": "."})
        read_result = read_tool.execute({"path": "linked-outside.txt"})

        assert search_result.ok
        assert "没有找到匹配项" in search_result.output
        assert not read_result.ok
        assert "路径超出允许的工作区范围" in read_result.output


def test_replace_in_file_tool():
    """LLM: verify that ReplaceInFileTool replaces text and reports the count.

    新手说明:
    在已有文件里替换一处文本，确认文件内容和返回信息正确。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = workspace / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def hello():\n    return 'old'\n", encoding="utf-8")

        tool = ReplaceInFileTool(workspace)
        result = tool.execute(
            {
                "path": "src/demo.py",
                "old": "return 'old'",
                "new": "return 'new'",
            }
        )

        assert result.ok
        assert "替换 1 处" in result.output
        assert target.read_text(encoding="utf-8") == "def hello():\n    return 'new'\n"


def test_fetch_url_and_http_request_tools():
    """LLM: verify that FetchUrlTool and HttpRequestTool work against a local test server.

    新手说明:
    启动本地 HTTP 服务，分别测试 GET 抓取和 POST 回显。
    """
    server = start_test_server()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        fetch_tool = FetchUrlTool(max_chars=2000, timeout=5)
        http_tool = HttpRequestTool(max_chars=2000, timeout=5)

        fetch_result = fetch_tool.execute({"url": base + "/page"})
        http_result = http_tool.execute(
            {
                "url": base + "/echo",
                "method": "POST",
                "headers": {"Content-Type": "text/plain; charset=utf-8"},
                "body": "hello api",
            }
        )

        assert fetch_result.ok
        assert "demo page" in fetch_result.output
        assert http_result.ok
        assert "hello api" in http_result.output
    finally:
        server.shutdown()
        server.server_close()


def test_web_tools_reject_unsafe_urls_and_bad_request_parameters():
    """LLM: verify that web tools reject file:// URLs, userinfo in URLs, and malformed headers/methods/bodies.

    新手说明:
    file:// 协议、带用户名密码的 URL、含换行的 header、非法 method、非字符串 body 都应被拒绝。
    """
    fetch_tool = FetchUrlTool(max_chars=2000, timeout=5)
    http_tool = HttpRequestTool(max_chars=2000, timeout=5)

    file_url = fetch_tool.execute({"url": "file:///etc/passwd"})
    userinfo_url = fetch_tool.execute({"url": "https://user:pass@example.com"})
    bad_header = http_tool.execute(
        {
            "url": "https://example.com",
            "headers": {"X-Test": "ok\nbad"},
        }
    )
    bad_method = http_tool.execute({"url": "https://example.com", "method": "GET\nPOST"})
    bad_body = http_tool.execute({"url": "https://example.com", "body": {"not": "text"}})

    assert not file_url.ok
    assert "http 或 https" in file_url.output
    assert not userinfo_url.ok
    assert "用户名或密码" in userinfo_url.output
    assert not bad_header.ok
    assert "headers 值不能包含换行" in bad_header.output
    assert not bad_method.ok
    assert "method 必须是有效的 HTTP 方法名" in bad_method.output
    assert not bad_body.ok
    assert "body 参数必须是字符串或标量文本" in bad_body.output
