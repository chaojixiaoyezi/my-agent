from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_doc_sync_module():
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "check_doc_sync.py"
    spec = importlib.util.spec_from_file_location("check_doc_sync", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_doc_sync_requires_module_progress_and_structure_docs():
    sync = _load_doc_sync_module()

    problems = sync.evaluate_sync(
        ["agent_py_agent/agent/log_analysis/dispatch/work_orders.py"],
        {"agent_py_agent/agent/log_analysis/dispatch/work_orders.py": ""},
    )

    assert any("docs/modules/log-analysis/02-progress.md" in item for item in problems)
    assert any("docs/modules/log-analysis/04-structure.md" in item for item in problems)


def test_doc_sync_accepts_matching_module_docs_and_comment_update():
    sync = _load_doc_sync_module()

    path = "agent_py_agent/agent/log_analysis/dispatch/work_orders.py"
    problems = sync.evaluate_sync(
        [
            path,
            "docs/modules/log-analysis/02-progress.md",
            "docs/modules/log-analysis/04-structure.md",
        ],
        {
            path: "\n".join(
                [
                    "diff --git a/x b/x",
                    "+def new_gate():",
                    "+    \"\"\"LLM: explain the new gate.\"\"\"",
                    "+    return True",
                ]
            )
        },
    )

    assert problems == []


def test_doc_sync_rejects_code_change_without_same_file_comment_update():
    sync = _load_doc_sync_module()

    path = "agent_py_agent/agent/subagent_workflows/planner.py"
    problems = sync.evaluate_sync(
        [
            path,
            "docs/modules/subagent/02-progress.md",
            "docs/modules/subagent/04-structure.md",
        ],
        {path: "+def new_planner_gate():\n+    return True"},
    )

    assert any("no same-file comment/doc update" in item for item in problems)
