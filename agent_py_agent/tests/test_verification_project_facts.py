from pathlib import Path

import pytest

from agent_py_agent.agent.verification.project_facts import (
    ENVIRONMENT_UNAVAILABLE,
    classify_verification_commands,
    project_facts_for,
)


# 函数用途: 单条命令的测试便捷读取——恰好一条证据时返回它，没有证据时返回 None。
def _one(command, **kwargs):
    events = classify_verification_commands(command, **kwargs)
    assert len(events) <= 1, events
    return events[0] if events else None


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

    full = _one(
        "python -m pytest -q",
        cwd=root,
        exit_code=0,
        output="24 passed",
    )
    targeted = _one(
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

    arbitrary = _one(
        "python -c 'print(1)'",
        cwd=root,
        exit_code=0,
        output="1",
    )
    hidden_failure = _one(
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
    result = _one(
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
    full_test = _one(
        "go test -v . 2>&1",
        cwd=root,
        exit_code=0,
        output="PASS",
    )
    targeted_build = _one(
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


def test_leading_cd_into_an_existing_directory_is_classified_in_that_directory(tmp_path: Path):
    root = _python_project(tmp_path)

    absolute = _one(
        f"cd {root} && python3 -m pytest tests/ -v 2>&1",
        cwd=tmp_path,
        exit_code=1,
        output="2 failed, 2 passed",
    )
    relative = _one(
        'cd "project" && pytest -q',
        cwd=tmp_path,
        exit_code=0,
        output="4 passed",
    )

    assert absolute is not None and relative is not None
    assert (absolute.status, absolute.cwd, absolute.root) == ("failed", str(root.resolve()), str(root.resolve()))
    assert (relative.scope, relative.status, relative.cwd) == ("full", "passed", str(root.resolve()))


def test_cd_prefix_only_counts_for_one_command_into_a_real_directory(tmp_path: Path):
    root = _python_project(tmp_path)

    for command in (
        f"cd {root / 'missing'} && pytest -q",  # 项目内不存在的目录：cd 会失败，返回码不属于 pytest
        f"cd {root}; pytest -q",
        f"cd {root} || pytest -q",
    ):
        assert _one(command, cwd=tmp_path, exit_code=0, output="passed") is None, command


def test_pipes_and_background_runs_never_become_verification(tmp_path: Path):
    root = _python_project(tmp_path)

    for command in (
        "python -m pytest -q | head -60",
        "python -m pytest -q|tee log.txt",
        "pytest -q |& tee log.txt",
        "pytest -q &",
        f"cd {root} && pytest -q | tail -5",
    ):
        assert _one(command, cwd=root, exit_code=0, output="failed") is None, command
    quoted = _one('pytest -k "fast|slow" -q', cwd=root, exit_code=0, output="passed")
    assert quoted is not None and quoted.status == "passed", "引号内的 | 只是参数，不是管道"


def _make_project(tmp_path: Path) -> Path:
    root = tmp_path / "make-project"
    (root / ".git").mkdir(parents=True)
    (root / "Makefile").write_text("test:\n\tpytest -q\n\nlint:\n\tpyflakes .\n", encoding="utf-8")
    return root


def test_exit_codes_126_and_127_mean_the_verifier_did_not_run(tmp_path: Path):
    root = _python_project(tmp_path)

    missing = _one("python -m pytest -q", cwd=root, exit_code=127, output="python: command not found")
    not_executable = _one("pytest -q", cwd=root, exit_code=126, output="permission denied")
    failed = _one("pytest -q", cwd=root, exit_code=1, output="1 failed")

    assert (missing.status, missing.exit_code) == (ENVIRONMENT_UNAVAILABLE, 127)
    assert not_executable.status == ENVIRONMENT_UNAVAILABLE
    assert failed.status == "failed", "只有 126/127 表示没运行，普通非零仍是失败"


def test_pytest_scope_follows_argument_shape(tmp_path: Path):
    root = _python_project(tmp_path)
    cases = {
        "pytest -q": "full",
        "python3 -m pytest tests/ -v": "full",
        "pytest tests/unit/": "full",
        "pytest tests/test_demo.py": "targeted",
        "pytest tests/test_demo.py::test_one": "targeted",
        "pytest -k fast -q": "targeted",
        "pytest -kfast": "targeted",
        "pytest -m slow": "targeted",
        "pytest --lf": "targeted",
        "pytest --deselect=tests/test_demo.py::test_one": "targeted",
    }
    for command, scope in cases.items():
        assert _one(command, cwd=root, exit_code=0, output="passed").scope == scope, command


def test_and_chain_proves_every_verification_only_when_it_returns_zero(tmp_path: Path):
    root = _make_project(tmp_path)

    both = classify_verification_commands("make test && make lint", cwd=root, exit_code=0, output="ok")
    with_cd = classify_verification_commands(f"cd {root} && make test && make lint", cwd=tmp_path, exit_code=0, output="ok")
    with_echo = classify_verification_commands("make test && echo done", cwd=root, exit_code=0, output="ok")

    assert [(e.canonical_command, e.kind, e.status) for e in both] == [("make test", "test", "passed"), ("make lint", "lint", "passed")]
    assert [e.cwd for e in with_cd] == [str(root.resolve())] * 2
    assert [e.canonical_command for e in with_echo] == ["make test"], "非验证段不记证据，但整体为 0 仍证明测试通过"
    for command, code in (("make test && make lint", 2), ("make test; make lint", 0), ("make test || make lint", 0),
                          ("make test && make lint | tee log", 0)):
        assert classify_verification_commands(command, cwd=root, exit_code=code, output="") == [], command


@pytest.mark.parametrize("command", ["pytest\necho done", "pytest -q\r\necho done", "cd tests && pytest\necho done",
                                     "cd tests\n&& pytest", "cd tests &&\npytest"])
@pytest.mark.parametrize("exit_code", [0, 2])
def test_newline_separated_commands_never_become_verification(tmp_path: Path, command: str, exit_code: int):
    root = _python_project(tmp_path)

    # 换行就是 shell 的命令分隔符：返回码来自最后一条命令（`cd tests` 换行后的 `&& pytest` 甚至是语法错误），
    # 证明不了 pytest 通过或失败；cd 前缀部分出现换行同样整条不算。
    assert _one(command, cwd=root, exit_code=exit_code, output="done") is None


@pytest.mark.parametrize("command", [
    "pytest --help", "pytest -h", "pytest --version", "pytest -V", "pytest --collect-only", "pytest --co -q",
    "pytest --collectonly", "python -m pytest --collect-only tests/", "pytest --fixtures", "pytest --markers",
    "pytest --setup-only", "pytest --setup-plan",
])
def test_pytest_arguments_that_do_not_run_tests_never_become_verification(tmp_path: Path, command: str):
    root = _python_project(tmp_path)

    assert _one(command, cwd=root, exit_code=0, output="collected 3 items") is None


def test_other_ecosystems_drop_non_executing_flags_but_keep_real_runs(tmp_path: Path):
    cargo = tmp_path / "cargo-project"
    cargo.mkdir()
    (cargo / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    go = tmp_path / "go-project"
    go.mkdir()
    (go / "go.mod").write_text("module example.test/demo\n\ngo 1.21\n", encoding="utf-8")
    make = _make_project(tmp_path)

    for command, root in (("cargo test --no-run", cargo), ("cargo test -- --list", cargo), ("go test -list . ./...", go),
                          ("go test -list=Test ./...", go), ("go test -c", go), ("go test -n ./...", go),
                          ("go build -n ./...", go), ("go build -help", go), ("make test -n", make),
                          ("make test --dry-run", make), ("make test --help", make), ("make test -i", make),
                          ("make test --touch", make), ("make test -v", make)):
        assert _one(command, cwd=root, exit_code=0, output="ok") is None, command
    # 同名短参数在别的命令里含义不同：pytest -v 只是啰嗦输出、make -k 只是继续执行，仍是真实运行。
    for command, root in (("cargo test", cargo), ("go test ./...", go), ("go build ./...", go), ("make test", make),
                          ("make test -k", make), ("cargo test -- --nocapture", cargo)):
        assert _one(command, cwd=root, exit_code=0, output="ok").status == "passed", command
    assert _one("pytest -v", cwd=_python_project(tmp_path), exit_code=0, output="ok").status == "passed"
