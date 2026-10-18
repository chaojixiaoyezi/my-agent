"""共享初始化只消费原进程拒门；不初始化真实W、模型或服务。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.capability import builtin_seed, declarative_index
from agent_py_agent.agent.common import global_write_guard as guard
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
from agent_py_agent.agent.user_space import home_layout


# LLM: 比较仅覆盖本case合成的共享树，包含目录和完整字节；不用mtime证明没有写入，也不扫描日常home。
# 函数用途: 保存拒绝前后的合成文件与模式，检测播种、镜像删除和fingerprint覆盖。
def _snapshot(root: Path) -> dict[str, object]:
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): [path.stat().st_mode,
            None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()]
            for path in [root, *sorted(root.rglob("*"))]}


# LLM: 仅搭建临时输入，源目录与目标目录分开；不调用被测初始化器预先替它完成动作。
# 函数用途: 为六个真实共享写入口准备已有标记及一个变化的内置Skill源。
def _case(tmp_path, monkeypatch, operation):
    home = tmp_path / "home"
    (home / "templates").mkdir(parents=True)
    (home / "system").mkdir()
    mirror, custom, indexes = home / "shared/builtin", home / "shared/skills", home / "shared/indexes"
    for path in (mirror, custom, indexes, home / "cache"):
        path.mkdir(parents=True, exist_ok=True)
    (mirror / "old-marker").write_text("must remain", encoding="utf-8")
    (indexes / "skills.jsonl").write_text("old skill index\n", encoding="utf-8")
    (indexes / "role_templates.jsonl").write_text("old role index\n", encoding="utf-8")
    fingerprint = home / "cache/skill_index.fingerprint"
    fingerprint.write_text("old fingerprint", encoding="utf-8")
    src = tmp_path / "builtin-source"
    skill = src / "case/skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: synthetic-skill\ndescription: synthetic\n---\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    calls = {
        "ensure_home": (lambda: home_layout.ensure_my_agent_home(home), "shared_home_initialization"),
        "seed_text": (lambda: home_layout._write_seed_file(home / "templates/new.md", "new seed"), "shared_home_initialization"),
        "seed_json": (lambda: home_layout._write_seed_json(home / "system/new.json", {"seed": True}), "shared_home_initialization"),
        "mirror": (lambda: builtin_seed._mirror_builtin(src, mirror), "shared_builtin_skills"),
        "skill_index": (lambda: builtin_seed.sync_skill_index(mirror, custom, indexes / "skills.jsonl", fingerprint), "shared_skill_index"),
        "role_index": (lambda: declarative_index._write_index(indexes / "role_templates.jsonl", [{"id": "new"}]), "shared_declarative_index"),
    }
    return home, *calls[operation]


@pytest.mark.parametrize("actor", ["worker", "executor"])
@pytest.mark.parametrize("operation", ["ensure_home", "seed_text", "seed_json", "mirror", "skill_index", "role_index"])
def test_restricted_actor_rejects_shared_seed_before_any_mutation(tmp_path, monkeypatch, actor, operation):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    monkeypatch.setattr(guard, "_restricted_executor", False)
    home, invoke, resource = _case(tmp_path, monkeypatch, operation)
    if actor == "worker":
        launch = mode.WorkerLaunch(WorkerPartition(0, 2, "seed-guard"),
            tmp_path / "gateway", home, os.getppid())
        monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"])
        assert mode.establish_worker_mode(launch) == {"ok": True}
        expected = guard.GlobalWriteInWorkerError
    else:
        guard.restrict_executor_global_writes()
        expected = guard.GlobalWriteInExecutorError
    before = _snapshot(home)
    error = None
    try:
        invoke()
    except BaseException as failure:
        error = failure
    after = _snapshot(home)
    facts = {"actor": actor, "operation": operation,
             "error_type": type(error).__name__ if error is not None else None,
             "error_code": getattr(error, "error_code", None),
             "shared_tree_equal": before == after,
             "changed_paths": sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))}
    (tmp_path / "SHARED_SEED_FACTS.json").write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
    assert type(error) is expected, facts
    assert error.resource == resource and str(error) == error.error_code
    assert before == after, facts
