from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.tooling.write_boundary import (
    _MAX_BOUNDARY_PATH_CHARS,
    WRITE_TOOL_NAMES,
    _boundary_paths,
    _display_path,
    _is_relative_to,
    _path_text,
    _resolve_boundary_path,
    validate_write_boundary,
)


class TestConstants:
    def test_write_tool_names(self):
        assert "write_file" in WRITE_TOOL_NAMES
        assert "append_file" in WRITE_TOOL_NAMES
        assert "replace_in_file" in WRITE_TOOL_NAMES

    def test_max_boundary_path_chars(self):
        assert _MAX_BOUNDARY_PATH_CHARS == 4096


class TestPathText:
    def test_path_text_valid_string(self):
        result = _path_text("/some/path")
        assert result == "/some/path"

    def test_path_text_with_whitespace(self):
        result = _path_text("  /some/path  ")
        assert result == "/some/path"

    def test_path_text_valid_path_object(self):
        result = _path_text(Path("/some/path"))
        assert result == "/some/path"

    def test_path_text_none_raises(self):
        with pytest.raises(ValueError, match="参数缺失"):
            _path_text(None)

    def test_path_text_invalid_type_raises(self):
        with pytest.raises(ValueError, match="必须是字符串路径"):
            _path_text(123)

    def test_path_text_empty_string_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            _path_text("   ")

    def test_path_text_too_long_raises(self):
        long_path = "a" * (_MAX_BOUNDARY_PATH_CHARS + 1)
        with pytest.raises(ValueError, match="过长"):
            _path_text(long_path)

    def test_path_text_control_char_raises(self):
        with pytest.raises(ValueError, match="控制字符"):
            _path_text("/path/with\x00null")


class TestIsRelativeTo:
    def test_is_relative_to_true(self):
        assert _is_relative_to(Path("/a/b/c"), Path("/a/b")) is True

    def test_is_relative_to_false(self):
        assert _is_relative_to(Path("/a/b"), Path("/a/b/c")) is False

    def test_is_relative_to_sibling(self):
        assert _is_relative_to(Path("/a/x"), Path("/a/y")) is False

    def test_is_relative_to_exact_match(self):
        path = Path("/a/b")
        assert _is_relative_to(path, path) is True


class TestDisplayPath:
    def test_display_path_relative(self, tmp_path):
        result = _display_path(tmp_path / "subdir" / "file.txt", tmp_path)
        assert result == f"subdir{Path('/')}file.txt"

    def test_display_path_absolute(self, tmp_path):
        other = Path("/completely/different")
        result = _display_path(other, tmp_path)
        assert result == str(other)


class TestBoundaryPaths:
    def test_boundary_paths_empty_input(self, tmp_path):
        result = _boundary_paths(None, tmp_path)
        assert result == []

    def test_boundary_paths_non_list(self, tmp_path):
        result = _boundary_paths("not a list", tmp_path)
        assert result == []

    def test_boundary_paths_valid_list(self, tmp_path):
        subdir = tmp_path / "allowed"
        subdir.mkdir()
        result = _boundary_paths(["allowed"], tmp_path)
        assert len(result) == 1
        assert result[0] == subdir.resolve()

    def test_boundary_paths_skips_invalid(self, tmp_path):
        result = _boundary_paths([None, 123], tmp_path)
        assert result == []


