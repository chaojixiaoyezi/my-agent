# LLM: Main-agent complex Live Lab cases validate root-agent durability without subagent delegation.
# 模块用途: 提供主代理复杂任务真实模型测试；这些 case 会关闭小傻妞，专门观察主代理自己是否稳。

from __future__ import annotations

"""main-agent complex E2E cases.

给人看的解释：
这里的测试不让主代理派小傻妞，而是让主代理自己完成任务。
这样能先把“单个 my-agent 是否足够硬”测出来，再决定什么时候继续压子代理链路。
"""

import json
import re
import textwrap
from pathlib import Path

from .main_agent_complex_large_log import (
    assert_large_log_report,
    main_large_log_prompt,
    seed_large_log,
)
from .shop_case import _external_asset_refs, _has_disabled_control

# LLM: Compatibility aliases keep existing tests importing old private helper names.
# 模块用途: 大日志逻辑已拆到 main_agent_complex_large_log，这里保留旧入口避免调用方断裂。
_main_large_log_prompt = main_large_log_prompt
_seed_large_log = seed_large_log
_assert_large_log_report = assert_large_log_report


# LLM: case_main_direct_web_app checks multi-file delivery through the root agent only.
# 函数用途: 让主代理自己写一个多文件家具品牌 Web app，并检查真实文件和基础链接。
def case_main_direct_web_app(lab) -> None:
    lab.section("CASE main_direct_web_app")
    _ensure_main_agent_only(lab)
    prompt = _main_direct_web_app_prompt()
    lab.record_prompt("main_direct_web_app", prompt)
    contract_path = _write_main_web_app_delivery_contract(lab)
    response = lab.run_command(
        lab.agent_command("run", prompt, "--delivery-contract-file", str(contract_path), "--save"),
        timeout=lab.args.timeout + 120,
    )
    (lab.responses_dir / "main_direct_web_app.stdout.txt").write_text(response.stdout, encoding="utf-8")
    output_root = lab.fixture_root / "lab_outputs" / "main-web-app"
    _assert_main_web_app_output(output_root)
    lab.log(f"main_web_app_output={output_root}")


# LLM: _write_main_web_app_delivery_contract puts the web QA contract into the main-agent repair loop.
# 函数用途: 把多文件网页验收合同写成 JSON 文件，供 CLI 通过 --delivery-contract-file 传给主代理。
def _write_main_web_app_delivery_contract(lab) -> Path:
    path = lab.fixture_root / ".my_agent" / "delivery_contracts" / "main_direct_web_app.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_main_web_app_delivery_contract(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


# LLM: _main_web_app_delivery_contract is a structured web-project contract, not prompt-derived logic.
# 函数用途: 定义 Live Lab 主代理网页产物必须经过 static_site_check 的机器验收字段。
def _main_web_app_delivery_contract() -> dict[str, object]:
    required_files = ["index.html", "styles.css", "app.js", "README.md"]
    return {
        "case_id": "main_direct_web_app",
        "bootstrap_contract": {
            "materialization_targets": [
                {"target_type": "file", "workspace_relative_path": f"lab_outputs/main-web-app/{name}"}
                for name in required_files
            ],
            "startup_actions": [{"action": "materialize_target", "priority": 1}],
        },
        "artifacts": [
            {
                "artifact_id": "main_web_app_root",
                "kind": "web_project",
                "preferred_path": "lab_outputs/main-web-app",
                "required": True,
                "validation_contract": {
                    "validator": "static_site_check",
                    "required_files": required_files,
                    "require_complete_html": True,
                    "strict_dom_bindings": True,
                },
            }
        ],
    }


# LLM: case_main_tool_failure_recovery makes a real tool miss recoverable instead of terminal.
# 函数用途: 让主代理先遇到一个缺失文件，再改读正确素材并写出恢复报告。
def case_main_tool_failure_recovery(lab) -> None:
    lab.section("CASE main_tool_failure_recovery")
    _ensure_main_agent_only(lab)
    prompt = _main_tool_failure_prompt()
    lab.record_prompt("main_tool_failure_recovery", prompt)
    response = lab.run_command(
        lab.agent_command("run", prompt, "--save"),
        timeout=lab.args.timeout + 120,
    )
    (lab.responses_dir / "main_tool_failure_recovery.stdout.txt").write_text(response.stdout, encoding="utf-8")
    output = lab.fixture_root / "lab_outputs" / "tool-recovery" / "report.md"
    _assert_tool_recovery_report(output)
    lab.log(f"tool_recovery_report={output}")


