"""宿主数据私有写入第二批（pw2）回归：append 家族与后续组的私有写入。

锁住三件事：umask 0o022 下新建文件 0600、新建目录 0700；预置的 0644 文件在下次写入时被收紧，
而已存在的目录（含 0755）权限一律不动（pdp 2026-10-03：私有写只动自己建的东西，与锁收私同口径）；
写入内容与公开版本逐字节一致。A 组覆盖 json_io 私有原语与三个真实调用点
（background_jobs 登记、参数修改账本、persona 版本账本）。
"""
from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_py_agent.agent.common.json_io import (
    append_jsonl_capped,
    append_private_jsonl_capped,
    append_private_jsonl_records,
    read_jsonl_objects,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@contextmanager
def _umask(value: int) -> Iterator[None]:
    old = os.umask(value)
    try:
        yield
    finally:
        os.umask(old)


# ---- A 组：append 家族（json_io 私有原语 + 真实调用点）----


def test_private_capped_append_creates_private_file_and_dir(tmp_path: Path) -> None:
    target_dir = tmp_path / "ledger"
    path = target_dir / "events.jsonl"

    with _umask(0o022):
        append_private_jsonl_capped(path, {"n": 1}, max_records=10)

    assert _mode(path) == 0o600
    assert _mode(target_dir) == 0o700
    assert read_jsonl_objects(path) == [{"n": 1}]


def test_private_capped_append_tightens_existing_wide_file_and_keeps_dir_mode(tmp_path: Path) -> None:
    target_dir = tmp_path / "ledger"
    target_dir.mkdir()
    path = target_dir / "events.jsonl"
    path.write_text('{"n": 0}\n', encoding="utf-8")
    os.chmod(path, 0o644)
    os.chmod(target_dir, 0o755)

    with _umask(0o022):
        append_private_jsonl_capped(path, {"n": 1}, max_records=10)

    assert _mode(path) == 0o600
    # pdp 2026-10-03：已存在的目录一律不改权限（只动自己建的东西）；文件本身仍被收紧到 0600。
    assert _mode(target_dir) == 0o755
    assert [row["n"] for row in read_jsonl_objects(path)] == [0, 1]


def test_private_capped_append_keeps_last_n(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    for index in range(5):
        append_private_jsonl_capped(path, {"n": index}, max_records=3)
    assert [row["n"] for row in read_jsonl_objects(path)] == [2, 3, 4]


def test_private_capped_content_matches_public_capped(tmp_path: Path) -> None:
    public = tmp_path / "public.jsonl"
    private = tmp_path / "private.jsonl"

    for index in range(5):
        record = {"index": index, "note": "中文", "nested": {"b": [1, 2], "a": None}}
        append_jsonl_capped(public, record, max_records=3)
        append_private_jsonl_capped(private, record, max_records=3)

    assert private.read_bytes() == public.read_bytes()


def test_private_batch_append_creates_private_file(tmp_path: Path) -> None:
    target_dir = tmp_path / "versions"
    path = target_dir / "versions.jsonl"

    with _umask(0o022):
        append_private_jsonl_records(path, [{"v": 1}, {"v": 2}])

    assert _mode(path) == 0o600
    assert _mode(target_dir) == 0o700
    assert [row["v"] for row in read_jsonl_objects(path)] == [1, 2]


def test_private_batch_append_tightens_existing_wide_file(tmp_path: Path) -> None:
    path = tmp_path / "versions.jsonl"
    path.write_text('{"v": 0}\n', encoding="utf-8")
    os.chmod(path, 0o644)

    with _umask(0o022):
        append_private_jsonl_records(path, [{"v": 1}])

    assert _mode(path) == 0o600
    assert [row["v"] for row in read_jsonl_objects(path)] == [0, 1]


def test_background_job_registry_is_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling import shell

    jobs = tmp_path / ".background_jobs"
    jobs.mkdir()
    registry = jobs / "registry.jsonl"
    registry.write_text('{"pid": 1}\n', encoding="utf-8")
    os.chmod(registry, 0o644)
    os.chmod(jobs, 0o755)

    with _umask(0o022):
        shell._record_background_job(jobs, pid=2, command="echo hi", log_path=tmp_path / "1.log")

    assert _mode(registry) == 0o600
    assert [row["pid"] for row in read_jsonl_objects(registry)] == [1, 2]


def test_parameter_change_ledger_is_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.settings import parameter_changes as changes

    config = tmp_path / "desktop.yaml"
    config.write_text('agent_name: "myagent"\n', encoding="utf-8")
    paths = changes.WritePaths(user_path=config)

    with _umask(0o022):
        report = changes.set_parameter("max_tokens", "32768", paths=paths, origin=changes.ChangeOrigin("test"))

    assert report["ok"], report
    ledger = changes.ledger_path(config)
    assert _mode(ledger) == 0o600
    assert read_jsonl_objects(ledger)


def test_persona_versions_ledger_is_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.capability.persona_repository import (
        PersonaMutationRequest,
        PersonaRepository,
    )

    root = tmp_path / "owner"
    root.mkdir()
    for name, content in (
        ("SOUL.md", "# SOUL\n"),
        ("USER.md", "# USER\n"),
        ("AGENTS.md", "# AGENTS\n"),
    ):
        (root / name).write_text(content, encoding="utf-8")
    repository = PersonaRepository(
        owner_home=root,
        soul_path=root / "SOUL.md",
        user_path=root / "USER.md",
        agents_path=root / "AGENTS.md",
        prompt_max_chars=20_000,
    )

    with _umask(0o022):
        repository.mutate(
            PersonaMutationRequest(target="user", action="add", content="称呼:小叶子", confirmed=True)
        )

    assert repository.versions_path.is_file()
    assert _mode(repository.versions_path) == 0o600


# ---- B 组：gateway_parts/io 三个通用写函数（42 个调用方共用）----


def test_gateway_json_atomic_write_is_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    target_dir = tmp_path / "inbox"
    path = target_dir / "request.json"

    with _umask(0o022):
        write_json_file_atomic(path, {"id": "req-1"})

    assert _mode(path) == 0o600
    assert _mode(target_dir) == 0o700
    assert path.read_text(encoding="utf-8") == json.dumps({"id": "req-1"}, ensure_ascii=False, indent=2, sort_keys=True)

def test_gateway_json_atomic_write_tightens_existing_wide_file(tmp_path: Path) -> None:
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    target_dir = tmp_path / "inbox"
    target_dir.mkdir()
    path = target_dir / "request.json"
    path.write_text("{}\n", encoding="utf-8")
    os.chmod(path, 0o644)
    os.chmod(target_dir, 0o755)

    with _umask(0o022):
        write_json_file_atomic(path, {"id": "req-2"})

    assert _mode(path) == 0o600
    # pdp 2026-10-03：已存在的 0755 目录不改权限。
    assert _mode(target_dir) == 0o755


def test_gateway_update_json_atomic_is_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    target_dir = tmp_path / "conversations"
    path = target_dir / "state.json"
    target_dir.mkdir()
    path.write_text(json.dumps({"n": 0}), encoding="utf-8")
    os.chmod(path, 0o644)
    os.chmod(target_dir, 0o755)

    with _umask(0o022):
        updated = update_json_file_atomic(path, lambda state: {**state, "n": 1})

    assert updated == {"n": 1}
    assert _mode(path) == 0o600
    # pdp 2026-10-03：已存在的 0755 目录不改权限。
    assert _mode(target_dir) == 0o755
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}


def test_gateway_request_enqueue_is_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.gateway_parts.io import write_gateway_request
    from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

    root = tmp_path / "gateway"
    paths = GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "state.json",
        heartbeat=root / "heartbeat.json",
        stop_request=root / "stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "history.jsonl",
    )
    paths.inbox.mkdir(parents=True)
    os.chmod(paths.inbox, 0o755)

    with _umask(0o022):
        target = write_gateway_request(paths, {"id": "req-3", "goal": "demo"})

    assert target == paths.inbox / "req-3.json"
    assert _mode(target) == 0o600
    # pdp 2026-10-03：已存在的 0755 目录不改权限。
    assert _mode(paths.inbox) == 0o755
    assert json.loads(target.read_text(encoding="utf-8"))["id"] == "req-3"


