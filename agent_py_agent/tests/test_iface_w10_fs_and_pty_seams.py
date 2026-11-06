"""IFACE-W10：只用隔离文件和可读替身，验证宿主注入不改变默认权限或读取顺序。"""
from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints

import pytest

from agent_py_agent.agent.tooling import filesystem_read_file as reads
from agent_py_agent.agent.tooling import pty_sessions as pty
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions, ReadFileTool
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions


class _Snapshot:
    """只实现文本快照读取合同，不依赖 TextFileIndex 或真实文件句柄。"""

    # LLM: 替身仅实现快照合同，不打开真实索引。
    # 函数用途: 保存文本、总数和一致性故障设置。
    def __init__(self, text, *, fail_check=None):
        self.text = text
        self.total_chars = len(text)
        self.total_lines = text.count("\n") + int(bool(text) and not text.endswith("\n"))
        self.events = []
        self.fail_check = fail_check
        self.checks = 0

    # LLM: 按调用次数抛错，不吞掉一致性故障。
    # 函数用途: 记录并模拟快照检查。
    def check_current(self):
        self.checks += 1
        self.events.append("check")
        if self.checks == self.fail_check:
            raise OSError("snapshot changed")

    # LLM: 先检查再交给 with 关闭流。
    # 函数用途: 打开内存文本流。
    def open(self):
        self.events.append("open")
        self.check_current()
        return io.StringIO(self.text)

    # LLM: 字符页同样保留读前读后检查。
    # 函数用途: 返回受限字符窗口。
    def read(self, offset, limit):
        self.events.append(("read", offset, limit))
        with self.open() as handle:
            handle.seek(offset)
            window = handle.read(limit)
        self.check_current()
        return window


class _Provider:
    # LLM: 不从目标路径推导任何额外权限。
    # 函数用途: 保存快照和调用目标。
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.targets = []

    # LLM: 真值不能决定是否使用一个合法可调用提供者。
    # 函数用途: 模拟假值提供者。
    def __bool__(self):
        # 合法可调用对象即使为假值也不能被误当成未注入。
        return False

    # LLM: 调用日志用于验证外层权限门的先后顺序。
    # 函数用途: 返回快照并记录获准目标。
    def __call__(self, target):
        self.targets.append(target)
        return self.snapshot


# LLM: 注入只走正式宿主构造参数，不进入模型 JSON。
# 函数用途: 构造指定访问策略的读取工具。
def _read_tool(root, *, provider=None, max_chars=100, **access):
    return ReadFileTool(root, max_chars, access_options=FileSystemAccessOptions(**access),
                        snapshot_provider=provider)


@pytest.mark.parametrize("params", [{}, {"start_line": 2}, {"end_line": 1},
                                    {"offset": 2, "max_chars": 4}, {"max_chars": 5}])
# LLM: 同一真实输入比较省略参数和显式 None，并要求索引缓存存在。
# 函数用途: 验证默认输出、信封和缓存兼容。
def test_default_snapshot_lookup_and_explicit_none_preserve_results(tmp_path, monkeypatch, params):
    target = tmp_path / "text.txt"
    target.write_text("甲乙\nsecond\n", encoding="utf-8")
    original = reads.text_file_index
    calls = []

    # LLM: 调用真实索引，不用替身代替默认路径。
    # 函数用途: 旁听默认索引查找。
    def observed(tool, path):
        calls.append((tool, path))
        return original(tool, path)

    monkeypatch.setattr(reads, "text_file_index", observed)
    omitted = ReadFileTool(tmp_path, 100)
    explicit = _read_tool(tmp_path)
    first = omitted.execute({"path": "text.txt", **params})
    second = explicit.execute({"path": "text.txt", **params})
    assert first.ok and second.ok
    assert first.output == second.output
    assert first.result_envelope == second.result_envelope
    assert calls == [(omitted, target), (explicit, target)]
    assert len(omitted._text_file_indexes) == len(explicit._text_file_indexes) == 1


@pytest.mark.parametrize("params", [{}, {"start_line": 2}, {"end_line": 1},
                                    {"offset": 2, "max_chars": 4}, {"max_chars": 5}])
