# LLM: 真实规范解析器、Agent 与 DoctorService 在临时 echo owner 下联通，不用替身报告冒充 CLI 端到端。
# 模块用途: 验证 memory doctor all 公共字段及旧诊断视图的可消费性。
import json

from agent_py_agent.cli.parser import build_parser


# LLM: 所有读写位于 pytest 临时目录，索引为空但合法；模型 echo，不访问网络或生产用户配置。
# 函数用途: 执行真实 CLI 解析到处理函数的诊断主链并取回 JSON。
def _invoke(tmp_path, monkeypatch, capsys, command):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text(f'model_backend: "echo"\nmy_agent_home: "{home}"\nworkspace_root: ""\n', encoding="utf-8")
    index = tmp_path / "INDEX.md"
    index.write_text("# Memory routes\n", encoding="utf-8")
    args = build_parser().parse_args([
        "--config", str(config), "--dir", str(workspace), *command,
        "--index", str(index), "--json",
    ])
    assert args.func(args) == 0
    return json.loads(capsys.readouterr().out)


# LLM: 同一空索引让路由值相等、归档扩展不同，仍要求 all 的四个公共字段形状一致。
# 函数用途: 在真实 CLI doctor all 上复现并锁定稳定机器结构。
def test_cli_doctor_all_common_fixed_schema(tmp_path, monkeypatch, capsys):
    report = _invoke(tmp_path, monkeypatch, capsys, ["memory", "doctor", "--scope", "all"])
    assert report["ok"] is True and report["scope"] == "all"
    assert set(report["common"]) == {"config", "routing", "archive", "warnings"}
    for field in report["common"].values():
        assert set(field) == {"runtime", "memory"}
    assert report["common"]["routing"]["runtime"]["route_count"] == 0
    assert report["common"]["routing"]["memory"]["load_error"] == ""
    assert "snapshots" not in report["common"]["archive"]["runtime"]
    assert "snapshots" in report["common"]["archive"]["memory"]
    assert "home_doctor" in report["runtime"] and "candidate" in report["memory"]


# LLM: 规范默认与 runtime 及历史 v1 都走真实 Agent，字段不因 common 改动而迁出原视图。
# 函数用途: 验证既有 memory/runtime/v1 JSON 投影兼容。
def test_cli_original_doctor_views(tmp_path, monkeypatch, capsys):
    memory = _invoke(tmp_path, monkeypatch, capsys, ["memory", "doctor"])
    runtime = _invoke(tmp_path, monkeypatch, capsys, ["memory", "doctor", "--scope", "runtime"])["runtime"]
    legacy = _invoke(tmp_path, monkeypatch, capsys, ["memory-doctor"])
    assert {"candidate", "curator", "migration", "retention", "config_warnings", "routing", "archive"} <= memory.keys()
    assert {"home", "home_doctor", "warnings", "config", "routing", "archive"} <= runtime.keys()
    assert runtime["routing"] == legacy["routing"]
    assert runtime["archive"] == legacy["archive"]
    assert runtime["config"] == legacy["config"]
    assert "candidate" not in legacy and "common" not in memory
