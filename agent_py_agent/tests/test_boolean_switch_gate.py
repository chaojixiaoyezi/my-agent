# LLM: 布尔开关二元律门禁的自测：合成最小仓库验证每条规则会抓到新增违例、基线只减不增，
#   并在真实仓库上确认没有基线外的新违例。纯 AST/文件读取，不导入产品代码、不写 ~/.my-agent。
"""Self-tests for scripts/check_boolean_switches_and_hot_reload.py."""
from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = ROOT / "scripts" / "check_boolean_switches_and_hot_reload.py"


def _gate():
    cached = sys.modules.get("bool_switch_gate")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("bool_switch_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the defining module through sys.modules
    spec.loader.exec_module(module)
    return module


_FILES = {
    "agent_py_agent/agent/settings/config.py": """
        from dataclasses import dataclass
        @dataclass
        class AgentConfig:
            stream_enabled: bool = True
            audit_enabled: bool = True
    """,
    "agent_py_agent/agent/capability/config.py": """
        from dataclasses import dataclass
        @dataclass
        class CapabilityConfig:
            tool_program_enabled: bool = True
    """,
    "agent_py_agent/agent/settings/deployment_policy.py": """
        from dataclasses import dataclass
        @dataclass
        class RuntimeGuardPolicy:
            terminal_block_enabled: bool = False
    """,
    "agent_py_agent/config/agent_config.yaml": "stream_enabled: true\naudit_enabled: true\n",
    "agent_py_agent/config/capability_config.yaml": "tool_program_enabled: true\n",
    "agent_py_agent/agent/settings/parameter_registry.py": """
        from .live_switches import LIVE_SWITCH_KEYS
        DEPLOYMENT_ONLY_KEYS = frozenset({"audit_enabled"})
        EFFECT_IMMEDIATE = "immediate"
        def parameter_registry(name):
            return EFFECT_IMMEDIATE if name in LIVE_SWITCH_KEYS else "restart_gateway"
    """,
    "agent_py_agent/agent/capability/runtime_config_reload.py": "DEPLOYMENT_CAPABILITY_KEYS = frozenset()\n",
    "agent_py_agent/agent/settings/user_config_capability.py": """
        TUNABLE_KEYS = {"max_tokens": None}
        USER_SETTINGS_BOUNDARY_KEYS = frozenset({"agents_source_enabled"})
    """,
    "agent_py_agent/agent/settings/parameter_changes.py": """
        from .live_switches import refresh_live_switches
        CLI_DEPLOYMENT_WRITABLE_KEYS = frozenset({"audit_enabled"})
        def set_parameter():
            refresh_live_switches()
        def reset_parameter():
            refresh_live_switches()
        def revert_change():
            refresh_live_switches()
    """,
    "agent_py_agent/agent/agent_core/runtime_mixin.py": """
        def _run_once_with_params(agent):
            refresh_live_switches(agent.config)
    """,
    "agent_py_agent/agent/auth/manager.py": "def authenticate(cfg):\n    refresh_live_switches(cfg)\n",
    "agent_py_agent/cli/daemon.py": "def tick(cfg):\n    refresh_live_switches(cfg)\n",
    "agent_py_agent/agent/settings/live_switches.py": """
        DEPLOY_LIVE_SWITCH_KEYS = frozenset({"audit_enabled", "terminal_block_enabled"})
        LIVE_SWITCH_KEYS = frozenset({"stream_enabled", "tool_program_enabled"}) | DEPLOY_LIVE_SWITCH_KEYS
        def refresh_live_switches(config=None):
            return frozenset()
    """,
    "agent_py_agent/agent/service.py": """
        class Service:
            def __init__(self, config):
                self.config = config
            def stream(self):
                return self.config.stream_enabled
    """,
}


def _repo(tmp_path: Path, **overrides: str) -> Path:
    for rel, text in {**_FILES, **overrides}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return tmp_path


def _rules(root: Path) -> set[tuple[str, str]]:
    violations, _facts = _gate().run_gate(root)
    return {(item.rule, item.name) for item in violations}


def test_clean_synthetic_repo_has_no_violations(tmp_path):
    assert _rules(_repo(tmp_path)) == set()


@pytest.mark.parametrize(("rel", "text", "expected"), [
    ("agent_py_agent/agent/settings/config.py",
     "from dataclasses import dataclass\n@dataclass\nclass AgentConfig:\n    stream_enabled: bool = True\n"
     "    audit_enabled: bool = True\n    store_result_cache_enabled: bool = False\n",
     ("B-CFG", "store_result_cache_enabled")),
    ("agent_py_agent/agent/feature.py", "def scan(*, cheap_checks: bool = False):\n    return cheap_checks\n",
     ("B-PARAM", "cheap_checks")),
    ("agent_py_agent/agent/feature.py", "def scan(*, include_archives: bool = True):\n    return include_archives\n",
     ("B-PARAM", "include_archives")),
    ("agent_py_agent/agent/feature.py", "def run(store):\n    return store.list_records(cheap_checks=False)\n",
     ("B-CALL", "cheap_checks")),
    ("agent_py_agent/agent/feature.py", "LOOKUP_CACHE_ENABLED = True\n", ("B-MODCONST", "LOOKUP_CACHE_ENABLED")),
    ("agent_py_agent/agent/feature.py",
     "class Backend:\n    def __init__(self, config):\n        self.stream = config.stream_enabled\n",
     ("A-SNAP", "stream_enabled")),
    ("agent_py_agent/agent/settings/user_config_capability.py",
     "TUNABLE_KEYS = {'audit_enabled': None}\nUSER_SETTINGS_BOUNDARY_KEYS = frozenset()\n",
     ("A-DEPLOY-WRITER", "audit_enabled")),
    ("agent_py_agent/agent/settings/live_switches.py",
     "DEPLOY_LIVE_SWITCH_KEYS = frozenset({'audit_enabled', 'terminal_block_enabled'})\n"
     "LIVE_SWITCH_KEYS = frozenset({'tool_program_enabled'}) | DEPLOY_LIVE_SWITCH_KEYS\n",
     ("A-UNCLASSIFIED", "stream_enabled")),
    ("agent_py_agent/agent/agent_core/runtime_mixin.py", "def _run_once_with_params(agent):\n    return None\n",
     ("A-HOOK", "refresh_live_switches")),
    ("agent_py_agent/agent/settings/parameter_registry.py",
     "DEPLOYMENT_ONLY_KEYS = frozenset({'audit_enabled'})\n", ("A-REGISTRY-EFFECT", "LIVE_SWITCH_KEYS")),
])
def test_each_rule_catches_a_new_violation(tmp_path, rel, text, expected):
    assert expected in _rules(_repo(tmp_path, **{rel: text}))


def test_declarations_must_stay_static_literals(tmp_path):
    root = _repo(tmp_path, **{"agent_py_agent/agent/settings/live_switches.py":
                              "LIVE_SWITCH_KEYS = build()\nDEPLOY_LIVE_SWITCH_KEYS = frozenset()\n"})
    with pytest.raises(_gate().GateError):
        _gate().run_gate(root)


def test_baseline_is_shrink_only(tmp_path):
    gate = _gate()
    root = _repo(tmp_path, **{"agent_py_agent/agent/feature.py": "def scan(*, bounded: bool = False):\n    return 1\n"})
    baseline = tmp_path / "baseline.json"
    assert gate.main(["--root", str(root), "--baseline", str(baseline), "--write-baseline"]) == 0
    assert gate.main(["--root", str(root), "--baseline", str(baseline)]) == 0
    assert gate.main(["--root", str(root), "--baseline", str(baseline), "--write-baseline"]) == 1
    data = json.loads(baseline.read_text(encoding="utf-8"))
    data["entries"].append("B-PARAM|agent_py_agent/agent/feature.py|other|bounded")
    baseline.write_text(json.dumps(data), encoding="utf-8")
    assert gate.main(["--root", str(root), "--baseline", str(baseline)]) == 1
    assert gate.main(["--root", str(root), "--baseline", str(baseline), "--strict"]) == 1


def test_repository_has_no_violation_outside_the_baseline():
    assert _gate().main(["--root", str(ROOT)]) == 0
