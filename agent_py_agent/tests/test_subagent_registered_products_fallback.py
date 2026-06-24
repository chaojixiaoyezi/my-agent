"""产出事实兜底钉子(C3/G4 实锤):runner 没产出可解析结果块,但子代理已在
artifact_registry 登记 ready 产物时,应据产物收尾为 DONE 而非埋没成 BLOCKED。"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent_mixin import (
    _is_self_negated_block,
    _ready_product_entry,
    _recover_with_registered_products,
    _registered_ready_products,
    _structured_from_registered_products,
)
from agent_py_agent.agent.artifacts.registry import (
    ArtifactRegistration,
    register_artifact,
)
from agent_py_agent.agent.subagents.model_runtime import SubAgentParsedOutput


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


# --- 自我否定兜底(g4w 实锤:子代理声明 BLOCKED 却已写 ready 产物) ---


def test_is_self_negated_block_true_for_block_without_request():
    s = SubAgentParsedOutput(found=True, ok=True, status="BLOCKED", capability_requests=[])
    assert _is_self_negated_block(s) is True


def test_is_self_negated_block_false_with_capability_request():
    s = SubAgentParsedOutput(found=True, ok=True, status="BLOCKED", capability_requests=[{"need": "x"}])
    assert _is_self_negated_block(s) is False


def test_is_self_negated_block_false_for_done():
    assert _is_self_negated_block(SubAgentParsedOutput(found=True, ok=True, status="DONE")) is False


def test_recover_upgrades_self_negated_block_with_products(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _register_ready_product(workspace, "sub-b")
    structured = SubAgentParsedOutput(
        found=True, ok=True, status="BLOCKED", blocked_reason="缺少核心交付物", capability_requests=[]
    )
    out = _recover_with_registered_products(
        _make_agent("sub-b", workspace), SimpleNamespace(run_id="sub-b"), structured
    )
    assert out.status == "DONE"
    assert "声明阻塞" in out.summary  # 保留 caveat 留痕


def test_recover_keeps_block_without_products(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    structured = SubAgentParsedOutput(found=True, ok=True, status="BLOCKED", capability_requests=[])
    out = _recover_with_registered_products(
        _make_agent("sub-c", workspace), SimpleNamespace(run_id="sub-c"), structured
    )
    assert out.status == "BLOCKED"  # 无 ready 产物 → 尊重真失败


def test_recover_respects_capability_request_block(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _register_ready_product(workspace, "sub-d")
    structured = SubAgentParsedOutput(
        found=True, ok=True, status="BLOCKED", capability_requests=[{"need": "x"}]
    )
    out = _recover_with_registered_products(
        _make_agent("sub-d", workspace), SimpleNamespace(run_id="sub-d"), structured
    )
    assert out.status == "BLOCKED"  # 正当求助即便有产物也不覆盖


def test_recover_no_interfere_with_normal_done(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _register_ready_product(workspace, "sub-e")
    structured = SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="原始")
    out = _recover_with_registered_products(
        _make_agent("sub-e", workspace), SimpleNamespace(run_id="sub-e"), structured
    )
    assert out.status == "DONE"
    assert out.summary == "原始"  # 正常 DONE 原样返回,不触碰


def test_recover_parse_failure_with_products(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _register_ready_product(workspace, "sub-f")
    structured = SubAgentParsedOutput(found=False, ok=False)  # 无可解析结果块
    out = _recover_with_registered_products(
        _make_agent("sub-f", workspace), SimpleNamespace(run_id="sub-f"), structured
    )
    assert out.status == "DONE"  # 原兜底场景仍生效


# --- 占位空壳质量闸(loop 实锤:占位据占位收尾把 BLOCKED 套娃推翻成 DONE) ---

# 真机抓到的占位形态:子代理被截断/未执行,系统把结果元数据块 materialize 成产物本身,
# 以 "# Subagent Result" 开头。它通过了 registry 的 is_file() 登记 ready,却零实质交付。
_PLACEHOLDER_STUB = (
    "# Subagent Result\n\n"
    "- run_id: subagent-x\n- status: BLOCKED\n"
    "- declared_output_ref: output/sec-adapter-audit.md\n\n"
    "## Summary\n\n上一轮子代理执行中断,未完成报告输出。任务未完成。\n"
)


def test_ready_product_entry_skips_placeholder_stub(tmp_path):
    """产物以 '# Subagent Result' 开头 = 系统兜底占位空壳,不计入 ready 产物。"""
    f = tmp_path / "sec-adapter-audit.md"
    f.write_text(_PLACEHOLDER_STUB, encoding="utf-8")
    assert _ready_product_entry(SimpleNamespace(status="ready", path=str(f)), set()) is None


def test_ready_product_entry_skips_empty_file(tmp_path):
    """空文件不算合格交付。"""
    f = tmp_path / "empty.md"
    f.write_text("", encoding="utf-8")
    assert _ready_product_entry(SimpleNamespace(status="ready", path=str(f)), set()) is None


def test_ready_product_entry_keeps_real_product(tmp_path):
    """真实产物(非占位)仍正常计入,不误伤。"""
    f = tmp_path / "real-audit.md"
    f.write_text(
        "# Adapter 安全审计\n\n## SEC-ADP-001 验签退化\n\n"
        "encrypt_key 缺失时验签退化为空操作,导致签名校验被绕过,存在伪造回调风险。\n",
        encoding="utf-8",
    )
    assert _ready_product_entry(SimpleNamespace(status="ready", path=str(f)), set()) is not None


def test_recover_keeps_block_when_only_placeholder_products(tmp_path):
    """端到端回归(loop 实锤真 bug):子代理诚实声明 BLOCKED,registry 里只有占位空壳产物
    时,兜底不能据占位把 BLOCKED 推翻成 DONE——占位据占位收尾是机制层糊弄。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    product = workspace / "output" / "sec-adapter-audit.md"
    product.parent.mkdir(parents=True, exist_ok=True)
    product.write_text(_PLACEHOLDER_STUB, encoding="utf-8")
    register_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=product,
            run_id="sub-stub",
            agent_id="sub-stub",
            status="ready",
        )
    )
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="BLOCKED",
        blocked_reason="未执行任何工具调用,无可验收证据",
        capability_requests=[],
    )
    out = _recover_with_registered_products(
        _make_agent("sub-stub", workspace), SimpleNamespace(run_id="sub-stub"), structured
    )
    assert out.status == "BLOCKED"  # 占位产物不足以推翻诚实的 BLOCKED
