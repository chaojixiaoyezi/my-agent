"""Read-only end-to-end stage timeline for one gateway request (S9)."""
# 模块用途: 按请求编号只读拼接平台创建、适配器入站、网关受理、排队、owner就绪、会话车道、回合、每轮模型、
#   托管工具与投递时间点，输出单条有序时间线和阶段耗时；任何来源缺失只记入coverage，不猜值。
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path

from ..adapter.ingress import gateway_ingress_id

# 与任务遥测文件名同一白名单：请求编号直接参与文件名，禁止路径分隔与特殊段。
_request_id_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_owner_segment_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,199}$")
_request_states = ("processing", "pending", "done", "failed", "terminal")
# 固定流水线次序；回合内的模型轮次与托管工具共用一档，按各自时间交错。
_stage_rank = {"provider_created": 0, "adapter_received": 1, "gateway_accepted": 2, "dispatched": 3,
               "owner_agent_ready": 4, "lane_wait_started": 5, "lane_acquired": 6, "turn_started": 7,
               "model_call_started": 8, "first_token": 8, "model_call_finished": 8, "tool_claimed": 8,
               "tool_settled": 8, "turn_finished": 9, "delivery_started": 10, "delivery_sent": 11}
_round_ms_fields = ("request_prepare_ms", "pre_call_housekeeping_ms", "supervision_sweep_ms", "context_assembly_ms",
                    "inter_round_gap_ms", "first_token_wait_ms", "duration_ms")


