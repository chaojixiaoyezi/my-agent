"""模块用途: 验证历史用量重算投影脚本（scripts/reproject_model_usage.py）的判定与只读边界。

这些测试用**合成账本**覆盖三类置信度（exact / partial / incomplete），并通过"跑前跑后 mtime 与
内容哈希完全一致"证明脚本只读。合成账本字段与产品真实落盘 schema 对齐
（thread_model_usage_event.v1 + model_call_summary.v1 + model_usage_breakdown.v1），
所以断言同时是脚本与运行时账本格式的对接契约。

LLM: 改动 reproject_model_usage.py 的判定规则时必须同步这里的期望值；这里断言的是"对外契约"
（协议判定、重算数字、置信度、只读），不是内部实现细节。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.reproject_model_usage import (
    ANTHROPIC,
    CONFIDENCE_EXACT,
    CONFIDENCE_INCOMPLETE,
    CONFIDENCE_PARTIAL,
    OPENAI_COMPATIBLE,
    classify_backends,
    discover_ledger_paths,
    main,
    project_file,
    protocol_input_tokens,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "reproject_model_usage.py"
# 任务书给出的真机样本：Anthropic 兼容会话旧口径把 33,802 读 + 4,111 写整轮记丢，只剩 97 输出。
REAL_WORLD_ANTHROPIC_SAMPLE = {
    "backends": ["anthropic_compatible"],
    "models": ["MiniMax-M2.7"],
    "accounted_input_tokens": 0,
    "output_tokens": 97,
    "cached_input_tokens": 33802,
    "cache_creation_input_tokens": 4111,
}


# 函数用途: 按真实账本 schema 合成一行 model_usage 事件，调用方用 kwargs 覆盖需要的字段。
def _ledger_row(event_id: str, **fields: Any) -> dict[str, Any]:
    calls: dict[str, Any] = {
        "schema": "model_call_summary.v1",
        "logical_model_turn_count": 1,
        "physical_model_attempt_count": 1,
        "model_retry_count": 0,
        "provider_http_attempt_count": 1,
        "provider_http_retry_count": 0,
        "status_counts": {"finished": 1},
        "backends": ["openai_compatible"],
        "models": ["deepseek-v4-flash"],
        "accounted_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "provider_usage_call_count": 1,
        "estimated_usage_call_count": 0,
        "ledger_projection": {"kind": "cumulative_snapshot_delta", "snapshot_digest": "synthetic"},
    }
    calls.update(fields)
    if "usage_breakdown" not in fields:
        calls["usage_breakdown"] = {
            "schema": "model_usage_breakdown.v1",
            "provider": {
                "input_tokens": calls["accounted_input_tokens"],
                "output_tokens": calls["output_tokens"],
                "cache_read_input_tokens": calls["cached_input_tokens"],
                "cache_write_input_tokens": calls["cache_creation_input_tokens"],
                "call_count": calls["provider_usage_call_count"],
            },
            "estimated": {
                "input_tokens": 0,
                "output_tokens": 0,
                "call_count": calls["estimated_usage_call_count"],
            },
        }
    if "total_tokens" not in fields:
        calls["total_tokens"] = calls["accounted_input_tokens"] + calls["output_tokens"]
    return {
        "schema_version": "thread_model_usage_event.v1",
        "event_id": event_id,
        "thread_id": "thread-synthetic",
        "request_id": f"request-{event_id}",
        "run_id": "run-synthetic",
        "task_id": "",
        "source": "main",
        "model_calls": calls,
        "created_at": 1.0,
    }


# 函数用途: 把若干合成行写成 jsonl 账本文件（供脚本与函数两种入口读取）。
def _write_ledger(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


# 函数用途: 记录整棵树的相对路径 → (mtime_ns, 大小, 内容哈希)，用来证明脚本没有写文件。
def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    snapshot: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[str(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size, digest)
    return snapshot


# 函数用途: 以子进程方式跑真实 CLI，保证被测入口与用户执行路径一致。
def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_reproject_anthropic_row_restores_dropped_cache_columns(tmp_path: Path) -> None:
    """真机样本：旧口径 accounted=0 时，重算必须补回缓存读 + 缓存写。"""
    ledger = _write_ledger(
        tmp_path / "model_usage" / "thread-anthropic.jsonl",
        [_ledger_row("evt-1", **REAL_WORLD_ANTHROPIC_SAMPLE)],
    )

    projection = project_file(ledger)

    assert projection.protocol == ANTHROPIC
    assert projection.confidence == CONFIDENCE_EXACT
    assert projection.legacy_total_tokens == 97
    assert projection.reprojected_input_tokens == 33802 + 4111
    assert projection.reprojected_total_tokens == 37913 + 97
    assert projection.delta_tokens == 37913
    assert projection.rows[0].basis == "anthropic_exclusive_cache_added"
    # 重算值必须与产品同一条归一语义一致，而不是工具自己算的一份数。
    assert protocol_input_tokens(ANTHROPIC, 0, 33802, 4111, 97) == 37913


def test_reproject_openai_row_keeps_cache_inclusive_total(tmp_path: Path) -> None:
    """OpenAI 兼容：prompt 总数已含缓存明细，重算不得再加一次缓存读。"""
    ledger = _write_ledger(
        tmp_path / "model_usage" / "thread-openai.jsonl",
        [
            _ledger_row(
                "evt-2",
                backends=["openai_compatible"],
                accounted_input_tokens=49552,
                output_tokens=581,
                cached_input_tokens=22528,
            )
        ],
    )

    projection = project_file(ledger)

    assert projection.protocol == OPENAI_COMPATIBLE
    assert projection.confidence == CONFIDENCE_EXACT
    assert projection.reprojected_input_tokens == 49552
    assert projection.reprojected_input_tokens != 49552 + 22528
    assert projection.delta_tokens == 0


def test_reproject_marks_partial_when_columns_or_provider_truth_missing(tmp_path: Path) -> None:
    """分栏不全：缺分栏字段或含本地估算调用时只能标 partial，不得当精确值。"""
    estimated = _write_ledger(
        tmp_path / "estimated.jsonl",
        [
            _ledger_row(
                "evt-3",
                accounted_input_tokens=1000,
                output_tokens=50,
                cached_input_tokens=400,
                provider_usage_call_count=2,
                estimated_usage_call_count=1,
            )
        ],
    )
    no_breakdown = _write_ledger(
        tmp_path / "no_breakdown.jsonl",
        [
            _ledger_row(
                "evt-4",
                accounted_input_tokens=1000,
                output_tokens=50,
                cached_input_tokens=400,
                usage_breakdown=None,
            )
        ],
    )

    estimated_projection = project_file(estimated)
    missing_projection = project_file(no_breakdown)

    assert estimated_projection.confidence == CONFIDENCE_PARTIAL
    assert "estimated_usage_calls=1" in estimated_projection.notes
    assert missing_projection.confidence == CONFIDENCE_PARTIAL
    assert "missing_columns=1" in missing_projection.notes
    assert estimated_projection.rows[0].confidence == CONFIDENCE_PARTIAL


def test_reproject_marks_incomplete_for_mixed_and_unknown_protocol(tmp_path: Path) -> None:
    """混合协议 / 认不出的后端标签都不可归属：只能标 incomplete 并给出区间。"""
    mixed_row = _write_ledger(
        tmp_path / "mixed_row.jsonl",
        [
            _ledger_row(
                "evt-5",
                backends=["openai_compatible", "anthropic_compatible"],
                accounted_input_tokens=1000,
                output_tokens=50,
                cached_input_tokens=400,
                cache_creation_input_tokens=100,
            )
        ],
    )
    unknown_row = _write_ledger(
        tmp_path / "unknown_row.jsonl",
        [
            _ledger_row(
                "evt-6",
                backends=["probe"],
                accounted_input_tokens=500,
                output_tokens=10,
                provider_usage_call_count=0,
                estimated_usage_call_count=2,
            )
        ],
    )

    mixed = project_file(mixed_row)
    unknown = project_file(unknown_row)

    assert mixed.confidence == CONFIDENCE_INCOMPLETE
    assert mixed.rows[0].reprojected_input_tokens == 1000
    assert mixed.rows[0].reprojected_input_tokens_high == 1500
    assert "mixed_protocol_row=anthropic_compatible,openai_compatible" in mixed.rows[0].notes
    assert unknown.confidence == CONFIDENCE_INCOMPLETE
    assert unknown.protocol == "unknown"
    assert "unknown_backend_labels=probe" in unknown.rows[0].notes


def test_reproject_mixed_protocol_file_is_incomplete_even_when_rows_are_exact(tmp_path: Path) -> None:
    """跨行协议不同的文件：每行各自可精确重算，但整份文件不得标 exact。"""
    ledger = _write_ledger(
        tmp_path / "mixed_file.jsonl",
        [
            _ledger_row("evt-7", accounted_input_tokens=1000, output_tokens=50, cached_input_tokens=400),
            _ledger_row(
                "evt-8",
                backends=["anthropic_compatible"],
                accounted_input_tokens=0,
                output_tokens=20,
                cached_input_tokens=500,
                cache_creation_input_tokens=60,
            ),
        ],
    )

    projection = project_file(ledger)

    assert projection.protocol == "mixed"
    assert projection.confidence == CONFIDENCE_INCOMPLETE
    assert all(row.confidence == CONFIDENCE_EXACT for row in projection.rows)
    assert "mixed_protocols_rows_individually_exact" in projection.notes
    assert projection.reprojected_input_tokens == 1000 + 560


def test_reproject_zero_rows_distinguish_real_zero_from_missing_columns(tmp_path: Path) -> None:
    """真零行可以标 exact；"字段缺失导致的 0"不是真零，必须降级为 partial。"""
    real_zero = _write_ledger(
        tmp_path / "real_zero.jsonl",
        [
            _ledger_row(
                "evt-21",
                backends=["probe"],
                provider_usage_call_count=0,
                estimated_usage_call_count=0,
            )
        ],
    )
    missing_columns = _write_ledger(
        tmp_path / "missing.jsonl",
        [
            _ledger_row(
                "evt-22",
                accounted_input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cached_input_tokens=None,
                cache_creation_input_tokens=None,
                usage_breakdown=None,
            )
        ],
    )

    zero_projection = project_file(real_zero)
    missing_projection = project_file(missing_columns)

    assert zero_projection.confidence == CONFIDENCE_EXACT
    assert zero_projection.rows[0].basis == "zero_usage_row"
    assert missing_projection.confidence == CONFIDENCE_PARTIAL
    assert "missing_columns=1" in missing_projection.notes


def test_reproject_file_bounds_cover_exact_rows(tmp_path: Path) -> None:
    """文件级区间必须把"没有区间的精确行"也算进上界，否则上界会小于主值。"""
    ledger = _write_ledger(
        tmp_path / "bounded.jsonl",
        [
            _ledger_row("evt-19", accounted_input_tokens=1000, output_tokens=50, cached_input_tokens=400),
            _ledger_row(
                "evt-20",
                backends=["openai_compatible", "anthropic_compatible"],
                accounted_input_tokens=1000,
                output_tokens=50,
                cached_input_tokens=400,
                cache_creation_input_tokens=100,
            ),
        ],
    )

    projection = project_file(ledger)

    assert projection.confidence == CONFIDENCE_INCOMPLETE
    assert projection.reprojected_input_tokens == 1000 + 1000
    assert projection.reprojected_input_tokens_high == 1000 + 1500
    assert projection.reprojected_total_tokens_high == 1000 + 50 + 1500 + 50


def test_reproject_cli_json_reports_counts_and_never_claims_exact_for_incomplete(tmp_path: Path) -> None:
    """CLI JSON：三类文件各有明确计数，字段名即机器契约。"""
    root = tmp_path / "workspaces"
    _write_ledger(
        root / "ws-a" / "conversations" / "model_usage" / "exact.jsonl",
        [_ledger_row("evt-9", **REAL_WORLD_ANTHROPIC_SAMPLE)],
    )
    _write_ledger(
        root / "ws-b" / "conversations" / "model_usage" / "partial.jsonl",
        [
            _ledger_row(
                "evt-10",
                accounted_input_tokens=1000,
                output_tokens=50,
                cached_input_tokens=400,
                estimated_usage_call_count=1,
            )
        ],
    )
    _write_ledger(
        root / "ws-c" / "conversations" / "model_usage" / "incomplete.jsonl",
        [
            _ledger_row(
                "evt-11",
                backends=["openai_compatible", "anthropic_compatible"],
                accounted_input_tokens=1000,
                output_tokens=50,
                cached_input_tokens=400,
            )
        ],
    )

    result = _run_script(str(root), "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "model_usage_reprojection.v1"
    assert payload["summary"]["files"] == 3
    assert payload["summary"]["exact"] == 1
    assert payload["summary"]["partial"] == 1
    assert payload["summary"]["incomplete"] == 1
    by_name = {Path(item["path"]).name: item for item in payload["files"]}
    assert by_name["exact.jsonl"]["protocol"] == ANTHROPIC
    assert by_name["exact.jsonl"]["reprojected_input_tokens"] == 37913
    assert by_name["exact.jsonl"]["legacy_total_tokens"] == 97
    assert by_name["incomplete.jsonl"]["confidence"] == CONFIDENCE_INCOMPLETE
    assert by_name["incomplete.jsonl"]["reprojected_input_tokens_high"] == 1400


def test_reproject_text_output_warns_that_incomplete_is_not_exact(tmp_path: Path) -> None:
    """人类可读输出必须显式写"旧口径/不完整，不可当精确值"，两栏标签对齐。"""
    ledger = _write_ledger(
        tmp_path / "model_usage" / "unknown.jsonl",
        [_ledger_row("evt-12", backends=["probe"], accounted_input_tokens=500, output_tokens=10)],
    )

    result = _run_script(str(ledger))

    assert result.returncode == 0, result.stderr
    assert "旧口径/不完整，不可当精确值" in result.stdout
    assert "===" in result.stdout
    assert "置信度" in result.stdout


def test_reproject_script_does_not_write_any_file(tmp_path: Path) -> None:
    """只读铁证：文本与 JSON 两种输出跑完，整棵树的 mtime 与内容哈希完全不变。"""
    root = tmp_path / "owner-home"
    ledger_dir = root / "workspace" / "runtime" / "workspaces" / "ws-a" / "conversations" / "model_usage"
    _write_ledger(ledger_dir / "thread-a.jsonl", [_ledger_row("evt-13", **REAL_WORLD_ANTHROPIC_SAMPLE)])
    _write_ledger(
        ledger_dir / "thread-b.jsonl",
        [_ledger_row("evt-14", backends=["probe"], accounted_input_tokens=500, output_tokens=10)],
    )
    before = _tree_snapshot(tmp_path)

    text_run = _run_script(str(root), "--rows")
    json_run = _run_script(str(root), "--json")
    direct = main([str(root)])

    assert text_run.returncode == 0, text_run.stderr
    assert json_run.returncode == 0, json_run.stderr
    assert direct == 0
    assert _tree_snapshot(tmp_path) == before


def test_reproject_source_keeps_no_write_or_network_calls() -> None:
    """静态守门：脚本源码里不得出现写文件/起进程/发网络的调用。"""
    source = SCRIPT.read_text(encoding="utf-8")

    for forbidden in ('write_text(', '.write(', '"w"', "'w'", '"a"', "subprocess", "urlopen", "socket"):
        assert forbidden not in source, f"重算脚本不得包含 {forbidden}"


def test_reproject_discovers_owner_home_workspace_and_ledger_paths(tmp_path: Path) -> None:
    """三种输入形态（owner home / model_usage 目录 / 单文件）都能发现同一份账本。"""
    root = tmp_path / "owner-home"
    workspace = root / "workspace" / "runtime" / "workspaces" / "ws-a"
    ledger_dir = workspace / "conversations" / "model_usage"
    ledger = _write_ledger(ledger_dir / "thread-a.jsonl", [_ledger_row("evt-15")])

    from_home, home_errors = discover_ledger_paths([str(root)])
    from_dir, dir_errors = discover_ledger_paths([str(ledger_dir)])
    from_file, file_errors = discover_ledger_paths([str(ledger)])

    assert home_errors == [] and dir_errors == [] and file_errors == []
    assert from_home == [ledger]
    assert from_dir == [ledger]
    assert from_file == [ledger]


def test_reproject_reports_missing_input_and_fails_incomplete_gate(tmp_path: Path) -> None:
    """错误输入要有明确错误码；--fail-on-incomplete 只对 incomplete 文件返回 2。"""
    missing = _run_script(str(tmp_path / "does-not-exist"))
    assert missing.returncode == 1
    assert "输入不存在" in missing.stderr

    incomplete = _write_ledger(
        tmp_path / "model_usage" / "unknown.jsonl",
        [_ledger_row("evt-16", backends=["probe"], accounted_input_tokens=500, output_tokens=10)],
    )
    gated = _run_script(str(incomplete), "--fail-on-incomplete")
    assert gated.returncode == 2

    exact = _write_ledger(
        tmp_path / "model_usage" / "exact.jsonl",
        [_ledger_row("evt-17", **REAL_WORLD_ANTHROPIC_SAMPLE)],
    )
    passed = _run_script(str(exact), "--fail-on-incomplete")
    assert passed.returncode == 0


def test_reproject_counts_unparsable_lines_without_crashing(tmp_path: Path) -> None:
    """坏行只计入 unparsable_lines 并把文件降级，不影响其它行重算。"""
    ledger = tmp_path / "model_usage" / "torn.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(_ledger_row("evt-18", **REAL_WORLD_ANTHROPIC_SAMPLE), ensure_ascii=False)
    ledger.write_text(good + "\n{not-json\n", encoding="utf-8")

    projection = project_file(ledger)

    assert projection.unparsable_lines == 1
    assert len(projection.rows) == 1
    assert projection.reprojected_input_tokens == 37913
    assert projection.confidence == CONFIDENCE_PARTIAL
    assert "unparsable_lines=1" in projection.notes


def test_reproject_backend_classification_is_open_world() -> None:
    """后端标签按语义归类：anthropic/openai/responses 可判定，认不出的进 unknown 不猜。"""
    assert classify_backends(("anthropic_compatible",))[0] == ANTHROPIC
    assert classify_backends(("openai_compatible",))[0] == OPENAI_COMPATIBLE
    assert classify_backends(("openai_responses",))[0] == "openai_responses"
    assert classify_backends(("some-future-anthropic-gateway",))[0] == ANTHROPIC
    assert classify_backends(("brand-new-backend",))[0] == "unknown"
    assert classify_backends(("brand-new-backend",))[2] == ("brand-new-backend",)
    assert classify_backends(())[0] == "unknown"
    assert classify_backends(("echo",))[0] == "unknown"