class TestResolveBoundaryPath:
    def test_resolve_relative_to_workspace(self, tmp_path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        result = _resolve_boundary_path("subdir", tmp_path)
        assert result == subdir.resolve()

    def test_resolve_absolute_path(self, tmp_path):
        abs_path = tmp_path / "abs.txt"
        result = _resolve_boundary_path(str(abs_path), tmp_path)
        assert result == abs_path.resolve()

    def test_resolve_strict_false_path(self, tmp_path):
        """Non-absolute path inside workspace resolves even if path doesn't exist."""
        result = _resolve_boundary_path("nonexistent_subdir/file.txt", tmp_path)
        assert result is not None
        assert "nonexistent_subdir" in str(result)

    def test_resolve_outside_workspace_raises(self, tmp_path):
        with pytest.raises(ValueError, match="超出允许的工作区"):
            _resolve_boundary_path(str(Path.home()), tmp_path)


class TestValidateWriteBoundaryNonWriteTools:
    def test_non_write_tool_returns_empty(self, tmp_path):
        result = validate_write_boundary(
            "read_file",
            {"path": "/some/path"},
            workspace_root=tmp_path,
            write_boundary={"allowed_write_roots": ["/tmp"]},
        )
        assert result == ""

    def test_none_write_boundary_returns_empty(self, tmp_path):
        result = validate_write_boundary(
            "write_file",
            {"path": "/some/path"},
            workspace_root=tmp_path,
            write_boundary=None,
        )
        assert result == ""


class TestValidateWriteBoundaryInvalidParams:
    def test_non_dict_params_returns_error(self, tmp_path):
        result = validate_write_boundary(
            "write_file",
            "not a dict",
            workspace_root=tmp_path,
            write_boundary={},
        )
        assert "必须是 JSON 对象" in result


class TestValidateWriteBoundaryAllowedRoots:
    def test_missing_allowed_write_roots(self, tmp_path):
        result = validate_write_boundary(
            "write_file",
            {"path": "file.txt"},
            workspace_root=tmp_path,
            write_boundary={},
        )
        assert "没有配置 allowed_write_roots" in result

    def test_path_outside_allowed_roots(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        result = validate_write_boundary(
            "write_file",
            {"path": "/tmp/forbidden.txt"},
            workspace_root=tmp_path,
            write_boundary={"allowed_write_roots": [str(allowed)]},
        )
        assert "超出" in result or "不在" in result

    def test_path_inside_allowed_roots(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "file.txt"
        result = validate_write_boundary(
            "write_file",
            {"path": str(target)},
            workspace_root=tmp_path,
            write_boundary={"allowed_write_roots": [str(allowed)]},
        )
        assert result == ""


class TestValidateWriteBoundaryForbiddenRoots:
    def test_path_in_forbidden_root_blocked(self, tmp_path):
        forbidden = tmp_path / "forbidden"
        forbidden.mkdir()
        target = forbidden / "file.txt"
        result = validate_write_boundary(
            "write_file",
            {"path": str(target)},
            workspace_root=tmp_path,
            write_boundary={
                "allowed_write_roots": [str(tmp_path)],
                "forbidden_write_roots": [str(forbidden)],
            },
        )
        assert "落在 forbidden_write_roots 内" in result

    def test_forbidden_inside_allowed_still_blocks(self, tmp_path):
        """When forbidden is a subdirectory of allowed, the forbidden rule wins."""
        allowed = tmp_path / "project"
        allowed.mkdir()
        forbidden_sub = allowed / "secret"
        forbidden_sub.mkdir()
        target = forbidden_sub / "file.txt"
        result = validate_write_boundary(
            "write_file",
            {"path": str(target)},
            workspace_root=tmp_path,
            write_boundary={
                "allowed_write_roots": [str(allowed)],
                "forbidden_write_roots": [str(forbidden_sub)],
            },
        )
        assert "落在 forbidden_write_roots 内" in result


class TestValidateWriteBoundaryLockedFiles:
    def test_locked_file_blocked(self, tmp_path):
        locked = tmp_path / "locked.txt"
        locked.write_text("content")
        result = validate_write_boundary(
            "write_file",
            {"path": str(locked)},
            workspace_root=tmp_path,
            write_boundary={
                "allowed_write_roots": [str(tmp_path)],
                "locked_files": [str(locked)],
            },
        )
        assert "已被 locked_files 锁定" in result

    def test_file_in_locked_directory_blocked(self, tmp_path):
        locked_dir = tmp_path / "locked_dir"
        locked_dir.mkdir()
        locked_file = locked_dir / "file.txt"
        result = validate_write_boundary(
            "write_file",
            {"path": str(locked_file)},
            workspace_root=tmp_path,
            write_boundary={
                "allowed_write_roots": [str(tmp_path)],
                "locked_files": [str(locked_dir)],
            },
        )
        assert "已被 locked_files 锁定" in result

    def test_unlocked_file_allowed(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        unlocked = allowed / "unlocked.txt"
        result = validate_write_boundary(
            "write_file",
            {"path": str(unlocked)},
            workspace_root=tmp_path,
            write_boundary={
                "allowed_write_roots": [str(allowed)],
                "locked_files": ["/other/locked"],
            },
        )
        assert result == ""


class TestValidateWriteBoundaryEdgeCases:
    def test_no_path_param_allowed(self, tmp_path):
        result = validate_write_boundary(
            "write_file",
            {},
            workspace_root=tmp_path,
            write_boundary={"allowed_write_roots": [str(tmp_path)]},
        )
        assert result == ""

    def test_multiple_allowed_roots(self, tmp_path):
        allowed1 = tmp_path / "dir1"
        allowed2 = tmp_path / "dir2"
        allowed1.mkdir()
        allowed2.mkdir()
        target = allowed2 / "file.txt"
        result = validate_write_boundary(
            "write_file",
            {"path": str(target)},
            workspace_root=tmp_path,
            write_boundary={
                "allowed_write_roots": [str(allowed1), str(allowed2)],
            },
        )
        assert result == ""

    def test_all_write_tools_validated(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "file.txt"
        for tool in WRITE_TOOL_NAMES:
            result = validate_write_boundary(
                tool,
                {"path": str(target)},
                workspace_root=tmp_path,
                write_boundary={"allowed_write_roots": [str(allowed)]},
            )
            assert result == "", f"Tool {tool} should pass"