# LLM: 与真实默认结果逐字段比较，回退默认索引即失败。
# 函数用途: 验证注入不改变文本投影和游标。
def test_injected_snapshot_matches_default_projection_without_index_lookup(tmp_path, monkeypatch, params):
    target = tmp_path / "text.txt"
    text = "甲乙\nsecond\n"
    target.write_text(text, encoding="utf-8")
    expected = ReadFileTool(tmp_path, 100).execute({"path": "text.txt", **params})
    snapshot = _Snapshot(text)
    provider = _Provider(snapshot)
    tool = _read_tool(tmp_path, provider=provider)
    monkeypatch.setattr(reads, "text_file_index", lambda *_: pytest.fail("default lookup used"))
    actual = tool.execute({"path": "text.txt", **params})
    assert actual.ok and actual.output == expected.output
    assert actual.result_envelope == expected.result_envelope
    assert provider.targets == [target]
    assert snapshot.events and snapshot.events[-1] == "check"
    assert not hasattr(tool, "_text_file_indexes")


@pytest.mark.parametrize("params", [{}, {"offset": 1, "max_chars": 2}])
# LLM: 磁盘与快照故意不同，不能只测参数被接受。
# 函数用途: 验证两种窗口确实读取注入内容。
def test_injected_snapshot_content_is_used_not_underlying_disk(tmp_path, params):
    target = tmp_path / "text.txt"
    target.write_text("disk sentinel", encoding="utf-8")
    provider = _Provider(_Snapshot("甲乙\n第二行"))
    result = _read_tool(tmp_path, provider=provider).execute({"path": str(target), **params})
    assert result.ok and "disk sentinel" not in result.output
    if params:
        assert "乙\n" in result.output
        assert result.result_envelope["read_window"]["total_chars"] == 6
    else:
        assert result.output.startswith("1: 甲乙\n2: 第二行\nfile_version=")
        assert result.result_envelope["read_window"]["total_lines"] == 2
    assert result.result_envelope["file_version"].startswith("file-v1:")


# LLM: 精确核对正文长度、续读游标和检查次数。
# 函数用途: 验证长行仍受原输出预算约束。
def test_injected_snapshot_retains_long_line_cursor_and_output_budget(tmp_path):
    (tmp_path / "text.txt").write_text("disk", encoding="utf-8")
    snapshot = _Snapshot("甲" * 100)
    result = _read_tool(tmp_path, provider=_Provider(snapshot), max_chars=10).execute({"path": "text.txt"})
    assert result.ok and result.output.startswith("1: " + "甲" * 7 + "\n")
    assert result.result_envelope["read_window"] == {
        "kind": "char_window", "offset": 0, "chars": 7, "next_offset": 7,
        "total_chars": 100, "complete": False,
    }
    assert "next_offset=7" in result.output and snapshot.checks == 2


@pytest.mark.parametrize("params", [{}, {"offset": 0}])
# LLM: 空快照沿默认空文件合同，不额外读取或改变标记。
# 函数用途: 比较空文件的默认和注入结果。
def test_empty_snapshot_preserves_empty_file_projection(tmp_path, params):
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    expected = ReadFileTool(tmp_path, 100).execute({"path": "empty.txt", **params})
    actual = _read_tool(tmp_path, provider=_Provider(_Snapshot(""))).execute({"path": "empty.txt", **params})
    assert actual.ok and actual.output == expected.output
    assert actual.result_envelope == expected.result_envelope


@pytest.mark.parametrize("params", [{}, {"offset": 0}, {"max_chars": 2}])
# LLM: 读后一致性故障不能变成空内容成功。
# 函数用途: 验证快照失效仍走原错误合同。
def test_snapshot_consistency_failure_remains_a_read_failure(tmp_path, params):
    (tmp_path / "text.txt").write_text("disk", encoding="utf-8")
    result = _read_tool(tmp_path, provider=_Provider(_Snapshot("hello", fail_check=2))).execute(
        {"path": "text.txt", **params})
    assert not result.ok and result.error_code == "TOOL_EXECUTION_FAILED"
    assert "snapshot changed" in result.output
    assert "file_version" not in result.result_envelope


@pytest.mark.parametrize("params, called", [({"start_line": 0}, True), ({"offset": -1}, False),
                                           ({"start_line": 2, "end_line": 1}, True)])
# LLM: 两种窗口的校验顺序原本不同，逐项保留。
# 函数用途: 验证非法参数不重排快照获取。
def test_file_argument_validation_order_is_unchanged(tmp_path, params, called):
    (tmp_path / "text.txt").write_text("disk", encoding="utf-8")
    provider = _Provider(_Snapshot("one\ntwo"))
    result = _read_tool(tmp_path, provider=provider).execute({"path": "text.txt", **params})
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert bool(provider.targets) is called


