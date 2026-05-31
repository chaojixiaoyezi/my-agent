from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


# LLM: home runtime tests prove the new ~/.my-agent layout is used by real SimpleAgent runs, not only docs.
# 函数用途: 验证 SimpleAgent 启动时会初始化 my-agent 家目录，并暴露 home_paths 给运行时后续模块使用。
def test_simple_agent_initializes_my_agent_home(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"

    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), repo)

    assert agent.home_paths.root == home.resolve()
    assert agent.home_paths.config_dir.is_dir()
    assert not agent.home_paths.workspace_tasks_dir.exists()
    assert agent.home_paths.memory_daily_dir.is_dir()
    assert agent.home_paths.memory_lessons_dir.is_dir()
    assert agent.home_paths.soul_md.exists()
    assert agent.home_paths.memory_md.exists()


# LLM: saved runs should get a clean task workspace while old repo-relative memory paths keep working.
# 函数用途: 验证普通 run 保存后，会在 home/tasks/date/task 下创建 output 交付区和 work 过程区。
def test_saved_run_creates_home_task_workspace(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[])
    agent = SimpleAgent(cfg, repo)

    agent.run("做一个示例网站", request_id="req-1", run_id="run-1", task_id="示例网站 E2E")

    task_root = home / "tasks" / date.today().isoformat() / "示例网站-e2e"
    assert (task_root / "output").is_dir()
    assert (task_root / "work").is_dir()
    assert (task_root / "work" / "runtime").is_dir()
    assert (task_root / "work" / "agents").is_dir()
    assert (task_root / "work" / "task.yaml").exists()
    state = json.loads((task_root / "work" / "state.json").read_text(encoding="utf-8"))
    assert state["request_id"] == "req-1"
    assert state["run_id"] == "run-1"
    assert state["task_id"] == "示例网站 E2E"
    assert (task_root / "work" / "timeline.jsonl").read_text(encoding="utf-8").strip()


# LLM: main context bundle tests pin the root-agent prompt contract before implementation.
# 函数用途: 验证普通保存 run 会生成主代理 context bundle，并把 refs-only 交接信息注入 prompt。
def test_saved_run_writes_main_context_bundle_v1(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[])
    agent = SimpleAgent(cfg, repo)

    result = agent.run("做一个示例网站", request_id="req-ctx", run_id="run-ctx", task_id="主代理任务")

    assert "# Main Agent Context Bundle v1" in result.prompt
    assert result.main_context_bundle_path
    bundle_path = Path(result.main_context_bundle_path)
    assert bundle_path.exists()
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "main_context_bundle.v1"
    assert payload["identity"]["owner_type"] == "main_agent"
    assert payload["scope"]["request_id"] == "req-ctx"
    assert payload["scope"]["run_id"] == "run-ctx"
    assert payload["scope"]["task_id"] == "主代理任务"
    assert payload["workspace_refs"]["primary_workspace_root"] == str(repo.resolve())
    assert payload["workspace_refs"]["my_agent_home"] == str(home.resolve())
    assert payload["task"]["user_prompt_preview"] == "做一个示例网站"
    assert result.main_context_bundle_markdown_path
    assert Path(result.main_context_bundle_markdown_path).exists()


# LLM: save=False remains a persistence boundary even when the prompt gets an ephemeral context bundle.
# 函数用途: 验证临时 run 可以看到主代理上下文说明，但不会写 context bundle 文件。
def test_no_save_run_keeps_main_context_bundle_ephemeral(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[])
    agent = SimpleAgent(cfg, repo)

    result = agent.run("临时诊断", save=False, request_id="req-nosave", run_id="run-nosave", task_id="诊断")

    assert "# Main Agent Context Bundle v1" in result.prompt
    assert result.main_context_bundle_path == ""
    assert result.main_context_bundle_markdown_path == ""
    assert not (home / "memory_archive" / "snapshots" / "context_bundles").exists()


# LLM: task-local runs must not inherit main-agent owner context through the new bundle path.
# 函数用途: 验证子代理/控制面隔离上下文不会注入主代理 context bundle。
def test_task_local_run_does_not_inject_main_context_bundle(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[])
    agent = SimpleAgent(cfg, repo)

    result = agent.run(
        "隔离任务",
        save=False,
        request_id="req-local",
        run_id="run-local",
        task_id="局部任务",
        context_scope="task_local",
    )

    assert "# Main Agent Context Bundle v1" not in result.prompt
    assert result.main_context_bundle_path == ""


