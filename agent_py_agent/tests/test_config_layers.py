from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.config_layers import ConfigLayer, merge_config_layers
from agent_py_agent.agent.settings.runtime_scope_config import (
    RuntimeConfigLayerRequest,
    load_runtime_config_layer,
    make_runtime_config_layer,
    merge_runtime_config_layers,
)


def test_shipped_system_prompt_matches_schema_default() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
    shipped = load_config(config_path).system_prompt
    schema_default = AgentConfig().system_prompt

    assert shipped == schema_default
    assert "缺少次要实现选择时，不要停下来反问" in shipped
    assert "不要输出多选菜单来代替工作" in shipped
    assert "任务规模大、耗时长或仅仅有可澄清之处，都不等于阻塞" in shipped
    assert shipped.index("自主决策：") < shipped.index("执行纪律：")
    assert "只要当前用户目标仍有你已知的未完成部分" in shipped
    assert "委派只是分工，不会缩小用户原始目标" in shipped
    assert "不能把 `|| true`、`|| echo` 等忽略失败包装后的外层成功当成内部成功" in shipped
    assert "有效测试不得仅为变绿而删除、跳过、放宽断言或改成只测存在" in shipped
    assert "go.mod" not in shipped
    assert "Rust" not in shipped


def test_merge_config_layers_tracks_winning_source() -> None:
    effective = merge_config_layers(
        [
            ConfigLayer(source="builtin", priority=0, values={"max_subagents": 10, "language": "en"}),
            ConfigLayer(source="owner", priority=20, values={"language": "zh"}),
            ConfigLayer(source="task", priority=40, values={"max_subagents": 30}),
        ]
    )

    assert effective.value("language") == "zh"
    assert effective.source_for("language") == {"source": "owner", "priority": 20}
    assert effective.value("max_subagents") == 30
    assert effective.source_for("max_subagents") == {"source": "task", "priority": 40}


def test_merge_config_layers_filters_allowed_keys_and_ignores_none() -> None:
    effective = merge_config_layers(
        [
            ConfigLayer(source="builtin", priority=0, values={"a": 1, "b": 2}),
            ConfigLayer(source="task", priority=10, values={"a": None, "b": 3, "c": 4}),
        ],
        allowed_keys={"a", "b"},
    )

    assert effective.values == {"a": 1, "b": 3}
    assert "c" not in effective.sources


def test_config_layer_can_be_built_from_dataclass_object() -> None:
    @dataclass
    class DemoConfig:
        alpha: int = 1
        beta: str = "two"

    layer = ConfigLayer.from_object(source="demo", priority=5, obj=DemoConfig())

    assert layer.values == {"alpha": 1, "beta": "two"}


def test_load_config_records_schema_and_file_sources(tmp_path) -> None:
    path = tmp_path / "agent_config.yaml"
    path.write_text("model_backend: echo\nmax_subagents: 17\n", encoding="utf-8")

    config = load_config(path)

    assert config.max_subagents == 17
    assert config.config_source_for("max_subagents") == {
        "source": str(path.resolve()),
        "priority": 20,
    }
    assert config.config_source_for("memory_top_k") == {
        "source": "schema_default",
        "priority": 0,
    }


def test_load_config_records_env_override_source(tmp_path, monkeypatch) -> None:
    path = tmp_path / "agent_config.yaml"
    path.write_text("api_key_env: TEST_MY_AGENT_KEY\napi_key: from_file\n", encoding="utf-8")
    monkeypatch.setenv("TEST_MY_AGENT_KEY", "from_env")

    config = load_config(path)

    assert config.api_key == "from_env"
    assert config.config_source_for("api_key") == {
        "source": "env:TEST_MY_AGENT_KEY",
        "priority": 80,
    }


