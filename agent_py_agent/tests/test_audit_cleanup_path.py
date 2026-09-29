"""audit-log --cleanup 必须按 owner canonical 路径解析，不能随进程 cwd 漂移。

背景：`resolve_audit_paths` 原来把配置里的相对路径（默认 `data/audit`）直接展开，于是
在不同 cwd 下运行 `audit-log --cleanup` 会清到不同的文件，甚至可能清掉工作区里恰好同名的
`data/audit/audit.jsonl`。本文件用两个不同 cwd 钉住"清的一定是同一个 canonical 文件"。
"""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.audit.paths import resolve_audit_paths
from agent_py_agent.agent.audit.query import AuditQuery
from agent_py_agent.cli.common import DEFAULT_CONFIG


# LLM: 只造本函数需要的最少字段；不读真实配置或密钥。
# 函数用途: 造一个只带审计路径配置的对象。
def config_with(audit_log_path="", **extra):
    return SimpleNamespace(audit_log_path=audit_log_path, **extra)


# LLM: 用真实 JSONL 行，时间戳可注入，覆盖新旧两侧。
# 函数用途: 往日志文件追加一条带时间戳的审计记录。
def write_entry(path, timestamp):
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": timestamp, "action": "read", "user_id": "u1", "status": "success"}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


# LLM: 相对路径 + 显式基准时，结果必须是基准下的路径；这是本次修复的核心合同。
# 函数用途: 验证相对审计路径按传入基准解析，不再依赖进程 cwd。
def test_relative_path_resolves_against_given_root(tmp_path):
    home = tmp_path / "owner-home"
    paths = resolve_audit_paths(config_with("data/audit"), root=home)
    assert paths.root == home / "data" / "audit"
    assert paths.log_file == home / "data" / "audit" / "audit.jsonl"


# LLM: 绝对路径不受基准影响；配置文件显式给了绝对路径时必须原样尊重。
# 函数用途: 验证绝对配置路径不被基准改写。
def test_absolute_path_ignores_root(tmp_path):
    absolute = tmp_path / "elsewhere" / "audit.jsonl"
    paths = resolve_audit_paths(config_with(str(absolute)), root=tmp_path / "owner-home")
    assert paths.log_file == absolute


# LLM: 不传 root 时保持旧语义（相对 cwd），避免破坏既有调用方和测试。
# 函数用途: 验证无基准时相对路径仍按旧的相对语义解析。
def test_without_root_keeps_relative_semantics():
    paths = resolve_audit_paths(config_with("data/audit"))
    assert paths.log_file == resolve_audit_paths(config_with("data/audit")).log_file
    assert paths.log_file.parts[-3:] == ("data", "audit", "audit.jsonl")
    assert not paths.log_file.is_absolute()


