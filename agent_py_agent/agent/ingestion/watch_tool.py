"""watch_stream 工具:高吞吐数据流的盯守摄取入口(open/pull/status/close/list)。

pull 是长轮询:块内持续「拉流→喂引擎」直到出现候选或等待额度用完——游标始终追平
流末尾,模型轮只在有东西可判时消耗。出站走与 web_fetch 同一道网络安全闸
(allowed_private_hosts 由调用层每次热注入,owner 授权即时生效)。
"""

from __future__ import annotations

import json
import logging
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
from .puller import DrainBudget
from .watch_learn import configure_spec, sample_source
from .watch_payloads import (
    AUDIT_PULL_GUIDANCE,
    PULL_GUIDANCE,
    attach_audit_receipt,
    attach_content_rules_count,
    attach_frequent_hit_alert,
    attach_judgment_note,
    attach_keep_watching_note,
    attach_overload_note,
    build_audit_record,
    content_rules_block,
    coverage_block,
    merge_frequent_hits,
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
    reopen_on_disk,
    state_dir,
    watch_id_for,
)
from .watch_tool_spec import build_watch_stream_spec

_TOOL_NAME = "watch_stream"
_HTTP_TIMEOUT_SECONDS = 15
_focus_log = logging.getLogger("my_agent.ingestion.audit_focus")  # 聚焦覆盖观测:接线断裂哨兵(见 _audit_focus_verdicts)


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
            "verdict": self._verdict,
            "status": self._status,
            "close": self._close,
            "list": self._list,
        }.get(action)
        if handler is None:
            return _err("action 须为 open/sample/configure/pull/verdict/status/close/list", "TOOL_INVALID_ARGUMENTS")
        return handler(owner_home, params)

    def _open(self, owner_home: Path, params: dict[str, Any]) -> ToolExecutionResult:
        resolved = self._resolve_open_source(params)
        if isinstance(resolved, ToolExecutionResult):
            return resolved
        url, mode = resolved
        state = registry.get_or_load(owner_home, watch_id_for(owner_home, url))
        resumed = state is not None
        if state is None:
            state = new_state(owner_home, url, params)
            state.source_mode = mode
            registry.put(state)
        _override_watch_window_from_audit_command(self, params)
        _apply_open_overrides(state, params)
        _apply_audit_guarantee(self, state, params)
        state.opened_by_run = self._current_run_id() or state.opened_by_run
        # 显式 open=用户重开意图:先把盘上 closed 翻回 False,否则 persist 的单调合并
        # (防收割覆写翻回 close)会把刚置 False 的内存态又吃回 True,收割自停盯守空转。
        reopen_on_disk(state)
        if not state.source_envelope:
            state.source_envelope = _probe_envelope_for(self._fetch_json, state)
        persist_state(state)
        if state.audit_guarantee or int(state.tuning.background_harvest or 0):
            # open 即开始覆盖:收割者立刻起跑,模型规划期间的流量也不丢。
            # 保证档无视 background_harvest=0 调参:durable 队列是契约件,不许关。
            from .harvester import ensure_harvester

            ensure_harvester(state, self._harvester_fetch())
        payload = render_open_payload(state, resumed, unjudged_backlog=_unjudged_spool_backlog(state))
        _attach_fanout_hint(payload, owner_home, state)
        attach_audit_receipt(payload, state)
        if state.audit_guarantee:
            payload["audit_guarantee"] = True
            payload["audit_note"] = (
                "/audit 保证档已生效(随 watch 持久化,重启/换人/子代理接手都在):每条记录"
                "都会入 durable 队列并逐条交你判,判读慢只会让待判涨、绝不丢;每条要交 verdict"
                "(pull 载荷里有完整契约),覆盖回执随 pull/status 可查。先对好数据格式再进入"
                "长期盯守:源花杂就 sample+configure 学判据——学出的判据只用于判得准,"
                "保证档下不会拿它筛掉任何一条。"
            )
        return _ok_payload(payload)

    def _resolve_open_source(self, params: dict[str, Any]) -> tuple[str, str] | ToolExecutionResult:
        return _resolve_open_source(self, params)

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
        consumer = self._current_run_id()
        with state.lock:
            state.last_puller_run_id = consumer or state.last_puller_run_id
        if state.audit_guarantee or int(state.tuning.background_harvest or 0):
            # 保证档强制 spool 消费路(background_harvest=0 调参也不放行 inline):
            # inline 路事件不入 durable 队列、没有逐条签收对账,违反 /audit 契约。
            harvested = _pull_from_spool(self, state, max_wait, consumer)
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
        from .sources import drain_watch_source
        from .watch_feedback import consume_feedback_inbox

        # inline 模式引擎属主在 pull 侧:同样先消费反馈收件箱(与 harvester 拍同语义)。
        consume_feedback_inbox(state, time.time())
        # content_mode(冷启动/passthrough 全量直通)inline 回落路一次 process 整批不分片——
        # 不钳会把整条存量一次抬给模型(与 harvester 记录尺寸同一把尺,防单 pull 洪泛
        # rubber-stamp)。判据与 harvester.is_content_mode 一致(直通关时走结构化降维、不钳)。
        from .harvester import content_batch_size, is_content_mode

        max_events = state.tuning.max_events_per_pull
        if is_content_mode(state):
            max_events = min(max_events, content_batch_size(state.tuning))
        budget = DrainBudget(
            max_events=max_events,
            page_limit=state.tuning.page_limit,
            deadline=time.time() + _HTTP_TIMEOUT_SECONDS,
        )
        drain = drain_watch_source(state, self._fetch_json, budget)
        state.totals["pulls"] += 1
        if drain.error:
            state.totals["http_errors"] += 1
            state.last_error = drain.error
            persist_state(state)
            return None
        _absorb_drain(state, drain, aggregate)
        # 冷启动直通与 harvester 路同契约:本源还没 configure 出 spec 时判据没学出来,存量
        # 不能靠结构规则筛(根因2)——inline 回落路(harvester 起不来时)也必须整批 full_read,
        # 否则回落一次就把存量要紧事筛掉。configure 后转 spec 驱动(passthrough 自带无条件直通)。
        cold_start = state.source_spec is None
        digest = state.engine.process(drain.events, time.time(), cold_start=cold_start)
        persist_state(state)
        audit_append(state, build_audit_record(drain, digest))
        return digest

    def _verdict(self, owner_home: Path, params: dict[str, Any]) -> ToolExecutionResult:
        """/audit 保证档的逐条结论签收:判读工把手里批的逐条结论交回来销账(ack-on-judge)。
        非保证档调用如实拒绝(那条路是 ack-on-next-pull,没有欠账可销)。"""
        state = _state_for(owner_home, params)
        if isinstance(state, ToolExecutionResult):
            return state
        if not state.audit_guarantee:
            return _err("本路不是 /audit 保证档:结论签收仅保证档适用(非保证档下一次 pull 即确认)", "TOOL_INVALID_ARGUMENTS")
        verdicts = _parse_verdicts(params.get("verdicts"))
        if not verdicts:
            return _err('缺 verdicts:形如 [{"ack_id":"12:0","verdict":"hit|clear|unsure","note":"…"}]', "TOOL_PARAMETER_REQUIRED")
        from .harvester import submit_verdicts

        result = submit_verdicts(state, consumer=self._current_run_id(), verdicts=verdicts)
        payload = {"action": "verdict", "watch_id": state.watch_id, **result}
        attach_audit_receipt(payload, state)
        if result.get("unknown_ack_ids"):
            payload["unknown_note"] = (
                "unknown_ack_ids 里的令牌不在你(或任何在途批)的欠账里——ack_id 必须原样取自"
                "本轮 pull 候选行,别手编/串批;这些条目没有入账,请核对后随正确的 ack_id 重交。"
            )
        return _ok_payload(payload) if result.get("ok") else _err(str(result.get("error") or "结论未入账"), "TOOL_INVALID_ARGUMENTS")

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
        return _current_run_id(self.agent)