@pytest.mark.parametrize("params", [{}, {"offset": 1}])
# LLM: 提供者成功也不能绕过真实文件的外层版本门。
# 函数用途: 验证注入读取仍拒绝版本冲突。
def test_outer_file_version_check_remains_authoritative(tmp_path, params):
    target = tmp_path / "text.txt"
    target.write_text("before", encoding="utf-8")

    # LLM: 只修改临时测试文件，主动制造真实版本冲突。
    # 函数用途: 返回快照前替换磁盘内容。
    def provider(path):
        assert path == target
        path.write_text("after replacement", encoding="utf-8")
        return _Snapshot("snapshot")

    result = _read_tool(tmp_path, provider=provider).execute({"path": "text.txt", **params})
    assert not result.ok and result.error_code == "STALE_VERSION" and result.retryable


@pytest.mark.parametrize("denial", ["owner", "dangerous", "symlink"])
@pytest.mark.parametrize("params", [{}, {"offset": 0}])
@pytest.mark.parametrize("injected", [False, True])
# LLM: 使用真实路径策略；危险根场景不设 owner 墙，拒绝必须早于默认索引或宿主提供者。
# 函数用途: 验证行页和字符页均不能绕过 owner、危险根或符号链接拒绝。
def test_path_denial_happens_before_snapshot_provider(tmp_path, monkeypatch, denial, params, injected):
    owner = tmp_path / "owner"
    owner.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    target = outside
    access = {"owner_scope_root": str(owner)}
    if denial == "dangerous":
        # owner 自己家优先放行；用 normal 模式的外部路径触发真正的危险根拒绝。
        access = {"path_dangerous_roots": [str(outside)]}
    elif denial == "symlink":
        target = owner / "link.txt"
        target.symlink_to(outside)
    provider = _Provider(_Snapshot("must not leak"))
    monkeypatch.setattr(reads, "text_file_index", lambda *_: pytest.fail("denied path reached default lookup"))
    result = _read_tool(owner, provider=provider if injected else None, **access).execute(
        {"path": str(target), **params})
    assert not result.ok
    assert result.error_code == ("PATH_DANGEROUS_ROOT_BLOCKED" if denial == "dangerous"
                                 else "PATH_OWNER_SCOPE_BLOCKED")
    assert provider.targets == [] and "must not leak" not in result.output


@pytest.mark.parametrize("params", [{}, {"offset": 0}])
@pytest.mark.parametrize("injected", [False, True])
# LLM: 固定基线 owner 墙优先于危险根的语义；不得为了拒绝用例变绿而改变生产策略。
# 函数用途: 验证默认和注入读取都继续允许 owner 自己家的普通文本。
def test_owner_home_precedence_over_dangerous_roots_is_unchanged(tmp_path, params, injected):
    owner = tmp_path / "owner"
    owner.mkdir()
    target = owner / "text.txt"
    target.write_text("owner data", encoding="utf-8")
    provider = _Provider(_Snapshot("owner data"))
    tool = _read_tool(owner, provider=provider if injected else None,
                      owner_scope_root=str(owner), path_dangerous_roots=[str(target)])
    result = tool.execute({"path": str(target), **params})
    assert result.ok and "owner data" in result.output
    assert provider.targets == ([target] if injected else [])


# LLM: 同一工具先拒绝，再只添加宿主授权根。
# 函数用途: 验证墙外注入读取仍需要正式授权。
def test_only_host_granted_external_root_can_reach_snapshot_provider(tmp_path):
    owner, external = tmp_path / "owner", tmp_path / "external"
    owner.mkdir()
    external.mkdir()
    target = external / "text.txt"
    target.write_text("source", encoding="utf-8")
    provider = _Provider(_Snapshot("permitted"))
    tool = _read_tool(owner, provider=provider, owner_scope_root=str(owner))
    assert not tool.execute({"path": str(target)}).ok
    assert not provider.targets
    tool.granted_external_roots = (external,)
    result = tool.execute({"path": str(target)})
    assert result.ok and "permitted" in result.output and provider.targets == [target]


