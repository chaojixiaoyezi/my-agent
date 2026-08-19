from pathlib import Path

from agent_py_agent.agent.verification.project_facts import (
    classify_verification_command,
    project_facts_for,
)


def _python_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n\n[tool.pytest.ini_options]\naddopts = '-q'\n",
        encoding="utf-8",
    )
    return root


def test_project_facts_use_exact_manifest_markers(tmp_path: Path):
    root = _python_project(tmp_path)

    facts = project_facts_for(root / "tests")

    assert facts is not None
    assert facts.root == root.resolve()
    assert facts.verify_commands == ("pytest",)


def test_python_verification_classifies_full_and_targeted_scope(tmp_path: Path):
    root = _python_project(tmp_path)

    full = classify_verification_command(
        "python -m pytest -q",
        cwd=root,
        exit_code=0,
        output="24 passed",
    )
    targeted = classify_verification_command(
        "pytest tests/test_demo.py::test_one -q",
        cwd=root,
        exit_code=1,
        output="1 failed",
    )

    assert full is not None
    assert (full.scope, full.status, full.kind) == ("full", "passed", "test")
    assert targeted is not None
    assert (targeted.scope, targeted.status, targeted.exit_code) == ("targeted", "failed", 1)


def test_arbitrary_or_chained_command_never_becomes_verification(tmp_path: Path):
    root = _python_project(tmp_path)

    arbitrary = classify_verification_command(
        "python -c 'print(1)'",
        cwd=root,
        exit_code=0,
        output="1",
    )
    hidden_failure = classify_verification_command(
        "pytest; echo done",
        cwd=root,
        exit_code=0,
        output="failed\ndone",
    )

    assert arbitrary is None
    assert hidden_failure is None


def test_declared_package_script_is_detected_without_prompt_inference(tmp_path: Path):
    root = tmp_path / "frontend"
    root.mkdir()
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run","lint":"eslint .","serve":"vite"}}',
        encoding="utf-8",
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

    facts = project_facts_for(root)
    result = classify_verification_command(
        "pnpm test src/example.test.ts",
        cwd=root,
        exit_code=0,
        output="passed",
    )

    assert facts is not None
    assert facts.verify_commands == ("pnpm run test", "pnpm run lint")
    assert result is not None
    assert (result.canonical_command, result.scope) == ("pnpm run test", "targeted")


def test_go_manifest_exposes_build_and_test_verification_without_prompt_inference(
    tmp_path: Path,
):
    root = tmp_path / "go-project"
    root.mkdir()
    (root / "go.mod").write_text("module example.test/demo\n\ngo 1.21\n", encoding="utf-8")

    facts = project_facts_for(root)
    full_test = classify_verification_command(
        "go test -v . 2>&1",
        cwd=root,
        exit_code=0,
        output="PASS",
    )
    targeted_build = classify_verification_command(
        "go build ./cmd/demo",
        cwd=root,
        exit_code=1,
        output="build failed",
    )

    assert facts is not None
    assert facts.verify_commands == ("go test", "go build")
    assert full_test is not None
    assert (full_test.kind, full_test.scope, full_test.status) == (
        "test",
        "full",
        "passed",
    )
    assert targeted_build is not None
    assert (targeted_build.kind, targeted_build.scope, targeted_build.status) == (
        "build",
        "targeted",
        "failed",
    )
