from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path
from typing import Any

from agent_py_agent.agent.contracts.staged_checkpoint_acceptance import staged_checkpoint_findings


class _FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _api_payloads() -> dict[str, dict[str, object]]:
    return {
        "https://api.example.test/week1": {
            "items": [
                {
                    "full_name": "org/alpha",
                    "html_url": "https://github.com/org/alpha",
                    "stargazers_count": 120,
                    "description": "Alpha project",
                    "language": "Python",
                },
                {
                    "full_name": "org/beta",
                    "html_url": "https://github.com/org/beta",
                    "stargazers_count": 90,
                    "description": "Beta project",
                    "language": "Go",
                },
            ]
        },
        "https://api.example.test/week2": {
            "items": [
                {
                    "full_name": "org/gamma",
                    "html_url": "https://github.com/org/gamma",
                    "stargazers_count": 180,
                    "description": "Gamma project",
                    "language": "Rust",
                },
                {
                    "full_name": "org/delta",
                    "html_url": "https://github.com/org/delta",
                    "stargazers_count": 140,
                    "description": "Delta project",
                    "language": "TypeScript",
                },
            ]
        },
    }


def _install_fake_urlopen(monkeypatch) -> None:
    payloads = _api_payloads()

    def fake_urlopen(request: Any, timeout: int):
        return _FakeResponse(payloads[request.full_url])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def _collection_params() -> dict[str, object]:
    return {
        "path": "outputs/source_data.json",
        "requests": [
            {"name": "2026-W01", "source_id": "src-w01", "url": "https://api.example.test/week1"},
            {"name": "2026-W02", "source_id": "src-w02", "url": "https://api.example.test/week2"},
        ],
        "item_path": "items",
        "limit_per_request": 2,
        "columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
        "fields": {
            "项目名": "full_name",
            "地址": "html_url",
            "上升 star 数": "stargazers_count",
            "中文解释": "description",
            "推荐理由": {"template": "stars={stargazers_count}; language={language}"},
        },
        "evidence_fields": ["项目名", "地址", "上升 star 数"],
        "completion_evidence": {"scope": "fixture", "method": "api_json_collection"},
    }


def _validation_contract() -> dict[str, object]:
    return {
        "required_sheets_min": 2,
        "required_columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
        "evidence_contract": {
            "required_fields": ["项目名", "地址", "上升 star 数"],
            "require_verified": True,
        },
        "collection_contract": {
            "source_json_ref": "outputs/source_data.json",
            "groups_path": "sheets",
            "items_path": "rows",
            "min_groups": 2,
            "min_items_per_group": 2,
            "required_item_fields": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
            "require_completion_evidence": True,
            "completion_evidence_path": "completion_evidence",
        },
        "staging_contract": {"source_json_ref": "outputs/source_data.json"},
    }


