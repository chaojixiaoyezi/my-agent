from __future__ import annotations

import json
import re
import time
from pathlib import Path

from agent_py_agent.agent.backends import BaseBackend, ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.static_site import run_static_site_check


class NaturalFurnitureRootBackend(BaseBackend):
    name = "natural_furniture_root_backend"

    def __init__(self, site_dir: Path):
        self.site_dir = site_dir
        self.prompts: list[str] = []
        self.root_prompts: list[str] = []
        self.runner = NaturalFurnitureRunnerBackend(site_dir)

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        if "# SubAgent Runner Task" in prompt:
            return self.runner.generate(prompt, on_chunk=on_chunk)
        self.root_prompts.append(prompt)
        if len(self.root_prompts) == 1:
            assert "用单文件html做一个高端现代家具品牌的网站首页" in prompt
            return ModelResponse(text=_tool_call(self._create_payload()), backend=self.name)
        if len(self.root_prompts) == 2:
            assert "create_subagents" in prompt
            return ModelResponse(text=_tool_call(_root_dispatch_payload()), backend=self.name)
        assert "dispatch_subagents" in prompt
        return ModelResponse(text="小傻妞团队已交付高端现代家具品牌首页，产物和验收线索都已写入。", backend=self.name)

    def _create_payload(self) -> dict[str, object]:
        return {
            "tool": "create_subagents",
            "dry_run": False,
            "count": 1,
            "role": "coordinator",
            "agent_name": "小傻妞-家具总控",
            "tool_preset": "coding",
            "extra_write_roots": [str(self.site_dir)],
            "goal": (
                f"请统筹完成高端现代家具品牌首页，最终只需要在 {self.site_dir / 'index.html'} "
                "交付单文件 HTML。不要写注释，不要使用失效图片，不要有失灵按钮。"
                "你可以自己做，也可以继续派下一层小小傻妞完成具体实现。"
            ),
            "plan": ["理解用户审美要求", "决定是否继续派下一层", "汇总产物 refs 和验收状态"],
            "acceptance_checks": [
                "index.html 必须存在",
                "页面必须是完整 HTML",
                "按钮或链接不能是空壳",
                "最终只汇报 refs，不把大段正文塞回主代理",
            ],
        }


class NaturalFurnitureRunnerBackend(BaseBackend):
    name = "natural_furniture_runner_backend"

    def __init__(self, site_dir: Path):
        self.site_dir = site_dir
        self.prompts: list[str] = []
        self.coordinator_calls = 0
        self.leaf_calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        agent_name = _agent_name_from_prompt(prompt)
        if "家具叶子" in agent_name:
            return self._leaf_response()
        if "家具总控" in agent_name:
            return self._coordinator_response()
        raise AssertionError(f"unknown runner agent: {agent_name}")

    def _coordinator_response(self) -> ModelResponse:
        self.coordinator_calls += 1
        if self.coordinator_calls == 1:
            return ModelResponse(text=_tool_call(self._schedule_leaf_payload()), backend=self.name)
        if self.coordinator_calls == 2:
            return ModelResponse(text=_tool_call(_child_dispatch_payload()), backend=self.name)
        return ModelResponse(text=_subagent_result("小傻妞已让小小傻妞完成 index.html，并完成本地验收。"), backend=self.name)

    def _leaf_response(self) -> ModelResponse:
        self.leaf_calls += 1
        if self.leaf_calls == 1:
            return ModelResponse(text=_tool_call(self._write_html_payload()), backend=self.name)
        return ModelResponse(text=_subagent_result(f"小小傻妞已写入 {self.site_dir / 'index.html'}。"), backend=self.name)

    def _schedule_leaf_payload(self) -> dict[str, object]:
        return {
            "tool": "schedule_child_subagents",
            "dry_run": False,
            "children": [
                {
                    "role": "worker",
                    "agent_name": "家具叶子",
                    "extra_write_roots": [str(self.site_dir)],
                    "goal": (
                        f"在 {self.site_dir / 'index.html'} 写一个高端现代家具品牌首页。"
                        "只输出完整 HTML，不写注释；按钮或链接必须有真实跳转或动作。"
                    ),
                    "plan": ["写单文件 HTML", "确认本地链接和按钮", "返回 refs"],
                    "acceptance_checks": ["index.html 存在", "HTML 完整", "无空壳按钮"],
                }
            ],
        }

    def _write_html_payload(self) -> dict[str, object]:
        return {
            "tool": "write_file",
            "path": str(self.site_dir / "index.html"),
            "content": _furniture_html(),
        }


def _tool_call(payload: dict[str, object]) -> str:
    return "[TOOL_CALL]\n" + json.dumps(payload, ensure_ascii=False) + "\n[/TOOL_CALL]"


def _root_dispatch_payload() -> dict[str, object]:
    return {"tool": "dispatch_subagents", "dry_run": False, "max_runners": 1}


def _child_dispatch_payload() -> dict[str, object]:
    return {"tool": "dispatch_subagents", "dry_run": False, "max_runners": 1}


