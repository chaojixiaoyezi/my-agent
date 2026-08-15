"""fleet_score 报告面口径钉子:只扫 output/ 交付 + 会话,排除 work/ 候选原料。

回归真机实测的 T6 缺陷:摄取层 watch_stream 把【初筛候选批】(带原始事件 ID)落进
tasks/<req>/work/blobs/tool_outputs,旧 fleet_score 递归扫 .txt 把这些当"上报"→误报
从 1 虚高到 89。修复后只认交付面(/output/ 子树)+ 会话消息。

跑:python3 -m pytest scripts/watch_harness/test_fleet_score_surface.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent / "fleet_score.py"


def _load():
    spec = importlib.util.spec_from_file_location("fleet_score", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet_score = _load()


def test_delivery_output_is_report_surface():
    # 交付面:即便嵌了 output/work/child_outputs(已整合进交付)也保留。
    assert fleet_score._is_process_surface(Path("owner/tasks/2026/req_1/output/report.md")) is False
    assert fleet_score._is_process_surface(Path("owner/tasks/2026/req_1/output/work/child_outputs/01.md")) is False


def test_conversation_messages_are_report_surface():
    assert fleet_score._is_process_surface(Path("owner/conversations/messages/thread-abc.jsonl")) is False


def test_work_sibling_is_process_surface():
    # work/ 兄弟目录 = 过程原料(子代理未整合的 child_outputs、候选批 blob、timeline)。
    assert fleet_score._is_process_surface(Path("owner/tasks/2026/req_1/work/child_outputs/02.md")) is True
    assert fleet_score._is_process_surface(Path("owner/tasks/2026/req_1/work/blobs/tool_outputs/watch_stream-x.txt")) is True
    assert fleet_score._is_process_surface(Path("owner/tasks/2026/req_1/work/timeline.jsonl")) is True


def test_audit_ndjson_excluded_even_outside_work():
    assert fleet_score._is_process_surface(Path("owner/watch_state/ws-abc.audit.ndjson")) is True


def test_end_to_end_excludes_candidate_material(tmp_path):
    # 造一个含"交付真命中"+"work 里候选原料(带真假事件 ID)"的任务树,验证只算交付面。
    answer = tmp_path / "key.jsonl"
    answer.write_text('{"event_id":"EVT-A-024231","source":"auth_log","emitted_at":1000.0}\n', encoding="utf-8")

    task = tmp_path / "owner/tasks/2026-07-02/req_1"
    (task / "output/work/child_outputs").mkdir(parents=True)
    (task / "work/blobs/tool_outputs").mkdir(parents=True)
    # 交付面:整合后的报告,上报了真命中。
    (task / "output/work/child_outputs/01.md").write_text(
        "确认命中 EVT-A-024231(session.established=true,结果端证实)。", encoding="utf-8"
    )
    # 过程面:候选批原料,含大量迷惑项原始 ID——绝不能计入误报。
    (task / "work/blobs/tool_outputs/watch_stream-x.txt").write_text(
        "candidates: EVT-A-000000 EVT-A-000001 EVT-B-000002 EVT-A-024231", encoding="utf-8"
    )

    files = fleet_score._iter_files([str(tmp_path / "owner")])
    excluded = fleet_score._count_excluded([str(tmp_path / "owner")])
    found = fleet_score._scan_reports(files)
    assert "EVT-A-024231" in found
    assert "EVT-A-000000" not in found  # 候选原料被排除
    assert excluded >= 1
