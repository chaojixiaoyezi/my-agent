from __future__ import annotations

import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # Python 3.10 backport


def test_pyproject_exposes_my_agent_console_script():
    project_root = Path(__file__).resolve().parents[2]
    data = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["name"] == "my-agent"
    assert data["project"]["scripts"]["my-agent"] == "agent_py_agent.__main__:main"
    assert "prompt_toolkit>=3.0" in data["project"]["dependencies"]

    from agent_py_agent.__main__ import main

    assert callable(main)


def test_workspace_root_resolves_relative_to_config_file():
    from agent_py_agent.__main__ import resolve_workspace_root
    from agent_py_agent.agent.config import load_config

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        config_path = root / "agent_config.yaml"
        config_path.write_text('workspace_root: "fixture"\n', encoding="utf-8")

        config = load_config(config_path)
        assert resolve_workspace_root(config, config_path) == (root / "fixture").resolve()