def _resolve_open_source(tool: WatchStreamTool, params: dict[str, Any]) -> tuple[str, str] | ToolExecutionResult:
    """open 的源解析:file 源走文件路径闸(不出网),HTTP 源走网络安全闸;
    mode=poll 声明快照接口(定时查),仅对 HTTP 源有意义。返回 (规范 URL, source_mode)。"""
    raw = str(params.get("url") or "").strip()
    mode = str(params.get("mode") or "").strip().lower()
    if raw.lower().startswith("file://") or raw.startswith("/"):
        from .sources import file_path_of, normalize_file_url

        try:
            url = normalize_file_url(raw)
        except ValueError as exc:
            return _err(f"url 无效: {exc}", "TOOL_INVALID_ARGUMENTS")
        decision = _file_access_policy(tool).check(file_path_of(url))
        if not decision.allowed:
            return _err(f"文件路径访问被拒: {decision.message or decision.code}", "TOOL_PERMISSION_DENIED")
        return url, ""
    try:
        url = _normalize_url(params.get("url"))
    except ValueError as exc:
        return _err(f"url 无效: {exc}(本地文件源请给 file:///绝对路径)", "TOOL_INVALID_ARGUMENTS")
    gate_error = _network_safety_error(
        _TOOL_NAME, url, _default_network_resolver, tool.allowed_private_hosts, tool.allow_private_resolution
    )
    if gate_error is not None:
        return gate_error
    return url, ("poll" if mode == "poll" else "")