def _subagent_result(summary: str) -> str:
    return (
        "[SUBAGENT_RESULT]\n"
        + json.dumps(
            {
                "status": "DONE",
                "summary": summary,
                "used_tools": ["schedule_child_subagents", "dispatch_subagents", "write_file"],
                "used_skills": [],
                "evidence": [{"kind": "note", "summary": summary, "ok": True}],
                "evidence_packets": [
                    {
                        "id": "evpkt-natural-furniture",
                        "claim": summary,
                        "checked_scope": "natural-language-e2e",
                        "evidence_refs": ["runner_result.json"],
                        "artifact_refs": ["index.html"],
                        "confidence": 0.9,
                    }
                ],
                "capability_requests": [],
                "artifacts": [{"path": "index.html", "kind": "html", "summary": "家具首页"}],
                "tests": [{"name": "static-site-smoke", "command": "static_site_check", "ok": True, "summary": "通过"}],
                "patches": [],
                "lessons": [],
                "next_actions": [],
                "blocked_reason": "",
                "failure_type": "",
            },
            ensure_ascii=False,
        )
        + "\n[/SUBAGENT_RESULT]"
    )


def _agent_name_from_prompt(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.startswith("- agent:"):
            return line.partition(":")[2].strip()
    scoped_prompt = prompt[prompt.find("## Execution Context JSON") :] if "## Execution Context JSON" in prompt else prompt
    match = re.search(r'"agent_name":\s*"([^"]+)"', scoped_prompt)
    if match:
        return match.group(1)
    return ""


def _furniture_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maison Lune</title>
<style>
body{margin:0;font-family:Inter,Arial,sans-serif;color:#171717;background:#f6f3ee}
.hero{min-height:88vh;display:grid;align-items:center;padding:7vw;background:linear-gradient(120deg,#f6f3ee,#d8d1c4)}
.nav{position:fixed;top:0;left:0;right:0;display:flex;justify-content:space-between;padding:22px 7vw;background:rgba(246,243,238,.78);backdrop-filter:blur(14px)}
h1{font-size:clamp(42px,8vw,112px);line-height:.92;margin:0;max-width:980px}
p{font-size:18px;line-height:1.7;max-width:620px}
.actions{display:flex;gap:14px;flex-wrap:wrap;margin-top:28px}
a,button{border:1px solid #171717;background:#171717;color:white;padding:14px 20px;text-decoration:none;cursor:pointer}
button.secondary{background:transparent;color:#171717}
#collections{padding:80px 7vw;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}
.item{min-height:220px;padding:24px;background:white;display:flex;align-items:end}
@media(max-width:760px){#collections{grid-template-columns:1fr}.nav{position:static}.hero{min-height:72vh}}
</style>
</head>
<body>
<nav class="nav"><strong>Maison Lune</strong><a href="#collections">系列</a></nav>
<main class="hero">
<section>
<h1>Quiet luxury for lived-in rooms</h1>
<p>以克制线条、天然材质和温暖比例打造高级现代家具首页，适合真实商业品牌首屏使用。</p>
<div class="actions"><a href="#collections">探索系列</a><button class="secondary" onclick="document.getElementById('collections').scrollIntoView({behavior:'smooth'})">预约陈列室</button></div>
</section>
</main>
<section id="collections"><article class="item">Modular Sofa</article><article class="item">Oak Dining</article><article class="item">Stone Lighting</article></section>
</body>
</html>
"""


def test_natural_language_root_drives_child_and_grandchild_e2e(tmp_path: Path) -> None:
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    config = AgentConfig(
        enable_tools=True,
        model_backend="echo",
        max_tool_rounds=8,
        runner_timeout_seconds="off",
        runner_concurrency="1",
        runner_start_rate="1",
        subagent_workspace="subs",
    )
    agent = SimpleAgent(config, tmp_path)
    backend = NaturalFurnitureRootBackend(site_dir)
    agent.backend = backend
    agent._subagent_worker_backend_override = backend.runner

    result = agent.run(
        "用单文件html做一个高端现代家具品牌的网站首页，风格高级、简洁、有设计感，适合真实商业品牌使用。只输出完整html，不要注释。",
        save=False,
    )
    records = _wait_for_subagent_records(agent, expected=2, artifact_path=site_dir / "index.html")
    site_check = run_static_site_check(
        {
            "name": "natural-furniture-site",
            "site_root": str(site_dir),
            "required_files": ["index.html"],
            "require_complete_html": True,
        },
        tmp_path,
    )

    assert "家具品牌首页" in result.response
    assert "已交付" in result.response
    assert "blocking_run_ids" not in result.response
    records_by_depth = sorted(records, key=lambda item: (item.depth, item.agent_name))
    assert [item.agent_name for item in records_by_depth] == ["小傻妞-家具总控", "小小傻妞-家具叶子"]
    assert {item.depth for item in records} == {1, 2}
    assert len(backend.runner.prompts) >= 3
    assert (site_dir / "index.html").exists()
    assert site_check.validation_result["ok"] is True


def _wait_for_subagent_records(agent: SimpleAgent, *, expected: int, artifact_path: Path) -> list:
    deadline = time.monotonic() + 10.0
    records = list(agent.subagents.list_runs())
    while (len(records) < expected or not artifact_path.exists()) and time.monotonic() < deadline:
        time.sleep(0.05)
        records = list(agent.subagents.list_runs())
    return records
