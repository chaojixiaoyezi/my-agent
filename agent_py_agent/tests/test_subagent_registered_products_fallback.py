"""产出事实兜底钉子(C3/G4 实锤):runner 没产出可解析结果块,但子代理已在
artifact_registry 登记 ready 产物时,应据产物收尾为 DONE 而非埋没成 BLOCKED。"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent_mixin import (
    _ready_product_entry,
    _registered_ready_products,
    _structured_from_registered_products,
)
from agent_py_agent.agent.artifacts.registry import (
    ArtifactRegistration,
    register_artifact,
)


def _make_agent(run_id: str, workspace) -> SimpleNamespace:
    task = SimpleNamespace(task_workspace_dir=str(workspace))

    def load(rid: str):
        if rid == run_id:
            return task
        raise FileNotFoundError(rid)

    return SimpleNamespace(subagents=SimpleNamespace(load=load))


def _register_ready_product(workspace, run_id: str, name: str = "deliver.md"):
    product = workspace / "output" / name
    product.parent.mkdir(parents=True, exist_ok=True)
    product.write_text("交付内容", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=product,
            run_id=run_id,
            agent_id=run_id,
            status="ready",
        )
    )
    return product


def test_ready_product_entry_accepts_ready_file(tmp_path):
    f = tmp_path / "out.md"
    f.write_text("hello", encoding="utf-8")
    entry = _ready_product_entry(SimpleNamespace(status="ready", path=str(f)), set())
    assert entry is not None
    assert entry["name"] == "out.md"
    assert entry["bytes"] == len(b"hello")


def test_ready_product_entry_skips_non_ready(tmp_path):
    f = tmp_path / "out.md"
    f.write_text("x", encoding="utf-8")
    assert _ready_product_entry(SimpleNamespace(status="missing", path=str(f)), set()) is None


def test_ready_product_entry_skips_nonexistent(tmp_path):
    rec = SimpleNamespace(status="ready", path=str(tmp_path / "ghost.md"))
    assert _ready_product_entry(rec, set()) is None


def test_ready_product_entry_dedups_same_path(tmp_path):
    f = tmp_path / "out.md"
    f.write_text("x", encoding="utf-8")
    rec = SimpleNamespace(status="ready", path=str(f))
    seen: set[str] = set()
    assert _ready_product_entry(rec, seen) is not None
    assert _ready_product_entry(rec, seen) is None


def test_registered_ready_products_reads_subagent_registry(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _register_ready_product(workspace, "sub-1")
    products = _registered_ready_products(_make_agent("sub-1", workspace), "sub-1")
    assert len(products) == 1
    assert products[0]["name"] == "deliver.md"


def test_registered_ready_products_empty_run_id():
    assert _registered_ready_products(SimpleNamespace(), "") == []


def test_registered_ready_products_unknown_run_id(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _register_ready_product(workspace, "sub-1")
    # load 抛 FileNotFoundError -> 无 workspace -> 空
    assert _registered_ready_products(_make_agent("sub-1", workspace), "other") == []


def test_structured_from_registered_products_synthesizes_done(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _register_ready_product(workspace, "sub-2")
    out = _structured_from_registered_products(
        _make_agent("sub-2", workspace), SimpleNamespace(run_id="sub-2")
    )
    assert out is not None
    assert out.found and out.ok
    assert out.status == "DONE"
    assert "deliver.md" in out.summary
    assert out.evidence and out.evidence[0]["kind"] == "registered_artifact"


def test_structured_from_registered_products_none_without_products(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    out = _structured_from_registered_products(
        _make_agent("sub-3", workspace), SimpleNamespace(run_id="sub-3")
    )
    assert out is None
