"""选项1-B:凭据文件(.env 家族/凭据存储)文件工具一律拒,.env.example 放行(安全模板)。"""
from __future__ import annotations

from agent.path_access_policy import PathAccessPolicy, _is_credential_filename


def test_credential_filename_detection():
    for yes in (".env", ".env.local", ".env.production", ".env.staging", ".git-credentials",
                "auth.json", ".anthropic_oauth.json", ".netrc", ".pgpass"):
        assert _is_credential_filename(yes) is True, yes
    for no in (".env.example", "normal.txt", "app.py", "config.yaml", "readme.md", ""):
        assert _is_credential_filename(no) is False, no


def test_policy_blocks_env_file(tmp_path):
    pol = PathAccessPolicy.from_values()
    d = pol.check(tmp_path / "project" / ".env")
    assert d.allowed is False and d.code == "PATH_CREDENTIAL_FILE_BLOCKED"
    # .env.example 放行
    assert pol.check(tmp_path / "project" / ".env.example").allowed is True
    # 普通文件放行
    assert pol.check(tmp_path / "project" / "app.py").allowed is True
