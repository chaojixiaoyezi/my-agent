"""watch_stream 工具:高吞吐数据流的盯守摄取入口(open/pull/status/close/list)。

pull 是长轮询:块内持续「拉流→喂引擎」直到出现候选或等待额度用完——游标始终追平
流末尾,模型轮只在有东西可判时消耗。出站走与 web_fetch 同一道网络安全闸
(allowed_private_hosts 由调用层每次热注入,owner 授权即时生效)。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from ..tooling.web import (
    _default_network_resolver,
    _format_http_error,
    _network_safety_error,
    _normalize_url,
)
from ..tooling.web_fetch_runtime import FetchRawRequest, PinResult, fetch_raw_response
from .puller import DrainBudget, drain_source
from .watch_learn import configure_spec, sample_source
from .watch_payloads import (
    PULL_GUIDANCE,
    attach_keep_watching_note,
    attach_spec_target_common_alert,
    build_audit_record,
    coverage_block,
    merge_spec_target_common,
    order_candidate_rows,
    render_open_payload,
    render_pull_payload,
    watch_block,
)
from .watch_state import (
    WatchState,
    audit_append,
    list_states,
    new_state,
    persist_state,
    refresh_scalars_from_disk,
    registry,
    watch_id_for,
)
from .watch_tool_spec import build_watch_stream_spec

_TOOL_NAME = "watch_stream"
_HTTP_TIMEOUT_SECONDS = 15


class WatchStreamTool(BaseTool):
    # 类用途: 把摄取层(结构化预聚合/初筛/背压)暴露成模型工具;每路数据流一个 watch,
    #   游标+引擎状态跨轮持久(进程内注册表+owner 盘上快照),补岗/重启从游标续。
    def __init__(self, agent: object) -> None:
        self.agent = agent
        self.spec: ToolSpec = build_watch_stream_spec()
        self.allowed_private_hosts: tuple[str, ...] = ()
        self.allow_private_resolution: bool | None = None

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        owner_home = self._owner_home()
        if owner_home is None:
            return _err("无 owner home 上下文,盯守状态存储不可用", "TOOL_UNAVAILABLE")
        action = str(params.get("action") or "pull").strip().lower()
        handler = {
            "open": self._open,
            "sample": self._sample,
            "configure": self._configure,
            "pull": self._pull,
            "status": self._status,
            "close": self._close,
            "list": self._list,
        }.get(action)
        if handler is None:
            return _err("action 须为 open/sample/configure/pull/status/close/list", "TOOL_INVALID_ARGUMENTS")
        return handler(owner_home, params)

    def _open(self, owner_home: Path, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            url = _normalize_url(params.get("url"))
        except ValueError as exc:
            return _err(f"url 无效: {exc}", "TOOL_INVALID_ARGUMENTS")
        gate_error = _network_safety_error(
            _TOOL_NAME, url, _default_network_resolver, self.allowed_private_hosts, self.allow_private_resolution
        )
        if gate_error is not None:
            return gate_error
        state = registry.get_or_load(owner_home, watch_id_for(owner_home, url))
        resumed = state is not None
        if state is None:
            state = new_state(owner_home, url, params)
            registry.put(state)
        _apply_open_overrides(state, params)
        if not state.source_envelope:
            state.source_envelope = _probe_source_envelope(self._fetch_json, state.source_url)
        persist_state(state)
        if int(state.tuning.background_harvest or 0):
            # open 即开始覆盖:收割者立刻起跑,模型规划期间的流量也不丢。
            from .harvester import ensure_harvester

            ensure_harvester(state, self._harvester_fetch())
        return _ok_payload(render_open_payload(state, resumed))

    def _sample(self, owner_home: Path, params: dict[str, Any]) -> ToolExecutionResult:
        """抓原始样本+字段分布给模型学判据(learn);不动盯守游标/引擎。"""
        state = _state_for(owner_home, params)
        if isinstance(state, ToolExecutionResult):
            return state
        return sample_source(self._fetch_json, state, params)

    def _configure(self, owner_home: Path, params: dict[str, Any]) -> ToolExecutionResult:
        """灌入模型学出的判据 spec(configure);引擎即时生效并随 watch 持久化。"""
        state = _state_for(owner_home, params)
        if isinstance(state, ToolExecutionResult):
            return state
        return configure_spec(state, params)

    def _pull(self, owner_home: Path, params: dict[str, Any]) -> ToolExecutionResult:
        state = _state_for(owner_home, params)
        if isinstance(state, ToolExecutionResult):
            return state
        max_wait = _float_in(params.get("max_wait_seconds"), 0.0, float(state.tuning.max_wait_cap_seconds))
        with state.lock:
            state.last_puller_run_id = self._current_run_id() or state.last_puller_run_id
        if int(state.tuning.background_harvest or 0):
            harvested = _pull_from_spool(self, state, max_wait)
            if harvested is not None:
                return harvested
        with state.lock:
            return self._pull_locked(state, max_wait)

    def _pull_locked(self, state: WatchState, max_wait: float) -> ToolExecutionResult:
        deadline = time.time() + max_wait
        aggregate = {"seen": 0, "pages": 0, "waited_rounds": 0}
        while True:
            digest = self._drain_and_digest(state, aggregate)
            if digest is None:
                return _source_error_result(state)
            if digest.candidates or time.time() >= deadline:
                break
            aggregate["waited_rounds"] += 1
            time.sleep(min(state.tuning.poll_interval_seconds, max(0.1, deadline - time.time())))
        extras = {"seen_this_call": aggregate["seen"], "pages_this_call": aggregate["pages"]}
        return _ok_payload(render_pull_payload(state, digest, extras))

    def _drain_and_digest(self, state: WatchState, aggregate: dict[str, int]):
        from .watch_feedback import consume_feedback_inbox

        # inline 模式引擎属主在 pull 侧:同样先消费反馈收件箱(与 harvester 拍同语义)。
        consume_feedback_inbox(state, time.time())
        budget = DrainBudget(
            max_events=state.tuning.max_events_per_pull,
            page_limit=state.tuning.page_limit,
            deadline=time.time() + _HTTP_TIMEOUT_SECONDS,
        )
        drain = drain_source(self._fetch_json, state.source_url, state.cursor, budget)
        state.totals["pulls"] += 1
        if drain.error:
            state.totals["http_errors"] += 1
            state.last_error = drain.error
            persist_state(state)
            return None
        _absorb_drain(state, drain, aggregate)
        digest = state.engine.process(drain.events, time.time())
        persist_state(state)
        audit_append(state, build_audit_record(drain, digest))
        return digest

    def _status(self, owner_home: Path, params: dict[str, Any]) -> ToolExecutionResult:
        state = _state_for(owner_home, params)
        if isinstance(state, ToolExecutionResult):
            return state
        with state.lock:
            refresh_scalars_from_disk(state)
        return _ok_payload(_status_payload(state))

    def _close(self, owner_home: Path, params: dict[str, Any]) -> ToolExecutionResult:
        state = _state_for(owner_home, params)
        if isinstance(state, ToolExecutionResult):
            return state
        with state.lock:
            state.closed = True
            persist_state(state)
        from .harvester import stop_harvester

        stop_harvester(state.watch_id)
        registry.drop(state.watch_id)
        return _ok_payload(_close_payload(state))

    def _list(self, owner_home: Path, _params: dict[str, Any]) -> ToolExecutionResult:
        rows = [_enriched_list_row(row) for row in list_states(owner_home)]
        return _ok_payload({"ok": True, "action": "list", "watches": rows, "count": len(rows)})

    def _fetch_json(self, url: str) -> tuple[bool, object, str]:
        return self._fetch_json_pinned(
            url, tuple(self.allowed_private_hosts or ()), self.allow_private_resolution
        )

    def _harvester_fetch(self):
        """收割线程用的 fetch:把【当下在场的出站授权】钉进闭包。

        allowed_private_hosts 由调用层每次工具调用临时热注入、调用返回即还原为空——
        收割线程跨调用长命,若直接用绑定方法,调用窗口之外的拉流全被出站闸拦
        (真机实锤:六路各 58~116 个 NETWORK_PRIVATE_HOST_BLOCKED 间歇断粮;其一
        子代理拉取变稀后收割彻底冻结,读游标掉出源滚动缓冲=真丢数据)。
        watch 本就是 owner 明确授权后才 open 得进来的(open 有同一道闸),钉住
        开启/续拉时刻的授权语义正确;撤销授权后的下一次 open/pull 会用新授权重钉。"""
        override = self.__dict__.get("_fetch_json")
        if override is not None:
            return override  # 实例级注入(测试假源/自定义拉取)优先,保持既有接缝
        hosts = tuple(self.allowed_private_hosts or ())
        allow_resolution = self.allow_private_resolution

        def _fetch(url: str) -> tuple[bool, object, str]:
            return self._fetch_json_pinned(url, hosts, allow_resolution)

        return _fetch

    def _fetch_json_pinned(
        self, url: str, hosts: tuple[str, ...], allow_resolution: bool | None
    ) -> tuple[bool, object, str]:
        response = fetch_raw_response(
            FetchRawRequest(
                tool=_TOOL_NAME,
                url=url,
                method="GET",
                headers={"User-Agent": "MyAgent-WatchStream/1.0"},
                data=None,
                timeout=_HTTP_TIMEOUT_SECONDS,
            ),
            format_http_error=_format_http_error,
            resolve_pin=lambda pin_url: self._resolve_pin_with(pin_url, hosts, allow_resolution),
        )
        if isinstance(response, ToolExecutionResult):
            return False, response.output, response.error_code
        try:
            return True, json.loads(response.body.decode("utf-8", "replace")), ""
        except json.JSONDecodeError as exc:
            return False, f"响应不是 JSON: {exc}", "TOOL_INVALID_ARGUMENTS"

    def _resolve_pin_with(
        self, url: str, hosts: tuple[str, ...], allow_resolution: bool | None
    ) -> PinResult:
        """与 web_fetch 同款:解析一次→过网络安全闸→pin 校验过的 IP(重定向逐跳重查)。"""
        try:
            resolved = tuple(str(ip) for ip in _default_network_resolver(urlsplit(url).hostname or ""))
        except Exception:
            resolved = ()
        err = _network_safety_error(_TOOL_NAME, url, lambda _h: resolved, hosts, allow_resolution)
        if err is not None:
            return PinResult(None, err)
        return PinResult(resolved[0] if resolved else None, None)

    def _owner_home(self) -> Path | None:
        raw = str(getattr(getattr(self.agent, "home_paths", None), "owner_home_dir", "") or "").strip()
        return Path(raw) if raw else None

    def _current_run_id(self) -> str:
        """当前拉流的 run(子代理 run_id 优先):编队补岗扫描据此判断岗上是谁、活没活着。"""
        from ..agent_core.runner.context import current_subagent_run_id

        run_id = current_subagent_run_id(self.agent)
        if run_id:
            return run_id
        return str(getattr(getattr(self.agent, "_current_run_params", None), "run_id", "") or "")


def _state_for(owner_home: Path, params: dict[str, Any]) -> WatchState | ToolExecutionResult:
    watch_id = str(params.get("watch_id") or "").strip()
    if not watch_id and params.get("url"):
        try:
            watch_id = watch_id_for(owner_home, _normalize_url(params.get("url")))
        except ValueError:
            watch_id = ""
    if not watch_id:
        return _err("缺 watch_id(或给 url);先 action=open 打开盯守", "TOOL_PARAMETER_REQUIRED")
    state = registry.get_or_load(owner_home, watch_id)
    if state is None:
        known = [row["watch_id"] for row in list_states(owner_home)]
        return _err(f"watch_id 不存在: {watch_id};已有: {known}(先 action=open)", "TOOL_INVALID_ARGUMENTS")
    return state


_ENVELOPE_VALUE_CAP = 300
_ENVELOPE_KEY_CAP = 8


def _probe_source_envelope(fetch_json, source_url: str) -> dict[str, Any]:
    """open 探针:拉一页,把源信封的【标量】元数据原样带回(如 schema_note/api 名)。

    只做结构化搬运(取非列表标量、截断),绝不解读语义——判据说明是源写给模型看的,
    代码不定性。探针失败返回空(不阻塞 open;pull 覆盖照常)。
    """
    joiner = "&" if "?" in source_url else "?"
    ok, payload, _code = fetch_json(f"{source_url}{joiner}since=0&limit=1")
    if not ok or not isinstance(payload, dict):
        return {}
    envelope: dict[str, Any] = {}
    for key, value in payload.items():
        if len(envelope) >= _ENVELOPE_KEY_CAP:
            break
        if isinstance(value, (str, int, float, bool)):
            envelope[str(key)] = value if not isinstance(value, str) else value[:_ENVELOPE_VALUE_CAP]
    return envelope


def _apply_open_overrides(state: WatchState, params: dict[str, Any]) -> None:
    raw_window = params.get("watch_window_seconds")
    if raw_window is not None:
        try:
            state.watch_window_seconds = max(0, int(str(raw_window).strip()))
        except (TypeError, ValueError):
            pass
    state.closed = False


def _absorb_drain(state: WatchState, drain, aggregate: dict[str, int]) -> None:
    state.last_error = ""
    state.cursor = drain.cursor
    state.last_reached_end = drain.reached_end
    state.last_pull_at = time.time()
    state.totals["gap_events"] += drain.gap_events
    aggregate["seen"] += len(drain.events)
    aggregate["pages"] += drain.pages


def _status_payload(state: WatchState) -> dict[str, Any]:
    from .harvester import harvester_block

    return {
        "ok": True,
        "action": "status",
        "watch_id": state.watch_id,
        "source_url": state.source_url,
        "source_spec": dict(state.source_spec) if state.source_spec else None,
        "coverage": coverage_block(state, {}),
        "watch": watch_block(state),
        "harvester": harvester_block(state),
        "totals": {**state.totals, **state.engine.totals},
    }


def _pull_from_spool(tool: WatchStreamTool, state: WatchState, max_wait: float) -> ToolExecutionResult | None:
    """后台连续摄取模式的 pull:确保收割者在跑,长轮询消费 spool 候选批。

    与 inline 模式的关键差别:等待期间【不持 state.lock】(收割线程每拍要锁);
    收割者起不来(且无别进程收割)→ 返回 None 回落 inline drain,行为零变化。
    """
    from .harvester import ensure_harvester, harvester_block

    ensured = ensure_harvester(state, tool._harvester_fetch())
    if ensured is None:
        return None
    remote = str(ensured.get("mode") or "") == "remote"
    records, backlog = _wait_for_spool_records(state, max_wait, remote=remote)
    with state.lock:
        state.last_pull_at = time.time()
        if not remote:
            # 收割者在本进程:落一次快照(消费时间戳进盘,供 idle 判定/补岗观测)。
            persist_state(state)
        payload = _render_spool_pull(state, records, backlog, harvester_block(state))
    return _ok_payload(payload)


def _wait_for_spool_records(
    state: WatchState, max_wait: float, *, remote: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """长轮询 spool 直到有候选批或超时;等待下限抬到一个收割节拍(max_wait=0 的即取式
    pull 也至少等收割者跑完一拍,不因线程刚起步而空手;有积压时仍即时返回)。"""
    from .harvester import read_spool_records

    deadline = time.time() + max(max_wait, state.tuning.poll_interval_seconds + 0.5)
    while True:
        _refresh_if_remote(state, remote)
        records, backlog = read_spool_records(state, max_candidates=state.tuning.max_candidates_per_pull)
        if records or time.time() >= deadline:
            return records, backlog
        time.sleep(min(state.tuning.poll_interval_seconds, max(0.1, deadline - time.time())))


def _refresh_if_remote(state: WatchState, remote: bool) -> None:
    """收割者在别的进程时,pull 侧每轮从盘上快照回灌覆盖类标量(展示新鲜覆盖)。"""
    if not remote:
        return
    with state.lock:
        refresh_scalars_from_disk(state)


def _render_spool_pull(
    state: WatchState,
    records: list[dict[str, Any]],
    backlog: dict[str, Any],
    harvester: dict[str, Any],
) -> dict[str, Any]:
    """spool 消费批 → 与 inline pull 同一契约的载荷(候选行/被压组/覆盖账,模型无感)。"""
    newest = records[-1] if records else {}
    payload = {
        "ok": True,
        "action": "pull",
        "watch_id": state.watch_id,
        "source_envelope": dict(state.source_envelope),
        "source_spec_configured": bool(state.source_spec),
        "candidates": order_candidate_rows(
            [row for record in records for row in (record.get("candidates") or [])]
        ),
        "suppressed_groups": list(newest.get("suppressed_groups") or []),
        "suppressed_groups_total": int(newest.get("suppressed_groups_total") or 0),
        "suppressed_events_this_call": sum(int(r.get("suppressed_events") or 0) for r in records),
        "overflow": {
            "count": sum(int(r.get("overflow_count") or 0) for r in records),
            "note": "达标但超出单批候选上限的事件(完整清单在审计账 watch_state/*.audit.ndjson)",
            "sample_stream_pos": [],
        },
        "coverage": coverage_block(
            state,
            {
                "spool_backlog_records": int(backlog.get("records_unread") or 0),
                "spool_backlog_candidates": int(backlog.get("candidates_unread") or 0),
            },
        ),
        "watch": watch_block(state),
        "engine_totals": dict(state.engine.totals),
        "harvester": harvester,
        "guidance": PULL_GUIDANCE,
    }
    if state.last_error:
        payload["last_source_error"] = state.last_error
    # P2 配反免疫告警(spool 路):合并本消费批各记录的告警,与 inline pull 同契约。
    attach_spec_target_common_alert(
        payload,
        merge_spec_target_common([record.get("spec_target_common") or [] for record in records]),
    )
    # 续蹲/清账信号(spool 路补齐):此前只 inline 路调用,真机主路(spool 消费)拿不到
    # keep_watching;窗口末尾清账信号(drain_before_close_note,不足4)更是只有 spool 路
    # 才有 backlog 计数——两路契约在这里真正对齐。
    attach_keep_watching_note(payload)
    return payload


def _close_payload(state: WatchState) -> dict[str, Any]:
    payload = {
        "ok": True,
        "action": "close",
        "watch_id": state.watch_id,
        "final_coverage": coverage_block(state, {}),
        "watch": watch_block(state),
    }
    # 不静默弃判(g8 不足4·末尾清账的账目半边):close 时 spool 还有已抬未判候选,把数目
    # 如实亮进关闭回执——弃了多少一目了然;要盯完就先 pull 清账再 close(纯结构计数,不拦)。
    backlog = _unjudged_backlog_at_close(state)
    payload["spool_backlog_candidates_at_close"] = backlog
    if backlog > 0:
        payload["discarded_backlog_note"] = (
            f"关闭时 spool 还有 {backlog} 条已初筛抬升的候选没被逐条重判——它们是盯守期内的事件,"
            "现在关闭即弃判。要盯完整就先继续 pull 把积压判完再 close(游标已持久化,重新 open 可续)。"
        )
    return payload


def _unjudged_backlog_at_close(state: WatchState) -> int:
    from .harvester import read_spool_cursor

    written = int(state.totals.get("spool_candidates", 0) or 0)
    if written <= 0:
        return 0
    consumed = int(read_spool_cursor(state).get("candidates_consumed") or 0)
    return max(0, written - consumed)


def _enriched_list_row(row: dict[str, Any]) -> dict[str, Any]:
    window = int(row.get("watch_window_seconds") or 0)
    elapsed = max(0.0, time.time() - float(row.get("opened_at") or 0.0))
    row["elapsed_seconds"] = round(elapsed, 1)
    row["window_complete"] = bool(window and elapsed >= window)
    return row


def _source_error_result(state: WatchState) -> ToolExecutionResult:
    body = {
        "ok": False,
        "watch_id": state.watch_id,
        "error": f"数据源拉取失败: {state.last_error}",
        "coverage": coverage_block(state, {}),
        "hint": "游标已持久化,源恢复后重新 pull 会从断点续读;连续失败可 status 查看并如实上报覆盖缺口。",
    }
    return ToolExecutionResult(
        _TOOL_NAME, False, json.dumps(body, ensure_ascii=False), error_code="NETWORK_REQUEST_FAILED"
    )


def _float_in(value: object, low: float, high: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return low
    return max(low, min(high, parsed))


def _ok_payload(payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False), result_envelope=payload)


def _err(message: str, code: str) -> ToolExecutionResult:
    return ToolExecutionResult(_TOOL_NAME, False, json.dumps({"ok": False, "error": message}, ensure_ascii=False), error_code=code)


__all__ = ["WatchStreamTool"]
