# LLM: WS-A4 专项验收套件，覆盖 CLI 引导配置韧性 (BIZ-010)、构建标识跨端一致性 (BIZ-007 / V15-01)、
# 统一 CLI 与 Slash 业务命令 (V02-02, V07-03, V13-03)、以及 TUI 底栏顺序与后台名册扩展视图 (G3, V13-01..02)。
# 模块用途: 自动化验证 CLI/TUI/构建标识/业务命令合同与有界视口约束。

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent import __version__
from agent_py_agent.agent.common.build_identity import get_build_identity
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports
from agent_py_agent.agent.gateway_parts.http_handlers import handle_status
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_parts.slash_command_types import SlashCommandContext
from agent_py_agent.cli.chat_parts.slash_commands import _handle_business_command
from agent_py_agent.cli.chat_parts.tui_background_agents import TuiBackgroundAgents
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    _render_footer,
)
from agent_py_agent.cli.chat_parts.tui_business_spaces import handle_tui_business_command
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.parser import build_parser, main
from agent_py_agent.cli.space_commands import (
    BusinessClient,
    BusinessCommandSession,
    PreparedBusinessCall,
    cmd_space,
    consume_business_command,
    run_business_cli,
)
from agent_py_agent.cli.team_commands import cmd_team
from agent_py_agent.cli.workflow_commands import (
    WorkflowTable,
    cmd_workflow,
    format_workflow_detail,
)
from agent_py_agent.tests.test_business_v12_f5_tui import dto, minimal
from agent_py_agent.tests.test_gateway_admission_wait import _paths


# LLM: 模拟已认证的 BusinessClient 传输层，按需返回成功回执与测试 DTO。
# 类用途: 为业务命令与 TUI 测试提供隔离且可捕获请求的 Protocol 客户端。
class FakeBusinessTransport:
    def __init__(self, responses: dict | None = None):
        self.calls: list[dict] = []
        self.responses: dict = responses or {}

    def __call__(self, path: str, request: dict, *, timeout: float = 10.0) -> tuple[int, dict]:
        self.calls.append(dict(request))
        action = request.get('action', '')
        if action in self.responses:
            body = self.responses[action]
        else:
            try:
                body = minimal(business_action_spec(action).result_schema)
            except Exception:
                body = {'items': [], 'coverage': 'complete', 'next_cursor': None}
        return 200, {
            'request_id': request.get('request_id', 'test-req'),
            'operation_id': 'op-1',
            'state': 'completed',
            'error': None,
            'data': body,
            'allowed_actions': [],
            'warnings': [],
            'effect_outcome': 'committed',
        }


