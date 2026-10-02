"""守卫：随包 YAML 配置键集合必须与前端参数目录一致，注释归属与 restart 语义对齐后端。

后端事实源（以后端为准）：
- 键集合与注释归属：agent_py_agent/agent/settings/parameter_registry.py 的 _descriptions_from_lines
  （空行或任何非注释行中断注释块，无上方注释时取行尾注释，同名键只取第一次）。
- 生效语义：parameter_registry.parameter_registry() 对每个参数声明 effect=EFFECT_GATEWAY_RESTART，
  user_config_capability.py 的 TUNABLE_KEYS 也没有 next_session 的键，因此目录里每项都要标记需要重启。

生成器：frontend/scripts/sync-backend-config.mjs（node 重新生成后运行本文件即可验证一致；
本守卫不依赖 node，直接对比 YAML 与目录 JSON）。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.settings.parameter_registry import _descriptions_from_lines

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "frontend" / "config" / "backend-config-catalog.json"
# (后端 source id, YAML 相对仓库根的路径)；目录里每项的 source 字段是 YAML 文件名。
YAML_FILES = (
    ("agent_config", "agent_py_agent/config/agent_config.yaml"),
    ("capability_config", "agent_py_agent/config/capability_config.yaml"),
)
SOURCE_TO_ID = {
    "agent_config.yaml": "agent_config",
    "capability_config.yaml": "capability_config",
}


# 函数用途: 读出目录 JSON 里全部字段。
def _catalog_fields() -> list[dict]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return [field for category in catalog["schema"]["categories"] for field in category["fields"]]


# 函数用途: 用后端同一函数读出某个 YAML 的键集合。
def _yaml_keys(source_id: str) -> set[str]:
    path = next(p for sid, p in YAML_FILES if sid == source_id)
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    return set(_descriptions_from_lines(text.splitlines()))


def test_catalog_covers_exactly_the_packaged_yaml_keys():
    fields = _catalog_fields()
    expected_total = 0
    for source_id, _yaml_path in YAML_FILES:
        yaml_keys = _yaml_keys(source_id)
        expected_total += len(yaml_keys)
        catalog_keys = {
            field["key"] for field in fields if SOURCE_TO_ID[field["source"]] == source_id
        }
        assert yaml_keys == catalog_keys, (
            f"{source_id} 键集合不一致：仅 YAML 有={sorted(yaml_keys - catalog_keys)}，"
            f"仅目录有={sorted(catalog_keys - yaml_keys)}。重新运行 node frontend/scripts/sync-backend-config.mjs"
        )
    assert len(fields) == expected_total, f"目录总数 {len(fields)} != YAML 键总数 {expected_total}"


def test_catalog_descriptions_match_backend_comment_rule():
    fields = _catalog_fields()
    for source_id, _yaml_path in YAML_FILES:
        text = (REPO_ROOT / next(p for sid, p in YAML_FILES if sid == source_id)).read_text(
            encoding="utf-8"
        )
        backend = _descriptions_from_lines(text.splitlines())
        for field in fields:
            if SOURCE_TO_ID[field["source"]] != source_id:
                continue
            expected = backend.get(field["key"], "")
            if expected:
                assert field["description"] == expected, (
                    f"{source_id}:{field['key']} 的说明与后端不一致：目录={field['description']!r} 后端={expected!r}"
                )


def test_catalog_restart_required_matches_backend_effect():
    fields = _catalog_fields()
    assert fields, "目录不应为空"
    assert all(field["restartRequired"] for field in fields), (
        "后端所有参数 effect=EFFECT_GATEWAY_RESTART（配置修改都要重启 Gateway 才生效），"
        "目录里每一项都应标记 restartRequired=true"
    )