# LLM: case_main_large_log_audit checks that big files are searched/audited by evidence, not pasted into context.
# 函数用途: 准备一个 100MB 日志，让主代理找关键错误并写审计报告。
def case_main_large_log_audit(lab) -> None:
    lab.section("CASE main_large_log_audit")
    _ensure_main_agent_only(lab)
    log_path = lab.fixture_root / "logs" / "huge_app.log"
    seed_large_log(log_path)
    prompt = main_large_log_prompt()
    lab.record_prompt("main_large_log_audit", prompt)
    response = lab.run_command(
        lab.agent_command("run", prompt, "--save"),
        timeout=lab.args.timeout + 180,
    )
    (lab.responses_dir / "main_large_log_audit.stdout.txt").write_text(response.stdout, encoding="utf-8")
    output = lab.fixture_root / "lab_outputs" / "large-log-audit" / "report.md"
    assert_large_log_report(output)
    lab.log(f"large_log_report={output}")


# LLM: _ensure_main_agent_only appends deterministic isolation overrides without touching user config.
# 函数用途: 在本轮 Live Lab 临时配置里关闭子代理，保证这些 case 测的是主代理自己。
def _ensure_main_agent_only(lab) -> None:
    marker = "# main-agent-complex overrides"
    text = lab.config_path.read_text(encoding="utf-8")
    if marker in text:
        return
    overrides = textwrap.dedent(
        f"""

        {marker}
        enable_subagents: false
        max_tool_rounds: 0
        request_timeout: {max(300, int(lab.args.timeout))}
        tool_read_max_chars: 50000
        tool_search_max_matches: 200
        tool_list_max_entries: 500
        """
    )
    lab.config_path.write_text(text + overrides, encoding="utf-8")


# LLM: _main_direct_web_app_prompt stays natural-language but explicit about main-agent-only execution.
# 函数用途: 生成多文件 Web app 测试提示词，不使用 dispatch/runner/contract 等内部术语。
def _main_direct_web_app_prompt() -> str:
    return textwrap.dedent(
        """
        这次你自己完成，不要派小傻妞。

        请做一个高端现代家具品牌的小型网站，适合真实商业品牌展示。
        需要是多文件项目，放到 lab_outputs/main-web-app/ 里面：
        - index.html
        - styles.css
        - app.js
        - README.md

        页面要高级、简洁、有设计感；不能用外部图片、外部字体、外部脚本。
        页面里的按钮和导航不能失灵，链接只允许指向本页面真实存在的区域。
        做完后你自己检查文件是否齐全、页面引用是否正确、有没有空链接或坏链接。
        """
    ).strip()


# LLM: _main_tool_failure_prompt creates a normal user recovery task with one intentional bad file.
# 函数用途: 生成工具失败恢复测试提示词，要求主代理遇到缺失文件后继续完成任务。
def _main_tool_failure_prompt() -> str:
    return textwrap.dedent(
        """
        这次你自己完成，不要派小傻妞。

        请先尝试读取 notes/does-not-exist.md。
        如果这个文件不存在，不要停，也不要假装读到了；请改读 notes/small_task.md 和 README.md。
        然后把你怎么恢复、最终读到了什么、下一步建议，写到 lab_outputs/tool-recovery/report.md。
        报告要让普通人能看懂，别只写一句话。
        """
    ).strip()