def test_load_config_rejects_yaml_config_source_injection(tmp_path) -> None:
    path = tmp_path / "agent_config.yaml"
    path.write_text(
        "\n".join(
            [
                "model_backend: echo",
                "config_sources: fake",
                "config_layers: fake",
                "config_path: fake",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.config_path == str(path.resolve())
    assert config.config_source_for("model_backend")["source"] == str(path.resolve())
    assert any("config_sources" in warning for warning in config.config_warnings)


def test_runtime_config_layers_preserve_base_sources_and_owner_override(tmp_path) -> None:
    path = tmp_path / "agent_config.yaml"
    path.write_text("model_backend: echo\nmax_subagents: 17\n", encoding="utf-8")
    base = load_config(path)
    owner = make_runtime_config_layer(
        RuntimeConfigLayerRequest(
            scope="owner",
            source="owner:local/main/config.yaml",
            values={"max_subagents": "25"},
            config_cls=type(base),
        )
    )

    effective = merge_runtime_config_layers(base, [owner])

    assert effective.value("max_subagents") == 25
    assert effective.source_for("max_subagents") == {
        "source": "owner:local/main/config.yaml",
        "priority": 30,
    }
    assert effective.source_for("model_backend") == {
        "source": str(path.resolve()),
        "priority": 20,
    }


def test_runtime_config_lower_priority_layer_cannot_override_env_source(tmp_path, monkeypatch) -> None:
    path = tmp_path / "agent_config.yaml"
    path.write_text("api_key_env: TEST_MY_AGENT_KEY\napi_key: from_file\n", encoding="utf-8")
    monkeypatch.setenv("TEST_MY_AGENT_KEY", "from_env")
    base = load_config(path)
    owner = make_runtime_config_layer(
        RuntimeConfigLayerRequest(
            scope="owner",
            source="owner:local/main/config.yaml",
            values={"api_key": "from_owner"},
            config_cls=type(base),
        )
    )

    effective = merge_runtime_config_layers(base, [owner])

    assert effective.value("api_key") == "from_env"
    assert effective.source_for("api_key") == {
        "source": "env:TEST_MY_AGENT_KEY",
        "priority": 80,
    }


def test_runtime_config_runtime_layer_can_override_env_source(tmp_path, monkeypatch) -> None:
    path = tmp_path / "agent_config.yaml"
    path.write_text("api_key_env: TEST_MY_AGENT_KEY\napi_key: from_file\n", encoding="utf-8")
    monkeypatch.setenv("TEST_MY_AGENT_KEY", "from_env")
    base = load_config(path)
    runtime = make_runtime_config_layer(
        RuntimeConfigLayerRequest(
            scope="runtime",
            source="run:override",
            values={"api_key": "from_runtime"},
            config_cls=type(base),
        )
    )

    effective = merge_runtime_config_layers(base, [runtime])

    assert effective.value("api_key") == "from_runtime"
    assert effective.source_for("api_key") == {"source": "run:override", "priority": 90}


def test_runtime_config_layer_rejects_unknown_and_internal_fields(tmp_path) -> None:
    path = tmp_path / "agent_config.yaml"
    path.write_text("model_backend: echo\n", encoding="utf-8")
    base = load_config(path)
    layer = make_runtime_config_layer(
        RuntimeConfigLayerRequest(
            scope="task",
            source="task:task-1/config.yaml",
            values={
                "model_backend": "echo",
                "config_sources": {"fake": "source"},
                "not_a_config_field": "ignored",
            },
            config_cls=type(base),
        )
    )

    effective = merge_runtime_config_layers(base, [layer])

    assert effective.value("model_backend") == "echo"
    assert "config_sources" not in layer.values
    assert "not_a_config_field" not in layer.values
    assert any("config_sources" in warning for warning in layer.warnings)
    assert any("not_a_config_field" in warning for warning in layer.warnings)


def test_load_runtime_config_layer_reads_yaml_override(tmp_path) -> None:
    path = tmp_path / "task_config.yaml"
    path.write_text("max_subagents: 31\nconfig_path: fake\n", encoding="utf-8")

    layer = load_runtime_config_layer(path, scope="task", config_cls=AgentConfig)

    assert layer.values["max_subagents"] == 31
    assert "config_path" not in layer.values
    assert layer.source == str(path.resolve())
    assert any("config_path" in warning for warning in layer.warnings)


def test_cli_runtime_config_environment_applies_scoped_overlay(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.settings.services.runtime_config_env import (
        apply_runtime_config_environment,
    )

    base_path = tmp_path / "agent_config.yaml"
    overlay_path = tmp_path / "runtime.yaml"
    base_path.write_text("model_backend: echo\nmax_subagents: 17\n", encoding="utf-8")
    overlay_path.write_text("max_subagents: 29\nconfig_layers: fake\n", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_RUNTIME_CONFIG", str(overlay_path))
    monkeypatch.setenv("MY_AGENT_RUNTIME_CONFIG_SCOPE", "run")

    config = apply_runtime_config_environment(load_config(base_path))

    assert config.max_subagents == 29
    assert config.config_source_for("max_subagents") == {"source": str(overlay_path.resolve()), "priority": 60}
    assert any("config_layers" in warning for warning in config.config_warnings)


def test_cli_runtime_config_environment_supports_multiple_layers(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.settings.services.runtime_config_env import (
        apply_runtime_config_environment,
    )

    base_path = tmp_path / "agent_config.yaml"
    owner_path = tmp_path / "owner.yaml"
    task_path = tmp_path / "task.yaml"
    base_path.write_text("model_backend: echo\nmax_subagents: 17\n", encoding="utf-8")
    owner_path.write_text("max_subagents: 21\n", encoding="utf-8")
    task_path.write_text("max_subagents: 33\n", encoding="utf-8")
    monkeypatch.setenv(
        "MY_AGENT_RUNTIME_CONFIG_LAYERS",
        f"owner={owner_path},task={task_path}",
    )

    config = apply_runtime_config_environment(load_config(base_path))

    assert config.max_subagents == 33
    assert config.config_source_for("max_subagents") == {"source": str(task_path.resolve()), "priority": 50}
