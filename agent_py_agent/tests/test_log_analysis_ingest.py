from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.log_analysis.ingest.pipeline import ingest_file
from agent_py_agent.agent.log_analysis.tools import security_query
from agent_py_agent.agent.log_analysis.tools.query_functions import SecurityQueryParams


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_jsonl(path: str | Path) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_manifest_and_checkpoint(result) -> None:
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["counts"]["stored"] == 3
    assert manifest["dedup_policy"] == "source_event_fingerprint"
    assert manifest["checkpoint_policy"] == "after_durable_write"

    checkpoint = json.loads(Path(result.checkpoint_path).read_text(encoding="utf-8"))
    assert checkpoint["last_committed_batch_id"] == result.batch_id
    assert checkpoint["cursor"]["content_hash"] == result.content_hash


def _assert_waf_event(events: list[dict]) -> None:
    waf_event = next(event for event in events if event["source_product"] == "waf")
    assert waf_event["alert_type"] == "Web攻击"
    assert waf_event["threat_name"] == "疑似命令执行"
    assert waf_event["raw_fields"]["告警类型"] == "Web攻击"
    assert waf_event["payload_truncated"] is True
    assert waf_event["payload_sha256"].startswith("sha256:")
    assert len(waf_event["payload"]) == 24
    assert waf_event["dedup_key"].startswith("sha256:")


def test_security_alert_v1_jsonl_ingest_manifest_checkpoint_and_dedup():
    fixture = _project_root() / "validation" / "security_fixtures" / "security_alert_v1.jsonl"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        result = ingest_file(
            fixture,
            root=root,
            source_id="security-fixture",
            payload_max_chars=24,
        )

        assert result.parsed_count == 3
        assert result.stored_count == 3
        assert result.duplicate_count == 0
        assert result.dead_letter_count == 0
        assert Path(result.manifest_path).exists()
        assert Path(result.checkpoint_path).exists()

        _assert_manifest_and_checkpoint(result)

        events = _read_jsonl(result.events_path)
        assert len(events) == 3
        _assert_waf_event(events)

        duplicate = ingest_file(
            fixture,
            root=root,
            source_id="security-fixture",
            payload_max_chars=24,
        )
        assert duplicate.stored_count == 0
        assert duplicate.duplicate_count == 3
        assert len(_read_jsonl(result.events_path)) == 3


def test_ingest_file_default_store_can_be_queried_end_to_end():
    fixture = _project_root() / "validation" / "security_fixtures" / "security_alert_v1.jsonl"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        result = ingest_file(fixture, root=root, source_id="query-fixture")

        response = security_query(
            SecurityQueryParams(
                root=root,
                start_time="2026-04-30T00:00:00Z",
                end_time="2026-04-30T23:59:59Z",
                limit=10,
            )
        )

        assert result.events_path.replace("\\", "/") == str(root / "events.jsonl").replace("\\", "/")
        assert response["row_count"] == result.stored_count == 3
        assert response["truncated"] is False
        assert len(response["preview_rows"]) == 3


def test_security_alert_v1_csv_ingest_maps_three_security_sources():
    fixture = _project_root() / "validation" / "security_fixtures" / "security_alert_v1.csv"

    with tempfile.TemporaryDirectory() as td:
        result = ingest_file(fixture, root=Path(td), source_id="csv-fixture")

        assert result.parsed_count == 3
        assert result.stored_count == 3
        assert result.dead_letter_count == 0

        events = _read_jsonl(result.events_path)
        assert {event["source_product"] for event in events} == {"waf", "edr", "vpn"}
        assert {event["user"] for event in events} == {"alice"}
        assert all(event["raw_fields"] for event in events)


def test_security_alert_v1_dead_letter_does_not_block_batch():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        bad_jsonl = root / "mixed.jsonl"
        bad_jsonl.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "告警ID": "good-1",
                            "事件时间": "2026-04-30T10:00:00Z",
                            "数据源": "waf-prod",
                            "告警类型": "Web攻击",
                            "攻击IP": "198.51.100.23",
                            "受害IP": "10.10.5.20",
                        },
                        ensure_ascii=False,
                    ),
                    "not-json",
                    "[1, 2, 3]",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        result = ingest_file(bad_jsonl, root=root / "out", source_id="mixed-source")

        assert result.parsed_count == 1
        assert result.stored_count == 1
        assert result.dead_letter_count == 2
        assert result.skipped_count == 1
        assert Path(result.checkpoint_path).exists()

        dead_letter_path = Path(result.dead_letter_refs[0]["path"])
        dead_letters = _read_jsonl(dead_letter_path)
        assert len(dead_letters) == 2
        assert dead_letters[0]["reason"].startswith("invalid JSON")
        assert dead_letters[1]["reason"] == "JSONL SecurityAlertV1 line must contain a JSON object"


def test_security_alert_v1_csv_bad_row_goes_to_dead_letter():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        csv_path = root / "bad.csv"
        csv_path.write_text(
            "告警ID,告警类型,攻击IP\n"
            "ok-1,Web攻击,198.51.100.23\n"
            "bad-1,Web攻击,198.51.100.23,extra-column\n",
            encoding="utf-8",
        )

        result = ingest_file(csv_path, root=root / "out", source_id="bad-csv")

        assert result.parsed_count == 1
        assert result.stored_count == 1
        assert result.dead_letter_count == 1
        dead_letters = _read_jsonl(result.dead_letter_refs[0]["path"])
        assert dead_letters[0]["reason"] == "CSV row has more columns than the header"
