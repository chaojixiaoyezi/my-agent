from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store import MemoryRecord
from agent_py_agent.agent.settings import AgentConfig


def test_simple_agent_initializes_my_agent_home(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"

    agent = SimpleAgent(
        AgentConfig(tool_protocol="text", my_agent_home=str(home), prompt_files=[]), repo
    )

    assert agent.home_paths.root == home.resolve()
    assert agent.home_paths.config_dir.is_dir()
    assert not agent.home_paths.workspace_tasks_dir.exists()
    assert agent.home_paths.owner_memory_daily_dir.is_dir()
    assert agent.home_paths.owner_memory_lessons_dir.is_dir()
    assert agent.home_paths.soul_md.exists()
    assert agent.home_paths.memory_md.exists()


def test_simple_agent_rewrites_runtime_config_paths_to_owner_home(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"

    agent = SimpleAgent(
        AgentConfig(tool_protocol="text", my_agent_home=str(home), prompt_files=[]), repo
    )

    owner_home = home / "owners" / "local" / "main"
    assert agent.config.memory_path == str(owner_home / "memory" / "long_term" / "memory.jsonl")
    assert agent.config.session_workspace == str(owner_home / "sessions")
    assert "/data/" not in agent.config.subagent_workspace
    assert "/data/" not in agent.config.gateway_workspace
    assert "/data/" not in agent.config.local_store_path
    assert (
        str(owner_home / "workspace" / "runtime" / "workspaces") in agent.config.subagent_workspace
    )
    assert (
        str(owner_home / "workspace" / "runtime" / "workspaces") in agent.config.gateway_workspace
    )
    assert str(owner_home / "workspace" / "runtime" / "workspaces") in agent.config.local_store_path
    assert not (repo / "data").exists()


def test_saved_run_creates_home_task_workspace(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    agent.run("做一个示例网站", request_id="req-1", run_id="run-1", task_id="示例网站 E2E")

    task_root = (
        home / "owners" / "local" / "main" / "tasks" / date.today().isoformat() / "示例网站-e2e"
    )
    assert (task_root / "output").is_dir()
    assert (task_root / "work").is_dir()
    assert (task_root / "work" / "runtime").is_dir()
    assert (task_root / "work" / "agents").is_dir()
    assert (task_root / "work" / "task.yaml").exists()
    workspace = json.loads((task_root / "work" / "run_workspace.json").read_text(encoding="utf-8"))
    state = json.loads((task_root / "work" / "state.json").read_text(encoding="utf-8"))
    assert workspace["request_id"] == "req-1"
    assert workspace["run_id"] == "run-1"
    assert workspace["task_id"] == "示例网站 E2E"
    assert workspace["task_root"] == str(task_root)
    assert workspace["output_dir"] == str(task_root / "output")
    assert workspace["work_dir"] == str(task_root / "work")
    assert workspace["owner_id"] == "local/main"
    assert workspace["owner_home"] == str((home / "owners" / "local" / "main").resolve())
    assert state["primary_run_id"] == "run-1"
    assert (task_root / "work" / "timeline.jsonl").read_text(encoding="utf-8").strip()
    assert not (home / "tasks").exists()


def test_saved_run_uses_prompt_slug_when_only_machine_ids_are_available(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    agent.run("分析多个项目源码并写一份中文报告", request_id="gw-123", run_id="run-456")

    date_root = home / "owners" / "local" / "main" / "tasks" / date.today().isoformat()
    task_dirs = [item for item in date_root.iterdir() if item.is_dir()]
    assert len(task_dirs) == 1
    assert task_dirs[0].name not in {"gw-123", "run-456"}
    assert not task_dirs[0].name.startswith(("gw-", "run-", "req-"))
    workspace = json.loads(
        (task_dirs[0] / "work" / "run_workspace.json").read_text(encoding="utf-8")
    )
    assert workspace["request_id"] == "gw-123"
    assert workspace["run_id"] == "run-456"


def test_same_prompt_new_run_reuses_task_workspace_with_timeline(tmp_path: Path):
    """R8 接力实锤后的语义裁决:同一 prompt 的新 run 复用同一任务目录接续
    (配置注释承诺"同一 prompt 会复用同一个任务目录,并用 work/timeline.jsonl
    记录多次 run";旧断言"各开一个目录"钉的是实现缺陷——接力会变重做,
    原 progress/产物/expected_outputs 声明全部失效)。"""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    prompt = "分析 all-agent 项目并写中文报告"
    agent.run(prompt, request_id="req-one", run_id="run-one")
    agent.run(prompt, request_id="req-two", run_id="run-two")

    date_root = home / "owners" / "local" / "main" / "tasks" / date.today().isoformat()
    task_dirs = sorted(item for item in date_root.iterdir() if item.is_dir())
    assert len(task_dirs) == 1, "同 prompt 接力必须复用同一任务目录"
    assert task_dirs[0].name == "分析-all-agent-项目并写中文报告"
    workspace = json.loads(
        (task_dirs[0] / "work" / "run_workspace.json").read_text(encoding="utf-8")
    )
    state = json.loads((task_dirs[0] / "work" / "state.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (task_dirs[0] / "work" / "refs" / "artifacts" / "manifest.json").read_text(encoding="utf-8")
    )
    timeline = [
        json.loads(line)
        for line in (task_dirs[0] / "work" / "timeline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert workspace["run_id"] == "run-two", "工作区身份随最新 run 更新"
    assert state["task_id"] == "run-two" and state["primary_run_id"] == "run-two"
    assert manifest["task_id"] == "run-two" and manifest["run_id"] == "run-two"
    assert 'task_id: "run-two"' in (task_dirs[0] / "work" / "task.yaml").read_text(encoding="utf-8")
    assert workspace["prompt_fingerprint"]
    assert len(timeline) == 4, "timeline 为每一次 run 记录开始与结构化终态"
    assert [
        (event["event_type"], event["run_id"], event.get("status", "")) for event in timeline
    ] == [
        ("run_workspace_saved", "run-one", ""),
        ("run_workspace_finished", "run-one", "DONE"),
        ("run_workspace_saved", "run-two", ""),
        ("run_workspace_finished", "run-two", "DONE"),
    ]


def test_same_task_resume_refreshes_run_identity_without_losing_workspace_facts(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    prompt = "持续维护同一个项目"
    agent.run(prompt, request_id="req-one", run_id="run-one", task_id="goal-one")
    task_root = home / "owners" / "local" / "main" / "tasks" / date.today().isoformat() / "goal-one"
    state_path = task_root / "work" / "state.json"
    manifest_path = task_root / "work" / "refs" / "artifacts" / "manifest.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "PAUSED",
            "progress": 0.6,
            "evidence_refs": ["output/report.md"],
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"] = [{"path": "output/report.md"}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    agent.run(prompt, request_id="req-two", run_id="run-two", task_id="goal-one")

    resumed_state = json.loads(state_path.read_text(encoding="utf-8"))
    resumed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert resumed_state["task_id"] == "goal-one"
    assert resumed_state["primary_run_id"] == "run-two"
    assert resumed_state["status"] == "DONE"
    assert resumed_state["progress"] == 1.0
    assert resumed_state["evidence_refs"] == ["output/report.md"]
    assert resumed_manifest["request_id"] == "req-two"
    assert resumed_manifest["run_id"] == "run-two"
    assert resumed_manifest["artifacts"] == [{"path": "output/report.md"}]


def test_workspace_identity_does_not_reuse_old_state_json(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    owner_tasks = home / "owners" / "local" / "main" / "tasks" / date.today().isoformat()
    old_task = owner_tasks / "分析-all-agent-项目并写中文报告"
    old_work = old_task / "work"
    old_work.mkdir(parents=True)
    (old_work / "state.json").write_text(
        json.dumps({"request_id": "req-one", "primary_run_id": "run-one"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (old_work / "task.yaml").write_text(
        'task_id: "分析-all-agent-项目并写中文报告"\nrequest_id: "req-one"\nrun_id: "run-one"\n',
        encoding="utf-8",
    )
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    agent.run("分析 all-agent 项目并写中文报告", request_id="req-one", run_id="run-one")

    assert not (old_work / "run_workspace.json").exists()
    new_task = owner_tasks / "分析-all-agent-项目并写中文报告-run-one"
    assert (new_task / "work" / "run_workspace.json").exists()


def test_long_project_prompt_gets_short_relevant_task_workspace_name(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    prompt = (
        "你现在只做一件事：认真阅读 /Users/example/study-agent/all-agent 下面的项目，"
        "分析这些项目的架构、模块和功能。\n\n要求：不要修改源码，最终写中文报告。"
    )
    agent.run(prompt, request_id="req-all-agent", run_id="run-all-agent")

    task_root = (
        home
        / "owners"
        / "local"
        / "main"
        / "tasks"
        / date.today().isoformat()
        / "all-agent-架构分析"
    )
    assert (task_root / "output").is_dir()
    workspace = json.loads((task_root / "work" / "run_workspace.json").read_text(encoding="utf-8"))
    task_yaml = (task_root / "work" / "task.yaml").read_text(encoding="utf-8")
    assert workspace["task_title"] == "all-agent-架构分析"
    assert workspace["prompt_fingerprint"]
    assert 'task_id: "run-all-agent"' in task_yaml
    assert 'task_title: "all-agent-架构分析"' in task_yaml


def test_two_provider_owners_write_separate_task_workspaces(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    feishu = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="u001",
            memory_path="memory-feishu.jsonl",
            prompt_files=[],
        ),
        repo,
    )
    wechat = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            my_agent_owner_provider="wechat",
            my_agent_owner_kind="user",
            my_agent_owner_id="u001",
            memory_path="memory-wechat.jsonl",
            prompt_files=[],
        ),
        repo,
    )

    feishu.run("整理飞书任务", request_id="req-f", run_id="run-f", task_id="任务A")
    wechat.run("整理微信任务", request_id="req-w", run_id="run-w", task_id="任务A")

    feishu_root = home / "owners" / "providers" / "feishu" / "users" / "u001" / "tasks"
    wechat_root = home / "owners" / "providers" / "wechat" / "users" / "u001" / "tasks"
    assert (feishu_root / date.today().isoformat() / "任务a" / "work" / "state.json").exists()
    assert (wechat_root / date.today().isoformat() / "任务a" / "work" / "state.json").exists()
    assert feishu.home_paths.owner_id == "providers/feishu/users/u001"
    assert wechat.home_paths.owner_id == "providers/wechat/users/u001"


def test_saved_run_writes_main_context_bundle_v1(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    result = agent.run(
        "做一个示例网站", request_id="req-ctx", run_id="run-ctx", task_id="主代理任务"
    )

    assert "# Main Agent Context Bundle v1" in result.prompt
    task_root = (
        home / "owners" / "local" / "main" / "tasks" / date.today().isoformat() / "主代理任务"
    )
    assert "# Current Task Workspace" in result.prompt
    assert f"- output_dir: {task_root / 'output'}" in result.prompt
    assert f"- work_dir: {task_root / 'work'}" in result.prompt
    assert result.main_context_bundle_path
    bundle_path = Path(result.main_context_bundle_path)
    assert bundle_path.exists()
    assert str(bundle_path).startswith(str(home / "owners" / "local" / "main" / "memory_archive"))
    assert not (home / "memory_archive" / "snapshots" / "context_bundles").exists()
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "main_context_bundle.v1"
    assert payload["identity"]["owner_type"] == "main_agent"
    assert payload["scope"]["request_id"] == "req-ctx"
    assert payload["scope"]["run_id"] == "run-ctx"
    assert payload["scope"]["task_id"] == "主代理任务"
    assert payload["workspace_refs"]["primary_workspace_root"] == str(repo.resolve())
    assert payload["workspace_refs"]["my_agent_home"] == str(home.resolve())
    assert payload["workspace_refs"]["owner_home"] == str(
        (home / "owners" / "local" / "main").resolve()
    )
    assert payload["workspace_refs"]["owner_tasks_root"] == str(
        (home / "owners" / "local" / "main" / "tasks").resolve()
    )
    assert "workspace_tasks_root" not in payload["workspace_refs"]
    assert payload["owner_model"]["task_workspace_refs"]["owner_tasks_root"] == str(
        (home / "owners" / "local" / "main" / "tasks").resolve()
    )
    assert payload["task"]["attributes"]["run_workspace"]["output_dir"] == str(task_root / "output")
    assert payload["task"]["attributes"]["run_workspace"]["work_dir"] == str(task_root / "work")
    assert "workspace_tasks_root" not in payload["owner_model"]["task_workspace_refs"]
    assert payload["task"]["user_prompt_preview"] == "做一个示例网站"
    assert result.main_context_bundle_markdown_path
    assert Path(result.main_context_bundle_markdown_path).exists()


def test_no_save_run_keeps_main_context_bundle_ephemeral(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    result = agent.run(
        "临时诊断", save=False, request_id="req-nosave", run_id="run-nosave", task_id="诊断"
    )

    assert "# Main Agent Context Bundle v1" in result.prompt
    assert result.main_context_bundle_path == ""
    assert result.main_context_bundle_markdown_path == ""
    assert not (home / "memory_archive" / "snapshots" / "context_bundles").exists()


def test_task_local_run_does_not_inject_main_context_bundle(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    run_home = tmp_path / "task" / "work" / "agents" / "run-local"
    run_home.mkdir(parents=True)
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    result = agent.run(
        "隔离任务",
        save=False,
        request_id="req-local",
        run_id="run-local",
        task_id="局部任务",
        context_scope="task_local",
        task_attributes={"agent_run_workspace_dir": str(run_home)},
    )

    assert "# Main Agent Context Bundle v1" not in result.prompt
    assert result.main_context_bundle_path == ""


def test_no_save_run_does_not_create_task_workspace_or_daily_memory(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)

    agent.run("临时诊断", save=False, request_id="req-nosave", run_id="run-nosave", task_id="诊断")

    task_root = home / "tasks" / date.today().isoformat() / "诊断"
    daily_path = home / "memory" / "daily" / f"{date.today().isoformat()}.jsonl"
    assert not task_root.exists()
    assert not (repo / "memory.jsonl").exists()
    assert not daily_path.exists()


def test_remember_promotes_owner_fact_without_daily_or_ops_body_mirror(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        tool_protocol="text", my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]
    )
    agent = SimpleAgent(cfg, repo)
    legacy_path = repo / "memory.jsonl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "role": "user",
                "content": "旧记忆仍可搜索",
                "kind": "preference",
                "tags": [],
                "created_at": 1.0,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    agent.remember("项目青黛的交付格式是表格", kind="fact")

    owner_memory = home / "owners" / "local" / "main" / "memory" / "long_term" / "memory.jsonl"
    owner_records = [
        json.loads(line) for line in owner_memory.read_text(encoding="utf-8").splitlines()
    ]
    daily_path = (
        home
        / "owners"
        / "local"
        / "main"
        / "memory"
        / "daily"
        / f"{date.today().isoformat()}.jsonl"
    )
    ops_path = home / "owners" / "local" / "main" / "memory" / "ops.jsonl"
    ops_records = [json.loads(line) for line in ops_path.read_text(encoding="utf-8").splitlines()]

    assert owner_records[-1]["content"] == "项目青黛的交付格式是表格"
    assert owner_records[-1]["kind"] == "fact"
    assert not daily_path.exists()
    assert ops_records
    assert all("content" not in row for row in ops_records)
    assert agent.memory.search("旧记忆", top_k=1) == []


def test_prompt_builder_uses_only_recalled_memory_envelope(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(
        my_agent_home=str(home),
        prompt_files=[],
        home_lesson_auto_read_limit=2,
    )
    agent = SimpleAgent(cfg, repo)
    agent.home_paths.owner_memory_md.write_text("记住：产物目录必须干净。\n", encoding="utf-8")
    (agent.home_paths.owner_memory_lessons_dir / "subagent.md").write_text(
        "子代理教训：路径必须由上层传递。\n", encoding="utf-8"
    )
    (agent.home_paths.owner_memory_lessons_dir / "video.md").write_text(
        "视频教训：不用读。\n", encoding="utf-8"
    )

    recalled = MemoryRecord(
        role="user",
        content="正式长期事实：青黛项目产物目录必须干净。",
        kind="fact",
        entry_id="mem-formal-1",
        attributes={"origin": "reviewed", "scope_type": "personal", "scope_key": "personal"},
    )
    prompt = agent.prompts.build("测试 subagent 派工", [recalled])

    assert prompt.count("<memory-context>") == 1
    assert "正式长期事实：青黛项目产物目录必须干净。" in prompt
    assert "记住：产物目录必须干净。" not in prompt
    assert "子代理教训：路径必须由上层传递。" not in prompt
    assert "视频教训：不用读。" not in prompt


def test_prompt_builder_reads_home_entry_files_every_round(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(tool_protocol="text", my_agent_home=str(home), prompt_files=[])
    agent = SimpleAgent(cfg, repo)
    agent.home_paths.owner_soul_md.write_text("人格规则：先证据后判断。\n", encoding="utf-8")
    agent.home_paths.owner_user_md.write_text("用户偏好：短汇报但要有验证。\n", encoding="utf-8")
    agent.home_paths.owner_agents_md.write_text("执行制度：每轮读关键文件。\n", encoding="utf-8")
    agent.home_paths.owner_memory_md.write_text("关键记忆：产物目录要干净。\n", encoding="utf-8")

    prompt = agent.prompts.build("继续测试")

    assert "# Home Entry: ASSISTANT PERSONA" in prompt
    assert "人格规则：先证据后判断。" in prompt
    assert "# Home Entry: CURRENT USER OR GROUP PROFILE" in prompt
    assert "用户偏好：短汇报但要有验证。" in prompt
    assert "# Home Entry: LONG-TERM WORKING AGREEMENT" in prompt
    assert "执行制度：每轮读关键文件。" in prompt
    assert "# Home Entry: memory.md" not in prompt
    assert "关键记忆：产物目录要干净。" not in prompt
    assert prompt.index("# Home Entry: LONG-TERM WORKING AGREEMENT") < prompt.index(
        "# Home Entry: ASSISTANT PERSONA"
    )
    assert prompt.index("# Home Entry: ASSISTANT PERSONA") < prompt.index(
        "# Home Entry: CURRENT USER OR GROUP PROFILE"
    )


def test_prompt_builder_ignores_legacy_root_home_files(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(tool_protocol="text", my_agent_home=str(home), prompt_files=[])
    agent = SimpleAgent(cfg, repo)
    agent.home_paths.owner_memory_md.write_text("owner 当前记忆。\n", encoding="utf-8")
    agent.home_paths.memory_md.write_text("旧根污染记忆。\n", encoding="utf-8")
    agent.home_paths.memory_hot_md.write_text("旧根 HOT 污染。\n", encoding="utf-8")

    prompt = agent.prompts.build("继续测试")

    assert "owner 当前记忆。" not in prompt
    assert "旧根污染记忆。" not in prompt
    assert "旧根 HOT 污染。" not in prompt
