#!/usr/bin/env python3
"""Curator 四类故障路径隔离取证: 用隔离 MY_AGENT_HOME 收集真实失败落账证据。

角色: 第 2 项(Curator 故障路径)的隔离取证器。每场景在全新临时 home 构造真实
Curator 依赖链(真实 ConversationStore/state_store/daily_store/CandidateService/
run_log, 与生产组装一致), 只替换模型后端为可控故障注入 stub, 注入四类故障并
观察 run() 的返回结果、state.json 落账与 runs JSONL 落账。

场景与注入方式:
  1. schema 损坏: 先成功 run 生成 state.json, 再损坏其 JSON, 观察后续 run 行为
  2. 模型乱码: 后端返回非 JSON 文本 -> parse 失败
  3. provider 超时: 后端挂起超过 timeout_seconds -> CuratorModelTimeoutError
  4. commit 失败: candidates.jsonl 目标被目录占位 -> 提交写入失败

约束:
  - 只操作脚本自建的临时 home(mkdtemp), 绝不触碰生产 home
  - 模型故障注入是确定性手段; 成功链(第 4 项)另行用真实 LLM 验证
  - 输出脱敏 JSON: 只含 status/failure_code/run_id 等结构化字段, 不含正文
  - 每场景独立 home, 互不污染
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[3]  # 仓库根(含 agent_py_agent/ 包)
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from agent_py_agent.agent.backends import ModelResponse  # noqa: E402
from agent_py_agent.agent.conversation.store import ConversationStore  # noqa: E402
from agent_py_agent.agent.memory_store.candidates import CandidateService  # noqa: E402
from agent_py_agent.agent.memory_store.curator import (  # noqa: E402
    MemoryCuratorConfig,
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_models import (  # noqa: E402
    CURATOR_OUTPUT_SCHEMA_VERSION,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog  # noqa: E402
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore  # noqa: E402
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore  # noqa: E402

VALID_EXTRACTION = json.dumps(
    {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [],
        "candidates": [],
        "processed_message_refs": [],
        "processed_audit_refs": [],
        "unresolved_refs": [],
        "warnings": [],
        "next_cursor": {"per_thread_cursors": [], "last_audit_event_id": None},
    },
    ensure_ascii=False,
)


class _FaultBackend:
    """可控故障注入后端: ok 返回合法提取, garbage 返回非 JSON, hang 挂起。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.name = "fault-inject"
        self.calls = 0

    def generate_structured(self, prompt: str, response_schema: dict | None = None) -> ModelResponse:
        self.calls += 1
        if self.mode == "garbage":
            return ModelResponse(text="这绝不是JSON{definitely-not-json", backend=self.name)
        if self.mode == "hang":
            time.sleep(120)  # 远超 timeout_seconds=2, 由 call_backend_with_timeout 掐断
        return ModelResponse(text=VALID_EXTRACTION, backend=self.name)


def _conversation_with_message(home: Path) -> None:
    """在隔离 home 造一条真实用户消息, 让 batch 非空。"""
    store = ConversationStore(home / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "我的个人电脑使用 macOS。",
            "channel": "internal",
            "metadata": {
                "session_id": "session-1",
                "request_id": "request-1",
                "task_id": "task-1",
                "run_id": "run-1",
            },
            "now": 11.0,
        }
    )


def _make_service(home: Path, backend: _FaultBackend) -> MemoryCuratorService:
    config = MemoryCuratorConfig(
        interval_seconds=60,
        turn_threshold=1,
        timeout_seconds=2,
        max_retries=0,
    )
    return MemoryCuratorService(
        config=config,
        dependencies=MemoryCuratorDependencies(
            backend=backend,
            conversation_store=ConversationStore(home / "conversations"),
            audit_dir=home / "audit",
            state_store=MemoryCuratorStateStore(home / "memory" / "curator" / "state.json"),
            daily_store=DailyMemoryStore(home / "memory" / "daily"),
            candidate_service=CandidateService(home / "memory" / "candidates.jsonl"),
            run_log=CuratorRunLog(home / "memory" / "curator" / "runs"),
            identity=MemoryCuratorIdentity(
                provider="fault-inject",
                model="curator-evidence",
                owner_id="evidence/local",
            ),
        ),
    )


