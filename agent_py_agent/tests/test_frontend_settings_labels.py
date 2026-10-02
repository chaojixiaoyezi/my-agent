"""守卫：前端设置页所有 `label="键（` 的表单项键必须都在权威键集合里。

权威键集合 = 随包 agent_config.yaml + capability_config.yaml + log_analysis_config.yaml
（用后端 parameter_registry._descriptions_from_lines 读出的顶层键）+ frontend/config/backend-config-catalog.json
（生成器产物，ds1 已重新生成为 270 项）。防止以后再积出对不上任何配置键的死表单项。

生成器：frontend/scripts/sync-backend-config.mjs；后端注册表：parameter_registry.py。
本守卫不依赖 node / npm / bun，直接读 YAML、目录 JSON 和前端源码。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent_py_agent.agent.settings.parameter_registry import _descriptions_from_lines

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_DIR = REPO_ROOT / "frontend" / "src" / "pages" / "settings"
CATALOG_PATH = REPO_ROOT / "frontend" / "config" / "backend-config-catalog.json"
STORE_PATH = REPO_ROOT / "frontend" / "src" / "stores" / "settingsStore.ts"
RUNTIME_JSON = REPO_ROOT / "frontend" / "config" / "frontend-runtime-config.json"
# 表单项的 label 键形态：`label="snake_case_key（中文说明）"`
_LABEL_KEY_PATTERN = re.compile(r'label="([A-Za-z_][A-Za-z0-9_]*)（')


# 函数用途: 读权威键集合（三份随包 YAML 顶层键 + 生成目录的字段键）。
def _authority_keys() -> set[str]:
    keys: set[str] = set()
    for name in ("agent_config", "capability_config", "log_analysis_config"):
        text = (REPO_ROOT / f"agent_py_agent/config/{name}.yaml").read_text(encoding="utf-8")
        keys.update(_descriptions_from_lines(text.splitlines()))
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    for category in catalog["schema"]["categories"]:
        for field in category["fields"]:
            keys.add(field["key"])
    return keys


# 函数用途: 从设置页源码里提取所有 `label="键（` 的表单项键。
def _settings_label_keys() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(SETTINGS_DIR.glob("Settings*.tsx")):
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in _LABEL_KEY_PATTERN.finditer(line):
                found.setdefault(match.group(1), []).append(f"{path.name}:{index}")
    return found


def test_settings_labels_all_exist_in_authority_keys():
    authority = _authority_keys()
    labels = _settings_label_keys()
    assert labels, "设置页没有提取到任何 label 键，检查源码是否被改坏"
    dead = {key: locs for key, locs in labels.items() if key not in authority}
    assert not dead, (
        f"设置页有 {len(dead)} 个表单项键不在权威键集合（三份随包 YAML + backend-config-catalog.json）："
        + "; ".join(f"{key}@{','.join(locs)}" for key, locs in sorted(dead.items()))
    )


def test_settings_store_groups_exist_in_runtime_config():
    store_text = STORE_PATH.read_text(encoding="utf-8")
    runtime = json.loads(RUNTIME_JSON.read_text(encoding="utf-8"))["defaults"]
    missing = []
    for match in re.finditer(r"frontendRuntimeConfig\.([A-Za-z_][A-Za-z0-9_]*)\b", store_text):
        group = match.group(1)
        if group == "tools":
            continue
        if group not in runtime:
            missing.append(group)
    assert not missing, f"settingsStore.ts 引用了 frontend-runtime-config.json 里不存在的组：{sorted(set(missing))}"