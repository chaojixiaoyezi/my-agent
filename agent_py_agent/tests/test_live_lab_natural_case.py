from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.live_lab.cases import (
    _assert_natural_html_output,
    _assert_no_subagent_state_blockers,
    _assert_persisted_subagent_state_clean,
    _external_asset_refs,
    _natural_html_prompt,
)
from scripts.live_lab.constants import REAL_CASES, SUITES
from scripts.live_lab.session import LabSessionManager


# LLM: The natural Live Lab case should exercise ordinary user wording, not internal orchestration terms.
# 函数用途: 确认家具页面 E2E 的提示词接近用户真实说法，并要求主代理派小傻妞而不是自己写。
def test_natural_html_prompt_uses_user_language():
    prompt = _natural_html_prompt()

    assert "小傻妞" in prompt
    assert "高端现代家具品牌" in prompt
    assert "lab_outputs/furniture-home/index.html" in prompt
    assert "不要依赖外部图片" in prompt
    assert "dispatch" not in prompt.lower()
    assert "runner" not in prompt.lower()
    assert "contract" not in prompt.lower()


# LLM: The natural suite must stay opt-in but real-LLM gated.
# 函数用途: 确认新增自然语言 E2E 入口不会偷偷跑真实模型，同时可被用户显式运行。
def test_natural_html_case_is_registered_as_real_opt_in_suite():
    assert SUITES["natural"] == ["health", "natural_html_subagent"]
    assert "natural_html_subagent" in REAL_CASES


# LLM: Natural Live Lab should not fail long page tasks because of an artificial test harness tool cap.
# 函数用途: 确认隔离配置默认不限制工具轮数，避免真实页面生成被测试台自己截断。
def test_live_lab_config_keeps_tool_rounds_unlimited(tmp_path):
    source = tmp_path / "agent_config.yaml"
    source.write_text("model_backend: echo\n", encoding="utf-8")
    args = SimpleNamespace(
        config=str(source),
        runs_dir=str(tmp_path / "runs"),
        run_id="natural-config",
        real_llm=False,
        count=2,
        timeout=180,
    )

    session = LabSessionManager(args)
    session.setup()

    text = session.config_path.read_text(encoding="utf-8")
    assert "max_tool_rounds: 0" in text


# LLM: Natural E2E validation checks concrete artifact facts instead of trusting the final prose.
# 函数用途: 确认 HTML 验收读取真实文件，并能发现空链接这类用户可见问题。
def test_assert_natural_html_output_rejects_empty_links(tmp_path):
    output = tmp_path / "lab_outputs" / "furniture-home" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text(
        '<html><body><a href="#">Furniture</a></body></html>',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="空链接"):
        _assert_natural_html_output(output)


# LLM: The natural canary must fail when the gateway response says the subagent chain is still blocked.
# 函数用途: 防止真实产物存在但主代理明确报告 blocking_run_ids 时，Live Lab 误报 PASS。
def test_assert_no_subagent_state_blockers_rejects_gateway_notice():
    stdout = (
        '{"ok": true, "response": "---\\n\\n## Subagent State Notice\\n\\n'
        '子代理链路尚未完整通过\\n- done_verified: 0\\n'
        '- blocking_run_ids: subagent-123"}'
    )

    with pytest.raises(RuntimeError, match="子代理链路仍阻塞"):
        _assert_no_subagent_state_blockers(stdout)


# LLM: Ordinary successful prose should pass the natural canary control-plane gate.
# 函数用途: 确认没有 blocker 信号的 gateway JSON 不会被误拦截。
def test_assert_no_subagent_state_blockers_accepts_clean_response():
    stdout = '{"ok": true, "response": "保存路径：lab_outputs/furniture-home/index.html\\n检查通过。"}'

    _assert_no_subagent_state_blockers(stdout)


# LLM: Live Lab should compare final prose with persisted task.json state before reporting pass.
# 函数用途: 复现自然语言 E2E 假绿：一个旧小傻妞仍是 PLANNING，但主代理文字说 done_verified。
def test_assert_persisted_subagent_state_clean_rejects_planning_run(tmp_path):
    subagents = tmp_path / ".my_agent" / "subagents"
    done = subagents / "subagent-done"
    planned = subagents / "subagent-planned"
    done.mkdir(parents=True)
    planned.mkdir()
    (done / "task.json").write_text(
        '{"id":"subagent-done","status":"DONE","verification_status":"VERIFIED","goal":"写 index.html"}',
        encoding="utf-8",
    )
    (planned / "task.json").write_text(
        '{"id":"subagent-planned","status":"PLANNING","verification_status":"UNVERIFIED","goal":"写 index.html"}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="持久化子代理状态仍未完成"):
        _assert_persisted_subagent_state_clean(tmp_path)


# LLM: A single verified subagent should satisfy the natural Live Lab persisted-state gate.
# 函数用途: 确认正常 DONE/VERIFIED 的小傻妞不会被状态门误拦截。
def test_assert_persisted_subagent_state_clean_accepts_verified_run(tmp_path):
    subagents = tmp_path / ".my_agent" / "subagents" / "subagent-done"
    subagents.mkdir(parents=True)
    (subagents / "task.json").write_text(
        '{"id":"subagent-done","status":"DONE","verification_status":"VERIFIED"}',
        encoding="utf-8",
    )

    _assert_persisted_subagent_state_clean(tmp_path)


# LLM: The natural canary should catch remote assets before visual E2E claims success.
# 函数用途: 确认外部图片、字体和 CSS 背景会被识别，避免单文件页面离线打开时失效。
def test_external_asset_refs_detects_remote_page_assets():
    html = """
    <link href="https://fonts.example/css" rel="stylesheet">
    <img src="https://cdn.example/hero.jpg">
    <div style="background-image:url('https://cdn.example/bg.jpg')"></div>
    """

    refs = _external_asset_refs(html.lower())

    assert '<link href="https://' in refs
    assert 'src="https://' in refs
    assert "url('https://" in refs


# LLM: Normal outbound content links are allowed; only page-rendering assets are blocked.
# 函数用途: 确认页面里的普通外部链接不会被误当成图片/字体/脚本依赖。
def test_external_asset_refs_allows_normal_links():
    html = '<a href="https://example.com/story">Furniture story</a>'

    assert _external_asset_refs(html.lower()) == []


# LLM: A complete single-file furniture page should satisfy the lightweight artifact gate.
# 函数用途: 确认可打开的家具 HTML 文件可以通过自然语言 E2E 的基础验收。
def test_assert_natural_html_output_accepts_complete_page(tmp_path):
    output = tmp_path / "lab_outputs" / "furniture-home" / "index.html"
    output.parent.mkdir(parents=True)
    output.write_text(
        '<!doctype html><html><body><main>高端家具 Furniture</main><a href="/shop">Shop</a></body></html>',
        encoding="utf-8",
    )

    _assert_natural_html_output(output)
