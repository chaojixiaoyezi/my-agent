"""发布干净度 gate：工作树未跟踪文件、运行目录可见性和真实制品内容。"""

from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path

from scripts.check_clean_package import (
    check_directory_findings,
    check_tarball_findings,
    check_zip_findings,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "tracked.txt").write_text("source", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    return root


def test_worktree_rejects_untracked_files_and_reports_size(tmp_path) -> None:
    root = _repo(tmp_path)
    payload = root / "data" / "audit" / "events.ndjson"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x" * 123)

    findings = check_directory_findings(root, runtime_warning_bytes=1)

    untracked = [item for item in findings if item.code == "UNTRACKED_FILE"]
    assert untracked[0].path == "data/audit/events.ndjson"
    assert untracked[0].size_bytes == 123
    assert any(item.code == "RUNTIME_DATA_PRESENT" and item.path == "data" for item in findings)


def test_ignored_large_runtime_directory_stays_visible_as_warning(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("data/\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    payload = root / "data" / "runtime.bin"
    payload.parent.mkdir()
    payload.write_bytes(b"x" * 64)

    findings = check_directory_findings(root, runtime_warning_bytes=1)

    assert not any(item.code == "UNTRACKED_FILE" for item in findings)
    warning = next(item for item in findings if item.code == "RUNTIME_DATA_PRESENT")
    assert warning.path == "data" and warning.size_bytes >= 64


def test_tar_artifact_rejects_runtime_data(tmp_path) -> None:
    archive_path = tmp_path / "release.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        content = b"private runtime state"
        info = tarfile.TarInfo("my-agent-1.0/data/sessions/user.json")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))

    findings = check_tarball_findings(archive_path)

    assert any(item.code == "RUNTIME_DATA_IN_ARTIFACT" for item in findings)


def test_wheel_artifact_rejects_oversized_member(tmp_path) -> None:
    archive_path = tmp_path / "my_agent.whl"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("agent_py_agent/large.bin", b"x" * 32)

    findings = check_zip_findings(archive_path, max_artifact_bytes=128, max_member_bytes=16)

    assert any(item.code == "ARTIFACT_MEMBER_TOO_LARGE" for item in findings)
