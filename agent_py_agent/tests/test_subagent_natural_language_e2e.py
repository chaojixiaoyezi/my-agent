from __future__ import annotations

import json
import re
from pathlib import Path

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.static_site_validator import run_static_site_check


# LLM: NaturalFurnitureRootBackend simulates only the top-level model choices from a plain user request.
# 类用途: 测试 root 只收到自然语言任务后，是否通过工具派小傻妞并触发子代理执行，而不是测试直接创建子代理。
class NaturalFurnitureRootBackend(BaseBackend):
    name = "natural_furniture_root_backend"

    # LLM: __init__ stores the product directory used by root tool-call payloads.
    # 函数用途: 保存本轮 E2E 的站点输出目录，后续 create_subagents 会把它作为真实写入根。
    def __init__(self, site_dir: Path):
        self.site_dir = site_dir
        self.prompts: list[str] = []
        self.root_prompts: list[str] = []
        self.runner = NaturalFurnitureRunnerBackend(site_dir)

    # LLM: generate drives root through create -> dispatch -> final using model-like tool calls.
    # 函数用途: 模拟主代理模型逐轮调用工具；用户 prompt 本身保持自然语言，不写内部术语。
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

    # LLM: _create_payload keeps root's delegation contract explicit and refs-first.
    # 函数用途: 构造主代理派给第一层小傻妞的任务，带真实产物目录，不要求用户懂内部参数。
    def _create_payload(self) -> dict[str, object]:
        return {
            "tool": "create_subagents",
            "apply": True,
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


# LLM: NaturalFurnitureRunnerBackend simulates child and grandchild model behavior behind dispatch.
# 类用途: 让小傻妞自己派小小傻妞、叶子节点自己写文件，主测试不直接操作下级 run。
class NaturalFurnitureRunnerBackend(BaseBackend):
    name = "natural_furniture_runner_backend"

    # LLM: __init__ stores target refs and per-role call counters for nested runner calls.
    # 函数用途: 保存 E2E 产物路径，并区分 coordinator 与 leaf 的多轮模型调用。
    def __init__(self, site_dir: Path):
        self.site_dir = site_dir
        self.prompts: list[str] = []
        self.coordinator_calls = 0
        self.leaf_calls = 0

    # LLM: generate routes each runner prompt by the current agent header, not by parent summaries.
    # 函数用途: 根据执行上下文中的 `- agent:` 行判断当前是哪一层，避免从工具输出文本误判角色。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        agent_name = _agent_name_from_prompt(prompt)
        if "家具叶子" in agent_name:
            return self._leaf_response()
        if "家具总控" in agent_name:
            return self._coordinator_response()
        raise AssertionError(f"unknown runner agent: {agent_name}")

    # LLM: _coordinator_response creates a grandchild, dispatches it, then reports refs.
    # 函数用途: 验证 coordinator 有完整工具能力，既能派工也能触发自己下级执行。
    def _coordinator_response(self) -> ModelResponse:
        self.coordinator_calls += 1
        if self.coordinator_calls == 1:
            return ModelResponse(text=_tool_call(self._schedule_leaf_payload()), backend=self.name)
        if self.coordinator_calls == 2:
            return ModelResponse(text=_tool_call(_child_dispatch_payload()), backend=self.name)
        return ModelResponse(text=_subagent_result("小傻妞已让小小傻妞完成 index.html，并完成本地验收。"), backend=self.name)

    # LLM: _leaf_response writes the HTML on the first turn and reports structured evidence on the second.
    # 函数用途: 验证叶子代理能独立写产物，不需要主代理代写或直接干预。
    def _leaf_response(self) -> ModelResponse:
        self.leaf_calls += 1
        if self.leaf_calls == 1:
            return ModelResponse(text=_tool_call(self._write_html_payload()), backend=self.name)
        return ModelResponse(text=_subagent_result(f"小小傻妞已写入 {self.site_dir / 'index.html'}。"), backend=self.name)

    # LLM: _schedule_leaf_payload passes a concrete write root to the next layer.
    # 函数用途: 构造小傻妞派给小小傻妞的任务，保留用户原始文件和交互约束。
    def _schedule_leaf_payload(self) -> dict[str, object]:
        return {
            "tool": "schedule_child_subagents",
            "apply": True,
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

    # LLM: _write_html_payload is the leaf's real product write tool call.
    # 函数用途: 生成单文件 HTML 内容，包含真实锚点和按钮动作，便于静态验收发现坏链接。
    def _write_html_payload(self) -> dict[str, object]:
        return {
            "tool": "write_file",
            "path": str(self.site_dir / "index.html"),
            "content": _furniture_html(),
        }


# LLM: _tool_call renders one JSON tool call block exactly as the runtime parser expects.
# 函数用途: 把测试模型的结构化工具参数包装成 [TOOL_CALL] 文本。
def _tool_call(payload: dict[str, object]) -> str:
    return "[TOOL_CALL]\n" + json.dumps(payload, ensure_ascii=False) + "\n[/TOOL_CALL]"


# LLM: _root_dispatch_payload asks root to execute the direct child through the normal dispatch tool.
# 函数用途: 构造主代理第二轮工具调用，只推进自己创建的直接 child。
def _root_dispatch_payload() -> dict[str, object]:
    return {"tool": "dispatch_subagents", "apply": True, "execute_runners": True, "max_runners": 1}


# LLM: _child_dispatch_payload asks a coordinator to execute its own direct child.
# 函数用途: 构造小傻妞第二轮工具调用，验证下级派工由当前父节点继续推进。
def _child_dispatch_payload() -> dict[str, object]:
    return {"tool": "dispatch_subagents", "apply": True, "execute_runners": True, "max_runners": 1}


# LLM: _subagent_result produces a minimal accepted runner result with refs-friendly evidence.
# 函数用途: 返回 runner 结构化结果块，让 dispatch/验收流程按机器字段推进。
def _subagent_result(summary: str) -> str:
    return (
        "[SUBAGENT_RESULT]\n"
        + json.dumps(
            {
                "status": "AWAITING_ACCEPTANCE",
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


# LLM: _agent_name_from_prompt reads only the execution-context header for current runner identity.
# 函数用途: 从 prompt 的 `- agent:` 行提取当前 runner 名称，避免从下级工具结果中误判。
def _agent_name_from_prompt(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.startswith("- agent:"):
            return line.partition(":")[2].strip()
    scoped_prompt = prompt[prompt.find("## Execution Context JSON") :] if "## Execution Context JSON" in prompt else prompt
    match = re.search(r'"agent_name":\s*"([^"]+)"', scoped_prompt)
    if match:
        return match.group(1)
    return ""


# LLM: _furniture_html returns a compact but realistic single-file site for static QA.
# 函数用途: 提供测试用 HTML 产物，包含完整结构、内联 CSS、真实锚点和按钮交互。
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


# LLM: test_natural_language_root_drives_child_and_grandchild_e2e is the user-style smoke path.
# 函数用途: 用户只发自然语言任务，验证 root 派小傻妞、小傻妞派小小傻妞，叶子节点写产物并通过本地 QA。
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

    result = agent.run(
        "用单文件html做一个高端现代家具品牌的网站首页，风格高级、简洁、有设计感，适合真实商业品牌使用。只输出完整html，不要注释。",
        save=False,
    )
    records = list(agent.subagents.list_runs())
    site_check = run_static_site_check(
        {
            "name": "natural-furniture-site",
            "site_root": str(site_dir),
            "required_files": ["index.html"],
            "require_complete_html": True,
        },
        tmp_path,
    )

    assert "子代理链路尚未完整通过" in result.response
    assert "done_verified: 0" in result.response
    assert "blocking_run_ids" in result.response
    assert [item.agent_name for item in records] == ["小傻妞-家具总控", "小小傻妞-家具叶子"]
    assert {item.depth for item in records} == {0, 1}
    assert len(backend.runner.prompts) >= 4
    assert (site_dir / "index.html").exists()
    assert site_check.validation_result["ok"] is True
