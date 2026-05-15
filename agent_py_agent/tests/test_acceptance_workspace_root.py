import json
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.acceptance_review_service import AcceptanceReviewOptions


# LLM: _write_static_page creates a tiny real artifact outside the manager's primary workspace.
# 函数用途: 准备多 workspace 验收测试用的 HTML 产物，确保 static_site_check 有真实文件可检查。
def _write_static_page(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """<!doctype html>
<html>
<head><title>Luma Home</title></head>
<body>
<nav><a href="#home">Home</a><a href="#products">Products</a><a href="#contact">Contact</a></nav>
<section id="home"><h1>Luma Home</h1><button onclick="document.getElementById('products').scrollIntoView()">Explore</button></section>
<section id="products"><h2>Signature furniture</h2></section>
<section id="contact"><h2>Visit our studio</h2></section>
<footer id="footer"><a href="#home">Back home</a></footer>
</body>
</html>
""",
        encoding="utf-8",
    )


# LLM: Parent acceptance should choose the workspace that contains the artifact, not the first CLI root.
# 函数用途: 复现真实 E2E 中 artifact 在 my-claude-code 下，但验收误扫代码仓库的问题。
def test_acceptance_tests_use_workspace_root_containing_artifact(tmp_path: Path):
    primary = tmp_path / "repo-root"
    secondary = tmp_path / "user-workspace"
    artifact = secondary / "deliverables" / "furniture-home" / "index.html"
    _write_static_page(artifact)
    agent = _agent_with_workspace_roots(primary, secondary)
    task = _acceptance_task_with_artifact(agent, artifact)

    agent.subagents.review_acceptance(
        task.id,
        options=AcceptanceReviewOptions(apply=False, execute_tests=True),
    )

    report = json.loads((Path(task.reports_dir) / "test_execution.json").read_text(encoding="utf-8"))
    record = report["records"][0]
    assert report["workspace_root"] == str(secondary.resolve())
    assert report["total_tests"] == 1
    assert report["failed"] == 0
    assert record["validation_result"]["checked_root"] == str(artifact.parent.resolve())


# LLM: _agent_with_workspace_roots creates the minimal echo agent for multi-root acceptance tests.
# 函数用途: 把 agent 初始化从测试主体中拆出，保留 primary/secondary workspace 语义。
def _agent_with_workspace_roots(primary: Path, secondary: Path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        primary,
        workspace_roots=[primary, secondary],
    )


# LLM: _acceptance_task_with_artifact creates and persists a task pointing at the secondary artifact.
# 函数用途: 准备待父级验收的子代理记录和 output refs，复现多工作区产物场景。
def _acceptance_task_with_artifact(agent: SimpleAgent, artifact: Path):
    task = agent.subagents.create_run(
        goal=f"Create furniture homepage at {artifact}",
        thought="artifact lives in secondary workspace",
        plan=["write html"],
        acceptance_checks=["index.html must pass static site checks"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    task.allowed_write_roots = [str(artifact)]
    agent.subagents.save(task)
    _write_acceptance_output_refs(task, artifact)
    return task


# LLM: _write_acceptance_output_refs writes the minimal output and runner result files for review.
# 函数用途: 让 review_acceptance 能读取 artifacts 和 runner success facts，不依赖真实模型运行。
def _write_acceptance_output_refs(task, artifact: Path) -> None:
    Path(task.output_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "status": "AWAITING_ACCEPTANCE",
                "summary": "site written",
                "artifacts": [{"path": str(artifact), "kind": "file"}],
                "tests": [],
                "blockers": [],
                "patches": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    Path(task.runner_result_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "structured_output_found": True,
                "structured_output_ok": True,
                "structured_parse_error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
