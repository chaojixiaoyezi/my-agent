from __future__ import annotations

from pathlib import Path


def test_replay_case_runner_passes_all_specs(tmp_path: Path):
    from agent_py_agent.tests.support.replay_case_runner import run_replay_case

    specs = sorted((Path(__file__).parent / "specs").glob("*.json"))
    assert specs

    failures: list[str] = []
    for spec in specs:
        result = run_replay_case(spec, tmp_path / spec.stem)
        if not result.ok:
            failures.append(f"{spec.name}: {result.errors}")

    assert failures == []