@pytest.mark.parametrize("kind", ["missing", "directory"])
# LLM: 无效文件目标必须在提供者前返回原错误码。
# 函数用途: 验证不能凭空读文件或把目录当正文。
def test_missing_or_directory_path_does_not_call_snapshot_provider(tmp_path, kind):
    target = tmp_path / "target"
    if kind == "directory":
        target.mkdir()
    provider = _Provider(_Snapshot("must not read"))
    result = _read_tool(tmp_path, provider=provider).execute({"path": str(target)})
    assert not result.ok and provider.targets == []
    assert result.error_code == ("PATH_IS_DIRECTORY" if kind == "directory" else "PATH_NOT_FOUND")


class _Process:
    # LLM: 两次 poll 故意可不同，以发现错误缓存。
    # 函数用途: 保存退出观察序列。
    def __init__(self, events, polls=(None, 7)):
        self.events = events
        self.polls = iter(polls)

    # LLM: 每次独立取值，多余调用会直接失败。
    # 函数用途: 记录一次退出观察。
    def poll(self):
        self.events.append("poll")
        return next(self.polls)


class _ReadableSession:
    """刻意不含 PtySession 的锁、FD、command 等不可读侧状态。"""

    # LLM: 不创建真实 FD 或进程，权限键只供查找检查。
    # 函数用途: 构造只读会话替身。
    def __init__(self, events, scope, polls=(None, 7)):
        self.events = events
        self.process = _Process(events, polls)
        self.access_scope = scope
        self.source_configurations = ()

    # LLM: 非法 UTF-8 字节钉住原替换解码和游标合同。
    # 函数用途: 记录读取并返回固定输出。
    def read(self, cursor, max_bytes):
        self.events.append(("read", cursor, max_bytes))
        return b"hello\xff", 9, True


class _Resolver:
    # LLM: 解析器保存完整权限键，不凭句柄名授权。
    # 函数用途: 保存会话和查找日志。
    def __init__(self, session, events):
        self.session = session
        self.events = events
        self.calls = []

    # LLM: 假值解析器仍须使用，不能回退全局注册表。
    # 函数用途: 模拟假值宿主解析器。
    def __bool__(self):
        return False

    # LLM: 可信宿主实现负责整体比较权限键。
    # 函数用途: 返回当前范围内会话或 None。
    def __call__(self, session_id, access_scope):
        self.events.append("lookup")
        self.calls.append((session_id, access_scope))
        if self.session is not None and session_id == "pty-unit" and access_scope == self.session.access_scope:
            return self.session
        return None


# LLM: owner 根由正式 Shell 宿主选项提供，不由模型构造。
# 函数用途: 构造当前测试的终端工具。
def _terminal(root):
    return pty.TerminalSessionTool(ShellTool(root, options=ShellToolOptions(
        workspace_roots=[root], owner_scope_root=str(root))))


@pytest.mark.parametrize("injected", [False, True])
# LLM: 整数转换有观察日志，状态与退出码要求独立 poll。
# 函数用途: 验证两条读取路径严格保留原调用顺序。
def test_read_lookup_validation_read_and_two_independent_polls(tmp_path, monkeypatch, injected):
    tool = _terminal(tmp_path)
    params = {"session_id": "  pty-unit  "}
    events = []

    class Integer:
        # LLM: 构造时不写事件，避免混淆参数校验位置。
        # 函数用途: 保存转换标签和值。
        def __init__(self, label, value):
            self.label, self.value = label, value

        # LLM: 仅处理器真正转换时记录事件。
        # 函数用途: 观察整数校验顺序。
        def __int__(self):
            events.append(self.label)
            return self.value

    params.update(cursor=Integer("cursor", 3), max_bytes=Integer("max_bytes", 4))
    scope = tool._access_scope(params)
    resolver = _Resolver(_ReadableSession(events, scope), events)
    if injected:
        monkeypatch.setattr(pty.pty_session_registry, "get", lambda *_: pytest.fail("registry fallback used"))
        result = tool._read(params, session_resolver=resolver)
    else:
        monkeypatch.setattr(pty.pty_session_registry, "get", resolver)
        result = tool.execute({"action": "read", **params})
    assert result.ok
    assert resolver.calls == [("pty-unit", scope)]
    assert events == ["lookup", "cursor", "max_bytes", ("read", 3, 4), "poll", "poll"]
    assert json.loads(result.output) == {
        "status": "running", "session_id": "pty-unit", "exit_code": 7,
        "output": "hello\ufffd", "cursor": 9, "truncated_before_cursor": True,
    }


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("found", [False, True])
@pytest.mark.parametrize("invalid", [{"cursor": "bad"}, {"max_bytes": [1]}, {"max_bytes": "bad"}])
# LLM: 缺失先于参数错误，两者都不能读取或 poll。
# 函数用途: 验证原错误优先级。
def test_missing_session_precedes_invalid_arguments_without_read_or_poll(tmp_path, monkeypatch, injected, found, invalid):
    tool = _terminal(tmp_path)
    params = {"session_id": "pty-unit", **invalid}
    events = []
    session = _ReadableSession(events, tool._access_scope(params)) if found else None
    resolver = _Resolver(session, events)
    if injected:
        result = tool._read(params, session_resolver=resolver)
    else:
        monkeypatch.setattr(pty.pty_session_registry, "get", resolver)
        result = tool._read(params)
    assert not result.ok
    assert result.error_code == ("TOOL_INVALID_ARGUMENTS" if found else "PROCESS_NOT_FOUND")
    assert events == ["lookup"]


