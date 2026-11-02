# LLM: 只用 pytest 临时 home 里的合成 owner 目录，经原 attach_run_task_workspace_context 建任务、
#   原 finish_run_task_workspace_if_needed 收口；沙箱 /tmp 里的链接、替换与目标链接都由测试在临时目录直接摆好，
#   受控替换只包住 os.stat/os.open 在指定文件名上先做一次替换再委托原调用，不替换发布实现。
# 模块用途: 回归 P1-R9-27——收口发布沙箱 /tmp 产物只经锚定 no-follow 描述符，链接/非普通/多链接与替换一律不发布，计数如实。
from __future__ import annotations

import json
import os
import random
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
    _publish_sandbox_tmp_outputs,
    attach_run_task_workspace_context,
    finish_run_task_workspace_if_needed,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

OUTSIDE_BYTES = b"outside-owner synthetic bytes\x00\x01"
_OK = SimpleNamespace(runtime_status="ok", runtime_reason="", runtime_source="")


# LLM: 合成普通 owner 与另一 owner 的私有文件；任务目录由原 attach 组件建立。
# 函数用途: 返回 agent、已绑定参数、任务根与另一 owner 的合成源文件。
def _task(tmp_path: Path, name: str = "publish"):
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", prompt_files=[], my_agent_home=str(tmp_path / "home"),
        my_agent_owner_provider="feishu", my_agent_owner_kind="user", my_agent_owner_id="ou_synthetic",
    ), tmp_path / "workspace")
    params = RunParams(save=True, source="cli_run", request_id=f"request-{name}", run_id=f"run-{name}",
                       task_id=f"task-{name}")
    bound = attach_run_task_workspace_context(agent, params, "合成沙箱产物发布任务")
    root = Path(bound.task_attributes["run_workspace"]["task_root"])
    other = resolve_owner_home(tmp_path / "home", OwnerIdentity.provider_user("feishu", "ou_other")).home_dir
    other.mkdir(parents=True, exist_ok=True)
    outside_file = other / "private.bin"
    outside_file.write_bytes(OUTSIDE_BYTES)
    return SimpleNamespace(agent=agent, bound=bound, root=root, outside_file=outside_file)


# 函数用途: 经原 finish 组件收口，返回结果根。
def _finish(case) -> str:
    return finish_run_task_workspace_if_needed(case.agent, case.bound, _OK)


# 函数用途: 读取任务 timeline 里的沙箱发布事件。
def _published_events(root: Path) -> list[dict]:
    timeline = root / "work" / "timeline.jsonl"
    if not timeline.exists():
        return []
    rows = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("event_type") == "sandbox_tmp_published"]


# 函数用途: 列出交付目录 .sandbox-tmp 下的全部普通文件相对路径。
def _delivered(root: Path) -> list[str]:
    base = root / "output" / ".sandbox-tmp"
    return sorted(str(path.relative_to(base)) for path in base.rglob("*") if path.is_file()) if base.is_dir() else []


# 函数用途: 确认任何交付文件都不含另一 owner 的合成字节。
def _no_outside_bytes_delivered(root: Path) -> bool:
    base = root / "output"
    return all(OUTSIDE_BYTES not in path.read_bytes() for path in base.rglob("*") if path.is_file() and not path.is_symlink())