# ---- C 组：subagents 工作区里的宿主状态文件 ----


def test_subagent_work_order_templates_are_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.utils import _write_if_missing, _write_json_if_missing

    work_dir = tmp_path / "work"
    text_path = work_dir / "DEBRIEF.md"
    json_path = work_dir / "output.json"

    with _umask(0o022):
        _write_if_missing(text_path, "# DEBRIEF\n\n")
        _write_json_if_missing(json_path, {"ok": True})

    assert _mode(text_path) == 0o600
    assert _mode(json_path) == 0o600
    assert _mode(work_dir) == 0o700
    assert text_path.read_text(encoding="utf-8") == "# DEBRIEF\n\n"
    assert json.loads(json_path.read_text(encoding="utf-8")) == {"ok": True}


def test_subagent_work_order_template_keeps_existing_wide_dir_mode(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.utils import _write_if_missing

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    os.chmod(work_dir, 0o755)
    path = work_dir / "DEBRIEF.md"

    with _umask(0o022):
        _write_if_missing(path, "new\n")

    # pdp 2026-10-03：已存在的 0755 目录不改权限；文件是新建，出生即 0600
    assert _mode(work_dir) == 0o755
    assert _mode(path) == 0o600
    assert path.read_text(encoding="utf-8") == "new\n"


def test_subagent_work_order_template_does_not_touch_existing_file(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.utils import _write_if_missing

    path = tmp_path / "DEBRIEF.md"
    path.write_text("old\n", encoding="utf-8")
    os.chmod(path, 0o644)

    _write_if_missing(path, "new\n")

    # 语义不变：文件已存在就完全不碰（既不改内容也不改权限）
    assert _mode(path) == 0o644
    assert path.read_text(encoding="utf-8") == "old\n"


def test_task_trash_manifest_is_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.task_trash import (
        TaskTrashMoveRequest,
        ensure_task_trash,
        move_to_task_trash,
    )

    task = tmp_path / "task"
    task.mkdir()
    source = task / "leftover.txt"
    source.write_text("x", encoding="utf-8")
    trash = ensure_task_trash(task)
    os.chmod(trash, 0o755)
    manifest = trash / "manifest.jsonl"
    os.chmod(manifest, 0o644)

    with _umask(0o022):
        result = move_to_task_trash(TaskTrashMoveRequest(task_dir=task, source_path="leftover.txt"))

    assert result.moved, result.blockers
    assert _mode(manifest) == 0o600
    # pdp 2026-10-03：已存在的 0755 目录不改权限。
    assert _mode(trash) == 0o755
    assert read_jsonl_objects(manifest)[0]["moved"] is True


def test_shell_gateway_audit_is_private(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.shell_gateway_execution import (
        ShellGatewayExecutionResult,
        _write_audit,
    )

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    audit = output_dir / "shell_gateway_audit.jsonl"
    audit.write_text('{"old": 1}\n', encoding="utf-8")
    os.chmod(audit, 0o644)
    os.chmod(output_dir, 0o755)
    result = ShellGatewayExecutionResult(decision={"argv": ["ls"], "run_id": "run-1", "request_id": "req-1"})

    with _umask(0o022):
        ref = _write_audit(output_dir, result)

    assert ref == str(audit)
    assert _mode(audit) == 0o600
    # pdp 2026-10-03：output_dir 是用户可见的命令产物目录，已存在就一位都不许动。
    assert _mode(output_dir) == 0o755
    rows = read_jsonl_objects(audit)
    assert rows[0] == {"old": 1}
    assert "argv" not in rows[1]["decision"]
