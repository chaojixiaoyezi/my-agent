from __future__ import annotations

import tempfile
import zipfile
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


def test_production_package_excludes_tests_and_dev_harnesses():
    project_root = Path(__file__).resolve().parents[2]
    data = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["tool"]["setuptools"]["include-package-data"] is False
    assert "agent_py_agent.tests*" in data["tool"]["setuptools"]["packages"]["find"]["exclude"]

    from package_boundary_policy import forbidden_distribution_member, is_dev_only_module

    assert is_dev_only_module("agent_py_agent.agent.contracts.offline_tool_contract")
    assert is_dev_only_module("agent_py_agent.cli.real_e2e_commands")
    assert not is_dev_only_module("agent_py_agent.agent.contracts.tool_gate")
    assert forbidden_distribution_member("agent_py_agent/tests/test_runtime.py")
    assert forbidden_distribution_member(
        "agent_py_agent/agent/contracts/offline_tool_contract.py"
    )
    assert not forbidden_distribution_member("agent_py_agent/agent/contracts/tool_gate.py")


def test_distribution_boundary_checks_real_archive_members(tmp_path):
    from scripts.check_distribution_boundary import forbidden_members

    wheel = tmp_path / "sample.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("agent_py_agent/agent/runtime.py", "")
        archive.writestr("agent_py_agent/tests/test_runtime.py", "")
        archive.writestr(
            "agent_py_agent/agent/contracts/offline_tool_contract.py",
            "",
        )

    assert forbidden_members(wheel) == [
        "agent_py_agent/agent/contracts/offline_tool_contract.py",
        "agent_py_agent/tests/test_runtime.py",
    ]


def test_distribution_boundary_rejects_members_missing_from_current_source(tmp_path):
    from scripts.check_distribution_boundary import source_missing_members

    source_root = tmp_path / "source"
    source_file = source_root / "agent_py_agent" / "agent" / "runtime.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("", encoding="utf-8")
    wheel = tmp_path / "sample.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("agent_py_agent/agent/runtime.py", "")
        archive.writestr("agent_py_agent/agent/deleted_workflow.py", "")
        archive.writestr("my_agent-0.3.0.dist-info/METADATA", "")

    assert source_missing_members(wheel, source_root) == [
        "agent_py_agent/agent/deleted_workflow.py"
    ]


def test_current_production_import_boundaries_have_no_unapproved_findings():
    from scripts.check_import_boundaries import check_import_boundaries

    project_root = Path(__file__).resolve().parents[2]
    assert check_import_boundaries(project_root) == []


def test_layer_boundary_rejects_new_reverse_import():
    from scripts.check_import_boundaries import _boundary_code

    assert (
        _boundary_code(
            "agent_py_agent.agent.tooling.new_tool",
            "agent_py_agent.agent.subagents.manager",
        )
        == "LAYER_BOUNDARY_FORBIDDEN"
    )


def test_workspace_root_resolves_relative_to_config_file():
    from agent_py_agent.agent.settings import load_config
    from agent_py_agent.cli.common import resolve_workspace_root

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        config_path = root / "agent_config.yaml"
        config_path.write_text('workspace_root: "fixture"\n', encoding="utf-8")

        config = load_config(config_path)
        assert resolve_workspace_root(config, config_path) == (root / "fixture").resolve()


def test_empty_workspace_root_resolves_to_current_working_directory():
    from agent_py_agent.agent.settings import load_config
    from agent_py_agent.cli.common import resolve_workspace_roots

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        config_path = root / "agent_config.yaml"
        current_dir = root / "task-workspace"
        current_dir.mkdir()
        config_path.write_text('workspace_root: ["", "/tmp/extra-my-agent-root"]\n', encoding="utf-8")

        config = load_config(config_path)
        roots = resolve_workspace_roots(config, config_path, current_dir=current_dir)

        assert roots[0] == current_dir.resolve()
        assert roots[1] == Path("/tmp/extra-my-agent-root").resolve()


def test_empty_workspace_root_defaults_to_process_cwd(monkeypatch):
    from agent_py_agent.agent.settings import load_config
    from agent_py_agent.cli.common import resolve_workspace_root

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        config_path = root / "agent_config.yaml"
        current_dir = root / "isolated-task"
        current_dir.mkdir()
        config_path.write_text('workspace_root: ""\n', encoding="utf-8")
        monkeypatch.chdir(current_dir)

        config = load_config(config_path)

        assert resolve_workspace_root(config, config_path) == current_dir.resolve()


def test_foreign_windows_workspace_root_is_ignored_on_posix():
    from agent_py_agent.agent.settings import load_config
    from agent_py_agent.cli.common import resolve_workspace_roots

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        config_path = root / "agent_config.yaml"
        current_dir = root / "task-workspace"
        current_dir.mkdir()
        config_path.write_text('workspace_root: ["", "C:/Users/example/project"]\n', encoding="utf-8")

        config = load_config(config_path)
        roots = resolve_workspace_roots(config, config_path, current_dir=current_dir)

        assert roots == [current_dir.resolve()]


def test_all_full_suite_workflows_install_feature_test_extras() -> None:
    """Push 与手动完整套件必须共享同一 feature 依赖合同，防止 CI 环境漂移。"""

    project_root = Path(__file__).resolve().parents[2]
    expected = 'python3 -m pip install -e ".[dev,secrets,scale]"'
    for relative in (".github/workflows/test.yml", ".github/workflows/full-tests.yml"):
        workflow = (project_root / relative).read_text(encoding="utf-8")
        assert expected in workflow, f"{relative} 未安装完整 feature test extras"
