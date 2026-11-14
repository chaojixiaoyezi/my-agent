"""MC4-2201：两真实子进程、三个崩溃切点，保留 review22 的调度和业务断言。"""
from __future__ import annotations

import json
import select
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store import curator_commit as cc
from agent_py_agent.tests import test_review18_contracts as original


def _service(root):
    output = json.loads((root / "output.json").read_text())
    store = original.fixtures.ConversationStore(root / "conversations")
    return original.fixtures._service(root, original.fixtures._StaticStructuredBackend(output), store)


def _emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def _signal(child):
    child.stdin.write("continue\n")
    child.stdin.flush()


def _receive(child):
    assert select.select([child.stdout], [], [], 20)[0], "child progress timeout"
    line = child.stdout.readline()
    assert line, (child.poll(), child.stderr.read())
    return json.loads(line)


def _stage_crashed_transaction(root: Path, cut: int) -> Path:
    """在尚未构建待测恢复服务前制造崩溃遗留事务与过期租约，返回 manifest 路径。"""
    store, thread, message = original.fixtures._conversation(root)
    output = original.fixtures._valid_output(thread.thread_id, message.message_id, message.content)
    (root / "output.json").write_text(json.dumps(output))
    return _spawn_crash_process(root, cut)


def _spawn_crash_process(root: Path, cut: int) -> Path:
    child = subprocess.Popen(
        [sys.executable, str(Path(original.__file__).resolve()), "crash", str(root), str(cut)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = child.communicate(timeout=20)
    assert child.returncode == 73, (child.returncode, stdout, stderr)
    manifests = list((root / "memory" / "curator" / "transactions").glob("*/manifest.json"))
    assert len(manifests) == 1
    return manifests[0]


def _contender(root, control):
    mode, name, seam, expired = control
    load, cleanup = cc._load_manifest, cc._cleanup_transaction
    class_restore = cc.CuratorBatchCommitter._restore_transaction
    restores = []

    def loaded(path, memory_root):
        manifest = load(path, memory_root)
        _emit("manifest", run_id=manifest["run_id"])
        sys.stdin.readline()
        return manifest

    def init_restoring(committer_self, manifest, *, paths_locked):
        restores.append(manifest["run_id"])
        return class_restore(committer_self, manifest, paths_locked=paths_locked)

    if mode == "init":
        # #21 Option A：构造阶段自动调用 recover_incomplete()，在构造前挂好并发观察探针。
        cc._load_manifest = loaded
        cc.CuratorBatchCommitter._restore_transaction = init_restoring
        _service(root)
        _emit("init_done", restores=restores)
        sys.stdin.readline()
        return

    # 事务目录为空时先行构造两个空闲服务并挂接钩子，再由父进程触发并发崩溃现场进入 run()。
    service = _service(root)
    restore, execute = service.committer._restore_transaction, service._execute

    def restoring(manifest, *, paths_locked):
        restores.append(manifest["run_id"])
        return restore(manifest, paths_locked=paths_locked)

    def cleaning(path):
        if name == "A" and seam == "cleanup_gap":
            _emit("cleanup")
            sys.stdin.readline()
        return cleanup(path)

    def executing(context):
        _emit("execute", lease_id=context.lease_id)
        sys.stdin.readline()
        return execute(context)

    cc._load_manifest, cc._cleanup_transaction = loaded, cleaning
    service.committer._restore_transaction, service._execute = restoring, executing
    _emit("ready")
    sys.stdin.readline()
    now = datetime.now(timezone.utc) + timedelta(hours=2) if expired else None
    result = service.run(reason="admin", now=now)
    _emit("result", status=result.status, failure_code=result.failure_code,
          restores=restores, backend_calls=service.backend.calls)
    sys.stdin.readline()  # 决策被父进程收齐前保持胜者存活，不能把胜者误当死租约。


@contextmanager
def _children(root, control):
    mode, seam, expired = control
    children = [subprocess.Popen(
        [sys.executable, __file__, str(root), mode, name, seam, str(int(expired))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for name in ("A", "B")]
    try:
        yield children
    finally:
        for child in children:
            stdout, stderr = child.communicate("\n" * 8, timeout=25)
            assert child.returncode == 0, (child.returncode, stdout, stderr)


def _decisions(children):
    ready = [_receive(child) for child in children]
    assert [row["event"] for row in ready] == ["manifest", "manifest"]
    _signal(children[0])
    first = _receive(children[0])
    _signal(children[1])
    second = _receive(children[1])
    if first["event"] == "cleanup":
        _signal(children[0])
        first = _receive(children[0])
    if second["event"] == "execute":
        _signal(children[1])
        second = _receive(children[1])
    if first["event"] == "execute":
        _signal(children[0])
        first = _receive(children[0])
    assert first["event"] == second["event"] == "result", (first, second)
    return [first, second]


@pytest.mark.parametrize("case", [(cut, seam, expired)
    for cut in (1, 2, 3) for seam in ("loaded_manifest", "cleanup_gap")
    for expired in (False, True)])
def test_two_services_restore_and_take_over_only_once(tmp_path, case):
    cut, seam, expired = case
    store, thread, message = original.fixtures._conversation(tmp_path)
    output = original.fixtures._valid_output(thread.thread_id, message.message_id, message.content)
    (tmp_path / "output.json").write_text(json.dumps(output))
    with _children(tmp_path, ("run", seam, expired)) as children:
        ready = [_receive(child) for child in children]
        assert [row["event"] for row in ready] == ["ready", "ready"]
        manifest = _spawn_crash_process(tmp_path, cut)
        for child in children:
            _signal(child)
        rows = _decisions(children)
        records = original.fixtures.CuratorRunLog(tmp_path / "memory" / "curator" / "runs").list()
        assert all(row["status"] in {"busy", "succeeded"} for row in rows), rows
        assert sum(len(row["restores"]) for row in rows) == 1, rows
        assert sum(row["status"] == "succeeded" for row in rows) == 1, rows
        assert sum(row["backend_calls"] for row in rows) == 1, rows
        assert sum(r.status == "recovered_rollback" for r in records) == 1
        assert not manifest.exists()


def test_two_services_init_recover_incomplete_only_once(tmp_path):
    """#21 Option A：两个新启动的 MemoryCuratorService 同时执行 __init__ recover_incomplete() 时只能恢复一次。"""
    manifest = _stage_crashed_transaction(tmp_path, 1)
    with _children(tmp_path, ("init", "loaded_manifest", False)) as children:
        discovered = [_receive(child) for child in children]
        assert [row["event"] for row in discovered] == ["manifest", "manifest"]
        _signal(children[0])
        first = _receive(children[0])
        _signal(children[1])
        second = _receive(children[1])
        assert first["event"] == second["event"] == "init_done", (first, second)
        assert sum(len(row["restores"]) for row in (first, second)) == 1, (first, second)
        records = original.fixtures.CuratorRunLog(tmp_path / "memory" / "curator" / "runs").list()
        assert sum(r.status == "recovered_rollback" for r in records) == 1
        assert not manifest.exists()
    post_service = _service(tmp_path)
    assert post_service.run(reason="admin").status == "succeeded"


if __name__ == "__main__":
    _contender(Path(sys.argv[1]), (sys.argv[2], sys.argv[3], sys.argv[4], bool(int(sys.argv[5]))))
