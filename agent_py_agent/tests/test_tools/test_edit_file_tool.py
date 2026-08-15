"""edit_file 工具钉子(P0-1 对照能力补齐:str_replace + 三级容错匹配)。

钉死契约:精确替换;多处命中需 replace_all;空白容错(行trim/全空白)按
真实文本替换;new 空串=删除;找不到给可操作错误;复用写入边界(原子写)。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool


def _tool(tmp: Path) -> EditFileTool:
    return EditFileTool(tmp)


def test_exact_single_replace(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\ntimeout = 30\ny = 2\n", encoding="utf-8")
    res = _tool(tmp_path).execute({"path": "a.py", "old_string": "timeout = 30", "new_string": "timeout = 60"})
    assert res.ok and "替换 1 处" in res.output
    assert f.read_text(encoding="utf-8") == "x = 1\ntimeout = 60\ny = 2\n"


def test_ambiguous_without_replace_all_errors(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("dup\ndup\n", encoding="utf-8")
    res = _tool(tmp_path).execute({"path": "b.txt", "old_string": "dup", "new_string": "x"})
    assert not res.ok and "不唯一" in res.output
    assert f.read_text(encoding="utf-8") == "dup\ndup\n", "歧义时不得改文件"


def test_replace_all(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("debug: true\nmode: true\n", encoding="utf-8")
    res = _tool(tmp_path).execute({"path": "c.yaml", "old_string": "true", "new_string": "false", "replace_all": True})
    assert res.ok and "替换 2 处" in res.output
    assert f.read_text(encoding="utf-8") == "debug: false\nmode: false\n"


def test_delete_via_empty_new(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("keep\nremove me\n", encoding="utf-8")
    res = _tool(tmp_path).execute({"path": "d.txt", "old_string": "remove me\n", "new_string": ""})
    assert res.ok
    assert "remove me" not in f.read_text(encoding="utf-8")


def test_exact_substring_preserves_indent(tmp_path):
    # 单行 old 是缩进行的子串 → 精确子串替换,周围(含缩进)天然不动
    f = tmp_path / "e0.py"
    f.write_text("def foo():\n        return 1\n", encoding="utf-8")
    res = _tool(tmp_path).execute({"path": "e0.py", "old_string": "return 1", "new_string": "return 2"})
    assert res.ok
    assert f.read_text(encoding="utf-8") == "def foo():\n        return 2\n"


def test_fuzzy_line_trim_match_reindents(tmp_path):
    # 多行 + 模型给无缩进版(精确子串失配)→ 行trim 容错命中,reindent 补回文件 8 空格缩进
    f = tmp_path / "e.py"
    f.write_text("if x:\n        a = 1\n        b = 2\n", encoding="utf-8")
    res = _tool(tmp_path).execute({"path": "e.py", "old_string": "a = 1\nb = 2", "new_string": "a = 100\nb = 200"})
    assert res.ok and "line_trimmed" in res.output
    assert f.read_text(encoding="utf-8") == "if x:\n        a = 100\n        b = 200\n", "容错命中后必须保留原缩进"


def test_fuzzy_whitespace_normalized_match(tmp_path):
    # 行内空白不一致(多个空格 vs 单空格)→ 全空白归一命中
    f = tmp_path / "f.py"
    f.write_text("a   =    1 + 2\n", encoding="utf-8")
    res = _tool(tmp_path).execute({"path": "f.py", "old_string": "a = 1 + 2", "new_string": "a = 99"})
    assert res.ok and "whitespace_normalized" in res.output
    assert f.read_text(encoding="utf-8") == "a = 99\n"


def test_not_found_gives_actionable_error(tmp_path):
    f = tmp_path / "g.txt"
    f.write_text("hello\n", encoding="utf-8")
    res = _tool(tmp_path).execute({"path": "g.txt", "old_string": "nonexistent", "new_string": "x"})
    assert not res.ok and "read_file" in res.output


def test_missing_file_and_identical_strings(tmp_path):
    miss = _tool(tmp_path).execute({"path": "none.txt", "old_string": "a", "new_string": "b"})
    assert not miss.ok and "不存在" in miss.output
    (tmp_path / "h.txt").write_text("a\n", encoding="utf-8")
    same = _tool(tmp_path).execute({"path": "h.txt", "old_string": "a", "new_string": "a"})
    assert not same.ok and "相同" in same.output
