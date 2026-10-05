"""cache_hit_report 的合成账本验收：多天、多用途、压缩来源、坏行、缺报与估算口径。

账本只读、不写文件；这里用 tmp_path 造两种真实布局（workspace 形态与直连形态）与坏行。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts.cache_hit_report import (
    _Bucket,
    _Dimensions,
    _ReportBuilder,
    _split_before_after,
    main,
)


def _ts(year: int, month: int, day: int, hour: int = 12) -> float:
    """本地时区的固定时刻，保证按天分组可预期。"""
    return datetime(year, month, day, hour).timestamp()


def _usage(provider: dict | None = None, estimated: dict | None = None) -> dict:
    return {
        "provider": {
            "call_count": 0, "input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0, "output_tokens": 0,
            **(provider or {}),
        },
        "estimated": {
            "call_count": 0, "unfinished_call_count": 0,
            "input_tokens": 0, "output_tokens": 0,
            **(estimated or {}),
        },
    }


def _event(event_id: str, *, created_at: float, **fields: object) -> dict:
    """可选字段走 **fields：保持 helper 参数不超过守卫阈值，调用点读起来仍是具名字段。"""
    source = str(fields.get("source") or "chat")
    purpose = str(fields.get("purpose") or "main")
    models = tuple(fields.get("models") or ("deepseek-v4",))
    model_calls: dict = {"models": list(models)}
    usage = _usage(fields.get("provider"), fields.get("estimated"))
    if bool(fields.get("root_only")):
        model_calls["usage_breakdown"] = usage
    else:
        model_calls["purpose_breakdown"] = {
            "schema": "model_call_purpose_breakdown.v1",
            purpose: {"usage_breakdown": usage},
        }
    return {
        "schema_version": "thread_model_usage_event.v1",
        "event_id": event_id,
        "thread_id": f"thread-{event_id}",
        "request_id": f"req-{event_id}",
        "run_id": "",
        "task_id": "",
        "source": source,
        "created_at": created_at,
        "model_calls": model_calls,
    }


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture()
def owner_home(tmp_path: Path) -> Path:
    """两种真实布局 + 坏行 + 空行的合成 owner home。"""
    owner = tmp_path / "owner"
    workspace_usage = (
        owner / "workspace" / "runtime" / "workspaces" / "demo-abcd"
        / "conversations" / "model_usage"
    )
    workspace_usage.mkdir(parents=True)
    rows = [
        _event("e1", created_at=_ts(2026, 10, 3), provider={
            "call_count": 2, "input_tokens": 1000, "cache_read_input_tokens": 800,
            "cache_write_input_tokens": 500, "output_tokens": 50,
        }),
        _event("e2", created_at=_ts(2026, 10, 4, 9), provider={
            "call_count": 3, "input_tokens": 2000, "cache_read_input_tokens": 1000, "output_tokens": 80,
        }, estimated={"call_count": 1, "unfinished_call_count": 1}),
        _event("e3", created_at=_ts(2026, 10, 4, 10), source="conversation_compact",
               purpose="auxiliary", provider={
                   "call_count": 1, "input_tokens": 500, "cache_read_input_tokens": 450, "output_tokens": 10,
               }),
    ]
    _write_rows(workspace_usage / "t1.jsonl", rows)
    with (workspace_usage / "t1.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{broken json\n")
        handle.write(json.dumps({"model_calls": {}}) + "\n")  # 缺 event_id
        handle.write("\n")

    direct_usage = owner / "conversations" / "model_usage"
    direct_usage.mkdir(parents=True)
    _write_rows(direct_usage / "t2.jsonl", [
        _event("e4", created_at=_ts(2026, 10, 4, 11), provider={
            "call_count": 1, "input_tokens": 100, "cache_read_input_tokens": 0, "output_tokens": 5,
        }),
        _event("e5", created_at=_ts(2026, 10, 4, 12), root_only=True, provider={
            "call_count": 1, "input_tokens": 60, "cache_read_input_tokens": 30, "output_tokens": 3,
        }),
    ])
    return owner


def _run_json(capsys, owner: Path, *extra: str) -> dict:
    assert main(["--owner-home", str(owner), "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def _row(payload: dict, **match: str) -> dict:
    for row in payload["rows"]:
        if all(row.get(key) == value for key, value in match.items()):
            return row
    raise AssertionError(f"没有匹配 {match} 的行: {payload['rows']}")


def test_report_groups_by_day_and_purpose_with_hit_rate(capsys, owner_home: Path) -> None:
    payload = _run_json(capsys, owner_home)
    assert payload["schema"] == "cache_hit_report.v1"

    day3 = _row(payload, 日期="2026-10-03", 用途="main")
    assert day3["call_count"] == 2
    assert day3["input_tokens"] == 1000
    assert day3["cache_read_input_tokens"] == 800
    assert day3["hit_rate"] == pytest.approx(0.8)  # cache_write=500 不算命中
    assert day3["output_tokens"] == 50

    day4_main = _row(payload, 日期="2026-10-04", 用途="main")
    assert day4_main["call_count"] == 4  # e2 + e4
    assert day4_main["input_tokens"] == 2100
    assert day4_main["cache_read_input_tokens"] == 1000
    assert day4_main["hit_rate"] == pytest.approx(1000 / 2100)
    assert day4_main["missed_input_tokens"] == 1100

    compact = _row(payload, 日期="2026-10-04", 用途="auxiliary")
    assert compact["call_count"] == 1 and compact["hit_rate"] == pytest.approx(0.9)


def test_estimated_counts_are_not_mixed_with_provider(capsys, owner_home: Path) -> None:
    payload = _run_json(capsys, owner_home)
    day4_main = _row(payload, 日期="2026-10-04", 用途="main")
    # provider 调用数只算供应商回报的 3（e2）+ 1（e4），估算的 1 单列在缺报里。
    assert day4_main["call_count"] == 4
    assert day4_main["estimated_calls"] == 1
    assert day4_main["estimated_unfinished_calls"] == 1
    assert payload["totals"]["estimated_calls"] == 1


def test_bad_lines_and_missing_event_id_are_counted(capsys, owner_home: Path) -> None:
    payload = _run_json(capsys, owner_home)
    assert payload["scan"]["bad_lines"] == 2  # 坏 JSON + 缺 event_id
    assert payload["scan"]["files"] == 2


def test_window_filters_by_day(capsys, owner_home: Path) -> None:
    payload = _run_json(capsys, owner_home, "--since", "2026-10-04")
    assert all(row["日期"] != "2026-10-03" for row in payload["rows"])
    assert payload["scan"]["skipped_window"] >= 1


def test_compare_splits_before_and_after(capsys, owner_home: Path) -> None:
    payload = _run_json(capsys, owner_home, "--compare", "2026-10-04")
    main_group = next(item for item in payload["compare"]["groups"] if item["用途"] == "main")
    assert main_group["before"]["call_count"] == 2
    assert main_group["before"]["input_tokens"] == 1000
    assert main_group["after"]["call_count"] == 4
    assert main_group["after"]["input_tokens"] == 2100
    assert main_group["after"]["missed_input_tokens"] == 1100
    assert main_group["before"]["hit_rate"] == pytest.approx(0.8)


def test_root_level_fallback_without_purpose_breakdown(capsys, owner_home: Path) -> None:
    payload = _run_json(capsys, owner_home)
    fallback = _row(payload, 日期="2026-10-04", 用途="(未分区)")
    assert fallback["call_count"] == 1 and fallback["input_tokens"] == 60


def test_by_source_keeps_compact_row_separate(capsys, owner_home: Path) -> None:
    payload = _run_json(capsys, owner_home, "--by-source")
    compact = _row(payload, 来源="conversation_compact")
    assert compact["用途"] == "auxiliary" and compact["call_count"] == 1
    chat_main = _row(payload, 日期="2026-10-04", 用途="main", 来源="chat")
    assert chat_main["call_count"] == 4


def test_table_output_uses_chinese_headers(capsys, owner_home: Path) -> None:
    assert main(["--owner-home", str(owner_home)]) == 0
    out = capsys.readouterr().out
    assert "调用数" in out and "命中率" in out and "缺报调用" in out
    assert "合计" in out and "2026-10-04" in out


def test_missing_owner_home_returns_error(capsys, tmp_path: Path) -> None:
    assert main(["--owner-home", str(tmp_path / "nope")]) == 2
    assert "不存在" in capsys.readouterr().err


def test_bucket_hit_rate_is_none_without_input() -> None:
    assert _Bucket().hit_rate is None
    bucket = _Bucket(call_count=1, input_tokens=100, cache_read_input_tokens=100)
    assert bucket.hit_rate == pytest.approx(1.0)
    assert bucket.missed_input_tokens == 0


def test_builder_and_split_keep_purpose_dimension() -> None:
    builder = _ReportBuilder(since="", until="", dimensions=_Dimensions(by_source=True))
    builder.add_record(_event("s1", created_at=_ts(2026, 10, 4), provider={
        "call_count": 1, "input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 1,
    }))
    before, after = _split_before_after(builder.groups, "2026-10-04")
    assert before == {}
    key = ("main", "chat")
    assert after[key].call_count == 1 and after[key].input_tokens == 10