# LLM: no-save must remain a hard persistence boundary even after home task workspaces are added.
# 函数用途: 验证 save=False 不会创建任务工作区，也不会写 legacy memory 或 daily mirror。
def test_no_save_run_does_not_create_task_workspace_or_daily_memory(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[])
    agent = SimpleAgent(cfg, repo)

    agent.run("临时诊断", save=False, request_id="req-nosave", run_id="run-nosave", task_id="诊断")

    task_root = home / "tasks" / date.today().isoformat() / "诊断"
    daily_path = home / "memory" / "daily" / f"{date.today().isoformat()}.jsonl"
    assert not task_root.exists()
    assert not (repo / "memory.jsonl").exists()
    assert not daily_path.exists()


# LLM: daily memory mirror keeps long-term memory day-sharded without removing legacy memory.jsonl yet.
# 函数用途: 验证每次写入旧 memory_path 时，也会镜像到 home/memory/daily/YYYY-MM-DD.jsonl。
def test_memory_add_mirrors_to_daily_home_ledger(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[])
    agent = SimpleAgent(cfg, repo)

    agent.remember("用户喜欢表格", kind="preference")

    legacy_records = (repo / "memory.jsonl").read_text(encoding="utf-8").splitlines()
    daily_path = home / "memory" / "daily" / f"{date.today().isoformat()}.jsonl"
    daily_records = daily_path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(daily_records[-1])

    assert legacy_records
    assert payload["kind"] == "preference"
    assert payload["role"] == "user"
    assert payload["content"] == "用户喜欢表格"


# LLM: home prompt context should read key memory every time and only matching lessons by simple filename signal.
# 函数用途: 验证 prompt 会带上 memory.md 关键记忆，并按任务关键词读取有限数量的 lesson 文件。
def test_prompt_builder_reads_key_memory_and_matching_lessons(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(my_agent_home=str(home), prompt_files=[], home_lesson_auto_read_limit=1)
    agent = SimpleAgent(cfg, repo)
    agent.home_paths.memory_md.write_text("记住：产物目录必须干净。\n", encoding="utf-8")
    (agent.home_paths.memory_lessons_dir / "subagent.md").write_text("子代理教训：路径必须由上层传递。\n", encoding="utf-8")
    (agent.home_paths.memory_lessons_dir / "video.md").write_text("视频教训：不用读。\n", encoding="utf-8")

    prompt = agent.prompts.build("测试 subagent 派工")

    assert "记住：产物目录必须干净。" in prompt
    assert "子代理教训：路径必须由上层传递。" in prompt
    assert "视频教训：不用读。" not in prompt


# LLM: root prompts should load the owner entry files every round, not only memory.md.
# 函数用途: 验证 SOUL/USER/AGENTS/memory 四个家目录关键文件会进入每轮 prompt。
def test_prompt_builder_reads_home_entry_files_every_round(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    cfg = AgentConfig(my_agent_home=str(home), prompt_files=[])
    agent = SimpleAgent(cfg, repo)
    agent.home_paths.soul_md.write_text("人格规则：先证据后判断。\n", encoding="utf-8")
    agent.home_paths.user_md.write_text("用户偏好：短汇报但要有验证。\n", encoding="utf-8")
    agent.home_paths.agents_md.write_text("执行制度：每轮读关键文件。\n", encoding="utf-8")
    agent.home_paths.memory_md.write_text("关键记忆：产物目录要干净。\n", encoding="utf-8")

    prompt = agent.prompts.build("继续测试")

    assert "# Home Entry: SOUL.md" in prompt
    assert "人格规则：先证据后判断。" in prompt
    assert "# Home Entry: USER.md" in prompt
    assert "用户偏好：短汇报但要有验证。" in prompt
    assert "# Home Entry: AGENTS.md" in prompt
    assert "执行制度：每轮读关键文件。" in prompt
    assert "# Home Entry: memory.md" in prompt
    assert "关键记忆：产物目录要干净。" in prompt
    assert prompt.index("# Home Entry: AGENTS.md") < prompt.index("# Home Entry: SOUL.md")
    assert prompt.index("# Home Entry: SOUL.md") < prompt.index("# Home Entry: USER.md")
    assert prompt.index("# Home Entry: USER.md") < prompt.index("# Home Entry: memory.md")
