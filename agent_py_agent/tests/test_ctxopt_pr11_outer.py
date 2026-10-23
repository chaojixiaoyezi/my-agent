# LLM: PR11聚焦测试只使用临时运行时和本地正式解析器，不发云请求或声明Linux内核验证。
# 模块用途: 锁住Python根、飞书动作权威、技能快路径和规范目录提示。
from pathlib import Path

import pytest

from agent_py_agent.agent import plugin_sandbox
from agent_py_agent.agent.tooling import feishu_cloud_tool
from agent_py_agent.agent.user_space import feishu_cloud_authorization as cloud_auth
from agent_py_agent.agent.workflows import script_runner

ROOT = Path(__file__).resolve().parents[1]


# LLM: 模拟便携Python前缀与独立真实解释器安装根，实际调用两条产品解析路径。
# 函数用途: 验证插件不漏掉脚本运行时的解释器根，且仍保留软链别名。
@pytest.mark.parametrize("layout", ["usr/local/lib/python", ".local/share/my-agent/runtime"])
def test_s11_plugin_uses_script_runtime_roots(tmp_path, monkeypatch, layout):
    prefix = tmp_path / layout
    executable = tmp_path / "actual-python/bin/python3"
    prefix.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    alias = tmp_path / "current"
    alias.symlink_to(prefix, target_is_directory=True)
    monkeypatch.setattr(script_runner.sys, "prefix", str(alias))
    monkeypatch.setattr(script_runner.sys, "base_prefix", str(prefix))
    monkeypatch.setattr(script_runner.sys, "executable", str(executable))
    _, expected = script_runner._interpreter_runtime("python3")
    roots = plugin_sandbox._python_runtime_prefixes()
    assert set(expected) <= set(roots)
    assert alias in roots and prefix in roots
    assert len(roots) == len(set(roots))


def test_s11_invalid_prefix_stays_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_sandbox.sys, "prefix", str(tmp_path / "missing"))
    with pytest.raises(plugin_sandbox.SandboxUnavailableError):
        plugin_sandbox._python_runtime_prefixes()


# LLM: 从统一操作表检查Schema、资源类型与副作用分类，而非只断言符号存在。
# 函数用途: 锁住飞书读写集合和真实工具策略不漂移。
def test_s12_cloud_operations_drive_authorization_and_tool():
    operations = getattr(cloud_auth, "CLOUD_OPERATIONS", {})
    assert set(operations) == {"sheet_meta", "sheet_read", "sheet_append", "sheet_update",
                               "document_read", "document_append", "wiki_resolve"}
    assert frozenset(op.action for op in operations.values() if not op.write) == cloud_auth.READ_ACTIONS
    assert frozenset(op.action for op in operations.values() if op.write) == cloud_auth.WRITE_ACTIONS
    assert not cloud_auth.READ_ACTIONS & cloud_auth.WRITE_ACTIONS
    assert set(operations) == cloud_auth.READ_ACTIONS | cloud_auth.WRITE_ACTIONS
    assert feishu_cloud_tool.READ_ACTIONS is cloud_auth.READ_ACTIONS
    assert feishu_cloud_tool.WRITE_ACTIONS is cloud_auth.WRITE_ACTIONS
    assert {op.action: op.resource_type for op in operations.values()} == feishu_cloud_tool._TYPES
    spec = feishu_cloud_tool.FeishuCloudTool.model_spec
    assert set(spec.input_schema["properties"]["action"]["enum"]) == set(operations)
    resolver = feishu_cloud_tool.FeishuCloudTool.runtime_policy.effect_resolver
    read_pairs = dict(dict(resolver.by_parameter)["action"])
    assert set(read_pairs) == cloud_auth.READ_ACTIONS
    assert set(read_pairs.values()) == {"read_only"}
    assert resolver.default_effect == "mutating"


# LLM: FF-14校验真实策略认可的直写路径与受管目录说明，不把owner目录当普通写根。
# 函数用途: 锁住技能快路径及真实Workflow结构核对动作。
def test_opt13_tiered_authoring_contract():
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    text = (ROOT / "skills/builtin/meta/writing-skills/SKILL.md").read_text()
    assert all(f"Tier {n}" in text for n in (1, 2, 3))
    fast = text.split("### Tier 1", 1)[1].split("### Tier 2", 1)[0]
    assert "<workspace>/.agents/skills/<name>/SKILL.md" in fast
    assert "<owner_home>/skills/<name>/SKILL.md" in fast
    assert "受管" in fast and "不能" in fast and "write_file" in fast
    assert "1–2" in fast or "1-2" in fast
    assert "不调用 `create_subagents`" in fast and "不调用 `package_build`" in fast
    assert "frontmatter" in fast and "本地" in fast
    assert "bind_skill" in text and 'action="preview"' in text and 'action="validate"' in text
    assert "dry_run" not in text
    actions = WorkflowsTool.model_spec.input_schema['properties']['action']['enum']
    assert 'preview' in actions
    if 'validate' not in actions:
        assert '仅当正式 schema 声明 `action="validate"` 时才使用该动作' in text
    assert "这一步叫摸底，不能省" not in text


