# LLM: 只用配置与fake进程边界核拓扑来源，不启动Agent、服务、模型或工具。
# 模块用途: 防止worker count回落、private Namespace覆盖及服务选错配置。
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.settings import AgentConfig, load_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.cli import common, gateway_process, gateway_service


def _config(tmp_path, count=None):
    path = tmp_path / "chosen.yaml"
    lines = ["model_backend: echo", f'my_agent_home: "{tmp_path / "home"}"']
    if count is not None:
        lines.append(f"gateway_worker_process_count: {count}")
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.mark.parametrize("value", [0, 1, 2, 17])
def test_yaml_and_dataclass_share_exact_worker_count(tmp_path, value):
    assert load_config(_config(tmp_path, value)).gateway_worker_process_count == value
    assert AgentConfig(gateway_worker_process_count=value).gateway_worker_process_count == value


@pytest.mark.parametrize("raw", ["true", "false", "-1", "1.5", '"2"', "null", "[]"])
def test_invalid_yaml_never_silently_falls_back(tmp_path, raw):
    with pytest.raises(ValueError, match="gateway_worker_process_count"):
        load_config(_config(tmp_path, raw))


@pytest.mark.parametrize("value", [True, False, -1, 1.0, "2", None, []])
def test_invalid_programmatic_config_is_rejected(value):
    with pytest.raises(ValueError, match="gateway_worker_process_count"):
        AgentConfig(gateway_worker_process_count=value)


def test_default_and_parameter_registry_are_host_boundary(tmp_path):
    assert load_config(_config(tmp_path)).gateway_worker_process_count == 0
    spec = parameter_registry()["gateway_worker_process_count"]
    assert spec.default == 0 and not spec.writable and spec.source == "agent"


def test_public_freeze_ignores_injected_count_and_config(tmp_path):
    path = _config(tmp_path, 2)
    args = Namespace(config=path, workspace_root="", _gateway_worker_count=91,
                     _gateway_frozen_config=AgentConfig(gateway_worker_process_count=88))
    frozen = common.frozen_gateway_arguments(args)
    assert frozen._gateway_worker_count == 2
    assert frozen._gateway_frozen_config.gateway_worker_process_count == 2
    assert args._gateway_worker_count == 91
    assert args._gateway_frozen_config.gateway_worker_process_count == 88


def test_public_run_passes_yaml_count_to_original_entry(tmp_path, monkeypatch):
    from agent_py_agent.cli import gateway_coordinated_run

    path = _config(tmp_path, 2)
    seen = []
    monkeypatch.setattr(gateway_process, "mark_hosting_gateway_process", lambda: None)
    monkeypatch.setattr(gateway_process, "wait_for_predecessor_exit", lambda _: True)
    monkeypatch.setattr(gateway_coordinated_run, "run_coordinated_gateway",
                        lambda args, **kwargs: seen.append((args._gateway_worker_count, kwargs)) or 19)
    monkeypatch.setattr(gateway_process, "make_agent", Mock(side_effect=AssertionError("no Agent")))
    result = gateway_process.cmd_gateway_run(Namespace(config=path, workspace_root="", _gateway_worker_count=9))
    assert result == 19 and seen == [(2, {"predecessor_exited": True})]


@pytest.mark.parametrize("configured,observed", [(2, 0), (0, 2), (3, 2)])
def test_public_start_rejects_different_live_topology(tmp_path, monkeypatch, capsys, configured, observed):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    path = _config(tmp_path, configured)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    monkeypatch.setattr(gateway_process, "make_agent", lambda _: SimpleNamespace(config=load_config(path)))
    monkeypatch.setattr(gateway_process, "gateway_paths", lambda _: paths)
    monkeypatch.setattr(gateway_process, "get_running_pid", lambda _: 321)
    monkeypatch.setattr(gateway_process, "is_pid_alive", lambda _: True)
    monkeypatch.setattr(gateway_process, "read_json_file", lambda _: {"worker_count": observed})
    spawn = Mock(side_effect=AssertionError("no spawn or kill"))
    monkeypatch.setattr(gateway_process, "_spawn_gateway_process", spawn)
    monkeypatch.setattr(gateway_process, "terminate_pid", spawn)
    assert gateway_process.cmd_gateway_start(Namespace(config=path, force=True)) == 2
    assert "GATEWAY_TOPOLOGY_CHANGE_REQUIRED" in capsys.readouterr().err
    spawn.assert_not_called()