@pytest.mark.parametrize("cursor, max_bytes, expected", [
    (None, None, (0, 32_000)), (-2, -1, (-2, 1)), (0, 0, (0, 32_000)),
    ("2", "999999999", (2, 1_000_000)), (True, True, (1, 1)),
])
# LLM: 固定 None、负数、零、字符串、布尔的历史强制转换。
# 函数用途: 验证游标转换和读取预算兼容。
def test_injected_read_preserves_integer_coercion_and_byte_limits(tmp_path, cursor, max_bytes, expected):
    tool = _terminal(tmp_path)
    params = {"session_id": "pty-unit", "cursor": cursor, "max_bytes": max_bytes}
    events = []
    resolver = _Resolver(_ReadableSession(events, tool._access_scope(params), (5, 5)), events)
    result = tool._read(params, session_resolver=resolver)
    assert result.ok and events == ["lookup", ("read", *expected), "poll", "poll"]
    assert json.loads(result.output)["status"] == "exited"
    assert json.loads(result.output)["exit_code"] == 5


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("changed", ["owner_home", "conversation_id", "write_roots", "read_roots",
                                      "protected_write_paths", "write_scope_denied_roots", "network_access",
                                      "protected_persona_root"])
# LLM: 每次只变一个范围字段，默认注册表和合规解析器都应拒绝。
# 函数用途: 验证完整权限键的传递和拒绝。
def test_pty_read_cannot_cross_any_access_scope_dimension(tmp_path, monkeypatch, injected, changed):
    tool = _terminal(tmp_path)
    params = {"session_id": "pty-unit", "__run_scope": {"session_id": "thread-a"}}
    scope = tool._access_scope(params)
    value = getattr(scope, changed)
    different = not value if isinstance(value, bool) else (("different",) if value is None or isinstance(value, tuple)
                                                         else "different")
    events = []
    session = _ReadableSession(events, replace(scope, **{changed: different}))
    if injected:
        resolver = _Resolver(session, events)
        result = tool._read(params, session_resolver=resolver)
        assert resolver.calls == [("pty-unit", scope)]
    else:
        registry = pty.PtySessionRegistry()
        registry._sessions["pty-unit"] = session
        monkeypatch.setattr(pty, "pty_session_registry", registry)
        result = tool.execute({"action": "read", **params})
    assert not result.ok and result.error_code == "PROCESS_NOT_FOUND"
    assert all(item == "lookup" for item in events)


# LLM: 不替换 get，由真实注册表完成权限与来源检查。
# 函数用途: 验证匹配范围仍可读取。
def test_default_registry_lookup_with_matching_scope_still_returns_session(tmp_path, monkeypatch):
    tool = _terminal(tmp_path)
    params = {"action": "read", "session_id": "pty-unit"}
    scope = tool._access_scope(params)
    events = []
    registry = pty.PtySessionRegistry()
    registry._sessions["pty-unit"] = _ReadableSession(events, scope, (None, None))
    monkeypatch.setattr(pty, "pty_session_registry", registry)
    result = tool.execute(params)
    assert result.ok and json.loads(result.output)["status"] == "running"
    assert events == [("read", 0, 32_000), "poll", "poll"]


# LLM: 每次调用绑定当前注册表，不捕获旧默认值。
# 函数用途: 验证显式 None 的默认兼容路径。
def test_explicit_none_session_resolver_uses_current_registry_lookup(tmp_path, monkeypatch):
    tool = _terminal(tmp_path)
    events = []
    resolver = _Resolver(None, events)
    monkeypatch.setattr(pty.pty_session_registry, "get", resolver)
    result = tool._read({"session_id": "missing"}, session_resolver=None)
    assert result.error_code == "PROCESS_NOT_FOUND" and events == ["lookup"]