# LLM: 验证 BIZ-010：当 MY_AGENT_CONFIG 指向不存在的配置文件时，--help 与 --version 依然必须成功退出 0。
# 函数用途: 验证 CLI 引导阶段在配置缺失时的韧性。
def test_biz010_help_and_version_with_missing_config(monkeypatch):
    nonexistent = "/nonexistent/config/desktop.yaml"
    monkeypatch.setenv("MY_AGENT_CONFIG", nonexistent)

    # 1. 进程内调用 main([]) 必须安全退出 0
    assert main(["--version"]) == 0
    assert main(["-V"]) == 0
    assert main(["--help"]) == 0
    assert main(["-h"]) == 0
    assert main(["space", "--help"]) == 0
    assert main(["workflow", "--help"]) == 0
    assert main(["team", "--help"]) == 0

    # 2. 真实子进程调用检验隔离与退出码 0
    env = {
        **os.environ,
        "MY_AGENT_CONFIG": nonexistent,
        "PYTHONPATH": str(os.environ.get("PYTHONPATH", "")),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    python_bin = sys.executable
    for args in [
        ["-m", "agent_py_agent", "--version"],
        ["-m", "agent_py_agent", "-V"],
        ["-m", "agent_py_agent", "--help"],
        ["-m", "agent_py_agent", "space", "--help"],
        ["-m", "agent_py_agent", "workflow", "--help"],
        ["-m", "agent_py_agent", "team", "--help"],
    ]:
        res = subprocess.run(
            [python_bin, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        assert res.returncode == 0, f"Failed for {args}: {res.stderr}"


# LLM: 验证 BIZ-007 / V15-01：CLI --version 与 Gateway /status 共享同一构建标识，且单进程内零重复子进程。
# 函数用途: 验证构建标识契约与 LRU 缓存特性。
def test_biz007_v1501_build_identity_parity_and_caching(capsys, tmp_path):
    identity = get_build_identity()
    assert isinstance(identity, dict)
    assert identity["version"] == __version__
    assert identity["runtime_prefix"] == sys.prefix
    assert "git_sha" in identity

    # 验证 lru_cache 幂等性与零额外子进程
    identity_again = get_build_identity()
    assert identity is identity_again

    # 验证 Gateway /status 暴露 build_identity
    sent_status = []
    sent_body = []

    class FakeStatusHandler:
        def _send_json(self, status: int, body: dict) -> None:
            sent_status.append(status)
            sent_body.append(body)

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state.write_text(json.dumps({"status": "running", "pid": os.getpid(), "started_at": 1000}), encoding="utf-8")
    server = SimpleNamespace(paths=paths)
    handle_status(FakeStatusHandler(), server)
    assert sent_status == [200]
    assert "build_identity" in sent_body[0]
    assert sent_body[0]["build_identity"] == identity

    # 验证 CLI --version 输出包含版本与 SHA
    main(["--version"])
    out = capsys.readouterr().out
    assert identity["version"] in out


# LLM: 验证工作流表格编号解析与两列格式：只含名称与关联空间，编号绑定快照与稳定 ref。
# 函数用途: 验证 V07-03 工作流两列表格与快照编号解析契约。
def test_workflow_table_two_columns_and_snapshot_resolution():
    table = WorkflowTable()
    items = [
        {"workflow_ref": "wf_1", "name": "数据提取", "scope": {"scope_mode": "spaces", "space_ids": ["sp_a"]}},
        {"workflow_ref": "wf_2", "name": "数据分析", "scope": {"scope_mode": "owner_global", "space_ids": []}},
    ]
    rows = table.update(items, list_snapshot_id="snap-v1")
    assert len(rows) == 2
    assert all(len(r) == 2 for r in rows)
    assert rows[0][0] == "1 数据提取"
    assert rows[0][1] == "sp_a"
    assert rows[1][0] == "2 数据分析"
    assert rows[1][1] == "所有空间"

    # 编号解析
    assert table.resolve("1") == "wf_1"
    assert table.resolve("2") == "wf_2"
    # 稳定 workflow_ref 解析
    assert table.resolve("wf_1") == "wf_1"
    assert table.resolve("wf_external_stable") == "wf_external_stable"

    # 快照校验失败
    with pytest.raises(ValueError, match="快照已更新"):
        table.resolve("1", expected_snapshot_id="snap-v2")

    # 列表更新后失效编号抛异常
    table.update([{"workflow_ref": "wf_2", "name": "数据分析"}], list_snapshot_id="snap-v2")
    with pytest.raises(ValueError, match="编号已失效"):
        table.resolve("1")


# LLM: 验证工作流详情展示函数包含用途、IO合同、版本、步骤摘要、运行引用及草稿状态。
# 函数用途: 验证 V07-03 详情多行文本渲染能力。
def test_format_workflow_detail():
    data = {
        "workflow": {
            "name": "报表汇总",
            "workflow_ref": "wf-report",
            "definition_revision": 3,
            "lifecycle": "active",
            "enabled": True,
        },
        "detail": {
            "summary": "自动汇总每周营业额并发送通知",
            "io_contract": {"inputs": ["week_id"], "outputs": ["report_url"]},
            "steps": [
                {"step_id": "step1", "name": "拉取数据", "mode": "deterministic_script"},
                {"step_id": "step2", "name": "生成图表", "mode": "agent_action"},
            ],
            "step_count": 2,
            "coverage": "complete",
        },
        "run_refs": ["run-001", "run-002"],
    }
    rendered = format_workflow_detail(data)
    assert "报表汇总" in rendered
    assert "自动汇总每周营业额并发送通知" in rendered
    assert "版本: 3" in rendered
    assert "状态: active" in rendered
    assert "week_id" in rendered
    assert "拉取数据" in rendered
    assert "run-001" in rendered


# LLM: 验证统一 CLI 与 Slash /space, /team, /workflow 命令及参数转发、幂等键与期望版本。
# 函数用途: 验证 V02-02, V07-03, V13-03 业务命令端到端调用与回执投影。
def test_unified_cli_and_slash_commands(capsys):
    wf_item = dto("WorkflowView", workflow_ref="wf-test", name="测试流程", scope={"scope_mode": "unassigned", "space_ids": []})
    transport = FakeBusinessTransport({
        "workflows.list": dto(
            "Result_workflows_list",
            items=[wf_item],
            list_snapshot_id="snap-123",
            coverage="complete",
            next_cursor=None,
        ),
        "workflows.get": dto(
            "Result_workflows_get",
            workflow=wf_item,
            detail=dto("WorkflowDetailView", summary="流程目的", io_contract=None, steps=[], step_count=0, coverage="complete"),
        ),
        "spaces.create": dto(
            "Result_spaces_create",
            space_id="sp-new",
            name="空间A",
        ),
        "spaces.update": dto(
            "Result_spaces_update",
            space=dto("SpaceView", space_id="sp-rename", name="已改名"),
        ),
        "background_agents.suspend": dto(
            "Result_background_agents_suspend",
            agent=dto("AgentView", agent_id="ag-1", identity_state="suspended"),
        ),
    })
    client = BusinessClient(transport)
    session = BusinessCommandSession(client)

    # 1. /workflow list
    res = consume_business_command("/workflow list", session=session)
    assert res["ok"] is True
    assert res["display_columns"] == ["名称", "关联空间"]
    assert len(res["display_rows"]) == 1

    # 2. /workflow show with index number
    res_show = consume_business_command("/workflow show 1", session=session)
    assert res_show["ok"] is True
    assert res_show["workflow_detail"]["purpose"] == "流程目的"
    assert "workflow_detail" in res_show
    assert "rendered" in res_show

    # 3. /space create
    res_create = consume_business_command("/space create 空间A", session=session)
    assert res_create["ok"] is True
    assert transport.calls[-1]["action"] == "spaces.create"
    assert transport.calls[-1]["params"]["name"] == "空间A"
    assert "idempotency_key" in transport.calls[-1]

    # 4. /space rename with expected_revision
    rename_payload = json.dumps({"params": {"space_id": "sp-rename", "name": "已改名"}, "expected_revision": 5})
    res_rename = consume_business_command(f"/space rename {rename_payload}", session=session)
    assert res_rename["ok"] is True
    assert transport.calls[-1]["expected_revision"] == 5

    # 5. /team pause (suspend) with expected_revision
    pause_payload = json.dumps({"params": {"agent_id": "ag-1"}, "expected_revision": 1})
    res_pause = consume_business_command(f"/team pause {pause_payload}", session=session)
    assert res_pause["ok"] is True
    assert transport.calls[-1]["action"] == "background_agents.suspend"
    assert transport.calls[-1]["params"]["agent_id"] == "ag-1"
    assert transport.calls[-1]["expected_revision"] == 1

    # 6. cmd_space, cmd_team, cmd_workflow via run_business_cli
    args_space = SimpleNamespace(business_client=client, business_args=["list"])
    assert cmd_space(args_space) == 0
    assert transport.calls[-1]["action"] == "spaces.list"

    args_team = SimpleNamespace(business_client=client, business_args=["list"])
    assert cmd_team(args_team) == 0
    assert transport.calls[-1]["action"] == "background_agents.list"

    args_wf = SimpleNamespace(business_client=client, business_args=["list"])
    assert cmd_workflow(args_wf) == 0
    assert transport.calls[-1]["action"] == "workflows.list"

    # 7. TUI / Slash 适配层验证
    lines = []
    agent_obj = SimpleNamespace(config=SimpleNamespace(gateway_port=0))
    # 直接注入 session 以避免网络连接
    agent_obj._tui_business_command_session = session
    agent_obj._tui_business_command_scope = "test-sess"

    assert handle_tui_business_command("/workflow list", agent=agent_obj, print_line=lines.append, session_id="test-sess") is True
    assert any("名称 | 关联空间" in line for line in lines)

    ctx = SlashCommandContext(agent=agent_obj, memory_limit=0, runtime_inject=[], prompt_files=[],
                              print_line=lines.append, conversation_id="test-sess", use_gateway=False)
    assert _handle_business_command("/space list", ctx, False) is True


# LLM: 验证 G3 / V13-01..02 TUI 底栏顺序、非取消导航及后台助手调度/阻断/扩展有界视口。
# 函数用途: 验证底栏段顺序（快捷键/权限在前，后台Agent与驻留徽标在尾部）及名册展开视口有界性。
def test_g3_v1301_v1302_tui_footer_and_blockers():
    runtime = TuiRuntime("thread-footer-ws-a4")
    snapshot = runtime.store.snapshot()

    # 1. 验证底栏段严格顺序: 现有控制 -> 后台Agent -> 后台驻留
    context = TuiRenderContext(
        width=240,
        background_process_count=3,
        background_agent_count=5,
        background_agent_running_count=2,
    )
    rendered = _render_footer(snapshot, context)
    text = "".join(part[1] for part in rendered)
    assert text.index("F4") < text.index("后台Agent：运行 2 / 总 5") < text.index("后台驻留 3")
    assert text.endswith(" · 后台Agent：运行 2 / 总 5 · ⚡ 后台驻留 3 项")

    # 2. 验证业务列表与会话底栏保持非取消 Ctrl+G 返回引导
    list_ctx = TuiRenderContext(width=200, business_list_active=True)
    list_text = "".join(p[1] for p in _render_footer(snapshot, list_ctx))
    assert list_text.startswith("  Ctrl+G 返回 · Enter 下钻/下一页 · ↑↓ 选择")

    thread_ctx = TuiRenderContext(
        width=200,
        business_thread_active=True,
        business_identity_state="available",
        business_work_state="running",
    )
    thread_text = "".join(p[1] for p in _render_footer(snapshot, thread_ctx))
    assert "Ctrl+G 返回" in thread_text
    assert "身份：可接单 · 工作：运行中" in thread_text

    # 3. 验证 TuiBackgroundAgents 显示模型与调度阻断原因码
    agent_data = dto(
        "AgentView",
        agent_id="ag_audit",
        name="审计官",
        space_id=None,
        identity_state="available",
        work_state="waiting",
        model_profile_id="profile-deepseek",
        purpose="全面审计代码与风险",
    )
    sched_data = dto(
        "BackgroundSchedulingView",
        state="paused",
        reason_code="ADMISSION_LIMIT_REACHED",
    )
    transport = FakeBusinessTransport({
        "spaces.list": dto("Result_spaces_list", items=[], coverage="complete", next_cursor=None),
        "background_agents.list": dto(
            "Result_background_agents_list",
            items=[agent_data],
            coverage="complete",
            next_cursor=None,
        ),
        "background_agents.get": dto(
            "Result_background_agents_get",
            agent=agent_data,
            scheduling=sched_data,
        ),
        "subagents.list": dto(
            "Result_subagents_list",
            items=[
                dto("SubagentView", run_id="sub-1", status="running"),
                dto("SubagentView", run_id="sub-2", status="done"),
            ],
            coverage="complete",
            next_cursor=None,
        ),
        "usage.get": dto(
            "Result_usage_get",
            actual=dto("UsageMeasure", input_tokens=10000, output_tokens=2800),
        ),
    })
    projection = TuiBackgroundAgents(BusinessClient(transport))
    projection.refresh()
    projection.load_root()
    projection.load_scheduling("ag_audit")
    lines = projection.lines(space_id=None)
    combined = "\n".join(lines)
    assert "模型：profile-deepseek" in combined
    assert "调度：paused（阻断：ADMISSION_LIMIT_REACHED）" in combined
    assert "审计官" in combined

    # 4. 验证展开与有界窗口（AGENTS.md Rule 13）
    assert not projection.is_expanded("ag_audit")
    assert projection.toggle_expanded("ag_audit") is True
    assert projection.is_expanded("ag_audit") is True

    # 载入子代理与用量检查
    children_result = projection.load_subagents("ag_audit", limit=5)
    projection.load_usage("ag_audit")
    expanded_lines = projection.lines()
    exp_text = "\n".join(expanded_lines)
    # 缺少准确execution树不能将后台agent_id冒充parent_run_id取子代理。
    assert children_result["data"]["coverage"] == "partial"
    assert "子代理 [sub-1]" not in exp_text
    assert "用量：12800 tokens" in exp_text

    # 验证滑动窗口有界性（不超过 100 项）
    for i in range(150):
        projection.toggle_expanded(f"item-{i}")
    assert len(projection._expanded) <= 100


# LLM: 验证真实线网环境下，自然语言与位置参数 Slash 命令通过 Gateway 进行 CAS 乐观锁自动推导与闭环。
# 函数用途: 端到端验证 /space rename, /space archive, /space restore, /team pause, /team resume, /team model, /team retire 的位置命令通过 consume_business_command 对真实 HTTP 网关的无缝调用。
def test_positional_cli_slash_commands_cas_resolution_roundtrip(tmp_path, monkeypatch):
    home = tmp_path / "cli-cas-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=True,
            business_spaces_enabled=True,
            memory_path="memory.jsonl",
            my_agent_home=str(home),
        ),
        tmp_path / "workspace",
    )
    wire_business_host_ports(agent)
    server = GatewayHTTPServer(
        0,
        gateway_paths_from_root(agent.home_paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=agent),
    )
    server.start()
    try:
        port = server.server.server_address[1]

        def http_transport(path: str, payload: dict, timeout: float = 10.0) -> tuple[int, dict]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            conn.request(
                "POST",
                path,
                json.dumps(payload).encode("utf-8"),
                {
                    "Content-Type": "application/json",
                    "X-Gateway-Token": server._local_client_credential,
                    "X-User-Id": "business-alice",
                    "X-Channel": "feishu",
                    "Host": f"127.0.0.1:{port}",
                },
            )
            res = conn.getresponse()
            return res.status, json.loads(res.read())

        client = BusinessClient(http_transport)
        session = BusinessCommandSession(client)

        # 1. /space create
        res_create = consume_business_command("/space create 空间Alpha", session=session)
        assert res_create["ok"] is True
        space_id = res_create["data"]["space_id"]
        assert res_create["data"]["name"] == "空间Alpha"
        assert res_create["data"]["revision"] == 0

        # 2. /space rename (自动拉取 spaces.get 获取 revision 0)
        res_rename = consume_business_command(f"/space rename {space_id} 空间Alpha-新", session=session)
        assert res_rename["ok"] is True
        assert res_rename["data"]["space"]["name"] == "空间Alpha-新"
        assert res_rename["data"]["space"]["revision"] == 1

        # 3. /space archive (自动拉取 spaces.get 获取 revision 1)
        res_archive = consume_business_command(f"/space archive {space_id}", session=session)
        assert res_archive["ok"] is True
        assert res_archive["data"]["space"]["state"] == "archived"
        assert res_archive["data"]["space"]["revision"] == 2

        # 4. /space restore (自动拉取 spaces.get 获取 revision 2)
        res_restore = consume_business_command(f"/space restore {space_id}", session=session)
        assert res_restore["ok"] is True
        assert res_restore["data"]["space"]["state"] == "active"
        assert res_restore["data"]["space"]["revision"] == 3

        # 5. /team create
        res_agent = consume_business_command(f"/team create {space_id} 助手1", session=session)
        assert res_agent["ok"] is True
        agent_id = res_agent["data"]["agent"]["agent_id"]

        # 6. /team pause (自动拉取 background_agents.get 获取 identity_revision)
        res_pause = consume_business_command(f"/team pause {agent_id}", session=session)
        assert res_pause["ok"] is True
        assert res_pause["data"]["agent"]["identity_state"] == "suspended"

        # 7. /team resume (自动拉取 background_agents.get 获取 identity_revision)
        res_resume = consume_business_command(f"/team resume {agent_id}", session=session)
        assert res_resume["ok"] is True
        assert res_resume["data"]["agent"]["identity_state"] == "available"

        # 8. /team model (自动拉取 background_agents.model.get 获取 selection_revision)
        res_model = consume_business_command(f"/team model {agent_id} default", session=session)
        assert res_model["ok"] is True

        # 9. /team retire 带简写 'cancel' 归一化为 'cancel_tasks' (自动拉取 identity_revision)
        res_retire = consume_business_command(f"/team retire {agent_id} cancel", session=session)
        assert res_retire["ok"] is True
        assert res_retire["data"]["agent"]["identity_state"] == "retired"
    finally:
        server.stop()