def test_service_formats_keep_selected_config_without_worker_flag(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    path = _config(tmp_path, 2)
    monkeypatch.setattr(gateway_service, "_service_working_directory", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(gateway_service, "_detect_venv_dir", lambda: None)
    monkeypatch.setattr(gateway_service, "_resolve_node_path", lambda: None)
    unit = gateway_service.generate_systemd_unit_text(config_path=path)
    plist = gateway_service.generate_launchd_plist_text(config_path=path)
    ET.fromstring(plist)
    assert str(path) in unit and str(path) in plist
    assert "--worker" not in unit and "--worker" not in plist


def test_install_passes_selected_config_before_effects(tmp_path, monkeypatch):
    path = _config(tmp_path, 2)
    install = Mock(return_value=True)
    monkeypatch.setattr(gateway_process, "install_service", install)
    assert gateway_process.cmd_gateway_install(Namespace(config=path, force=False)) == 0
    install.assert_called_once_with(force=False, config_path=Path(path))
    path.write_text("gateway_worker_process_count: true\n")
    with pytest.raises(ValueError):
        gateway_process.cmd_gateway_install(Namespace(config=path, force=True))
    assert install.call_count == 1


def test_selected_config_argv_survives_spaces_and_xml_characters(tmp_path, monkeypatch):
    import json
    import xml.etree.ElementTree as ET

    path = tmp_path / "chosen & special % cfg.yaml"
    path.write_text("gateway_worker_process_count: 2\n")
    monkeypatch.setattr(gateway_service, "_service_working_directory", lambda *args, **kwargs: tmp_path)
    unit = gateway_service.generate_systemd_unit_text(config_path=path)
    assert json.dumps(str(path), ensure_ascii=False).replace("%", "%%") in unit
    document = ET.fromstring(gateway_service.generate_launchd_plist_text(config_path=path))
    assert str(path) in [node.text for node in document.findall(".//array/string")]


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("configured,observed", [(2, 0), (0, 2), (3, 2)])
def test_restart_topology_change_refuses_before_stop_or_safe_restart(tmp_path, monkeypatch, force, configured, observed):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import gateway_coordinated_run

    path = _config(tmp_path, configured)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    monkeypatch.setattr(gateway_coordinated_run, "_cold_gateway_paths", lambda _: paths)
    monkeypatch.setattr(gateway_process, "get_running_pid", lambda _: 123)
    monkeypatch.setattr(gateway_process, "is_pid_alive", lambda _: True)
    monkeypatch.setattr(gateway_process, "read_json_file", lambda _: {"worker_count": observed})
    effects = Mock(side_effect=AssertionError("数量变化不能先停止服务"))
    monkeypatch.setattr(gateway_process, "safe_restart_from_cli", effects)
    monkeypatch.setattr(gateway_process, "cmd_gateway_stop", effects)
    monkeypatch.setattr(gateway_process, "make_agent", effects)
    assert gateway_process.cmd_gateway_restart(Namespace(config=path, force=force)) == 2
    effects.assert_not_called()


def test_status_separates_yaml_target_from_observed_state(tmp_path, monkeypatch, capsys):
    import json

    from agent_py_agent.agent.gateway_parts.io import GatewayJsonReadReport
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    path = _config(tmp_path, 0)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    monkeypatch.setattr(gateway_process, "make_agent", lambda _: SimpleNamespace(config=load_config(path)))
    monkeypatch.setattr(gateway_process, "gateway_paths", lambda _: paths)
    monkeypatch.setattr(gateway_process, "read_pid_record", lambda _: {"pid": 123})
    monkeypatch.setattr(gateway_process, "is_pid_alive", lambda _: True)
    monkeypatch.setattr(gateway_process, "read_json_file_report", lambda p, **_: GatewayJsonReadReport(
        {"pid": 123, "status": "running", "worker_count": 2, "c_run_id": "old-c"} if p == paths.state else {}))
    assert gateway_process.cmd_gateway_status(Namespace(config=path)) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if "拓扑=" in line)
    assert json.loads(line.split("拓扑=", 1)[1]) == {
        "configured_worker_process_count": 0, "observed_state_worker_count": 2,
    }


def test_count0_mp_source_refuses_before_agent_credentials_or_recovery(tmp_path, monkeypatch, capsys):
    import json

    from agent_py_agent.cli import gateway_coordinated_run

    path = _config(tmp_path, 0)
    frozen = common.frozen_gateway_arguments(Namespace(config=path))
    paths = gateway_coordinated_run._cold_gateway_paths(frozen)
    paths.root.mkdir(parents=True)
    paths.state.write_text(json.dumps({"pid": 123, "status": "stopped", "worker_count": 2, "c_run_id": "old-c"}))
    monkeypatch.setattr(gateway_process, "mark_hosting_gateway_process", lambda: None)
    monkeypatch.setattr(gateway_process, "wait_for_predecessor_exit", lambda _: True)
    effects = Mock(side_effect=AssertionError("来源拒绝必须在业务副作用前"))
    monkeypatch.setattr(gateway_process, "make_agent", effects)
    monkeypatch.setattr(gateway_process, "_preflight_required_local_credential", effects)
    monkeypatch.setattr(gateway_process, "_cmd_gateway_run_setup", effects)
    assert gateway_process.cmd_gateway_run(Namespace(config=path)) == 2
    assert "GATEWAY_REVERSE_MIGRATION_UNSUPPORTED" in capsys.readouterr().err
    effects.assert_not_called()
