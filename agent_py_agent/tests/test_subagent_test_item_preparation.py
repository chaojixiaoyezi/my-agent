"""测试子代理验收测试项目录推断。"""

from agent_py_agent.agent.subagents.execution_test_items import (
    TestItemPreparationRequest,
    prepare_test_items,
)


def test_prepare_test_items_infers_single_artifact_working_dir(tmp_path):
    target_dir = tmp_path / "strings"
    target_dir.mkdir()
    artifact = target_dir / "test_string_tools.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "test_string_tools.py",
                "validation_method": "command",
                "command": "python -m unittest discover -s . -p 'test_*.py'",
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["working_dir"] == "strings"


def test_prepare_test_items_keeps_explicit_working_dir(tmp_path):
    target_dir = tmp_path / "strings"
    target_dir.mkdir()
    artifact = target_dir / "test_string_tools.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "test_string_tools.py",
                "validation_method": "command",
                "command": "python -m unittest discover -s . -p 'test_*.py'",
                "working_dir": ".",
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["working_dir"] == "."


def test_prepare_test_items_ignores_out_of_workspace_artifacts(tmp_path):
    outside = tmp_path.parent / "test_outside.py"

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "test_outside.py",
                "validation_method": "command",
                "command": "python -m unittest discover -s . -p 'test_*.py'",
            }],
            output={"artifacts": [{"path": str(outside)}]},
            workspace_root=tmp_path,
        )
    )

    assert "working_dir" not in prepared[0]


def test_prepare_test_items_keeps_workspace_cwd_when_command_names_relative_artifact(tmp_path):
    target_dir = tmp_path / "grandchild_sorting_edge"
    target_dir.mkdir()
    artifact = target_dir / "test_sorting_edges.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "test_sorting_edges.py",
                "validation_method": "command",
                "command": "python -m pytest grandchild_sorting_edge/test_sorting_edges.py -v",
            }],
            output={"artifacts": [{"path": "grandchild_sorting_edge/test_sorting_edges.py"}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["working_dir"] == "."


def test_prepare_test_items_recovers_nested_relative_artifact_command_cwd(tmp_path):
    target_dir = tmp_path / "run-1" / "grandchild_sorting_edge"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "test_sorting_edges.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "test_sorting_edges.py",
                "validation_method": "command",
                "command": "python -m pytest grandchild_sorting_edge/test_sorting_edges.py -v",
            }],
            output={"artifacts": [{"path": "grandchild_sorting_edge/test_sorting_edges.py"}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["working_dir"] == "run-1"


def test_prepare_test_items_converts_safe_cd_chain_into_working_dir(tmp_path):
    """LLM: Verifies model-style cd && pytest commands become bounded cwd plus plain command."""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "test_solution.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "pytest test_solution.py",
                "validation_method": "command",
                "command": f"cd {target_dir} && python3 -m pytest test_solution.py -v",
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["command"] == "python3 -m pytest test_solution.py -v"
    assert prepared[0]["working_dir"] == "deliverables/leaf"


def test_prepare_test_items_strips_safe_cd_chain_even_with_working_dir(tmp_path):
    """LLM: Verifies explicit working_dir does not leave a redundant shell cd chain in command."""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "test_solution.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "pytest test_solution.py",
                "validation_method": "command",
                "command": f"cd {target_dir} && python3 -m pytest test_solution.py -v",
                "working_dir": str(target_dir),
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["command"] == "python3 -m pytest test_solution.py -v"
    assert prepared[0]["working_dir"] == str(target_dir)


def test_prepare_test_items_infers_pytest_when_runner_only_reports_test_artifact(tmp_path):
    """LLM: Verifies missing tests can still execute workspace-local test_*.py artifacts."""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "test_solution.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[],
            output={"artifacts": [{"path": str(artifact), "kind": "test"}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared == [{
        "name": "artifact pytest test_solution.py",
        "validation_method": "command",
        "command": "python3 -m pytest test_solution.py -q",
        "working_dir": "deliverables/leaf",
    }]


def test_prepare_test_items_normalizes_pytest_method_with_command(tmp_path):
    """LLM: Verifies runner `validation_method=pytest` remains executable by TestExecutor."""
    target_dir = tmp_path / "deliverables" / "leaf"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "test_solution.py"
    artifact.write_text("pass\n", encoding="utf-8")

    prepared = prepare_test_items(
        TestItemPreparationRequest(
            tests=[{
                "name": "pytest alias",
                "validation_method": "pytest",
                "command": "python3 -m pytest test_solution.py -q",
            }],
            output={"artifacts": [{"path": str(artifact)}]},
            workspace_root=tmp_path,
        )
    )

    assert prepared[0]["validation_method"] == "command"
    assert prepared[0]["working_dir"] == "deliverables/leaf"