# LLM: 这是缺陷本身：同一份配置在不同 cwd 下必须清同一个文件。
# 函数用途: 在两个不同工作目录下各建一个同名文件，验证 cleanup 只动 canonical 的那一个。
def test_cleanup_targets_same_file_from_different_cwds(tmp_path, monkeypatch):
    home = tmp_path / "owner-home"
    canonical = home / "data" / "audit" / "audit.jsonl"
    old = time.time() - 400 * 24 * 3600
    write_entry(canonical, old)
    write_entry(canonical, time.time())

    # 在另一个 cwd 里放一个同名文件：旧行为会清到它。
    other_cwd = tmp_path / "project"
    decoy = other_cwd / "data" / "audit" / "audit.jsonl"
    write_entry(decoy, old)
    decoy_before = decoy.read_bytes()

    config = config_with("data/audit")
    # 第一次：从 tmp_path 运行，必须清掉 canonical 里的过期条目。
    monkeypatch.chdir(tmp_path)
    assert AuditQuery(config, root=home).cleanup_old_entries(days=30) == 1
    # 第二次：换到另一个 cwd、重建过期条目，仍然只能清 canonical 那一个。
    write_entry(canonical, old)
    monkeypatch.chdir(other_cwd)
    assert AuditQuery(config, root=home).cleanup_old_entries(days=30) == 1

    remaining = [json.loads(line) for line in canonical.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(remaining) == 1 and remaining[0]["timestamp"] > old
    assert decoy.read_bytes() == decoy_before, "工作区里的同名文件绝不能被碰"


# LLM: 保留期 <= 0 表示永久保留；此时连 canonical 文件也不得改动。
# 函数用途: 验证关闭保留时不删除任何记录。
def test_non_positive_days_deletes_nothing(tmp_path):
    home = tmp_path / "owner-home"
    canonical = home / "data" / "audit" / "audit.jsonl"
    write_entry(canonical, time.time() - 400 * 24 * 3600)
    before = canonical.read_bytes()
    assert AuditQuery(config_with("data/audit"), root=home).cleanup_old_entries(days=0) == 0
    assert canonical.read_bytes() == before


# LLM: 基准必须真正约束范围——cleanup 只应读写 canonical 文件，不越出给定 home。
# 函数用途: 验证清理后的文件仍落在传入的 owner home 之内。
def test_cleanup_stays_inside_given_root(tmp_path):
    home = tmp_path / "owner-home"
    outside = tmp_path / "outside" / "data" / "audit" / "audit.jsonl"
    write_entry(outside, time.time() - 400 * 24 * 3600)
    outside_before = outside.read_bytes()
    write_entry(home / "data" / "audit" / "audit.jsonl", time.time() - 400 * 24 * 3600)

    query = AuditQuery(config_with("data/audit"), root=home)
    assert query._audit_file.is_relative_to(home)
    query.cleanup_old_entries(days=30)
    assert outside.read_bytes() == outside_before


# LLM: 探测到的根目录也必须随基准移动，避免只在文件字段上修好、根字段仍漂移。
# 函数用途: 验证查询视图的根与文件两个字段都按基准解析。
def test_query_view_root_follows_given_root(tmp_path):
    home = tmp_path / "owner-home"
    query = AuditQuery(config_with("data/audit"), root=home)
    assert query._audit_root == home / "data" / "audit"
    assert query._audit_file == home / "data" / "audit" / "audit.jsonl"


# LLM: 配置里指向具体文件（带后缀）时，基准仍然生效且父目录正确。
# 函数用途: 验证带后缀的相对配置路径同样按基准解析。
def test_relative_file_path_with_suffix(tmp_path):
    home = tmp_path / "owner-home"
    paths = resolve_audit_paths(config_with("logs/my-audit.jsonl"), root=home)
    assert paths.root == home / "logs"
    assert paths.log_file == home / "logs" / "my-audit.jsonl"


# LLM: 真实部署形态：owner home 下 logs/audit/audit.jsonl 是 canonical 活文件。
# 函数用途: 用与生产相同的相对路径形状验证解析结果落在 owner home 内。
def test_owner_home_logs_audit_shape(tmp_path):
    home = tmp_path / "main"
    paths = resolve_audit_paths(config_with("logs/audit"), root=home)
    assert paths.log_file == home / "logs" / "audit" / "audit.jsonl"
    assert paths.log_file.is_relative_to(home)

# LLM: 运行时路径里有权威审计目录时，必须优先用它；CLI 不能再靠"配置空值 → 默认 data/audit"这条隐式链。
# 函数用途: 验证 runtime_audit_path 优先于配置解析。
def test_runtime_audit_path_wins_over_config_defaults(tmp_path):
    runtime_dir = tmp_path / "owner-home" / "logs" / "audit"
    query = AuditQuery(
        config_with(""),
        root=tmp_path / "owner-home",
        runtime_audit_path=runtime_dir,
    )
    assert query._audit_root == runtime_dir
    assert query._audit_file == runtime_dir / "audit.jsonl"


# LLM: 运行时路径与配置基准指向同一个目录时必须落在一起，CLI 与 Agent 两条入口不能各写各的。
# 函数用途: 验证两套入口解析出的审计文件是同一个。
def test_runtime_path_matches_configured_canonical_root(tmp_path):
    home = tmp_path / "owner-home"
    runtime_dir = home / "logs" / "audit"
    via_config = AuditQuery(config_with("logs/audit"), root=home)
    via_runtime = AuditQuery(config_with(""), root=home, runtime_audit_path=runtime_dir)
    assert via_config._audit_file == via_runtime._audit_file


# LLM: 不给运行时路径时保持既有行为，旧调用方和既有测试不受影响。
# 函数用途: 验证省略运行时路径时仍按配置与基准解析。
def test_without_runtime_path_keeps_config_resolution(tmp_path):
    home = tmp_path / "owner-home"
    query = AuditQuery(config_with("data/audit"), root=home)
    assert query._audit_file == home / "data" / "audit" / "audit.jsonl"


# LLM: dev 2026-09-29 指出的缺口——前面几条都直接构造 AuditQuery，等于只测了查询视图、
#   没测 CLI 那一行（而那一行就是修复本身）。这条走真实入口 cmd_audit_log，在隔离的 owner home 里
#   断言解析出的审计路径就是运行时权威地址，并且与进程 cwd 无关。
# 函数用途: 经 CLI 入口验证 audit-log 用的是运行时权威地址、且不随 cwd 漂移。
def test_cli_entry_uses_runtime_audit_path_regardless_of_cwd(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.cli import audit_log_cmd

    home_root = tmp_path / "agent-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home_root))

    # 隔离的 owner home：用真实解析口径算出来，避免自己拼路径拼错。
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.home_root import configured_home_root
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_config,
        resolve_owner_home,
    )
    from agent_py_agent.cli.common import load_config

    config = load_config(str(DEFAULT_CONFIG))
    base = home_paths(configured_home_root(config))
    owner = resolve_owner_home(base.root, owner_identity_from_config(config))
    home = home_paths_with_owner(base, owner)
    expected_dir = Path(home.owner_logs_dir) / "audit"

    seen = {}

    class RecordingQuery:
        # 函数用途: 顶替真实 AuditQuery，只记录 CLI 传进来的权威路径。
        def __init__(self, config, *, root=None, runtime_audit_path=None):
            seen["root"] = root
            seen["runtime_audit_path"] = runtime_audit_path

        # 函数用途: 顶替清理动作，不去动任何真实文件。
        def cleanup_old_entries(self, *, days):
            seen["cleanup_days"] = days
            return 0

    monkeypatch.setattr(audit_log_cmd, "AuditQuery", RecordingQuery)

    # 走 --cleanup 分支：这正是这次修复的真实用途（清理目标地址）。
    args = SimpleNamespace(
        config=str(DEFAULT_CONFIG),
        recent_users=False,
        summary=False,
        cleanup=True,
        days=30,
    )

    # 从两个不同 cwd 各跑一次：CLI 拿到的权威路径必须一样，且就是 owner_logs_dir/audit。
    other_cwd = tmp_path / "some-project"
    other_cwd.mkdir()
    for cwd in (tmp_path, other_cwd):
        monkeypatch.chdir(cwd)
        seen.clear()
        assert audit_log_cmd.cmd_audit_log(args) == 0
        assert seen["runtime_audit_path"] == expected_dir, (
            f"CLI 必须把运行时权威地址交给 AuditQuery（cwd={cwd}）"
        )


# LLM: 权威地址必须真的落在 owner home 里，不能因为环境变量或配置漂到别处。
# 函数用途: 验证 CLI 解析出的运行时审计目录位于当前 owner home 之下。
def test_cli_runtime_audit_path_stays_inside_owner_home(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.home_root import configured_home_root
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_config,
        resolve_owner_home,
    )
    from agent_py_agent.cli.common import load_config
    from agent_py_agent.cli.workspace_resolution import runtime_paths_for_config

    home_root = tmp_path / "agent-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home_root))
    config = load_config(str(DEFAULT_CONFIG))
    base = home_paths(configured_home_root(config))
    owner = resolve_owner_home(base.root, owner_identity_from_config(config))
    home = home_paths_with_owner(base, owner)

    audit_dir = runtime_paths_for_config(config)["audit_log_path"]
    assert str(audit_dir).startswith(str(home.owner_home_dir))
    assert audit_dir.name == "audit"


# LLM: dev 2026-09-29 第 6 条：运行时权威审计路径带后缀时，它本身就是文件，不能再拼一次
#   "/audit.jsonl"——否则查询会落到 <文件>/audit.jsonl，查询为空、清理空转。写入端
#   audit/logger.py 用的是 resolve_audit_paths，所以查询端必须复用同一规则。
# 函数用途: 验证带后缀的运行时权威路径被当成文件本身。
def test_runtime_audit_path_with_suffix_is_the_file_itself(tmp_path):
    custom = tmp_path / "owner-home" / "logs" / "custom-audit.jsonl"
    query = AuditQuery(
        config_with(""),
        root=tmp_path / "owner-home",
        runtime_audit_path=custom,
    )
    assert query._audit_file == custom, "带后缀的权威路径就是文件本身，不能再拼 audit.jsonl"
    assert query._audit_root == custom.parent


# LLM: 与写入端对齐：两边拿同一个带后缀的路径，解析出的文件必须一模一样，否则查询查不到写入内容。
# 函数用途: 验证查询端与写入端对同一带后缀路径解析一致。
def test_runtime_audit_path_matches_writer_semantics_for_suffixed_path(tmp_path):
    custom = tmp_path / "owner-home" / "logs" / "custom-audit.jsonl"
    via_writer = resolve_audit_paths(config_with(str(custom)))
    via_query = AuditQuery(
        config_with(""),
        root=tmp_path / "owner-home",
        runtime_audit_path=custom,
    )
    assert via_query._audit_file == via_writer.log_file
    assert via_query._audit_root == via_writer.root


# LLM: dev 2026-09-29 第 2 条：前面的 CLI 用例顶替了 AuditQuery，只断言"传出去的路径"，
#   抓不到「写入端与 CLI 各算各的路径」这种分叉。这条走完整链路：
#   按 Agent 的方式解析运行时路径 -> 用写入端 AuditLogger 真写文件 -> 调真实 cmd_audit_log 清理
#   -> 同一个文件里的过期行被删、新行保留。路径一旦分叉，这里必然红。
# 函数用途: 端到端验证写入端与 CLI 清理指向同一个文件。
def test_audit_log_cleanup_end_to_end_uses_the_writer_file(tmp_path, monkeypatch):
    import json
    import os
    import time
    from types import SimpleNamespace

    from agent_py_agent.agent.audit.logger import AuditLogger
    from agent_py_agent.cli import audit_log_cmd
    from agent_py_agent.cli.common import load_config
    from agent_py_agent.cli.workspace_resolution import runtime_paths_for_config

    home_root = tmp_path / "agent-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home_root))

    # Agent 启动时会往 config 注入运行时权威路径；这里在「读配置」这一层模拟同一行为，
    # 让 CLI 内部的 load_config 也拿到同一份值——路径一旦分叉，下面的断言就会红。
    audit_dir = runtime_paths_for_config(load_config(str(DEFAULT_CONFIG)))["audit_log_path"]

    import agent_py_agent.cli.common as cli_common

    original_load = cli_common.load_config

    def injected_load(path):
        loaded = original_load(path)
        loaded.audit_log_path = str(audit_dir)
        return loaded

    monkeypatch.setattr(cli_common, "load_config", injected_load)
    config = injected_load(str(DEFAULT_CONFIG))

    # 写入端真写两行：一行超期、一行新鲜。
    writer = AuditLogger(config)
    target = Path(writer._audit_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    old_ts = time.time() - 400 * 24 * 3600
    fresh_ts = time.time()
    rows = [
        {"timestamp": old_ts, "action": "read", "user_id": "u1", "status": "success"},
        {"timestamp": fresh_ts, "action": "read", "user_id": "u1", "status": "success"},
    ]
    target.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    # 经真实 CLI 入口清理。
    args = SimpleNamespace(
        config=str(DEFAULT_CONFIG),
        recent_users=False,
        summary=False,
        cleanup=True,
        days=30,
    )
    # CLI 内部自己 load_config（函数内导入），上面已经把 common.load_config 换成注入版。
    assert audit_log_cmd.cmd_audit_log(args) == 0

    # 同一个文件里：过期行没了，新行还在。
    remaining = [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(remaining) == 1, f"应当只剩新行，实际 {len(remaining)} 行"
    assert remaining[0]["timestamp"] == fresh_ts