def _write_fetch_url_artifact(root: Path, ref: str, payload: dict[str, object]) -> None:
    artifact = root / ref
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            {
                "content": "status=200\ncontent_type=application/json\n\n" + json.dumps(payload, ensure_ascii=False),
                "kind": "tool_output",
                "tool": "fetch_url",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _source_artifact_collection_params(ref: str) -> dict[str, object]:
    params = _collection_params()
    params.pop("requests")
    params["source_artifacts"] = [{"artifact_ref": ref, "name": "2026-W01", "source_id": "artifact-w01"}]
    return params


def _install_range_urlopen(monkeypatch) -> None:
    def fake_urlopen(request: Any, timeout: int):
        week = request.full_url.rsplit("week=", 1)[-1]
        return _FakeResponse(
            {
                "items": [
                    {
                        "description": None,
                        "full_name": f"org/project-{week}-a",
                        "html_url": f"https://github.com/org/project-{week}-a",
                        "language": "Python",
                        "stargazers_count": int(week) * 10,
                    },
                    {
                        "description": f"Project {week} B",
                        "full_name": f"org/project-{week}-b",
                        "html_url": f"https://github.com/org/project-{week}-b",
                        "language": "Go",
                        "stargazers_count": int(week) * 20,
                    },
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def _range_collection_params() -> dict[str, object]:
    params = _collection_params()
    params.pop("requests")
    params["request_ranges"] = [
        {
            "end_date": "2026-01-21",
            "name_template": "{YYYY}-W{ww}",
            "source_id_template": "src-{YYYY}{ww}",
            "start_date": "2026-01-01",
            "step_days": 7,
            "url_template": "https://api.example.test/range?from={start_date}&to={end_date}&week={index}",
        }
    ]
    params["fields"]["中文解释"] = {"path": "description", "default_template": "Repository {full_name} uses {language}"}
    return params


def test_api_json_collection_writes_sourced_grouped_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    _install_fake_urlopen(monkeypatch)
    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(_collection_params())

    assert result.ok is True
    checkpoint = json.loads((tmp_path / "outputs/source_data.json").read_text(encoding="utf-8"))
    assert len(checkpoint["sheets"]) == 2
    assert checkpoint["sheets"][0]["rows"][0]["field_source_ids"]["项目名"] == ["src-w01"]
    assert len(checkpoint["claims"]) == 12

    findings = staged_checkpoint_findings(
        [{"preferred_path": "unused.xlsx", "validation_contract": _validation_contract()}],
        tmp_path,
    )
    assert findings == []


# LLM: Archived JSON artifacts should be first-class collection sources, not prose copied by the model.
# 函数用途: 验证已登记/归档的工具输出 JSON 可被转换为带 source_refs/claims 的 checkpoint。
def test_api_json_collection_builds_checkpoint_from_source_artifacts(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    ref = "memory_archive/artifacts/tool_outputs/fetch_url-1.json"
    _write_fetch_url_artifact(tmp_path, ref, _api_payloads()["https://api.example.test/week1"])

    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(_source_artifact_collection_params(ref))

    assert result.ok is True
    checkpoint = json.loads((tmp_path / "outputs/source_data.json").read_text(encoding="utf-8"))
    assert checkpoint["sheets"][0]["name"] == "2026-W01"
    assert checkpoint["sheets"][0]["rows"][0]["field_source_ids"]["项目名"] == ["artifact-w01"]
    assert checkpoint["source_refs"][0]["artifact_ref"] == ref
    assert checkpoint["source_refs"][0]["source_type"] == "artifact_json"
    assert len(checkpoint["claims"]) == 6

    contract = _validation_contract()
    collection = contract["collection_contract"]
    assert isinstance(collection, dict)
    collection["min_groups"] = 1
    findings = staged_checkpoint_findings(
        [{"preferred_path": "unused.xlsx", "validation_contract": contract}],
        tmp_path,
    )
    assert findings == []


def test_api_json_collection_expands_date_range_and_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    _install_range_urlopen(monkeypatch)
    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(_range_collection_params())

    assert result.ok is True
    checkpoint = json.loads((tmp_path / "outputs/source_data.json").read_text(encoding="utf-8"))
    assert [sheet["name"] for sheet in checkpoint["sheets"]] == ["2026-W01", "2026-W02", "2026-W03"]
    assert checkpoint["sheets"][0]["rows"][0]["中文解释"] == "Repository org/project-1-a uses Python"


def test_api_json_collection_applies_top_level_url_template_to_ranges(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    _install_range_urlopen(monkeypatch)
    params = _range_collection_params()
    ranges = params["request_ranges"]
    assert isinstance(ranges, list)
    params["url_template"] = ranges[0].pop("url_template")

    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(params)

    assert result.ok is True
    checkpoint = json.loads((tmp_path / "outputs/source_data.json").read_text(encoding="utf-8"))
    assert len(checkpoint["sheets"]) == 3


# LLM: Range templates should accept common year aliases without leaking KeyError as UNKNOWN_ERROR.
# 函数用途: 验证模型常用 {year} 与 {YYYY} 等价，避免批量 API 采集因模板别名中断。
def test_api_json_collection_range_template_supports_year_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    _install_range_urlopen(monkeypatch)
    params = _range_collection_params()
    ranges = params["request_ranges"]
    assert isinstance(ranges, list)
    ranges[0]["name_template"] = "{year}-W{index:02d}"
    ranges[0]["source_id_template"] = "src-{year}-{week}"

    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(params)

    assert result.ok is True
    checkpoint = json.loads((tmp_path / "outputs/source_data.json").read_text(encoding="utf-8"))
    assert checkpoint["sheets"][0]["name"] == "2026-W01"
    assert checkpoint["source_refs"][0]["source_id"] == "src-2026-01"


# LLM: Batch API range templates accept both common machine placeholder styles.
# 函数用途: 验证 ${name} 占位写法归一到同一结构化模板逻辑，而不是变成错误 URL。
def test_api_json_collection_range_template_supports_shell_style_placeholders(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    _install_range_urlopen(monkeypatch)
    params = _range_collection_params()
    ranges = params["request_ranges"]
    assert isinstance(ranges, list)
    ranges[0]["name_template"] = "${year}-W${index:02d}"
    ranges[0]["source_id_template"] = "src-${year}-${week}"
    ranges[0]["url_template"] = "https://api.example.test/range?from=${start_date}&to=${end_date}&week=${index}"

    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(params)

    assert result.ok is True
    checkpoint = json.loads((tmp_path / "outputs/source_data.json").read_text(encoding="utf-8"))
    assert checkpoint["sheets"][0]["name"] == "2026-W01"
    assert checkpoint["source_refs"][0]["uri"].endswith("from=2026-01-01&to=2026-01-07&week=1")


def test_api_json_collection_unknown_range_placeholder_is_tool_argument_error(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    params = _range_collection_params()
    ranges = params["request_ranges"]
    assert isinstance(ranges, list)
    ranges[0]["name_template"] = "{missing_key}-W{index:02d}"

    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(params)

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "unknown range template placeholder" in result.output


def test_api_json_collection_rejects_columns_without_field_mapping(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    params = _collection_params()
    params["columns"] = ["项目名", "地址", "缺少映射列"]

    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(params)

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "fields missing mappings" in result.output


def test_api_json_collection_rejects_empty_evidence_field_without_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    _install_range_urlopen(monkeypatch)
    params = _range_collection_params()
    params["fields"]["中文解释"] = {"path": "description"}
    params["evidence_fields"] = ["项目名", "地址", "上升 star 数", "中文解释"]

    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(params)

    assert result.ok is False
    assert result.error_code == "API_JSON_EMPTY_EVIDENCE_FIELD"
    assert "default/default_template" in result.output
    assert not (tmp_path / "outputs/source_data.json").exists()


# LLM: Batch collection owns safe pacing; the model cannot lower it below the generic floor.
# 函数用途: 验证大量请求时系统强制使用最小节流，避免批量 API 任务触发限流后退化为空转。
def test_api_json_collection_enforces_batch_delay_floor(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    calls: list[float] = []

    def fake_urlopen(request: Any, timeout: int):
        return _FakeResponse(
            {
                "items": [
                    {
                        "description": "Project",
                        "full_name": "org/project",
                        "html_url": "https://github.com/org/project",
                        "language": "Python",
                        "stargazers_count": 100,
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda seconds: calls.append(seconds))
    params = _range_collection_params()
    ranges = params["request_ranges"]
    assert isinstance(ranges, list)
    ranges[0]["end_date"] = "2026-03-25"
    params["request_delay_seconds"] = 0
    params["limit_per_request"] = 1

    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(params)

    assert result.ok is True
    assert len(calls) == 11
    assert all(seconds >= 6.5 for seconds in calls)


# LLM: API collection should absorb one bounded rate-limit retry at the tool gate.
# 函数用途: 验证 HTTP 429/403 带 Retry-After 时由采集工具有限重试，而不是把模型推向手写数据。
def test_api_json_collection_retries_rate_limited_http_response(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.tooling.api_json_collection import ApiJsonCollectionTool

    calls = 0
    sleeps: list[float] = []

    def fake_urlopen(request: Any, timeout: int):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                {"Retry-After": "1"},
                None,
            )
        return _FakeResponse(_api_payloads()["https://api.example.test/week1"])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    params = _collection_params()
    params["requests"] = [{"name": "2026-W01", "source_id": "src-w01", "url": "https://api.example.test/week1"}]

    result = ApiJsonCollectionTool(tmp_path, timeout=3).execute(params)

    assert result.ok is True
    assert calls == 2
    assert sleeps == [1.0]
