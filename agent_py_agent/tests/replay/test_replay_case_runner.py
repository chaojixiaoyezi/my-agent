from __future__ import annotations

from pathlib import Path


# LLM: Replay cases should be runnable from declarative specs so real failures become durable regression assets.
# 函数用途: 验证 replay spec runner 能遍历当前所有 golden trace 规格并全部通过。
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
