# LLM: H15只在pytest临时目录通过真实参数中心保存能力配置；不读取或修改用户的实际配置。
# 模块用途: 回归无agent配置路径时，完整脱敏回执不能在写成功后抛TypeError。
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.config import load_capability_config
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool, _full_config_receipt


# LLM: 明确主owner身份只服务本地临时fixture；环境里的真实配置入口必须清除，避免测试越界写文件。
# 函数用途: 构造只有capability配置文件的可信主代理。
def _capability_agent(tmp_path, monkeypatch, *, with_config):
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    path = tmp_path / "capability_config.yaml"
    path.write_text("capability_candidate_limit: 5\n", encoding="utf-8")
    agent = SimpleNamespace(root=tmp_path, capability_config_path=path,
                            home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main"),
                            config=AgentConfig(max_tokens=777) if with_config else None)
    return agent, path


# LLM: 保存与回执走完整产品入口，finally独立读盘证明TypeError发生前保存已提交。
# 函数用途: 锁住无config_path时真实保存、完整回执及当前运行配置/默认值回退。
@pytest.mark.parametrize("with_config", [True, False])
def test_h15_capability_save_without_config_path_returns_full_receipt(tmp_path, monkeypatch, with_config):
    agent, path = _capability_agent(tmp_path, monkeypatch, with_config=with_config)
    before = agent.config
    try:
        result = UserConfigTool(agent).execute({"action": "set", "key": "capability_candidate_limit",
                                               "value": "8", "include_full_config": True})
    finally:
        assert load_capability_config(path).capability_candidate_limit == 8
    assert result.ok, result.output
    report = json.loads(result.output)
    assert report["config"]["capability_candidate_limit"] == "8"
    assert report["config"]["max_tokens"] == str(777 if with_config else AgentConfig().max_tokens)
    assert agent.config is before
    assert report["changed_keys"] == ["capability_candidate_limit"]


# LLM: 原有带路径完整回执仍须读磁盘，不能被新回退逻辑改成只看旧运行配置。
# 函数用途: 验证正式配置加载和capability回读路径不退化。
def test_h15_full_receipt_with_path_still_reads_saved_config(tmp_path):
    user = tmp_path / "agent.yaml"
    capability = tmp_path / "capability.yaml"
    user.write_text("max_tokens: 321\n", encoding="utf-8")
    capability.write_text("capability_candidate_limit: 9\n", encoding="utf-8")
    report = _full_config_receipt(user, capability)
    assert report["max_tokens"] == "321"
    assert report["capability_candidate_limit"] == "9"