def _run_record(home: Path) -> dict[str, object] | None:
    """读 runs JSONL 最后一条的结构化摘要(不含正文)。"""
    runs_dir = home / "memory" / "curator" / "runs"
    if not runs_dir.exists():
        return None
    files = sorted(runs_dir.glob("*.jsonl"))
    if not files:
        return None
    rows = []
    for line in files[-1].read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        return None
    last = rows[-1]
    return {
        "run_id": last.get("run_id", ""),
        "status": last.get("status", ""),
        "reason": last.get("reason", ""),
        "failure_code": last.get("failure_code", ""),
        "provider": last.get("provider", ""),
        "model": last.get("model", ""),
    }


def _state_summary(home: Path) -> dict[str, object]:
    path = home / "memory" / "curator" / "state.json"
    if not path.exists():
        return {"present": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"present": True, "parse_error": True}
    return {
        "present": True,
        "last_failure_code": state.get("last_failure_code", ""),
        "lease_id": state.get("lease_id", ""),
    }


def _run_scenario(
    name: str,
    *,
    backend_mode: str,
    with_message: bool,
    damage_state: bool = False,
    block_candidates: bool = False,
) -> dict[str, object]:
    """执行一个故障场景并收集结构化证据。"""
    home = Path(tempfile.mkdtemp(prefix=f"curator-evidence-{name}-"))
    evidence: dict[str, object] = {"scenario": name, "home": str(home)}
    try:
        if with_message:
            _conversation_with_message(home)
        if block_candidates:
            # candidates.jsonl 目标用目录占位 -> 提交写入 IsADirectoryError
            (home / "memory").mkdir(parents=True, exist_ok=True)
            (home / "memory" / "candidates.jsonl").mkdir()
        service = _make_service(home, _FaultBackend(backend_mode))
        if damage_state:
            # 先成功 run 生成真实 state.json, 再写坏
            first = service.run(reason="admin", force=True)
            evidence["first_run"] = {
                "status": first.status,
                "failure_code": first.failure_code,
            }
            state_path = home / "memory" / "curator" / "state.json"
            state_path.write_text("{损坏的JSON", encoding="utf-8")
        try:
            result = service.run(reason="admin", force=True)
            evidence["returned"] = {
                "status": result.status,
                "reason": result.reason,
                "failure_code": result.failure_code,
                "run_id": result.run_id,
            }
        except BaseException as exc:  # noqa: BLE001 - 取证要如实记录"冒泡"本身
            evidence["raised"] = {
                "type": type(exc).__name__,
                "detail": str(exc)[:200],
            }
        evidence["run_log"] = _run_record(home)
        evidence["state"] = _state_summary(home)
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)
    return evidence


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="curator_failure_evidence",
        description="Curator 四类故障路径隔离取证(不裁决, 只收集结构化落账证据)。",
    )
    parser.add_argument("--output", required=True, help="证据 JSON 输出路径")
    args = parser.parse_args(argv)

    results = [
        _run_scenario("schema_damaged", backend_mode="ok", with_message=True, damage_state=True),
        _run_scenario("model_garbage", backend_mode="garbage", with_message=True),
        _run_scenario("provider_timeout", backend_mode="hang", with_message=True),
        _run_scenario("commit_failed", backend_mode="ok", with_message=False, block_candidates=True),
    ]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "curator-failure-evidence.v1",
        "note": "故障注入取证; 模型故障注入为确定性手段, 成功链另行真实 LLM 验证",
        "scenarios": results,
    }
    with output.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"curator_failure_evidence: 证据已写入 {output}")
    for item in results:
        print(f"  [{item['scenario']}] returned={item.get('returned')} raised={item.get('raised')} "
              f"run_log={item.get('run_log', {}).get('failure_code')} "
              f"state_failure={item.get('state', {}).get('last_failure_code')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