def _current_run_id(agent: object) -> str:
    """当前拉流的 run(子代理 run_id 优先):编队补岗扫描据此判断岗上是谁、活没活着。"""
    from ..agent_core.runner.context import current_subagent_run_id

    run_id = current_subagent_run_id(agent)
    if run_id:
        return run_id
    return str(getattr(getattr(agent, "_current_run_params", None), "run_id", "") or "")


def _file_access_policy(tool: WatchStreamTool):
    """file 源的路径闸:与文件系统工具同源的策略(危险根拒读 + 多用户 owner 墙)。"""
    from ..path_access_policy import PathAccessPolicy

    config = getattr(tool.agent, "config", None)
    return PathAccessPolicy.from_values(
        mode=getattr(config, "path_access_mode", "normal"),
        dangerous_roots=getattr(config, "path_dangerous_roots", None),
        owner_scope_root=str(getattr(getattr(tool.agent, "home_paths", None), "owner_home_dir", "") or ""),
    )


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


def _probe_envelope_for(fetch_json, state: WatchState) -> dict[str, Any]:
    """open 探针分流:file 源无信封(本地文件没有源自带元数据);poll 源原样 GET 一次
    (快照接口没有 since/limit 参数语义);cursor 源沿用翻页探针。"""
    from .sources import probe_poll_envelope, source_kind

    kind = source_kind(state.source_url, state.source_mode)
    if kind == "file":
        return {}
    if kind == "poll":
        return probe_poll_envelope(fetch_json, state.source_url, _ENVELOPE_VALUE_CAP, _ENVELOPE_KEY_CAP)
    return _probe_source_envelope(fetch_json, state.source_url)


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


def _sibling_sources_same_run(owner_home: Path, state: WatchState) -> list[str]:
    """本 run 已在盯的【其它】未关闭 watch 的源地址(结构探针:同 run 开 2+ 路 = 一个子代理
    独扛多源反模式)。纯按 opened_by_run 计数,不判内容;run 为空(无编排上下文)时不触发。"""
    run_id = str(state.opened_by_run or "").strip()
    if not run_id:
        return []
    others: list[str] = []
    for row in list_states(owner_home):
        if row.get("watch_id") == state.watch_id or row.get("closed"):
            continue
        if str(row.get("opened_by_run") or "") == run_id:
            others.append(str(row.get("source_url") or ""))
    return others


def _attach_fanout_hint(payload: dict[str, Any], owner_home: Path, state: WatchState) -> None:
    """一个 run 开了 2+ 路 watch 时,把"一源一子代理扇出"的结构化提示怼进 open 回执
    (根因4:一个子代理独扛多源→串行判必积压、顶回粗筛漏真事)。纯结构计数触发,不判内容。"""
    siblings = _sibling_sources_same_run(owner_home, state)
    if not siblings:
        return
    total = len(siblings) + 1
    payload["fanout_hint"] = (
        f"你这一个代理已经同时在盯 {total} 路源了(本路 + 已开 {len(siblings)} 路)。"
        "一个代理串行盯多源会积压判读、拖慢实时性,还会把内容型源顶回结构粗筛漏掉真要紧事——"
        "【一个数据源/一个接口/一份大文件应当是一个专属子代理】(各自开 watch、各自判、各自报,"
        "天然并行)。如果你是主代理:用 create_subagents 一次 items 一源一个 long_running 子代理"
        "分头盯;如果你已经是子代理:用 schedule_child_subagents 把每路源派给孙代理。把已开的多路"
        "watch 拆到各自的代理里去,别在这一个代理里串着盯。判读慢是模型的事,一源一工足矣——不要"
        "为一个源多派判读工,多派只会排队干等、不加速、白占资源(照 claude-code:主代理手动定、绝不按积压自动扩)。"
    )
    payload["watches_this_run"] = total


def _apply_open_overrides(state: WatchState, params: dict[str, Any]) -> None:
    _apply_window_override(state, params.get("watch_window_seconds"))
    state.closed = False


def _apply_audit_guarantee(tool: WatchStreamTool, state: WatchState, params: dict[str, Any]) -> None:
    """/audit 保证档置位(单调棘轮:置上不因后续 open 缺参而降级——保证是用户级契约)。
    激活判据只认结构化事实,不再回扫 prompt/goal 文本:
    task_attributes 里的保证档标志是唯一入口——这是跨轮/跨 spawn 树可靠的信号:
    前台创建路(root_user_prompt 是用户原文时)由 gateway/orchestration
    一次性盖上,主代理自己和它委派的判读子代理/孙代理此后每轮都读得到(继承靠 state/attributes
    层,不靠 goal 文本)。gateway 只在消息以 /audit 命令开头时盖标；普通聊天里提到该词不会激活。
    模型工具参数也不能自行升级保证档，避免普通任务绕过用户的显式特殊模式选择。
    置位时快照引擎有损计数基线。"""
    if state.audit_guarantee:
        return
    if not _audit_requested(tool):
        return
    state.audit_guarantee = True
    state.audit_baseline = {
        key: int(state.engine.totals.get(key, 0) or 0)
        for key in ("suppressed", "overflow", "audit_throttled")
    }