@pytest.mark.parametrize("injected", [False, True])
# LLM: 两条路径都使用真实注册表 get；来源失效时不得读取或 poll。
# 函数用途: 验证注册表来源有效性门保持生效。
def test_registry_source_invalidation_precedes_read(tmp_path, monkeypatch, injected):
    tool = _terminal(tmp_path)
    params = {"session_id": "pty-unit"}
    events = []
    registry = pty.PtySessionRegistry()
    scope = tool._access_scope(params)
    registry._sessions["pty-unit"] = _ReadableSession(events, scope)
    monkeypatch.setattr(pty, "pty_session_registry", registry)
    monkeypatch.setattr(pty, "process_source_configurations_current", lambda *args, **kwargs: False)
    monkeypatch.setattr(registry, "close", lambda *args: events.append(("retire", args)))
    result = tool._read(params, session_resolver=registry.get) if injected else tool._read(params)
    assert not result.ok and result.error_code == "PROCESS_NOT_FOUND"
    assert events == [("retire", ("pty-unit", scope))]


# LLM: 对象故意没有权限键、来源、FD 或锁；权限筛选仍由可信解析器承担。
# 函数用途: 验证读取只依赖最小只读协议。
def test_injected_resolver_accepts_minimal_read_only_session(tmp_path):
    tool = _terminal(tmp_path)
    params = {"session_id": "pty-unit"}
    events = []
    session = SimpleNamespace(process=_Process(events, (None, None)),
                              read=lambda cursor, max_bytes: (b"minimal", 7, False))

    # LLM: 可信宿主核对完整 scope 后交付最小只读对象。
    # 函数用途: 解析当前测试的最小会话。
    def resolver(session_id, access_scope):
        assert session_id == "pty-unit" and access_scope == tool._access_scope(params)
        events.append("lookup")
        return session

    result = tool._read(params, session_resolver=resolver)
    assert result.ok and json.loads(result.output)["output"] == "minimal"
    assert events == ["lookup", "poll", "poll"]


# LLM: 直接解析注解，不能退化成具体索引或可写会话类型。
# 函数用途: 核对公开注入协议签名。
def test_protocol_signatures_expose_only_minimal_host_contracts():
    assert get_type_hints(reads.TextSnapshotProvider.__call__) == {
        "target": Path, "return": reads.TextReadSnapshot,
    }
    assert get_type_hints(pty.PtyReadableSessionResolver.__call__) == {
        "session_id": str, "access_scope": pty.PtyAccessScope,
        "return": pty.PtyReadableSession | None,
    }


# LLM: 精确比较属性集合，并要求禁止额外 JSON 字段。
# 函数用途: 验证模型看不到注入参数。
def test_model_schemas_do_not_expose_host_injection():
    assert set(ReadFileTool.model_spec.input_schema["properties"]) == {
        "path", "start_line", "end_line", "offset", "max_chars",
    }
    assert set(pty.TerminalSessionTool.model_spec.input_schema["properties"]) == {
        "action", "session_id", "command", "working_dir", "data", "append_newline",
        "cursor", "max_bytes", "columns", "rows",
    }
    assert ReadFileTool.model_spec.input_schema["additionalProperties"] is False
    assert pty.TerminalSessionTool.model_spec.input_schema["additionalProperties"] is False


# LLM: 直接调用 execute 也不能把伪造 JSON 字段当宿主实现。
# 函数用途: 验证模型参数不能选提供者或解析器。
def test_json_arguments_cannot_select_host_providers(tmp_path, monkeypatch):
    (tmp_path / "text.txt").write_text("real source", encoding="utf-8")
    provider = _Provider(_Snapshot("forged source"))
    result = ReadFileTool(tmp_path, 100).execute({"path": "text.txt", "snapshot_provider": provider})
    assert result.ok and "real source" in result.output and not provider.targets
    tool = _terminal(tmp_path)
    events = []
    injected = _Resolver(_ReadableSession(events, tool._access_scope({})), events)
    monkeypatch.setattr(pty.pty_session_registry, "get", lambda *_: None)
    missing = tool.execute({"action": "read", "session_id": "pty-unit", "session_resolver": injected})
    assert missing.error_code == "PROCESS_NOT_FOUND" and injected.calls == []
