"""一键容器安装器：默认镜像构建、自检和透明 CLI 包装器。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def test_container_install_builds_probes_and_writes_transparent_wrapper(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "fake-container-runtime"
    log = tmp_path / "runtime.log"
    runtime.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >>\"$FAKE_RUNTIME_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path),
            "FAKE_RUNTIME_LOG": str(log),
            "MYAGENT_SRC": str(repo),
            "MYAGENT_HOME_DIR": str(home),
            "MYAGENT_BIN_DIR": str(bin_dir),
            "MYAGENT_CONTAINER_RUNTIME": str(runtime),
            "MYAGENT_IMAGE": "my-agent:test",
        }
    )

    result = subprocess.run(
        ["bash", str(repo / "install.sh"), "--container"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    wrapper = (bin_dir / "my-agent").read_text(encoding="utf-8")
    assert "runtime run --rm" in wrapper
    assert "--read-only" in wrapper
    assert "seccomp=$SECCOMP_PROFILE" in wrapper
    assert "--volume \"$workspace:/workspace\"" in wrapper
    assert (home / "container-security" / "seccomp-bwrap.json").is_file()
    calls = log.read_text(encoding="utf-8")
    assert "build -f" in calls
    assert "agent_py_agent.agent.tooling.sandbox --quiet" in calls


def test_bwrap_seccomp_profile_keeps_default_deny_and_narrow_namespace_allowlist() -> None:
    repo = Path(__file__).resolve().parents[2]
    profile = json.loads((repo / "deploy" / "seccomp-bwrap.json").read_text(encoding="utf-8"))

    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    extra = profile["syscalls"][0]
    assert extra["action"] == "SCMP_ACT_ALLOW"
    assert set(extra["names"]) == {
        "clone",
        "clone3",
        "mount",
        "pivot_root",
        "umount",
        "umount2",
        "unshare",
    }
    worker = (repo / "deploy" / "k8s" / "worker.yaml").read_text(encoding="utf-8")
    assert "type: Localhost" in worker
    assert "localhostProfile: my-agent/seccomp-bwrap.json" in worker