def _audit_requested(tool: WatchStreamTool) -> bool:
    from ..common.audit_activation import attributes_request_audit

    # 结构化标志(跨轮/跨子代理可靠):当前任务属性(子代理=task.attributes 经 runner
    # 设进上下文;主代理=gateway 建的 run_params.task_attributes)。
    return attributes_request_audit(_current_audit_attributes(tool.agent))


def _current_audit_attributes(agent: object) -> dict[str, Any] | None:
    """当前 runner 上下文的 task_attributes:子代理走线程本地上下文(run_flow 从 task.attributes
    设入),主代理走 _current_run_params.task_attributes(gateway 建)。两条都读,任一命中即可。"""
    try:
        from ..agent_core.runner.context import current_task_attributes

        attrs = current_task_attributes(agent)
        if isinstance(attrs, dict) and attrs:
            return attrs
    except Exception:
        pass
    params = getattr(agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", None)
    return attrs if isinstance(attrs, dict) else None


def _override_watch_window_from_audit_command(tool: WatchStreamTool, params: dict[str, Any]) -> None:
    """gateway 解析 /audit <N><d|h|m> 后写入结构化属性，据此钉住 watch 窗口。

    该值盖过模型自行传入的窗口，再走同一条 _apply_window_override；裸 /audit 不改窗口。
    watch 工具不读取用户原文，也不做自然语言推断。
    """
    from ..common.audit_activation import AUDIT_WINDOW_ATTR

    attrs = _current_audit_attributes(tool.agent)
    seconds = attrs.get(AUDIT_WINDOW_ATTR) if isinstance(attrs, dict) else None
    if seconds:
        params["watch_window_seconds"] = seconds


def _apply_window_override(state: WatchState, raw_window: object) -> None:
    """带窗口的 open 且旧窗已走完(或 close 过)= 新一场盯守:窗口起点重置到现在。
    否则重开的盯守沿用旧 opened_at,窗口生下来就"已走完"——收割线程按窗口完成立即
    自停、模型看 window_complete=true 直接收工,重开静默空转(真机实锤:重启后二次
    任务 open 带 720s 窗,opened_at 还是 5200s 前)。
    窗口未走完的中途 open(补岗接管续岗)不重置——续的还是原窗,语义不变。"""
    if raw_window is None:
        return
    try:
        new_window = max(0, int(str(raw_window).strip()))
    except (TypeError, ValueError):
        return
    elapsed = time.time() - float(state.opened_at or 0.0)
    old_window_done = state.watch_window_seconds > 0 and elapsed >= state.watch_window_seconds
    if state.closed or old_window_done:
        state.opened_at = time.time()
    state.watch_window_seconds = new_window


def _absorb_drain(state: WatchState, drain, aggregate: dict[str, int]) -> None:
    state.last_error = ""
    state.cursor = drain.cursor
    # file 源:aux_cursor=读后行号(轮转重读会回落,正确);其余源恒 0=不变。
    state.line_cursor = int(getattr(drain, "aux_cursor", 0) or 0)
    if float(getattr(drain, "fetched_at", 0.0) or 0.0) > 0:
        state.last_poll_at = float(drain.fetched_at)
    state.last_reached_end = drain.reached_end
    state.last_pull_at = time.time()
    state.totals["gap_events"] += drain.gap_events
    aggregate["seen"] += len(drain.events)
    aggregate["pages"] += drain.pages


def _status_payload(state: WatchState) -> dict[str, Any]:
    from .harvester import harvester_block

    payload = {
        "ok": True,
        "action": "status",
        "watch_id": state.watch_id,
        "source_url": state.source_url,
        "source_mode": state.source_mode,
        "judgment_note": state.judgment_note or None,
        "source_spec": dict(state.source_spec) if state.source_spec else None,
        "coverage": coverage_block(state, {}),
        "watch": watch_block(state),
        "harvester": harvester_block(state),
        "totals": {**state.totals, **state.engine.totals},
        # 内容过滤规则审计:建了哪条(spec 的 normal_* 条目)、各拦了多少、合计——
        # 减负可核算不静默。收割者在别的进程时按本进程最近载入的快照呈现(最终一致)。
        "content_rules": content_rules_block(state.engine),
        # 档位一眼可见(默认档 false):status 能直接核对"这路到底在不在保证档"。
        "audit_guarantee": bool(state.audit_guarantee),
    }
    attach_audit_receipt(payload, state)
    return payload


def _parse_verdicts(raw: object) -> list[dict[str, Any]]:
    """verdicts 参数解析:原生数组直收;模型把数组转义成 JSON 字符串的也兼容(常见)。
    非法形态返回空,由调用方给出格式提示。"""
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _pull_from_spool(
    tool: WatchStreamTool, state: WatchState, max_wait: float, consumer: str
) -> ToolExecutionResult | None:
    """后台连续摄取模式的 pull:确保收割者在跑,长轮询消费 spool 候选批。

    与 inline 模式的关键差别:等待期间【不持 state.lock】(收割线程每拍要锁);
    收割者起不来(且无别进程收割)时,spool 里只要还有已抬未判的候选就仍从 spool
    消费(不能因为线程起不来就静默跳到源游标 inline 路,把积压孤儿跳过去);
    积压清完才返回 None 回落 inline drain。
    """
    from .harvester import ensure_harvester, harvester_block

    ensured = ensure_harvester(state, tool._harvester_fetch())
    if ensured is None and not state.audit_guarantee and _unjudged_spool_backlog(state) <= 0:
        return None
    # /audit 保证档永不回落 inline drain:inline 路事件不进 durable 队列(判了没有逐条
    # 签收对账),违反"全量入队+判完才签收"。收割者起不来时如实空批(载荷 harvester
    # 块可见 off),慢=延迟,绝不换一条会漏账的路。
    remote = str((ensured or {}).get("mode") or "") == "remote"
    records, backlog = _wait_for_spool_records(
        state, max_wait, remote=remote, consumer=consumer
    )
    # /audit:每批候选另起【无历史聚焦模型调用】判读,把权威判据回填载荷(治长盯守"clear 惯性沟"
    # ——真机实锤:判读塞进长命子代理累积会话,连判 ~25 条 clear 后把明显真事也顺手判 clear)。
    # 在锁外算(模型调用慢,绝不持 state.lock 卡收割线程);失败/无后端返回空=回退子代理自判。
    focus = _audit_focus_verdicts(tool, state, records)
    # focus 判定【权威签收】(结构化,不靠 rut 子代理):focus 在干净上下文逐条判出的结论,直接
    # 按 ack-on-judge 记账签收。真机实锤——子代理会无视 payload 里的 focus_verdict、按累积会话
    # "clear 惯性沟"把明显真事也判 clear(focus 判 11/11 hit,子代理最终记 9 clear);所以不把
    # 判定押在子代理复判上,focus 判过的条目在这里就权威签收,子代理只需对 hit 补 record_finding。
    # 签收失败绝不拦 pull(回退子代理自判)。
    if focus and state.audit_guarantee:
        from .harvester import submit_verdicts

        focus_rows = [
            {"ack_id": aid, "verdict": (v or {}).get("verdict"), "note": ((v or {}).get("evidence") or "")[:400]}
            for aid, v in focus.items()
            if (v or {}).get("verdict") in ("hit", "clear", "unsure")
        ]
        if focus_rows:
            try:
                submit_verdicts(state, consumer=consumer, verdicts=focus_rows)
            except Exception:  # noqa: BLE001 —— 权威签收只做增益不拦路
                pass
        # focus 判 hit 的当场落 finding 上报(append-only,不被 rut 子代理 clear 覆盖、不赌子代理
        # 记得 record_finding):真事被 focus 抓到就直接让用户看得到。按 event_id 去重。
        _emit_focus_hit_findings(tool, state, records, focus)
    with state.lock:
        state.last_pull_at = time.time()
        if not remote:
            # 收割者在本进程:落一次快照(消费时间戳进盘,供 idle 判定/补岗观测)。
            persist_state(state)
        payload = _render_spool_pull(
            state, records, backlog, harvester_block(state), focus=focus
        )
    return _ok_payload(payload)


def _emit_focus_hit_findings(
    tool: WatchStreamTool, state: WatchState, records: list[dict[str, Any]],
    focus: dict[str, dict[str, str]] | None,
) -> int:
    """focus 判 hit 的候选【当场落 finding 上报】(权威 surfacing):findings.jsonl 是 append-only,
    不会被 rut 子代理后来的 clear 覆盖,也不赌子代理记得 record_finding——focus 在干净上下文判出
    真命中就直接让用户看得到。按 event_id 去重(接管重投/重开可能把同一候选再次呈现,避免重复
    上报)。best-effort:任何一步失败都不拦 pull(退回子代理自行 record_finding)。"""
    if not focus or not state.audit_guarantee:
        return 0
    hits: list[tuple[str, str, str]] = []
    for record in records or []:
        for row in record.get("candidates") or []:
            if not isinstance(row, dict):
                continue
            verdict = focus.get(str(row.get("ack_id"))) or {}
            if verdict.get("verdict") != "hit":
                continue
            event = row.get("event") if isinstance(row.get("event"), dict) else {}
            eid = str(event.get("event_id") or "").strip()
            if eid:
                hits.append((eid, str(row.get("stream_pos") or ""), str(verdict.get("evidence") or "")))
    if not hits:
        return 0
    try:
        import time as _time
        import uuid as _uuid

        from ..agent_core.runner.context import current_subagent_run_id
        from ..agent_core.runtime.record_finding_tool import _findings_ledger_path
        from ..common.json_io import append_jsonl_records, read_json_object_report

        ledger_path, _scope = _findings_ledger_path(tool.agent)
        if not ledger_path:
            return 0
        seen_path = state_dir(state.owner_home) / f"{state.watch_id}.focus_emitted.json"
        report = read_json_object_report(seen_path, context="watch_focus_emitted")
        seen = set(report.payload.get("event_ids") or []) if isinstance(report.payload, dict) else set()
        run_id = current_subagent_run_id(tool.agent)
        new_records: list[dict[str, object]] = []
        for eid, spos, evidence in hits:
            if eid in seen:
                continue
            seen.add(eid)
            new_records.append({
                "version": 1,
                "id": f"rf-{_uuid.uuid4().hex[:12]}",
                "kind": "hit",
                "claim": f"盯守命中(focus 权威判读): {eid}" + (f" — {evidence[:400]}" if evidence else ""),
                "evidence_refs": [eid] + ([spos] if spos else []),
                "confidence": "focus_judge:无历史聚焦判读(不受长盯守 clear 惯性沟污染)",
                "created_at": _time.time(),
                "run_id": run_id,
                "source": "audit_focus",
                "watch_id": state.watch_id,
            })
        if not new_records:
            return 0
        from pathlib import Path as _Path

        append_jsonl_records(_Path(ledger_path), new_records)
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(
            __import__("json").dumps({"event_ids": sorted(seen)}, ensure_ascii=False),
            encoding="utf-8",
        )
        return len(new_records)
    except Exception:  # noqa: BLE001 —— surfacing 只做增益不拦路
        return 0


def _audit_focus_verdicts(
    tool: WatchStreamTool, state: WatchState, records: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """/audit 保证档:本批候选的无历史聚焦判读结论 {ack_id: {verdict, evidence}}。
    非保证档 / 无候选 / 无模型后端(如测试台桩 agent)一律返回空——回退子代理自判(今天的行为),
    聚焦判读只做增益不做拦路。"""
    if not state.audit_guarantee:
        return {}
    rows = [row for rec in (records or []) for row in (rec.get("candidates") or []) if isinstance(row, dict)]
    if not rows:
        return {}
    from .audit_judge import focus_judge_candidates

    focus = focus_judge_candidates(tool.agent, _audit_source_note(state), rows)
    # 聚焦覆盖可观测化(治上一版"假后端 harness 绿、真网关红"的根源=退空静默):有候选却一条
    # 聚焦判据都没拿到,就是接线断了在真 agent 上没发起模型调用,必须留痕。真机实证这条路在真
    # 网关活 agent 上确实跑通(focus_attached=候选数),这个 warning 是给未来的接线回归当哨兵。
    # focus_judge_candidates 内已按无后端/抛错/解析空各记一条更细的 warning。
    if not focus:
        _focus_log.warning(
            "audit focus 空覆盖:%d 条候选无一得到聚焦判据(watch=%s)——回退子代理自判",
            len(rows), state.watch_id,
        )
    else:
        _hits = sum(1 for v in focus.values() if v.get("verdict") == "hit")
        _focus_log.info(
            "audit focus:watch=%s 覆盖 %d/%d 候选,其中 %d 判 hit", state.watch_id, len(focus), len(rows), _hits,
        )
    return focus


def _audit_source_note(state: WatchState) -> str:
    """聚焦判读的领域判据来源(源自带的,代码不内置领域词):源信封 schema_note + 用户教的判读须知。"""
    parts: list[str] = []
    note = str((state.source_envelope or {}).get("schema_note") or "").strip()
    if note:
        parts.append(note)
    if state.judgment_note:
        parts.append(str(state.judgment_note).strip())
    return " / ".join(parts)


def _wait_for_spool_records(
    state: WatchState, max_wait: float, *, remote: bool, consumer: str = ""
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """长轮询 spool 直到有候选批或超时;等待下限抬到一个收割节拍(max_wait=0 的即取式
    pull 也至少等收割者跑完一拍,不因线程刚起步而空手;有积压时仍即时返回)。"""
    from .harvester import judge_quota, read_spool_records

    deadline = time.time() + max(max_wait, state.tuning.poll_interval_seconds + 0.5)
    while True:
        _refresh_if_remote(state, remote)
        # 消费口粮与判读反压同一把尺(judge_quota):直通批整批取走,别按旧上限剁成六截。
        records, backlog = read_spool_records(
            state, max_candidates=judge_quota(state.tuning), consumer=consumer,
        )
        if records or time.time() >= deadline:
            return records, backlog
        time.sleep(min(state.tuning.poll_interval_seconds, max(0.1, deadline - time.time())))


def _refresh_if_remote(state: WatchState, remote: bool) -> None:
    """收割者在别的进程时,pull 侧每轮从盘上快照回灌覆盖类标量(展示新鲜覆盖)。"""
    if not remote:
        return
    with state.lock:
        refresh_scalars_from_disk(state)


def _attach_redelivery_notes(payload: dict[str, Any], backlog: dict[str, Any]) -> None:
    """接管重投的结构化提示(消费者身份变化触发):前任拉走没确认判完的在途批被原样
    重投给继任者——把"接管续的不只是游标、还有缓冲区"讲清楚,并把不可恢复缺口如实亮账。
    保证档欠账重投(结论没交齐,同人换人都会拿到同一批)另给欠账清单。"""
    redelivered = int(backlog.get("redelivered_candidates") or 0)
    pending_verdicts = int(backlog.get("pending_verdicts") or 0)
    if pending_verdicts > 0:
        payload["pending_verdicts"] = pending_verdicts
        payload["pending_ack_ids"] = list(backlog.get("pending_ack_ids") or [])
        payload["redelivery_note"] = (
            f"保证档欠账重投:这批候选交付过,但还有 {pending_verdicts} 条没交逐条结论"
            "(欠账令牌见 pending_ack_ids;可能是你上一轮没交齐,也可能是前任留下的)。"
            "系统不发新批——把欠账的每条读完,用 action=verdict 逐条交结论(hit 先"
            " record_finding),交齐才签收。已交过的条目不在欠账里,重复判无害,漏判才是丢。"
        )
    elif redelivered > 0:
        payload["redelivered_candidates"] = redelivered
        payload["redelivery_note"] = (
            f"本批 {redelivered} 条候选是上一任消费者取走后没确认判完的在途批,现在原样重投给你"
            "(接管续的不只是游标,还有这份缓冲区)——前任可能判了一半也可能全没判:"
            "逐条重判并把确认命中的照常上报;就算个别已被报过,重复上报无害,漏判才是丢。"
        )
    gap = int(backlog.get("redelivery_gap_candidates") or 0)
    if gap > 0:
        payload["spool_redelivery_gap_candidates"] = gap


def _attach_focus_verdicts(
    candidate_rows: list[dict[str, Any]], focus: dict[str, dict[str, str]] | None
) -> int:
    """把无历史聚焦判读结论按 ack_id 回填进候选行(focus_verdict/focus_evidence)。返回回填条数。
    纯结构化搬运:代码不判真假,只把模型在【干净上下文】里给出的权威判据挂到对应候选上。"""
    if not focus:
        return 0
    attached = 0
    for row in candidate_rows:
        verdict = focus.get(str(row.get("ack_id")))
        if not verdict:
            continue
        row["focus_verdict"] = verdict.get("verdict")
        if verdict.get("evidence"):
            row["focus_evidence"] = verdict.get("evidence")
        attached += 1
    return attached


def _render_spool_pull(
    state: WatchState,
    records: list[dict[str, Any]],
    backlog: dict[str, Any],
    harvester: dict[str, Any],
    focus: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """spool 消费批 → 与 inline pull 同一契约的载荷(候选行/被压组/覆盖账,模型无感)。"""
    newest = records[-1] if records else {}
    candidate_rows_this_call = [row for record in records for row in (record.get("candidates") or [])]
    focus_count = _attach_focus_verdicts(candidate_rows_this_call, focus)
    # 积压口径=未判完(未读+在途)再扣掉本批刚交到模型手里的:交付≠判完,消费者死在
    # 判读中途的批不消失;但"你手里这批"不算"还堆着的",否则清账信号永远差一批。
    backlog_beyond_this_call = max(
        0,
        int(backlog.get("candidates_unjudged", backlog.get("candidates_unread")) or 0)
        - len(candidate_rows_this_call),
    )
    payload = {
        "ok": True,
        "action": "pull",
        "watch_id": state.watch_id,
        "source_envelope": dict(state.source_envelope),
        "source_spec_configured": bool(state.source_spec),
        "candidates": order_candidate_rows(candidate_rows_this_call),
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
                "spool_backlog_candidates": backlog_beyond_this_call,
            },
        ),
        "watch": watch_block(state),
        "engine_totals": dict(state.engine.totals),
        "harvester": harvester,
        # 档位一眼可见(默认档也显示 false):治真机"以为开了 /audit 实际跑 triage"的静默失效。
        "audit_guarantee": bool(state.audit_guarantee),
        "guidance": AUDIT_PULL_GUIDANCE if state.audit_guarantee else PULL_GUIDANCE,
    }
    if state.last_error:
        payload["last_source_error"] = state.last_error
    if focus_count > 0:
        payload["focus_judged"] = focus_count
        payload["focus_judgment_note"] = (
            f"本批 {focus_count} 条候选已由【独立聚焦判读】在无历史上下文里逐条判过(结论在每条的 "
            "focus_verdict/focus_evidence 上)——这条路不受你长盯守累积上下文的影响(真机实锤:长会话"
            "连判多条 clear 后会把明显真事也顺手判 clear,聚焦判读专治这个惯性沟)。据 focus_verdict "
            "提交每条 verdict:focus_verdict=hit 的先 record_finding 再交 hit;=clear/=unsure 的照交。"
            "【只可在你能从该条 response 正文举出具体反证时改判(哪个方向都行),严禁凭习惯/因为前面都是 "
            "clear 就改判】——聚焦判读把每条的正文证据放在 focus_evidence 里,先核对再提交。"
        )
    attach_audit_receipt(payload, state)
    _attach_redelivery_notes(payload, backlog)
    # 过载如实标注(P1 反乱报):未判积压(未读+在途)扣掉手里这批仍 ≥ 一个判读口粮 →
    # 挂 overload 块,明确"别为追进度批量乱报、宁可如实留积压"。纯积压计数触发,不判内容。
    from .harvester import overload_threshold

    attach_overload_note(
        payload,
        backlog_beyond_this_call,
        threshold=overload_threshold(state.tuning),
        backpressure_active=bool(harvester.get("backpressure_active")),
    )
    # 【2026-07-09 照 终端应用 退役"按积压自动扩容判读工"】终端应用 无 auto-scale:派几个判读
    # 子代理由主代理手动定(一源一工),超出并发排队,绝不看积压自动多派。真机实锤:按积压狂派只会
    # 招一堆判读工排队干等、不加速反浪费资源(瓶颈是模型判读速度+执行槽位,不是工数)。故不再往载荷挂
    # 按积压 fanout 指令;判读并发=一源一判读子代理(单消费者),见 AUDIT_PULL_GUIDANCE。
    attach_judgment_note(payload, state)
    # 高频命中类调查告警(spool 路):合并本消费批各记录的告警,与 inline pull 同契约;
    # 内容规则减负账同批汇总(命中数,零静默)。
    attach_frequent_hit_alert(
        payload,
        merge_frequent_hits([record.get("frequent_hits") or [] for record in records]),
    )
    attach_content_rules_count(payload, sum(int(r.get("normal_rule_hits") or 0) for r in records))
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
    # 不静默弃判(g8 不足4·末尾清账的账目半边):close 时 spool 还有已抬未确认判完的候选
    # (未读的 + 交付出去没 ack 的在途批),把数目如实亮进关闭回执——弃了多少一目了然;
    # 要盯完就先 pull 清账再 close(纯结构计数,不拦)。
    backlog = _unjudged_spool_backlog(state)
    payload["spool_backlog_candidates_at_close"] = backlog
    attach_audit_receipt(payload, state)
    if backlog > 0:
        payload["discarded_backlog_note"] = (
            (
                f"【/audit 保证档违约警告】关闭时还有 {backlog} 条已入队候选没有逐条结论——"
                "保证档契约是一条不漏,现在 close 就是弃判这批(覆盖回执 pending 不会归零,"
                "用户凭证不成立)。先继续 pull+verdict 清完待判再 close;确因任务终止而弃,"
                "把这个数字如实上报给用户,别说'盯完了'。"
            )
            if state.audit_guarantee
            else (
                f"关闭时 spool 还有 {backlog} 条已初筛抬升、未确认判完的候选(没人取的积压,或你/前任"
                "刚取走还没用下一次 pull 确认判完的在途批)——它们是盯守期内的事件,现在关闭即弃判。"
                "要盯完整就先继续 pull:有积压会交给你判,刚判完的批会被确认清账;清零后再 close"
                "(游标已持久化,重新 open 可续)。"
            )
        )
    return payload


def _unjudged_spool_backlog(state: WatchState) -> int:
    """已抬进 spool 而未【确认判完】的候选数:未读 + 在途(交付出去没被确认判完)。
    与 wake_backstop.lane_unjudged_backlog 同一把尺(单消费者:一路一份读游标 ack 口径)。"""
    from .harvester import consumed_and_acked_on_disk

    written = int(state.totals.get("spool_candidates", 0) or 0)
    if written <= 0:
        return 0
    _consumed, acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    return max(0, written - acked)


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