# LLM: _assert_main_web_app_output validates concrete files and static-only references.
# 函数用途: 检查主代理真实写出的多文件网站，而不是相信最终口头回复。
def _assert_main_web_app_output(output_root: Path) -> None:
    index = output_root / "index.html"
    css = output_root / "styles.css"
    js = output_root / "app.js"
    readme = output_root / "README.md"
    missing = [str(path) for path in (index, css, js, readme) if not path.exists()]
    if missing:
        raise RuntimeError(f"主代理多文件 Web app 缺少文件: {missing}")
    html = index.read_text(encoding="utf-8", errors="replace").lower()
    css_text = css.read_text(encoding="utf-8", errors="replace").lower()
    js_text = js.read_text(encoding="utf-8", errors="replace").lower()
    if "styles.css" not in html or "app.js" not in html:
        raise RuntimeError("主代理 Web app 没有正确引用 styles.css 或 app.js。")
    if "<html" not in html or "<body" not in html or "</html>" not in html:
        raise RuntimeError("主代理 Web app 的 HTML 结构不完整。")
    if "href=\"#\"" in html or 'href="/' in html or _has_disabled_control(html):
        raise RuntimeError("主代理 Web app 包含空链接、根路径坏链接或 disabled 控件。")
    external_assets = _external_asset_refs(html + "\n" + css_text + "\n" + js_text)
    if external_assets:
        raise RuntimeError(f"主代理 Web app 依赖外部渲染资源: {external_assets[:5]}")
    if "furniture" not in html and "家具" not in index.read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError("主代理 Web app 不像家具品牌页面。")
    if "addeventlistener" not in js_text and "function" not in js_text:
        raise RuntimeError("主代理 Web app 的 app.js 缺少基本交互逻辑。")
    if "body" not in css_text:
        raise RuntimeError("主代理 Web app 的 styles.css 缺少基础页面样式。")
    readme_text = readme.read_text(encoding="utf-8", errors="replace")
    _assert_main_web_app_readme(readme_text, html)
    _assert_main_web_app_static_check(output_root)


# LLM: _assert_main_web_app_readme checks README file refs without making prose wording authoritative.
# 函数用途: README 只要求列出核心文件 token 和真实页面锚点，不把目录名或自然语言措辞当机器事实。
def _assert_main_web_app_readme(readme_text: str, html: str) -> None:
    lowered = readme_text.lower()
    required_terms = ["index.html", "styles.css", "app.js"]
    missing = [term for term in required_terms if term not in lowered]
    if missing:
        raise RuntimeError(f"主代理 Web app README 缺少核心文件说明: {missing}")
    ids = _html_ids(html)
    missing_anchors = [item for item in _markdown_anchor_refs(readme_text) if item not in ids]
    if missing_anchors:
        raise RuntimeError(f"主代理 Web app README 提到了不存在的页面锚点: {missing_anchors}")


# LLM: _assert_main_web_app_static_check reuses product validation in strict mode for root-agent web output.
# 函数用途: 用通用 static_site_check 检查多文件网站的坏链接、缺资源、HTML 骨架和 JS/DOM id 不一致。
def _assert_main_web_app_static_check(output_root: Path) -> None:
    from agent_py_agent.agent.subagents.static_site_validator import run_static_site_check

    record = run_static_site_check(
        {
            "name": "live-lab-main-web-app-static-site",
            "site_root": str(output_root),
            "required_files": ["index.html", "styles.css", "app.js", "README.md"],
            "require_complete_html": True,
            "strict_dom_bindings": True,
        },
        output_root.parent.parent,
    )
    if not record.passed:
        raise RuntimeError(f"主代理 Web app static_site_check 未通过: {record.error} {record.validation_result}")


# LLM: _html_ids extracts page ids for README and link consistency checks.
# 函数用途: 从 HTML 摘要里提取 id，不执行网页，也不读取外部资源。
def _html_ids(html: str) -> set[str]:
    return set(re.findall(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", html, flags=re.IGNORECASE))


# LLM: _markdown_anchor_refs extracts README in-page refs such as `#hero`.
# 函数用途: 找出 README 写给用户看的页面锚点，避免文档和真实页面对不上。
def _markdown_anchor_refs(readme_text: str) -> list[str]:
    return sorted(set(re.findall(r"`#([A-Za-z0-9_-]+)`", readme_text or "")))


# LLM: _assert_tool_recovery_report checks recovery evidence from the real output file.
# 函数用途: 检查工具失败恢复报告必须提到缺失文件、替代素材和真实输出。
def _assert_tool_recovery_report(output: Path) -> None:
    if not output.exists():
        raise RuntimeError(f"工具失败恢复报告不存在: {output}")
    content = output.read_text(encoding="utf-8", errors="replace")
    lowered = content.lower()
    if "does-not-exist" not in lowered:
        raise RuntimeError("工具失败恢复报告没有提到最初缺失的文件。")
    if "small_task" not in lowered and "readme" not in lowered:
        raise RuntimeError("工具失败恢复报告没有提到替代读取的真实素材。")
    if len(content.strip()) < 120:
        raise RuntimeError("工具失败恢复报告过短，不足以说明恢复过程。")


__all__ = [
    "case_main_direct_web_app",
    "case_main_large_log_audit",
    "case_main_tool_failure_recovery",
]
