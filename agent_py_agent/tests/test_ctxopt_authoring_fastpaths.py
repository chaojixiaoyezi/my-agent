# LLM: 静态文案合同不替代真实Skill/Workflow运行，发现和绑定另做集成测试。
# 模块用途: 锁定原生对象保留、风险分级及不强制多代理的创作路径。
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.skills import parse_skill_file

ROOT = Path(__file__).resolve().parents[1] / 'skills/builtin'
ENTRIES = ('meta/writing-skills', 'planning/brainstorming', 'planning/writing-plans',
           'orchestration/subagent-driven-development', 'review/requesting-code-review',
           'plugins/learn-external-agent')


# LLM: 每个方法都应按风险缩放流程，不能用打包或全局多代理门禁替代明确目标。
# 函数用途: 检查正式frontmatter和六份流程文案的共同约束。
@pytest.mark.parametrize('entry', ENTRIES)
def test_risk_tiered_native_authoring(entry):
    path = ROOT / entry / 'SKILL.md'
    card = parse_skill_file(path, source='builtin', require_frontmatter=True)
    body = path.read_text()
    assert card.name == Path(entry).name and len(card.description) <= 80
    assert '不自动打包' in body and '简单任务' in body and '高风险' in body
    assert '审批' in body and '验证' in body
    for removed in ('这一步叫摸底，不能省', '每个任务至少三个子代理',
                    '每批做完停下来给用户复查', '复评一次也不能省'):
        assert removed not in body


# LLM: 文案必须区分预览与真实业务完成，不发明创建或安装授权。
# 函数用途: 防止原生Skill创作再次退化成能力包和200字假成功。
def test_writing_skill_preserves_runtime_contracts():
    body = (ROOT / 'meta/writing-skills/SKILL.md').read_text()
    for token in ('Skill', 'Workflow', 'Plugin', 'Capability Pack', 'skill_ids',
                  'ANALYZER_UNAVAILABLE', 'SKILL_NOT_FOUND', 'run_inputs', 'step_inputs', 'preview'):
        assert token in body
