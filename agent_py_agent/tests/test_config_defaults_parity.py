"""P7 全量默认值一致性：随包 agent_config.yaml 加载结果必须等于 AgentConfig() 默认值。

来源：三线收口 P7（2026-10-01）。此前只有零星核对（test_config_validation.py、
test_merged_config_knobs.py 各查少数键）；本文件用正式加载器 load_config 读随包 YAML，
对 AgentConfig 全部字段逐个断言。

设计：
- 加载器写入的运行时元数据（config_layers / config_path / config_sources / config_warnings /
  memory_config_warnings）不在随包 YAML 里，跳过值比较，但断言键仍存在——键被删时测试失败，
  防止"跳过清单"烂掉。
- 值级白名单 _VALUE_WHITELIST 当前为空：没有任何"YAML 写空表示自动"类的合理差异。
  以后如果出现此类差异，放进白名单时必须写明原因，且断言该键仍不一致（变一致 = 白名单烂掉，
  测试失败要求删除）。
- api_key 由 api_key_env 指向的环境变量注入（config_sources 的 env 层），测试里清掉
  AGENT_API_KEY 再加载，避免本机环境把 api_key 污染成不一致。

复现方法：
    cd <worktree> && PYTHONPATH=$PWD $PY -m pytest agent_py_agent/tests/test_config_defaults_parity.py -q --tb=short
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from agent_py_agent.agent.settings.config import AgentConfig, load_config

_PACKAGED = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"

# 加载器写入的运行时元数据：不是 YAML 值，加载后由 loader 决定；键从 AgentConfig 消失即失败。
_META_SKIP_KEYS = {
    "config_layers",
    "config_path",
    "config_sources",
    "config_warnings",
    "memory_config_warnings",
}

# 值级白名单：YAML 写空表示自动、与 dataclass 默认不同但合理的键。当前为空。
# 放进这里必须写明原因，且测试要求该键仍然不一致（变一致说明差异已消除，应删掉白名单项）。
_VALUE_WHITELIST: dict[str, str] = {}


def test_packaged_yaml_defaults_match_agent_config_dataclass(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)  # api_key 由 env 层注入，清掉避免污染
    shipped = load_config(_PACKAGED)
    defaults = AgentConfig()
    names = {item.name for item in fields(AgentConfig)}

    for key in _META_SKIP_KEYS:
        assert key in names, f"跳过清单里的键已不存在，请从 _META_SKIP_KEYS 删除：{key}"

    for key, reason in _VALUE_WHITELIST.items():
        assert key in names, f"值级白名单里的键已不存在，请删除：{key}（{reason}）"
        assert getattr(shipped, key) != getattr(defaults, key), (
            f"值级白名单里的键 {key} 已一致，请从 _VALUE_WHITELIST 删除（原原因：{reason}）")

    for item in fields(AgentConfig):
        if item.name in _META_SKIP_KEYS or item.name in _VALUE_WHITELIST:
            continue
        assert getattr(shipped, item.name) == getattr(defaults, item.name), (
            f"随包 YAML 与 AgentConfig 默认值不一致：{item.name} "
            f"shipped={getattr(shipped, item.name)!r} default={getattr(defaults, item.name)!r}")


def test_whitelist_reason_text_is_kept_in_sync_with_reality():
    """值级白名单的键必须都是 AgentConfig 真实字段，原因必须非空，防止名单烂掉。"""
    names = {item.name for item in fields(AgentConfig)}
    assert set(_VALUE_WHITELIST) <= names, set(_VALUE_WHITELIST) - names
    assert all(str(reason).strip() for reason in _VALUE_WHITELIST.values())