# LLM: 只接受有限正数时间；布尔、字符串、NaN、负数一律视为缺失。
# 函数用途: 把可选时间戳规范为秒级浮点或None。
def _instant(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


# LLM: 只接受有限非负数耗时；其它类型视为缺失，不补零。
# 函数用途: 把可选毫秒耗时规范为一位小数或None。
def _millis(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 1)


# LLM: 渲染本机时区毫秒ISO-8601，仅用于人读；计算一律用原浮点。
# 函数用途: 把Unix秒转成可读时间字符串。
def _iso(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().isoformat(timespec="milliseconds")


# LLM: 读失败、非对象或解析失败都返回None，由调用方记coverage；不跟随异常回显路径。
# 函数用途: 尽力读取一个JSON对象文件。
def _read_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# LLM: 按网关已知状态目录依次找同名请求，返回第一份身份一致的记录；不扫描目录。
# 函数用途: 定位一条网关请求记录及其所在状态。
def find_request_record(gateway_root: Path, request_id: str) -> tuple[dict | None, str]:
    if not _request_id_pattern.match(str(request_id or "")):
        return None, ""
    for state in _request_states:
        record = _read_object(Path(gateway_root) / "requests" / state / f"{request_id}.json")
        if record is not None and str(record.get("id") or record.get("request_id") or "") == request_id:
            return record, state
    return None, ""


# LLM: owner编号只允许相对的安全段路径；含..、绝对路径或异常字符一律拒绝，防止拼出owners目录外路径。
# 函数用途: 由请求记录得出执行owner的家目录。
def request_owner_home(owners_dir: Path, request: dict) -> Path | None:
    dispatch = request.get("host_im_dispatch.v1")
    owner = str((dispatch.get("owner_id") if isinstance(dispatch, dict) else "") or request.get("owner_id") or "").strip()
    segments = owner.split("/") if owner else []
    if not segments or not all(_owner_segment_pattern.match(segment) for segment in segments):
        return None
    return Path(owners_dir).joinpath(*segments)


# LLM: 入站记录名由通道+平台消息编号确定性算出；timestamp是平台创建时间（平台时钟），created_at是适配器落盘时间（本机时钟）。
# 函数用途: 读出一条请求对应的平台创建与适配器接收时间。
def ingress_times(gateway_root: Path, request: dict) -> dict:
    metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
    channel = str(metadata.get("channel") or metadata.get("adapter") or "").strip()
    message_id = str(metadata.get("message_id") or "").strip()
    if not channel or not message_id:
        return {}
    record = _read_object(Path(gateway_root) / "channel-delivery" / "ingress" / "records"
                          / f"{gateway_ingress_id(channel, message_id)}.json")
    if record is None or str(record.get("provider_message_id") or "") != message_id:
        return {}
    times = {"provider_created_at": _instant(record.get("timestamp")), "adapter_received_at": _instant(record.get("created_at"))}
    return {key: value for key, value in times.items() if value is not None}


# LLM: 投递回执名是请求编号的sha256；只认request_id一致的那一份，不扫描sent目录。
# 函数用途: 读出最终回复的投递开始与送达时间。
def delivery_times(gateway_root: Path, request_id: str) -> dict:
    name = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    record = _read_object(Path(gateway_root) / "channel-delivery" / "sent" / f"{name}.json")
    if record is None or str(record.get("request_id") or "") != request_id:
        return {}
    times = {"delivery_started_at": _instant(record.get("dispatch_started_at")),
             "delivery_sent_at": _instant(record.get("sent_at")) or _instant(record.get("dispatch_completed_at"))}
    return {key: value for key, value in times.items() if value is not None}


# LLM: 只读当前活动卷，跳过代次头帧和坏行；不加锁，读到半行即忽略；超卷封存的早期轮次不在此列。
# 函数用途: 返回一条请求的已记录模型轮次行。
def round_rows(owner_home: Path, request_id: str) -> list[dict]:
    path = Path(owner_home) / "data" / "telemetry" / "task_calls" / f"{request_id}.rounds.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in lines:
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        row = frame.get("row") if isinstance(frame, dict) else None
        if isinstance(row, dict) and _instant(row.get("started_at")) is not None:
            rows.append(row)
    return rows


# LLM: 只读打开owner的runtime.db（零等待），按task_id=request_id连到回合与托管工具；打不开只记缺失原因类型。
# 函数用途: 读取请求对应的回合记录和托管工具操作时间。
def runtime_rows(owner_home: Path, request_id: str) -> tuple[list[dict], list[dict], str]:
    import sqlite3

    from ..io.readonly_sqlite import readonly_sqlite_connection

    database = Path(owner_home) / "runtime.db"
    if not database.is_file():
        return [], [], "runtime_db_missing"
    try:
        with readonly_sqlite_connection(database, timeout=0.0, require_private_directory=False) as conn:
            runs = [dict(zip(("status", "created_at", "closed_at"), row)) for row in conn.execute(
                "SELECT status, created_at, closed_at FROM task_runs WHERE task_id=? ORDER BY created_at", (request_id,))]
            operations = [dict(zip(("operation_type", "status", "created_at", "handler_started_at", "settled_at"), row))
                          for row in conn.execute(
                "SELECT op.operation_type, op.status, op.created_at, op.handler_started_at, op.settled_at "
                "FROM task_runs tr JOIN agent_runs ar ON ar.task_run_id = tr.task_run_id "
                "JOIN tool_operations op ON op.agent_run_id = ar.agent_run_id "
                "WHERE tr.task_id=? ORDER BY op.created_at", (request_id,))]
    except (OSError, ValueError, sqlite3.Error) as error:
        return [], [], f"runtime_db_unavailable:{type(error).__name__}"
    return runs, operations, ""


# LLM: 纯函数；先按固定流水线档位、同档按时间排序；偏移以本机时钟的首个点为原点，跨时钟负差原样保留。
# 函数用途: 把时间点列表整理成有序时间线。
def _ordered(points: list[tuple[str, float, dict]]) -> tuple[list[dict], str]:
    ordered = [point for _, point in sorted(enumerate(points), key=lambda item: (_stage_rank[item[1][0]], item[1][1], item[0]))]
    local = [point for point in ordered if point[0] != "provider_created"]
    origin_stage, origin = (local or ordered or [("", 0.0, {})])[0][:2]
    previous = None
    stages = []
    for stage, at, extra in ordered:
        stages.append({"stage": stage, "at": at, "at_iso": _iso(at), "offset_ms": round((at - origin) * 1000.0, 1),
                       "since_previous_ms": None if previous is None else round((at - previous) * 1000.0, 1), **extra})
        previous = at
    return stages, origin_stage


# LLM: 两端都有才计算，缺任一端返回None。
# 函数用途: 计算两个时间点间的毫秒差。
def _span(start: float | None, end: float | None) -> float | None:
    return round((end - start) * 1000.0, 1) if start is not None and end is not None else None


# LLM: 只读，不写任何文件；请求编号先过白名单，任何来源缺失写入coverage，缺失值不补零；
#   平台创建时间来自平台时钟，若早于本机接收出现负差，只报告skew并改用本机原点计算端到端。
# 函数用途: 组装一条请求从平台创建到回复送达的阶段时间线与汇总耗时。
def message_stage_timeline(*, gateway_root: Path, request_id: str, owners_dir: Path | None = None,
                           owner_home: Path | None = None) -> dict:
    request_id = str(request_id or "").strip()
    if not _request_id_pattern.match(request_id):
        return {"request_id": "", "ok": False, "error_code": "REQUEST_ID_INVALID"}
    request, state = find_request_record(Path(gateway_root), request_id)
    if request is None:
        return {"request_id": request_id, "ok": False, "error_code": "REQUEST_NOT_FOUND"}
    coverage: dict[str, str] = {}
    timing = request.get("request_stage_timing") if isinstance(request.get("request_stage_timing"), dict) else {}
    ingress = ingress_times(Path(gateway_root), request)
    for key in ("provider_created_at", "adapter_received_at"):
        # 入站记录已清理时，用派发时抄进请求记录的同源时间。
        if key not in ingress and _instant(timing.get(key)) is not None:
            ingress[key] = _instant(timing.get(key))
    if not ingress:
        coverage["ingress"] = "missing"
    delivery = delivery_times(Path(gateway_root), request_id)
    if not delivery:
        coverage["delivery"] = "missing"
    home = owner_home or (request_owner_home(Path(owners_dir), request) if owners_dir is not None else None)
    rows: list[dict] = []
    runs: list[dict] = []
    operations: list[dict] = []
    if home is None:
        coverage["owner"] = "unresolved"
    else:
        rows = round_rows(home, request_id)
        if not rows:
            coverage["rounds"] = "missing"
        runs, operations, runtime_error = runtime_rows(home, request_id)
        if runtime_error:
            coverage["runtime_db"] = runtime_error
        elif not runs:
            coverage["task_run"] = "missing"
    points: list[tuple[str, float, dict]] = []
    for stage, key in (("provider_created", "provider_created_at"), ("adapter_received", "adapter_received_at")):
        if key in ingress:
            points.append((stage, ingress[key], {}))
    accepted = _instant(timing.get("accepted_at")) or _instant(request.get("submitted_at")) or _instant(request.get("created_at"))
    if accepted is not None:
        points.append(("gateway_accepted", accepted, {}))
    for stage, key in (("dispatched", "dispatched_at"), ("owner_agent_ready", "owner_agent_ready_at"),
                       ("lane_wait_started", "lock_wait_started_at"), ("lane_acquired", "lock_acquired_at")):
        at = _instant(timing.get(key))
        if at is not None:
            points.append((stage, at, {}))
    turn_started = _instant(runs[0].get("created_at")) if runs else None
    if turn_started is not None:
        points.append(("turn_started", turn_started, {}))
    model_ms = assembly_ms = 0.0
    for index, row in enumerate(rows, 1):
        started = _instant(row.get("started_at"))
        extra: dict = {"round": index, "model": str(row.get("model") or "")}
        extra.update({key: value for key in _round_ms_fields if (value := _millis(row.get(key))) is not None})
        stages_ms = row.get("stages_ms") if isinstance(row.get("stages_ms"), dict) else {}
        sub = {str(key): value for key, raw in stages_ms.items() if (value := _millis(raw)) is not None}
        if sub:
            extra["stages_ms"] = sub
        points.append(("model_call_started", started, extra))
        if "first_token_wait_ms" in extra:
            points.append(("first_token", started + extra["first_token_wait_ms"] / 1000.0, {"round": index}))
        if "duration_ms" in extra:
            model_ms += extra["duration_ms"]
            points.append(("model_call_finished", started + extra["duration_ms"] / 1000.0, {"round": index}))
        assembly_ms += extra.get("context_assembly_ms", 0.0)
    tool_seconds = 0.0
    for operation in operations:
        created, settled = _instant(operation.get("created_at")), _instant(operation.get("settled_at"))
        extra = {"tool": str(operation.get("operation_type") or ""), "status": str(operation.get("status") or "")}
        if created is not None:
            points.append(("tool_claimed", created, extra))
        if settled is not None:
            points.append(("tool_settled", settled, dict(extra)))
            if created is not None:
                tool_seconds += max(0.0, settled - created)
    turn_finished = (_instant(runs[-1].get("closed_at")) if runs else None) or _instant(request.get("completed_at"))
    if turn_finished is not None:
        points.append(("turn_finished", turn_finished, {}))
    for stage, key in (("delivery_started", "delivery_started_at"), ("delivery_sent", "delivery_sent_at")):
        if key in delivery:
            points.append((stage, delivery[key], {}))
    provider_to_adapter = _span(ingress.get("provider_created_at"), ingress.get("adapter_received_at"))
    warnings = ["provider_clock_ahead_of_host"] if provider_to_adapter is not None and provider_to_adapter < 0 else []
    origin_key = ("provider_created_at" if provider_to_adapter is not None and provider_to_adapter >= 0
                  else "adapter_received_at" if "adapter_received_at" in ingress else "")
    start = ingress.get(origin_key) if origin_key else accepted
    first_model = _instant(rows[0].get("started_at")) if rows else None
    summary = {
        "provider_to_adapter_ms": provider_to_adapter,
        "adapter_to_gateway_ms": _span(ingress.get("adapter_received_at"), accepted),
        "queue_wait_ms": _millis(timing.get("queue_wait_ms")), "agent_init_ms": _millis(timing.get("agent_init_ms")),
        "lane_wait_ms": _millis(timing.get("lock_wait_ms")),
        "accepted_to_turn_start_ms": _span(accepted, turn_started),
        "turn_start_to_first_model_ms": _span(turn_started, first_model),
        "model_rounds": len(rows), "model_total_ms": round(model_ms, 1), "context_assembly_total_ms": round(assembly_ms, 1),
        "managed_tool_count": len(operations), "managed_tool_total_ms": round(tool_seconds * 1000.0, 1),
        "turn_ms": _span(turn_started, turn_finished),
        "finish_to_delivery_ms": _span(turn_finished, delivery.get("delivery_sent_at")),
        "end_to_end_ms": _span(start, delivery.get("delivery_sent_at") or turn_finished),
        "end_to_end_origin": (origin_key[:-3] if origin_key else "gateway_accepted") if start is not None else None,
    }
    stages, origin_stage = _ordered(points)
    return {"request_id": request_id, "ok": True, "request_state": state, "origin_stage": origin_stage,
            "summary": {key: value for key, value in summary.items() if value is not None},
            "stages": stages, "coverage": coverage, "warnings": warnings}


__all__ = ["delivery_times", "find_request_record", "ingress_times", "message_stage_timeline",
           "request_owner_home", "round_rows", "runtime_rows"]
