from __future__ import annotations

import tomllib
from pathlib import Path


def test_pyproject_exposes_my_agent_console_script():
    project_root = Path(__file__).resolve().parents[2]
    data = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["name"] == "my-agent"
    assert data["project"]["scripts"]["my-agent"] == "agent_py_agent.__main__:main"
    assert "prompt_toolkit>=3.0" in data["project"]["dependencies"]

    from agent_py_agent.__main__ import main

    assert callable(main)
