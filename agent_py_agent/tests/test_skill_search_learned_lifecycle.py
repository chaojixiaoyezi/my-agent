# 模块用途: skill_search get 读取自学 Skill 时，只读附带与 self_map 同一读口的登记 version/revision；
#   正文与 content_sha256 仍是冻结版本，普通 Skill 的 get 字节合同另由原测试固定。只用 tmp 隔离 home。
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.tests.test_skill_learning import NAME, _output, _publish_first, _runtime
from agent_py_agent.tests.test_skill_learning_versions import _update


# 与 test_skill_learning_versions 相同：固定配额小快照的单调钟，准入只由真实额度决定。
@pytest.fixture(autouse=True)
def _pinned_quota_snapshot_clock(monkeypatch):
    from agent_py_agent.agent.user_space import owner_quota
    monkeypatch.setattr(owner_quota, "time", SimpleNamespace(**{**vars(time), "monotonic": lambda: 100.0}))


def _get(ctx):
    agent = SimpleNamespace(home_paths=ctx.home, current_skill_snapshot=lambda: ctx.skills.snapshot_for(force_reload=True))
    outcome = SkillSearchTool(agent).execute({'action': 'get', 'skill_id': f'owner:{NAME}'})
    assert outcome.ok, outcome.output
    return json.loads(outcome.output)


def test_learned_skill_get_reports_registry_version_and_revision(tmp_path):
    ctx = _runtime(tmp_path, _output())
    _publish_first(ctx)
    first = _get(ctx)
    assert first['lifecycle']['authorized'] is True
    assert (first['lifecycle']['version'], first['lifecycle']['revision']) == (1, 1)
    assert _update(ctx).status == 'updated'
    second = _get(ctx)
    record = ctx.store.load_registry().skills[NAME]
    assert (second['lifecycle']['version'], second['lifecycle']['revision']) == (record.version, record.revision) == (2, 2)
    # 版本字段与冻结正文同源：hash 随新版本变化，与登记 sha256 一致。
    assert second['content_sha256'] == record.sha256 != first['content_sha256']
