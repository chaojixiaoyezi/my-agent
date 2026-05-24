from __future__ import annotations

import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_artifact_repair import (
    append_artifact_finding_repair_actions,
)
from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_recovery_models import (
    RecoveryActionLedger,
)
from agent_py_agent.tests.test_main_agent_delivery_closeout_staged import _enriched_report


# LLM: dotted API names are finding facts, not files to patch.
# 函数用途: 验证 `app.goBrowse` 这类 JS API finding value 不会被 closeout 拼成假 repair target。
def test_delivery_closeout_does_not_treat_dotted_api_findings_as_file_targets() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _static_site_contract(workspace)
        site = workspace / "outputs/site"
        site.mkdir(parents=True)
        (site / "index.html").write_text(_site_html(), encoding="utf-8")
        (site / "app.js").write_text("const app = {};", encoding="utf-8")

        _, actions = _enriched_report(workspace, contract)

        action = actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]
        assert str(site / "index.html") in action["repair_targets"]
        assert str(site / "app.js") in action["repair_targets"]
        assert not any(item.endswith("/app.goBrowse") for item in action["repair_targets"])


# LLM: Open-world artifact repair should not depend on a fixed suffix allowlist.
# 函数用途: 验证已存在的新格式产物文件会进入 repair_targets，而不是因为后缀不在表里被丢掉。
def test_delivery_closeout_repair_targets_accept_existing_open_world_files(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "outputs" / "dataset"
    artifact_dir.mkdir(parents=True)
    table = artifact_dir / "metrics.parquet"
    table.write_bytes(b"PAR1")
    ledger = RecoveryActionLedger(actions=[], seen=set())

    append_artifact_finding_repair_actions(
        {
            "artifacts": [
                {
                    "ok": False,
                    "artifact_id": "dataset",
                    "kind": "parquet",
                    "path": str(artifact_dir),
                    "acceptance_report": {
                        "findings": [
                            {
                                "code": "ARTIFACT_CONTENT_INVALID",
                                "location": "metrics.parquet:row=1",
                            }
                        ]
                    },
                }
            ]
        },
        ledger,
    )

    assert ledger.actions
    assert str(table) in ledger.actions[0]["repair_targets"]


def _static_site_contract(workspace: Path) -> dict[str, object]:
    return {
        "artifacts": [
            {
                "artifact_id": "site",
                "kind": "web_project",
                "path": str(workspace / "outputs/site"),
                "validation_contract": {"validator": "static_site_check", "required_files": ["index.html", "app.js"]},
            }
        ]
    }


def _site_html() -> str:
    return '<!doctype html><html><body><button onclick="app.goBrowse()">Go</button><script src="app.js"></script></body></html>'
