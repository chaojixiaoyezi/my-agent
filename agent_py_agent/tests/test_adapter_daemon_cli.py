from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.cli.adapter_daemon import print_daemon_adapter_status


def test_adapter_daemon_status_reports_bad_state_json(tmp_path, capsys):
    (tmp_path / "adapter_state.json").write_text("{bad json", encoding="utf-8")

    print_daemon_adapter_status(1234, tmp_path / "adapter.pid", SimpleNamespace(root=tmp_path))

    err = capsys.readouterr().err
    assert "adapter running: pid=1234" in err
    assert "state_load_error=cli.adapter_daemon.state.read" in err