# LLM: 正常嵌套文件、可执行位与 24 MiB 二进制按字节发布；大文件读取每次不超过 1 MiB，timeline 计数等于真实发布数。
# 函数用途: 验证常规嵌套、生产规模二进制与有界分块读取。
def test_nested_and_large_binary_outputs_publish_exactly(tmp_path, monkeypatch):
    case = _task(tmp_path)
    source = case.root / "work" / ".sandbox-tmp"
    big = random.Random(27).randbytes(24 * 1024 * 1024)
    (source / "proj" / "pkg").mkdir(parents=True)
    (source / "proj" / "pkg" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (source / "proj" / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (source / "proj" / "run.sh").chmod(0o755)
    (source / "big.bin").write_bytes(big)
    reads = []
    original_read = os.read

    def tracking_read(fd, length):
        if os.fstat(fd).st_size == len(big):
            reads.append(length)
        return original_read(fd, length)

    monkeypatch.setattr(os, "read", tracking_read)
    assert _finish(case) == str(case.root)
    monkeypatch.undo()

    target = case.root / "output" / ".sandbox-tmp"
    assert _delivered(case.root) == ["big.bin", "proj/pkg/main.py", "proj/run.sh"]
    assert (target / "big.bin").read_bytes() == big
    assert reads and max(reads) <= 1024 * 1024
    assert stat.S_IMODE((target / "proj" / "run.sh").stat().st_mode) == 0o755
    assert [event["count"] for event in _published_events(case.root)] == [3]
    assert not any(name.startswith(".") for name in os.listdir(target))


# LLM: 叶子链接、目录链接、多链接与 FIFO 都指向或代表不可发布的源；只有普通单链接文件被发布，计数只算它。
# 函数用途: 验证源链接、非普通与多链接条目不发布，另一 owner 字节不出现在交付目录。
def test_source_links_nonregular_and_multilink_entries_are_skipped(tmp_path):
    case = _task(tmp_path)
    source = case.root / "work" / ".sandbox-tmp"
    source.mkdir(parents=True)
    (source / "ok.txt").write_text("ok", encoding="utf-8")
    (source / "linked.txt").symlink_to(case.outside_file)
    (source / "linked-dir").symlink_to(case.outside_file.parent, target_is_directory=True)
    os.link(case.outside_file, source / "hard.bin")
    os.mkfifo(source / "pipe")

    _finish(case)

    assert _delivered(case.root) == ["ok.txt"]
    assert [event["count"] for event in _published_events(case.root)] == [1]
    assert _no_outside_bytes_delivered(case.root)


# LLM: 主位置任一祖先是链接属于不安全而非不存在：不得回落任务根旧位置，也不发布链接目标里的另一 owner 字节。
#   work 本身是链接时原 finish 资格检查先拒绝；直接调用两参发布函数同样不发布。
# 函数用途: 验证链接祖先的整体拒绝与不回落。
@pytest.mark.parametrize("linked", ["sandbox-tmp", "work"])
def test_linked_ancestor_is_refused_without_fallback(tmp_path, linked):
    case = _task(tmp_path)
    outside = case.outside_file.parent / "linked-tmp"
    (outside / ".sandbox-tmp").mkdir(parents=True)
    (outside / "outside.bin").write_bytes(OUTSIDE_BYTES)
    (outside / ".sandbox-tmp" / "outside.bin").write_bytes(OUTSIDE_BYTES)
    (case.root / ".sandbox-tmp").mkdir()
    (case.root / ".sandbox-tmp" / "fallback.txt").write_text("旧位置", encoding="utf-8")
    if linked == "sandbox-tmp":
        (case.root / "work" / ".sandbox-tmp").symlink_to(outside, target_is_directory=True)
        assert _finish(case) == str(case.root)
    else:
        work = case.root / "work"
        work.rename(case.root / "work-real")
        work.symlink_to(outside, target_is_directory=True)
        assert _finish(case) == ""
        assert _publish_sandbox_tmp_outputs(case.root, "run-direct") == []

    assert _delivered(case.root) == []
    assert _published_events(case.root) == [] or linked == "work"
    assert _no_outside_bytes_delivered(case.root)


# LLM: 主位置真实不存在才使用任务根旧位置；主位置是普通文件（不安全）时不回落。
# 函数用途: 验证兼容回落布局的准入条件与 timeline 来源。
@pytest.mark.parametrize("primary", ["absent", "regular-file"])
def test_fallback_layout_only_when_primary_is_absent(tmp_path, primary):
    case = _task(tmp_path)
    (case.root / ".sandbox-tmp" / "notes").mkdir(parents=True)
    (case.root / ".sandbox-tmp" / "notes" / "a.txt").write_text("hi", encoding="utf-8")
    if primary == "regular-file":
        (case.root / "work" / ".sandbox-tmp").write_text("not a directory", encoding="utf-8")

    _finish(case)

    if primary == "absent":
        assert _delivered(case.root) == ["notes/a.txt"]
        events = _published_events(case.root)
        assert [(event["count"], event["source"]) for event in events] == [(1, str(case.root / ".sandbox-tmp"))]
    else:
        assert _delivered(case.root) == [] and _published_events(case.root) == []


# LLM: 目标基目录链接、嵌套目录链接与叶子链接都不得被跟随：链接外文件逐字节不变，其余正常产物照常发布。
# 函数用途: 验证目标链接不被跟随且计数只含真正发布的文件。
@pytest.mark.parametrize("linked", ["base", "nested-dir", "leaf"])
def test_destination_links_are_not_followed(tmp_path, linked):
    case = _task(tmp_path)
    source = case.root / "work" / ".sandbox-tmp"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "a.txt").write_text("nested", encoding="utf-8")
    (source / "top.txt").write_text("top", encoding="utf-8")
    outside = case.outside_file.parent / "dest-outside"
    outside.mkdir()
    (outside / "top.txt").write_bytes(OUTSIDE_BYTES)
    target = case.root / "output" / ".sandbox-tmp"
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = {"base": [], "nested-dir": ["top.txt"], "leaf": ["nested/a.txt"]}[linked]
    if linked == "base":
        target.symlink_to(outside, target_is_directory=True)
    else:
        target.mkdir()
        link = target / ("nested" if linked == "nested-dir" else "top.txt")
        link.symlink_to(outside if linked == "nested-dir" else outside / "top.txt",
                        target_is_directory=linked == "nested-dir")

    _finish(case)

    assert sorted(os.listdir(outside)) == ["top.txt"] and (outside / "top.txt").read_bytes() == OUTSIDE_BYTES
    published = [str(path.relative_to(target)) for path in sorted(target.rglob("*"))
                 if not path.is_symlink() and path.is_file()] if linked != "base" else []
    assert published == expected
    assert [event["count"] for event in _published_events(case.root)] == ([len(expected)] if expected else [])


# LLM: 发现之后、打开之前被替换（链接或另一普通文件）的源一律不发布；包装只在 replaced.txt 的指定调用点先替换一次再委托原调用。
# 函数用途: 验证发现与打开之间的受控替换不会发布替换后的内容。
@pytest.mark.parametrize(("hook", "replacement"), [
    ("second-stat", "regular"), ("open", "regular"), ("open", "symlink"),
])
def test_replacement_between_discovery_and_open_is_not_published(tmp_path, monkeypatch, hook, replacement):
    case = _task(tmp_path)
    source = case.root / "work" / ".sandbox-tmp"
    source.mkdir(parents=True)
    replaced = source / "replaced.txt"
    replaced.write_text("原始内容", encoding="utf-8")
    (source / "z-ok.txt").write_text("ok", encoding="utf-8")
    seen = {"stat": 0, "swapped": False}

    def swap() -> None:
        staged = source / ".staged"
        if replacement == "symlink":
            staged.symlink_to(case.outside_file)
        else:
            staged.write_bytes(OUTSIDE_BYTES)
        os.replace(staged, replaced)
        seen["swapped"] = True

    original_stat, original_open = os.stat, os.open

    def hooked_stat(path, *args, **kwargs):
        if path == "replaced.txt" and kwargs.get("dir_fd") is not None and not seen["swapped"]:
            seen["stat"] += 1
            if hook == "second-stat" and seen["stat"] == 2:
                swap()
        return original_stat(path, *args, **kwargs)

    def hooked_open(path, flags, *args, **kwargs):
        if hook == "open" and path == "replaced.txt" and not flags & os.O_CREAT and not seen["swapped"]:
            swap()
        return original_open(path, flags, *args, **kwargs)

    # 原能力探测按函数对象查 supports_dir_fd；包装函数委托原调用，需登记为同等能力，否则发布按无能力平台整体不发布。
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, hooked_stat, hooked_open})
    monkeypatch.setattr(os, "stat", hooked_stat)
    monkeypatch.setattr(os, "open", hooked_open)
    _finish(case)
    monkeypatch.undo()

    assert seen["swapped"]
    assert _delivered(case.root) == ["z-ok.txt"]
    assert [event["count"] for event in _published_events(case.root)] == [1]
    assert _no_outside_bytes_delivered(case.root)