def test_ex02_external_learning_uses_canonical_skill_path():
    text = (ROOT / "skills/builtin/plugins/learn-external-agent/SKILL.md").read_text()
    assert ".agents/skills/<skill-name>/SKILL.md" in text
    assert ".agent/skills" not in text


@pytest.mark.parametrize("name,relative", [
    ("writing-skills", "meta/writing-skills"),
    ("learn-external-agent", "plugins/learn-external-agent"),
])
def test_changed_skills_parse_and_read_back_through_service(tmp_path, skill_catalog_factory, name, relative):
    path = ROOT / "skills/builtin" / relative / "SKILL.md"
    catalog = skill_catalog_factory(tmp_path / "home", extra_roots=[path.parent.parent])
    entry = catalog.snapshot.resolve(name)
    assert entry is not None
    assert entry.name == name
    assert catalog.snapshot.read_body(entry.stable_id).strip() == path.read_text().strip()


def test_f14_host_write_hint_uses_canonical_skill_path(tmp_path):
    from agent_py_agent.agent.path_access_policy import _host_write_block

    root = tmp_path / ".my-agent"
    skill = root / "owners/local/main/skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("example")
    decision = _host_write_block(skill, root)
    assert decision is not None and not decision.allowed
    assert ".agents/skills" in decision.message
    assert ".agent/skills" not in decision.message


# LLM: FF-12跑真实ProcessSessionTool→外置归档→live reducer；只替换无外部进程的status数据源。
# 函数用途: 验证外置JSON在有无handler_details时均保留失败尾部、退出码和逻辑归档引用。
@pytest.mark.parametrize('include_details', [False, True])
def test_process_actual_archive_keeps_output_tail(tmp_path, monkeypatch, include_details):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.agent_core.tool_context.reducer import (
        render_tool_result_for_live_prompt,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling import process_sessions as sessions
    from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, ToolSuccessFacts
    from agent_py_agent.tests._tool_runtime_harness import canonical_history_call

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
        execution_mode='local_unmanaged'), tmp_path / 'workspace')
    payload = {'session_id': 'bg-audit', 'status': 'exited', 'exit_code': 23, 'timed_out': False,
               'output_tail': 'HEAD\n' + 'n' * 3970 + '\nFINAL-ERROR'}
    monkeypatch.setattr(sessions, 'process_access_scope', lambda *_: SimpleNamespace(owner_home=tmp_path, is_bound=lambda: True))
    monkeypatch.setattr(sessions.process_registry, 'status', lambda *_: payload)
    outcome = sessions.ProcessSessionTool(tmp_path, tmp_path).execute({'action': 'status', 'session_id': 'bg-audit'})
    assert outcome.ok, outcome.output
    monkeypatch.setattr(archive, '_tool_output_archive_root', lambda *_: tmp_path / 'archive')
    params = SimpleNamespace(request_id='audit-request', task_id='', tool_runtime_snapshot=agent.tools.runtime_snapshot())
    call = canonical_history_call('process_session', {'action': 'status', 'session_id': 'bg-audit'}, call_id='audit-process')
    projection = archive.archive_tool_output_projection(agent, params, call, outcome)
    body = '\n'.join(block.text for block in projection.content_blocks if block.type == 'text')
    metadata = dict(projection.metadata)
    if include_details:
        metadata['handler_details'] = outcome.result_envelope
    result = ToolResult.succeeded(call, body, facts=ToolSuccessFacts(output_trust='external_data',
        output_redaction='default', metadata=metadata))
    record = projection.metadata['archive_output_record']
    assert record['output_externalized']
    rendered = render_tool_result_for_live_prompt(result, record)
    assert 'HEAD' in rendered and 'FINAL-ERROR' in rendered
    assert '"exit_code":23' in rendered and '"timed_out":false' in rendered
    assert '<untrusted_tool_result source="process_session">' in rendered and 'read_artifact_hint' in rendered
    assert 'history-run:audit-process' in rendered and str(tmp_path / 'archive') not in rendered
    assert json.loads(outcome.output) == payload


# LLM: 归档前预览和live摘要会连续投影；二次投影必须保留流尾、原总数与省略事实。
# 函数用途: 防止修复归档截断时引入摘要二次归约丢字段的回归。
@pytest.mark.parametrize('shape', ['status', 'processes', 'network'])
def test_process_domain_summary_is_idempotent(shape):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_context.live_summary import orchestration_live_summary

    payload = {'session_id': 'bg-audit', 'status': 'exited', 'exit_code': 23,
               'output_tail': 'HEAD\n' + 'n' * 3970 + '\nFINAL-ERROR'}
    if shape == 'processes':
        payload = {'processes': [{**payload, 'session_id': f'bg-{index}'} for index in range(30)]}
    elif shape == 'network':
        payload['network'] = {'observations': [{'status': 'unverified', 'note': 'n' * 500} for _ in range(30)]}
    result = SimpleNamespace(tool_name='process_session', output=json.dumps(payload),
                             output_trust='runtime', metadata={})
    record = {'scoped_call_id': 'run:process'}
    first = orchestration_live_summary(result, record)
    result.output = first
    second = orchestration_live_summary(result, record)
    assert second == first
    assert 'FINAL-ERROR' in second
