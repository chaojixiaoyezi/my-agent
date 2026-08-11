"""watch_stream 工具:高吞吐数据流的盯守摄取入口(open/pull/status/close/list)。

pull 是长轮询:块内持续「拉流→喂引擎」直到出现候选或等待额度用完——游标始终追平
流末尾,模型轮只在有东西可判时消耗。出站走与 web_fetch 同一道网络安全闸
(allowed_private_hosts 由调用层每次热注入,owner 授权即时生效)。
"""

from __future__ import annotations

import json
import math
import re
import time
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..tooling.models import (
    BaseTool,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from ..tooling.web import (
    _default_network_resolver,
    _format_http_error,
    _network_safety_error,
)
from ..tooling.web_fetch_runtime import FetchRawRequest, PinResult, fetch_raw_response
from .config import tuning_from_params
from .puller import DrainBudget
from .source_http import (
    SourceHttpRequest,
    normalize_source_http_request,
    normalize_top_level_response_field,
    public_source_envelope,
    render_source_http_request,
)
from .watch_learn import configure_spec, sample_source
from .watch_payloads import (
    AUDIT_PULL_GUIDANCE,
    PULL_GUIDANCE,
    attach_audit_receipt,
    attach_content_rules_count,
    attach_frequent_hit_alert,
    attach_judgment_note,
    attach_overload_note,
    attach_source_progress,
    build_audit_record,
    candidate_model_view,
    content_rules_block,
    coverage_block,
    merge_frequent_hits,
    order_candidate_rows,
    render_open_payload,
    render_pull_payload,
    source_binding_block,
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
    source_id_for,
    watch_id_for,
)
from .watch_tool_spec import (
    build_watch_stream_model_spec,
    build_watch_stream_runtime_policy,
)

_TOOL_NAME = "watch_stream"
_HTTP_TIMEOUT_SECONDS = 15
_AUDIT_DEFAULT_BATCH_WAIT_SECONDS = 15.0
# Before a source has one successful delivery, reserve enough output for the
# opaque per-row token, classification, score and a useful reason.  Afterwards
# the durable cursor derives this estimate from the model's actual serialized
# verdict rows.  This is a transport budget only: it never interprets a score,
# note, field name or business conclusion.
_AUDIT_INITIAL_VERDICT_OUTPUT_TOKENS_PER_RECORD = 64
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_AUDIT_SOURCE_REF_RE = re.compile(
    r"^audit://(?P<watch_id>ws-[0-9a-f]{10})/candidate/(?P<ack_id>[0-9]+:[0-9]+)$"
)
_UNRESOLVED_WATCH_RESOURCE_SCOPE = "logical:watch:unresolved"
_ACTION_PARAMETER_NAMES: dict[str, frozenset[str]] = {
    "open": frozenset(
        {
            "url",
            "mode",
            "http_request",
            "source_adapter",
            "record_list_field",
            "cursor_field",
            "cursor_semantics",
            "cursor_position_semantics",
            "has_more_field",
            "record_boundary",
            "source_id",
            "source_profile_ref",
            "document_refs",
            "watch_window_seconds",
            "poll_query_seconds",
            "rare_threshold",
            "max_candidates_per_pull",
            "guarantee_batch_max_tokens",
            "full_read_per_pull",
        }
    ),
    "sample": frozenset({"url", "watch_id", "sample_count"}),
    "configure": frozenset({"url", "watch_id", "spec", "judgment_note"}),
    "pull": frozenset({"url", "watch_id", "max_wait_seconds", "target_records"}),
    "verdict": frozenset({"url", "watch_id", "delivery_ref", "verdicts"}),
    "inspect": frozenset({"watch_id", "ack_id", "source_ref"}),
    "status": frozenset({"url", "watch_id"}),
    "close": frozenset({"url", "watch_id"}),
    "list": frozenset(),
}


def _action_parameter_error(action: str, params: dict[str, Any]) -> str:
    """Reject fields belonging to another action before any handler runs.

    Native providers do not consistently support a top-level ``oneOf`` tool
    schema.  The public schema therefore stays flat, while this mechanical
    discriminator enforces the same action-specific shape at the single
    execution choke point.  It never inspects source data or user prose.
    """

    allowed = _ACTION_PARAMETER_NAMES.get(action)
    if allowed is None:
        return ""
    # Registry injects only these two host-owned fields after the public Schema
    # and runtime gate have already accepted the call.  They are execution
    # identity, not action parameters.  Keep the allowlist exact so arbitrary
    # model fields still fail closed.
    metadata_fields = {
        "action",
        "tool",
        "call_id",
        "__run_scope",
        "__tool_call_id",
    }
    extras = sorted(
        str(name) for name in params if name not in allowed and name not in metadata_fields
    )
    if not extras:
        return ""
    rendered = ", ".join(extras)
    return f"action={action} 不接受这些参数：{rendered}；请只提交该 action 的参数"


class WatchStreamTool(BaseTool):
    # 类用途: 把摄取层(结构化预聚合/初筛/背压)暴露成模型工具;每路数据流一个 watch,
    #   游标+引擎状态跨轮持久(进程内注册表+owner 盘上快照),重启从游标续。
    def __init__(self, agent: object) -> None:
        self.agent = agent
        surfaces = (
            "ordinary",
            "audit_prepare",
            "audit_binding",
            "audit_source",
            "audit_coordinator",
        )
        self._model_specs: dict[str, ToolModelSpec] = {
            surface: build_watch_stream_model_spec(surface=surface)
            for surface in surfaces
        }
        self._runtime_policies: dict[str, ToolRuntimePolicy] = {
            surface: build_watch_stream_runtime_policy(surface=surface)
            for surface in surfaces
        }
        self.allowed_private_hosts: tuple[str, ...] = ()
        self.allow_private_resolution: bool | None = None

    def _current_surface(self) -> str:
        from ..common.audit_activation import (
            attributes_request_audit,
            current_audit_attributes,
            structured_audit_source_binding_attributes,
            structured_audit_source_worker_attributes,
        )
        from ..conversation.authority import (
            CONVERSATION_AUDIT_PREPARE_ATTR,
            CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
        )

        attrs = current_audit_attributes(self.agent)
        if structured_audit_source_worker_attributes(attrs):
            return "audit_source"
        if structured_audit_source_binding_attributes(attrs):
            return "audit_binding"
        if attributes_request_audit(attrs):
            return "audit_coordinator"
        if (
            isinstance(attrs, dict)
            and attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is True
            and attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is True
        ):
            return "audit_prepare"
        return "ordinary"

    @property
    def model_spec(self) -> ToolModelSpec:
        """Expose only actions authorized by the current typed runtime role."""

        return self._model_specs[self._current_surface()]

    @property
    def runtime_policy(self) -> ToolRuntimePolicy:
        """Return the policy paired with the same typed surface as model_spec."""

        return self._runtime_policies[self._current_surface()]

    def availability(self) -> ToolAvailability:
        """The role-specific schema narrows actions; the read surface stays usable."""

        return ToolAvailability.ready()

    def effective_resource_scopes(
        self,
        arguments: dict[str, Any],
        write_boundary: dict[str, Any] | None,
        workspace_root: Path,
    ) -> tuple[str, ...]:
        """Alias every watch handle to the durable ``watch_id`` before claim.

        ``ResourceScopePolicy.resource_domains`` only aliases parameter names;
        it cannot know that a URL and the returned ``watch_id`` name the same
        persisted watch.  The executor calls this hook after trusted parameter
        completion and before the mutating operation is claimed, so this is the
        single safe seam for adding the canonical identity shared with
        ``record_finding(watch_id=...)``.
        """

        _ = (write_boundary, workspace_root)
        owner_home = self._owner_home()
        if owner_home is None:
            return (_UNRESOLVED_WATCH_RESOURCE_SCOPE,)
        return _effective_watch_resource_scopes(self, owner_home, arguments)

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        owner_home = self._owner_home()
        if owner_home is None:
            return _err("无 owner home 上下文,盯守状态存储不可用", "TOOL_UNAVAILABLE")
        action = str(params.get("action") or "").strip().lower()
        if not action:
            return _err(
                "action 必填，须明确选择 open/sample/configure/pull/verdict/inspect/status/close/list；"
                "系统不会把缺失动作猜成 pull",
                "TOOL_INVALID_ARGUMENTS",
            )
        binding_conflict = _pending_audit_source_binding_conflict(
            self.agent,
            action,
            params,
        )
        if binding_conflict:
            return _err(
                "来源子代理提交的 open 参数与当前 Audit 已发布的精确来源绑定冲突: "
                + ",".join(binding_conflict),
                "TOOL_INVALID_ARGUMENTS",
                reported_code="AUDIT_SOURCE_BINDING_CONFLICT",
            )
        handler = {
            "open": self._open,
            "sample": self._sample,
            "configure": self._configure,
            "pull": self._pull,
            "verdict": self._verdict,
            "inspect": self._inspect,
            "status": self._status,
            "close": self._close,
            "list": self._list,
        }.get(action)
        if handler is None:
            return _err(
                "action 须为 open/sample/configure/pull/verdict/inspect/status/close/list",
                "TOOL_INVALID_ARGUMENTS",
            )
        parameter_error = _action_parameter_error(action, params)
        if parameter_error:
            return _err(
                parameter_error,
                "TOOL_INVALID_ARGUMENTS",
                reported_code="TOOL_ACTION_PARAMETER_MISMATCH",
            )
        if _source_worker_forbidden_action(self.agent, action):
            from ..common.audit_activation import (
                current_audit_attributes,
                structured_audit_source_binding_attributes,
                structured_audit_source_worker_attributes,
            )

            attrs = current_audit_attributes(self.agent)
            if not structured_audit_source_binding_attributes(
                attrs
            ) and not structured_audit_source_worker_attributes(attrs):
                return _err(
                    "Audit 协调代理不直接打开、采集或签收原始来源；"
                    "请为每个现场已确认的来源创建一个独立叶子子代理，由该子代理打开恰好一条来源。"
                    "协调代理仍可 list/status/inspect/close。",
                    "TOOL_PERMISSION_DENIED",
                    reported_code="AUDIT_COORDINATOR_SOURCE_ACTION_FORBIDDEN",
                )
            return _err(
                "来源子代理只能执行当前结构化阶段允许的 watch 动作："
                "绑定前只能打开/查看来源，绑定后只能消费、签收和只读检查自己的 watch；"
                "配置和关闭由 Audit 协调层负责。",
                "TOOL_PERMISSION_DENIED",
            )
        return handler(owner_home, params)

    def _open(self, owner_home: Path, params: dict[str, Any]) -> ToolHandlerOutcome:
        context = _resolve_open_watch_context(self, owner_home, params)
        if isinstance(context, ToolHandlerOutcome):
            return context
        committed = _commit_open_watch(self, context, params)
        if isinstance(committed, ToolHandlerOutcome):
            return committed
        payload, runtime_transition = committed
        return _ok_payload(payload, runtime_transition=runtime_transition)

    def _resolve_open_source(
        self, params: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any], dict[str, str]] | ToolHandlerOutcome:
        return _resolve_open_source(self, params)

    def _sample(self, owner_home: Path, params: dict[str, Any]) -> ToolHandlerOutcome:
        """抓原始样本+字段分布，不动盯守游标/引擎。"""
        state = _state_for(self, owner_home, params)
        if isinstance(state, ToolHandlerOutcome):
            return state
        return sample_source(self._fetch_json, state, params)

    def _configure(self, owner_home: Path, params: dict[str, Any]) -> ToolHandlerOutcome:
        """普通 watch 可配置筛选；Audit 的业务要求只保存在命名任务目标中。"""
        state = _state_for(self, owner_home, params)
        if isinstance(state, ToolHandlerOutcome):
            return state
        if state.audit_guarantee:
            return _err(
                "/audit 不接受来源判据配置；判断标准和评分要求属于本次 Audit 任务目标，"
                "采集层只保存地址、游标和记录边界等结构事实。",
                "TOOL_INVALID_ARGUMENTS",
            )
        return configure_spec(state, params)

    def _pull(self, owner_home: Path, params: dict[str, Any]) -> ToolHandlerOutcome:
        state = _state_for(self, owner_home, params)
        if isinstance(state, ToolHandlerOutcome):
            return state
        candidate_limit = _pull_target_records(params)
        if isinstance(candidate_limit, ToolHandlerOutcome):
            return candidate_limit
        source_authority: dict[str, object] | None = None
        if state.audit_guarantee:
            source_authority, authorization_error = _source_worker_authority(
                self.agent,
                state,
                require_current_config=True,
            )
            if authorization_error is not None:
                return authorization_error
        max_wait = _resolved_pull_wait(state, params)
        consumer = self._current_run_id()
        if state.audit_guarantee or int(state.tuning.background_harvest or 0):
            # 保证档强制 spool 消费路(background_harvest=0 调参也不放行 inline):
            # inline 路事件不入 durable 队列、没有逐条签收对账,违反 /audit 契约。
            harvested = _pull_from_spool(
                self,
                state,
                max_wait,
                consumer,
                candidate_limit=candidate_limit,
                source_authority=source_authority,
            )
            if harvested is not None:
                return harvested
        with state.lock:
            return self._pull_locked(state, max_wait)

    def _pull_locked(self, state: WatchState, max_wait: float) -> ToolHandlerOutcome:
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
        from .sources import (
            apply_cursor_page_feedback,
            drain_watch_source,
            persist_file_fragment,
        )
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
        apply_cursor_page_feedback(state, drain)
        if not drain.error and not persist_file_fragment(state, drain):
            drain.error = "未完成记录片段无法持久化，未推进游标"
            drain.error_code = "SOURCE_FRAGMENT_PERSIST_FAILED"
        state.totals["pulls"] += 1
        if drain.error:
            state.totals["http_errors"] += 1
            state.last_error = drain.error
            state.last_error_code = drain.error_code or "NETWORK_REQUEST_FAILED"
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

    def _verdict(self, owner_home: Path, params: dict[str, Any]) -> ToolHandlerOutcome:
        """/audit 保证档的逐条结论签收:判读工把手里批的逐条结论交回来销账(ack-on-judge)。
        非保证档调用如实拒绝(那条路是 ack-on-next-pull,没有欠账可销)。"""
        state = _state_for(self, owner_home, params)
        if isinstance(state, ToolHandlerOutcome):
            return state
        if not state.audit_guarantee:
            return _err(
                "本路不是 /audit 保证档:结论签收仅保证档适用(非保证档下一次 pull 即确认)",
                "TOOL_INVALID_ARGUMENTS",
            )
        request = _verdict_tool_request(params)
        if isinstance(request, ToolHandlerOutcome):
            return request
        # Initial settlement and later review are the same source-worker
        # authority surface.  A replaced/stale attempt may inspect history but
        # must never append a newer projection after its lease has been fenced.
        source_authority, authorization_error = _source_worker_authority(
            self.agent,
            state,
        )
        if authorization_error is not None:
            return authorization_error
        run_id = self._current_run_id()
        bound = _bound_runtime_verdicts(self, state, request, run_id)
        if isinstance(bound, ToolHandlerOutcome):
            return bound
        verdicts, output_observation = bound
        from .harvester import submit_verdicts

        result = submit_verdicts(
            state,
            consumer=run_id,
            verdicts=verdicts,
            delivery_ref=request.delivery_ref or None,
            source_authority=source_authority,
            **output_observation,
        )
        return _render_verdict_tool_result(
            self,
            state,
            request,
            verdicts,
            result,
            run_id=run_id,
        )

    def _status(self, owner_home: Path, params: dict[str, Any]) -> ToolHandlerOutcome:
        state = _state_for(self, owner_home, params)
        if isinstance(state, ToolHandlerOutcome):
            return state
        with state.lock:
            refresh_scalars_from_disk(state)
        return _ok_payload(_status_payload(state, agent=self.agent))

    def _inspect(self, owner_home: Path, params: dict[str, Any]) -> ToolHandlerOutcome:
        """按 ack_id 只读找回 /audit 原始日志和逐条结论，不推进游标、不改变任务状态。"""
        inspect_params = dict(params)
        source_ref = str(inspect_params.get("source_ref") or "").strip()
        if source_ref:
            matched = _AUDIT_SOURCE_REF_RE.fullmatch(source_ref)
            if matched is None:
                return _err(
                    "inspect 的 source_ref 不是有效 Audit 记录引用",
                    "TOOL_INVALID_ARGUMENTS",
                )
            ref_watch_id = matched.group("watch_id")
            ref_ack_id = matched.group("ack_id")
            explicit_watch_id = str(inspect_params.get("watch_id") or "").strip()
            explicit_ack_id = str(inspect_params.get("ack_id") or "").strip()
            if (
                explicit_watch_id
                and explicit_watch_id != ref_watch_id
                or explicit_ack_id
                and explicit_ack_id != ref_ack_id
            ):
                return _err(
                    "inspect 的 source_ref 与显式 watch_id/ack_id 不一致",
                    "TOOL_INVALID_ARGUMENTS",
                    reported_code="AUDIT_SOURCE_REF_MISMATCH",
                )
            inspect_params["watch_id"] = ref_watch_id
            inspect_params["ack_id"] = ref_ack_id
        state = _state_for(self, owner_home, inspect_params)
        if isinstance(state, ToolHandlerOutcome):
            return state
        if not state.audit_guarantee:
            return _err(
                "本路不是 /audit 保证档，没有逐条审计账可查",
                "TOOL_INVALID_ARGUMENTS",
            )
        ack_id = str(inspect_params.get("ack_id") or "").strip()
        if not ack_id:
            return _err("inspect 缺 ack_id", "TOOL_PARAMETER_REQUIRED")
        from .harvester import inspect_audit_record

        payload = inspect_audit_record(state, ack_id)
        return (
            _ok_payload({"action": "inspect", **payload})
            if payload.get("ok")
            else _err(str(payload.get("error") or "审计记录不存在"), "TOOL_INVALID_ARGUMENTS")
        )

    def _close(self, owner_home: Path, params: dict[str, Any]) -> ToolHandlerOutcome:
        state = _state_for(self, owner_home, params)
        if isinstance(state, ToolHandlerOutcome):
            return state
        if state.audit_guarantee:
            # A model-authored close must not terminate a named Audit or discard
            # its drain backlog.  The exact host command owns that lifecycle and
            # reaches close_audit_watches_for_task without going through this
            # generic tool action.
            return _err(
                "命名 Audit 只能通过精确的 /audit <name> clear 生命周期停止；"
                "watch_stream close 不会关闭它，也不会丢弃待判记录。",
                "TOOL_PERMISSION_DENIED",
                reported_code="AUDIT_NAMED_CLEAR_REQUIRED",
            )
        from .watch_state import close_watch_state

        close_watch_state(state, reason="watch_tool_close")
        return _ok_payload(_close_payload(state))

    def _list(self, owner_home: Path, _params: dict[str, Any]) -> ToolHandlerOutcome:
        rows = [
            _enriched_list_row(owner_home, row, agent=self.agent)
            for row in _visible_state_rows(self, owner_home)
        ]
        return _ok_payload(
            {
                "ok": True,
                "action": "list",
                "watches": rows,
                "count": len(rows),
                "scope": _watch_list_scope(self),
            }
        )

    def _fetch_json(self, request: SourceHttpRequest) -> tuple[bool, object, str]:
        return self._fetch_json_pinned(
            request,
            tuple(self.allowed_private_hosts or ()),
            self.allow_private_resolution,
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

        def _fetch(request: SourceHttpRequest) -> tuple[bool, object, str]:
            return self._fetch_json_pinned(request, hosts, allow_resolution)

        return _fetch

    def _fetch_json_pinned(
        self,
        request: SourceHttpRequest,
        hosts: tuple[str, ...],
        allow_resolution: bool | None,
    ) -> tuple[bool, object, str]:
        response = fetch_raw_response(
            FetchRawRequest(
                tool=_TOOL_NAME,
                url=request.url,
                method=request.method,
                headers=dict(request.headers),
                data=request.data,
                timeout=_HTTP_TIMEOUT_SECONDS,
            ),
            format_http_error=_format_http_error,
            resolve_pin=lambda pin_url: self._resolve_pin_with(pin_url, hosts, allow_resolution),
        )
        if isinstance(response, ToolHandlerOutcome):
            return False, response.output, response.error_code
        try:
            return True, json.loads(response.body.decode("utf-8", "replace")), ""
        except json.JSONDecodeError as exc:
            return False, f"响应不是 JSON: {exc}", "SOURCE_ENVELOPE_INVALID"

    def _resolve_pin_with(
        self, url: str, hosts: tuple[str, ...], allow_resolution: bool | None
    ) -> PinResult:
        """与 web_fetch 同款:解析一次→过网络安全闸→pin 校验过的 IP(重定向逐跳重查)。"""
        try:
            resolved = tuple(
                str(ip) for ip in _default_network_resolver(urlsplit(url).hostname or "")
            )
        except Exception:
            resolved = ()
        err = _network_safety_error(_TOOL_NAME, url, lambda _h: resolved, hosts, allow_resolution)
        if err is not None:
            return PinResult(None, err)
        return PinResult(resolved[0] if resolved else None, None)

    def _owner_home(self) -> Path | None:
        raw = str(
            getattr(getattr(self.agent, "home_paths", None), "owner_home_dir", "") or ""
        ).strip()
        return Path(raw) if raw else None

    def _current_run_id(self) -> str:
        return _current_run_id(self.agent)


@dataclass(frozen=True)
class _OpenWatchContext:
    state: WatchState
    resumed: bool
    audit_root_task_id: str
    same_audit_task: bool


def _resolve_open_watch_context(
    tool: WatchStreamTool,
    owner_home: Path,
    params: dict[str, Any],
) -> _OpenWatchContext | ToolHandlerOutcome:
    identity_error = _audit_prepare_open_identity_error(tool.agent, params)
    if identity_error is not None:
        return identity_error
    resolved = _resolve_open_source(tool, params)
    if isinstance(resolved, ToolHandlerOutcome):
        return resolved
    url, mode, request_facts, adapter_facts = resolved
    watch_identity = _watch_id_for_resolved_source(
        tool,
        owner_home,
        params,
        url=url,
        mode=mode,
        request_facts=request_facts,
        adapter_facts=adapter_facts,
    )
    if isinstance(watch_identity, ToolHandlerOutcome):
        return watch_identity
    watch_id, audit_id, prepare_id = watch_identity
    loaded = _load_open_watch(
        owner_home,
        url,
        params,
        watch_id=watch_id,
        source_mode=mode,
        request_facts=request_facts,
        adapter_facts=adapter_facts,
        prepare_id=prepare_id,
    )
    if isinstance(loaded, ToolHandlerOutcome):
        return loaded
    state, resumed = loaded
    prepare_error = _prepare_open_watch(
        tool,
        state,
        params,
        request_facts,
        adapter_facts,
        prepare_id,
        probe_audit_id=audit_id or prepare_id,
    )
    if prepare_error is not None:
        _drop_new_open_watch(state, resumed)
        return prepare_error
    current_epoch = _audit_run_epoch(tool.agent)
    same_audit_task = bool(
        resumed
        and audit_id
        and state.audit_guarantee
        and state.audit_root_task_id == audit_id
        and int(state.audit_run_epoch or 0) == current_epoch
        and not state.closed
    )
    return _OpenWatchContext(
        state=state,
        resumed=resumed,
        audit_root_task_id=audit_id,
        same_audit_task=same_audit_task,
    )


def _load_open_watch(
    owner_home: Path,
    url: str,
    params: dict[str, Any],
    *,
    watch_id: str,
    source_mode: str,
    request_facts: dict[str, Any],
    adapter_facts: dict[str, str],
    prepare_id: str,
) -> tuple[WatchState, bool] | ToolHandlerOutcome:
    state = registry.get_or_load(owner_home, watch_id)
    resumed = state is not None
    if state is None:
        state = new_state(owner_home, url, params, watch_id=watch_id)
        state.source_mode = source_mode
        if request_facts:
            state.source_envelope = {"request": request_facts}
        if adapter_facts:
            state.source_envelope["adapter"] = adapter_facts
        registry.put(state)
        return state, False
    with state.lock:
        refresh_scalars_from_disk(state)
    if str(state.source_mode or "") != str(source_mode or ""):
        return _err(
            "这个 watch 已钉住另一种来源推进方式；请在准确命名 Audit 的 prepare 中发布新绑定。",
            "TOOL_INVALID_ARGUMENTS",
            reported_code="AUDIT_SOURCE_BINDING_IMMUTABLE",
        )
    existing_request = state.source_envelope.get("request")
    if request_facts and existing_request != request_facts:
        return _err(
            "这个 watch 已钉住另一份现场验证请求；请停止后创建新的命名 Audit。",
            "TOOL_INVALID_ARGUMENTS",
            reported_code="AUDIT_SOURCE_BINDING_IMMUTABLE",
        )
    existing_adapter = state.source_envelope.get("adapter")
    if adapter_facts and existing_adapter != adapter_facts:
        return _err(
            "这个 watch 已钉住另一版来源适配器；请在准确命名 Audit 的 prepare 中发布新版本。",
            "TOOL_INVALID_ARGUMENTS",
            reported_code="AUDIT_SOURCE_BINDING_IMMUTABLE",
        )
    return state, resumed


def _prepare_open_watch(
    tool: WatchStreamTool,
    state: WatchState,
    params: dict[str, Any],
    request_facts: dict[str, Any],
    adapter_facts: dict[str, str],
    prepare_id: str,
    *,
    probe_audit_id: str,
) -> ToolHandlerOutcome | None:
    if not state.owner_id:
        state.owner_id = str(
            getattr(getattr(tool.agent, "home_paths", None), "owner_id", "") or ""
        ).strip()
    if prepare_id and not state.audit_guarantee:
        if state.prepare_root_task_id and state.prepare_root_task_id != prepare_id:
            return _err(
                "这个来源探针属于另一条命名 Audit，当前准备轮不能复用。",
                "TOOL_PERMISSION_DENIED",
                reported_code="AUDIT_SOURCE_PROBE_SCOPE_MISMATCH",
            )
        state.prepare_root_task_id = prepare_id
    boundary_error = _apply_file_record_boundary(state, params)
    if boundary_error is not None:
        return boundary_error
    if _is_structural_source_envelope(state.source_envelope):
        return None
    envelope = _probe_envelope_for(
        tool._fetch_json,
        state,
        params,
        request_facts=request_facts,
        adapter_facts=adapter_facts,
        audit_id=probe_audit_id,
    )
    probe_error_code = str(envelope.get("probe_error_code") or "").strip().upper()
    if probe_error_code:
        probe_error = str(
            envelope.get("probe_error")
            or envelope.get("invalid_reason")
            or "数据源探针失败"
        )
        control_code, reported_code = _source_tool_error_codes(probe_error_code)
        return _err(
            probe_error,
            control_code,
            reported_code=reported_code,
        )
    if not _is_structural_source_envelope(envelope):
        return _err(_source_envelope_error(envelope), "SOURCE_ENVELOPE_INVALID")
    state.source_envelope = envelope
    if envelope.get("mode") in {"cursor", "poll", "adapter"}:
        state.source_mode = "" if envelope["mode"] == "cursor" else str(envelope["mode"])
    return None


def _drop_new_open_watch(state: WatchState, resumed: bool) -> None:
    if not resumed:
        registry.drop(state.watch_id)


def _commit_open_watch(
    tool: WatchStreamTool,
    context: _OpenWatchContext,
    params: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, object] | None] | ToolHandlerOutcome:
    state = context.state
    with _audit_open_transition_guard(tool.agent, context.audit_root_task_id):
        if state.audit_guarantee or context.audit_root_task_id:
            parent_error = _audit_parent_open_error(tool.agent, context.audit_root_task_id)
            if parent_error is not None:
                _drop_new_open_watch(state, context.resumed)
                return parent_error
        commit_error = _persist_open_watch(tool, context, params)
        if commit_error is not None:
            _drop_new_open_watch(state, context.resumed)
            return commit_error
        _start_open_watch_harvester(tool, state)
        return _open_watch_payload(tool, context)


def _persist_open_watch(
    tool: WatchStreamTool,
    context: _OpenWatchContext,
    params: dict[str, Any],
) -> ToolHandlerOutcome | None:
    state = context.state
    with state.lock:
        refresh_scalars_from_disk(state)
        _apply_audit_guarantee(tool, state, params)
        previous_run_epoch = max(0, int(state.audit_run_epoch or 0))
        context_error = _apply_audit_context(tool, state)
        if context_error is not None:
            return context_error
        run_rollover = int(state.audit_run_epoch or 0) > previous_run_epoch
        if not context.same_audit_task:
            _override_watch_window_from_audit_command(tool, state, params)
            may_reopen = _apply_open_overrides(
                state,
                params,
                allow_terminal_reopen=run_rollover,
            )
        else:
            may_reopen = False
        binding_error = _pin_source_binding(state, params, resumed=context.resumed)
        if binding_error is not None:
            return binding_error
        if not context.same_audit_task and may_reopen:
            reopen_on_disk(state)
        persist_state(state)
    return None


def _start_open_watch_harvester(tool: WatchStreamTool, state: WatchState) -> None:
    if not (
        state.audit_guarantee or int(state.tuning.background_harvest or 0)
    ) or not _collection_window_is_open(state):
        return
    from .harvester import ensure_harvester

    ensure_harvester(
        state,
        tool._harvester_fetch(),
        on_records_ready=_audit_source_ready_callback(tool, state),
        on_window_finalized=_audit_source_finalized_callback(tool, state),
    )


def _open_watch_payload(
    tool: WatchStreamTool,
    context: _OpenWatchContext,
) -> tuple[dict[str, Any], dict[str, object] | None]:
    state = context.state
    payload = render_open_payload(
        state,
        context.resumed,
        unjudged_backlog=_unjudged_spool_backlog(state),
    )
    attach_audit_receipt(
        payload,
        state,
        include_objective=not _source_scoped_audit_actor(tool.agent),
    )
    if not state.audit_guarantee:
        return payload, None
    from .source_worker import ensure_audit_source_worker

    source_worker = ensure_audit_source_worker(tool.agent, state)
    payload.update(
        {
            "source_worker": source_worker,
            "audit_guarantee": True,
            "batch_context": _audit_batch_context_estimate(state, tool.agent),
            "audit_consumer_contract": {
                "watch_id": state.watch_id,
                "delivery": "durable_at_least_once",
                "settlement": "explicit_verdict_per_ack",
            },
            "audit_note": (
                "/audit 保证档已生效并随 watch 持久化：完整记录先落 durable 队列，"
                "每条通过 ack_id/source_ref 对账；未提交结论的记录保持 pending 并可重投。"
                "coverage.audit_receipt 是覆盖事实。如何分析、是否委派以及如何汇报由当前 Agent"
                "依据用户目标和可用工具决定。"
            ),
        }
    )
    transition = (
        _context_refresh_transition("durable_tool_scope_changed")
        if source_worker.get("context_refresh_required") is True
        else None
    )
    return payload, transition


def _watch_id_for_resolved_source(
    tool: WatchStreamTool,
    owner_home: Path,
    params: dict[str, Any],
    *,
    url: str,
    mode: str,
    request_facts: dict[str, Any],
    adapter_facts: dict[str, str],
) -> tuple[str, str, str] | ToolHandlerOutcome:
    """Derive the exact durable identity used by ``open`` from typed facts."""

    audit_id = _audit_root_task_id(tool.agent) if _audit_requested(tool) else ""
    prepare_id = _audit_prepare_root_task_id(tool.agent)
    watch_scope = _audit_watch_scope_id(tool.agent, audit_id)
    if prepare_id:
        from .source_binding import audit_prepare_probe_scope_id

        prepare_request_id = _audit_prepare_request_id(tool.agent)
        raw_boundary = params.get("record_boundary")
        record_boundary = (
            raw_boundary
            if isinstance(raw_boundary, dict)
            else {"mode": "line"} if url.lower().startswith("file://") else None
        )
        try:
            watch_scope = audit_prepare_probe_scope_id(
                task_id=prepare_id,
                prepare_request_id=prepare_request_id,
                source_id=params.get("source_id"),
                source_mode=mode,
                request_facts=request_facts,
                adapter_facts=adapter_facts,
                record_boundary=record_boundary,
                poll_query_seconds=tuning_from_params(params).poll_query_seconds,
            )
        except (TypeError, ValueError) as exc:
            return _err(
                f"Audit 来源探针身份无效: {exc}",
                "TOOL_INVALID_ARGUMENTS",
                reported_code="AUDIT_SOURCE_IDENTITY_REQUIRED",
            )
    return watch_id_for(owner_home, url, watch_scope), audit_id, prepare_id


def _visible_watch_ids_for_alias(
    tool: WatchStreamTool,
    owner_home: Path,
    *,
    source_url: str = "",
    source_id: str = "",
) -> tuple[str, ...]:
    """Resolve persisted aliases only inside the caller's typed watch scope."""

    selected_url = str(source_url or "").strip()
    selected_source = str(source_id or "").strip()
    matches: list[str] = []
    for row in _visible_state_rows(tool, owner_home):
        if selected_url and str(row.get("source_url") or "").strip() != selected_url:
            continue
        if selected_source and str(row.get("source_id") or "").strip() != selected_source:
            continue
        watch_id = str(row.get("watch_id") or "").strip()
        if watch_id:
            matches.append(watch_id)
    return tuple(dict.fromkeys(matches))


def _effective_watch_resource_scopes(
    tool: WatchStreamTool,
    owner_home: Path,
    params: dict[str, Any],
) -> tuple[str, ...]:
    """Project URL/ref/source aliases onto the handler's durable watch id."""

    explicit_watch_id = str(params.get("watch_id") or "").strip()
    if explicit_watch_id:
        return (f"logical:watch:{explicit_watch_id}",)

    source_ref = str(params.get("source_ref") or "").strip()
    if source_ref:
        matched = _AUDIT_SOURCE_REF_RE.fullmatch(source_ref)
        if matched is not None:
            return (f"logical:watch:{matched.group('watch_id')}",)

    normalized = _normalize_open_source(params) if params.get("url") else None
    if isinstance(normalized, tuple):
        url, mode, request_facts, adapter_facts = normalized
        identity = _watch_id_for_resolved_source(
            tool,
            owner_home,
            params,
            url=url,
            mode=mode,
            request_facts=request_facts,
            adapter_facts=adapter_facts,
        )
        if isinstance(identity, tuple):
            return (f"logical:watch:{identity[0]}",)
        persisted = _visible_watch_ids_for_alias(
            tool,
            owner_home,
            source_url=url,
            source_id=str(params.get("source_id") or ""),
        )
        if persisted:
            return tuple(f"logical:watch:{watch_id}" for watch_id in persisted)

    source_id = str(params.get("source_id") or "").strip()
    if source_id:
        persisted = _visible_watch_ids_for_alias(
            tool,
            owner_home,
            source_id=source_id,
        )
        if persisted:
            return tuple(f"logical:watch:{watch_id}" for watch_id in persisted)

    # Invalid/missing handles cannot legitimately reach a mutating handler, but
    # the operation path still receives a stable conservative scope rather than
    # silently claiming with no resource protection.
    return (_UNRESOLVED_WATCH_RESOURCE_SCOPE,)


def _normalize_open_source(
    params: dict[str, Any],
) -> tuple[str, str, dict[str, Any], dict[str, str]] | ToolHandlerOutcome:
    """Normalize source identity without filesystem/network authorization I/O."""

    raw_value = params.get("url")
    if isinstance(raw_value, (list, tuple, dict, set)):
        return _err(
            "url 一次只接受单个来源地址；多个来源请分别调用 open，每次传一个 URL。",
            "TOOL_INVALID_ARGUMENTS",
        )
    raw = str(raw_value or "").strip()
    if raw.startswith(("[", "{")):
        return _err(
            "url 收到了列表/对象文本，不是单个来源地址；多个来源请分别调用 open，"
            "每次传一个 URL（可在同一工具轮并列发起）。",
            "TOOL_INVALID_ARGUMENTS",
        )
    mode = str(params.get("mode") or "").strip().lower()
    if mode not in {"", "cursor", "poll", "adapter"}:
        return _err(
            "mode 只支持 cursor、poll 或 adapter；不给时沿用 cursor。",
            "TOOL_INVALID_ARGUMENTS",
        )
    if raw.lower().startswith("file://") or raw.startswith("/"):
        from .sources import normalize_file_url

        if params.get("http_request") is not None or params.get("source_adapter") is not None:
            return _err(
                "文件来源不能配置 http_request 或 source_adapter",
                "TOOL_INVALID_ARGUMENTS",
            )

        try:
            url = normalize_file_url(raw)
        except ValueError as exc:
            return _err(f"url 无效: {exc}", "TOOL_INVALID_ARGUMENTS")
        return url, "", {}, {}
    if params.get("record_boundary") is not None:
        return _err(
            "record_boundary 目前只适用于 file 来源",
            "TOOL_INVALID_ARGUMENTS",
        )
    try:
        from .source_adapter import normalize_source_adapter

        adapter_facts = (
            normalize_source_adapter(params.get("source_adapter"))
            if mode == "adapter"
            else {}
        )
        if mode != "adapter" and params.get("source_adapter") is not None:
            raise ValueError("source_adapter 只适用于 mode=adapter")
        url, request_facts = normalize_source_http_request(
            params.get("url"),
            params.get("http_request"),
            poll=mode in {"poll", "adapter"},
        )
    except ValueError as exc:
        return _err(
            f"HTTP 来源配置无效: {exc}(本地文件源请给 file:///绝对路径)",
            "TOOL_INVALID_ARGUMENTS",
        )
    return url, (mode if mode in {"poll", "adapter"} else ""), request_facts, adapter_facts


def _resolve_open_source(
    tool: WatchStreamTool, params: dict[str, Any]
) -> tuple[str, str, dict[str, Any], dict[str, str]] | ToolHandlerOutcome:
    """Resolve one source without assuming vendor, endpoint, or cursor names."""

    normalized = _normalize_open_source(params)
    if isinstance(normalized, ToolHandlerOutcome):
        return normalized
    url, mode, request_facts, adapter_facts = normalized
    if url.lower().startswith("file://"):
        from .sources import file_path_of

        decision = _file_access_policy(tool).check(file_path_of(url))
        if not decision.allowed:
            return _err(
                f"文件路径访问被拒: {decision.message or decision.code}", "TOOL_PERMISSION_DENIED"
            )
        return normalized
    gate_error = _network_safety_error(
        _TOOL_NAME,
        url,
        _default_network_resolver,
        tool.allowed_private_hosts,
        tool.allow_private_resolution,
    )
    if gate_error is not None:
        return gate_error
    return normalized


def _pending_audit_source_binding_conflict(
    agent: object,
    action: str,
    params: dict[str, Any],
) -> list[str]:
    """Reject a child attempt to rewrite its host-selected source transport."""

    if action != "open":
        return []
    from ..common.audit_activation import (
        AUDIT_SOURCE_OPEN_ATTR,
        AUDIT_SOURCE_OPEN_FIELDS,
        current_audit_attributes,
        structured_audit_source_binding_attributes,
    )

    attrs = current_audit_attributes(agent)
    if not structured_audit_source_binding_attributes(attrs):
        return []
    binding = attrs.get(AUDIT_SOURCE_OPEN_ATTR) if isinstance(attrs, dict) else None
    if not isinstance(binding, dict):
        return []
    return [
        name
        for name in AUDIT_SOURCE_OPEN_FIELDS
        if (name in params or name in binding) and params.get(name) != binding.get(name)
    ]


def _current_run_id(agent: object) -> str:
    """当前消费者 run id；用于 durable inflight 的租约与重投身份。"""
    from ..agent_core.runner.context import current_subagent_run_id

    run_id = current_subagent_run_id(agent)
    if run_id:
        return run_id
    return str(getattr(getattr(agent, "_current_run_params", None), "run_id", "") or "")


# LLM: Audit lineage uses the ingress request id shared by descendants; a child run id is not a root identity.
# 函数用途: 取得本次 Audit 的稳定根编号，让同一网址的不同审计各有自己的游标、账本和停止范围。
def _audit_root_task_id(agent: object) -> str:
    from ..common.audit_activation import audit_lineage_task_id

    return audit_lineage_task_id(agent)


def _audit_watch_scope_id(agent: object, audit_root_task_id: object) -> str:
    """Select the named Audit's stable watch namespace from trusted attrs."""

    from ..common.audit_activation import (
        AUDIT_RUN_EPOCH_ATTR,
        audit_watch_scope_id,
        current_audit_attributes,
    )

    attrs = current_audit_attributes(agent)
    epoch = attrs.get(AUDIT_RUN_EPOCH_ATTR) if isinstance(attrs, dict) else 0
    return audit_watch_scope_id(audit_root_task_id, epoch)


def _audit_run_epoch(agent: object) -> int:
    """Return the exact typed Audit run epoch without inspecting model prose."""

    from ..common.audit_activation import AUDIT_RUN_EPOCH_ATTR

    attrs = _current_audit_attributes(agent)
    try:
        return max(
            0,
            int(attrs.get(AUDIT_RUN_EPOCH_ATTR) or 0)
            if isinstance(attrs, dict)
            else 0,
        )
    except (TypeError, ValueError):
        return 0


def _audit_prepare_root_task_id(agent: object) -> str:
    """Return the exact named Audit id for a typed prepare turn only."""

    from ..conversation.authority import (
        CONVERSATION_AUDIT_PREPARE_ATTR,
        CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
    )

    attrs = _current_audit_attributes(agent)
    if not isinstance(attrs, dict):
        return ""
    if (
        attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is not True
        or attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is not True
    ):
        return ""
    return str(attrs.get("conversation_task_id") or "").strip()


def _audit_prepare_request_id(agent: object) -> str:
    """Return the exact typed request id for the current prepare turn."""

    from ..conversation.authority import CONVERSATION_TURN_REQUEST_ID_ATTR

    attrs = _current_audit_attributes(agent)
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get(CONVERSATION_TURN_REQUEST_ID_ATTR) or "").strip()


# LLM: A prepare probe is publishable evidence, so its stable identity must be
# chosen before the probe is created.  This gate reads only the typed prepare
# scope and exact tool fields; it never infers a source identity from prose or
# endpoint content.
# 函数用途: 防止准备轮先由宿主生成模型未知的 source_id，造成不同来源无法稳定对应工作者。
def _audit_prepare_open_identity_error(
    agent: object,
    params: dict[str, Any],
) -> ToolHandlerOutcome | None:
    if not _audit_prepare_root_task_id(agent):
        return None
    missing: list[str] = []
    if not str(params.get("source_id") or "").strip():
        missing.append("source_id")
    if not missing:
        return None
    return _err(
        "Audit 准备轮的 open 必须先明确稳定 source_id；说明、脚本、测试、Skill 和"
        "文档采用什么形式由当前主代理根据现场学习结果决定。缺少：" + ",".join(missing),
        "TOOL_PARAMETER_REQUIRED",
        reported_code="AUDIT_SOURCE_IDENTITY_REQUIRED",
    )


# LLM: The canonical conversation task transition lock is reused here rather
# than inventing an Audit scheduler lock. Standalone agents have no named-work
# parent and keep the existing unscoped behavior.
# 函数用途: 让来源启用提交与命名 Audit 停止共用同一把跨进程锁。
def _audit_open_transition_guard(agent: object, audit_id: str):
    selected = str(audit_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    guard = getattr(store, "task_transition_guard", None)
    if not selected or store is None or not callable(guard):
        return nullcontext()
    return guard(selected)


# LLM: An Audit source may commit only while its durable named parent remains
# active. The check consumes typed task state and never inspects prompts,
# source names, or model prose.
# 函数用途: 来源正式启用前按会话任务账复核父 Audit；已停止或状态不可读都拒绝启动。
def _audit_parent_open_error(
    agent: object,
    audit_id: str,
) -> ToolHandlerOutcome | None:
    selected = str(audit_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None
    if not selected or not callable(getattr(store, "task_transition_guard", None)):
        return _err(
            "Audit 父任务状态不可用，来源未启动。",
            "AUDIT_PARENT_STATE_UNAVAILABLE",
        )
    from .source_worker import audit_parent_reconcile_state

    active, parent_state = audit_parent_reconcile_state(agent, selected)
    if active:
        return None
    if parent_state == "inactive":
        return _err(
            "Audit 已停止或结束，来源未启动。",
            "AUDIT_PARENT_INACTIVE",
        )
    return _err(
        "Audit 父任务状态不可用，来源未启动。",
        "AUDIT_PARENT_STATE_UNAVAILABLE",
    )


def _file_access_policy(tool: WatchStreamTool):
    """file 源的路径闸:与文件系统工具同源的策略(危险根拒读 + 多用户 owner 墙)。"""
    from ..path_access_policy import PathAccessPolicy

    config = getattr(tool.agent, "config", None)
    return PathAccessPolicy.from_values(
        mode=getattr(config, "path_access_mode", "normal"),
        dangerous_roots=getattr(config, "path_dangerous_roots", None),
        owner_scope_root=str(
            getattr(getattr(tool.agent, "home_paths", None), "owner_home_dir", "") or ""
        ),
    )


def _state_for(
    tool: WatchStreamTool,
    owner_home: Path,
    params: dict[str, Any],
) -> WatchState | ToolHandlerOutcome:
    watch_id = str(params.get("watch_id") or "").strip()
    if not watch_id and params.get("url"):
        resolved = _resolve_open_source(tool, params)
        if isinstance(resolved, ToolHandlerOutcome):
            return resolved
        resolved_url, _mode, _request, _adapter = resolved
        audit_root_task_id = _audit_root_task_id(tool.agent) if _audit_requested(tool) else ""
        prepare_root_task_id = _audit_prepare_root_task_id(tool.agent)
        watch_scope = _audit_watch_scope_id(tool.agent, audit_root_task_id) or (
            f"prepare:{prepare_root_task_id}" if prepare_root_task_id else ""
        )
        watch_id = watch_id_for(owner_home, resolved_url, watch_scope)
    if not watch_id:
        current_audit = _current_visible_audit_task_id(tool.agent)
        if current_audit:
            matching = [
                str(row.get("watch_id") or "").strip()
                for row in list_states(owner_home)
                if bool(row.get("audit_guarantee"))
                and str(row.get("audit_root_task_id") or "").strip() == current_audit
                and str(row.get("watch_id") or "").strip()
            ]
            if len(matching) == 1:
                watch_id = matching[0]
            elif len(matching) > 1:
                return _err(
                    "当前 Audit 有多个数据源，缺 watch_id 时无法唯一选择；"
                    "先 action=list，再传对应 watch_id。",
                    "TOOL_PARAMETER_REQUIRED",
                )
    if not watch_id:
        return _err("缺 watch_id(或给 url);先 action=open 打开盯守", "TOOL_PARAMETER_REQUIRED")
    state = registry.get_or_load(owner_home, watch_id)
    if state is None:
        known = [row["watch_id"] for row in _visible_state_rows(tool, owner_home)]
        return _err(
            f"watch_id 不存在: {watch_id};已有: {known}(先 action=open)", "TOOL_INVALID_ARGUMENTS"
        )
    current_audit = _current_visible_audit_task_id(tool.agent)
    state_audit_scope = str(state.audit_root_task_id or state.prepare_root_task_id or "").strip()
    if current_audit and state.audit_guarantee and not state_audit_scope:
        return _err(
            "这个旧 watch 缺少可核对的命名 Audit 归属，当前任务不能访问。",
            "TOOL_PERMISSION_DENIED",
        )
    if state_audit_scope and (not current_audit or state_audit_scope != current_audit):
        return _err(
            "这个 watch 属于另一条 Audit，当前任务不能消费或修改。",
            "TOOL_PERMISSION_DENIED",
        )
    assigned_watch = _source_worker_watch_id(tool.agent)
    if assigned_watch and state.watch_id != assigned_watch:
        return _err(
            "来源子代理只能访问结构化绑定的那一条 watch。",
            "TOOL_PERMISSION_DENIED",
        )
    return state


# LLM: 会话运行时 carries one TurnContext into both tool exposure and execution, while
# 终端交互 filters the visible tool pool with the same permission context used
# at invocation.  Watch resources need the same invariant: a durable task may
# discover and execute only handles bound to that exact typed task identity.
# 函数用途: 让 list 与 status/pull/verdict/inspect/close 使用同一任务范围，避免模型
# 从 owner 的历史列表看见另一条 Audit 后误选旧 watch。
def _visible_state_rows(
    tool: WatchStreamTool,
    owner_home: Path,
) -> list[dict[str, Any]]:
    rows = list_states(owner_home)
    assigned_watch = _source_worker_watch_id(tool.agent)
    if assigned_watch:
        return [row for row in rows if str(row.get("watch_id") or "").strip() == assigned_watch]
    current_task = _current_visible_audit_task_id(tool.agent)
    if not current_task:
        return [
            row
            for row in rows
            if not bool(row.get("audit_guarantee"))
            and not str(row.get("prepare_root_task_id") or "").strip()
        ]
    exact_task_rows = [
        row
        for row in rows
        if current_task
        in {
            str(row.get("audit_root_task_id") or "").strip(),
            str(row.get("prepare_root_task_id") or "").strip(),
        }
    ]
    return exact_task_rows


# LLM: A scoped empty list is not proof that the owner has no detached named
# work.  会话运行时 builds model-visible tools and execution from one TurnContext;
# 长期助手 applies the same enabled-toolset scope to catalog reads and calls.
# Keep watch handles least-privileged while making the list's observation
# boundary explicit, so a model cannot mistake a local zero for a global zero.
# 函数用途: 返回不含资源标识的结构化列表范围；普通轮次不会泄露命名 Audit 的 watch。
def _watch_list_scope(tool: WatchStreamTool) -> dict[str, object]:
    assigned_watch = _source_worker_watch_id(tool.agent)
    current_task = _current_visible_audit_task_id(tool.agent)
    if assigned_watch:
        kind = "assigned_source"
    elif current_task:
        kind = "current_named_audit"
    else:
        kind = "ordinary_turn"
    return {
        "kind": kind,
        "is_global": False,
        "includes_named_audit_resources": bool(current_task),
        "excluded_resource_kinds": [] if current_task else ["named_audit"],
    }


def _current_visible_audit_task_id(agent: object) -> str:
    """Select an Audit resource scope only from the current typed turn."""

    prepare_task = _audit_prepare_root_task_id(agent)
    if prepare_task:
        return prepare_task
    attrs = _current_audit_attributes(agent)
    if not isinstance(attrs, dict):
        return ""
    from ..common.audit_activation import (
        attributes_request_audit,
        structured_audit_source_binding_attributes,
        structured_audit_source_worker_attributes,
    )

    if not (
        attributes_request_audit(attrs)
        or structured_audit_source_binding_attributes(attrs)
        or structured_audit_source_worker_attributes(attrs)
    ):
        return ""
    return _audit_root_task_id(agent)


# LLM: A source worker's resource scope comes only from runner-local typed
# attributes.  Prompt text and a model-selected watch_id cannot widen it.
# 函数用途: 取得当前来源子代理被绑定的唯一 watch，普通代理返回空。
def _source_worker_watch_id(agent: object) -> str:
    from ..common.audit_activation import (
        AUDIT_SOURCE_WATCH_ID_ATTR,
        AUDIT_SOURCE_WORKER_ATTR,
        current_audit_attributes,
    )

    attrs = current_audit_attributes(agent)
    if not isinstance(attrs, dict) or attrs.get(AUDIT_SOURCE_WORKER_ATTR) is not True:
        return ""
    return str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or "").strip()


# LLM: A bound source worker and its short pre-binding phase are both
# source-scoped.  This shared typed predicate controls payload projection; it
# never infers authority from names, prompts, URLs or model prose.
# 函数用途: 判断当前执行者是否只属于一条 Audit 数据源。
def _source_scoped_audit_actor(agent: object) -> bool:
    from ..common.audit_activation import (
        current_audit_attributes,
        structured_audit_source_binding_attributes,
        structured_audit_source_worker_attributes,
    )

    attrs = current_audit_attributes(agent)
    return structured_audit_source_binding_attributes(
        attrs
    ) or structured_audit_source_worker_attributes(attrs)


# LLM: Lifecycle-mutating watch actions remain with the Audit coordinator; the
# dedicated worker cannot expand or terminate its own authority.
# 函数用途: 阻止来源子代理自行开源、改源或关源，保持用户/Audit 根任务掌握生命周期。
def _source_worker_forbidden_action(agent: object, action: str) -> bool:
    if _source_worker_watch_id(agent):
        return action in {
            "open",
            "sample",
            "configure",
            "close",
        }
    from ..common.audit_activation import (
        attributes_request_audit,
        current_audit_attributes,
        structured_audit_source_binding_attributes,
    )

    attrs = current_audit_attributes(agent)
    if structured_audit_source_binding_attributes(attrs):
        return action not in {"open", "sample", "status", "list"}
    if attributes_request_audit(attrs):
        # The named Audit root stays a coordinator.  Raw collection and first
        # verdicts belong to one typed leaf worker per source; the root may
        # inspect evidence and own lifecycle closure without consuming a source.
        return action not in {"list", "status", "inspect", "close"}
    return False


# LLM: Pull and initial verdict share one typed authority result. The returned
# lease token is carried to the eventual cursor commit, not reduced to a
# one-time boolean at tool entry.
# 函数用途: 获取来源工作者结构化租约凭证，并将拒绝统一转换为 watch_stream 工具错误。
def _source_worker_authority(
    agent: object,
    state: WatchState,
    *,
    require_current_config: bool = False,
) -> tuple[dict[str, object] | None, ToolHandlerOutcome | None]:
    from .source_worker import authorize_source_worker_action

    result = authorize_source_worker_action(
        agent,
        state,
        require_current_config=require_current_config,
    )
    if bool(result.get("ok")):
        return result, None
    reported_code = str(result.get("error_code") or "AUDIT_SOURCE_AUTHORIZATION_FAILED")
    control_code = (
        "TOOL_UNAVAILABLE" if reported_code.endswith("_UNAVAILABLE") else "TOOL_PERMISSION_DENIED"
    )
    return (
        None,
        _err(
            str(result.get("error") or "来源工作者授权失败"),
            control_code,
            reported_code=reported_code,
        ),
    )


def _probe_envelope_for(
    fetch_json,
    state: WatchState,
    params: dict[str, Any],
    *,
    request_facts: dict[str, Any],
    adapter_facts: dict[str, str],
    audit_id: str,
) -> dict[str, Any]:
    """探针只记录采集结构，不搬运响应中的业务标量值。"""
    from .sources import source_kind

    kind = source_kind(state.source_url, state.source_mode)
    if kind == "file":
        return {"mode": "file", "record_boundary": "line", "valid": True}
    if kind == "adapter":
        return _probe_adapter_source_envelope(
            fetch_json,
            state,
            request_facts=request_facts,
            adapter_facts=adapter_facts,
            audit_id=audit_id,
        )
    return _probe_http_source_envelope(
        fetch_json,
        state.source_url,
        params=params,
        requested_poll=kind == "poll",
        request_facts=request_facts,
    )


def _is_structural_source_envelope(envelope: object) -> bool:
    if not isinstance(envelope, dict):
        return False
    if envelope.get("mode") == "file":
        return bool(envelope.get("record_boundary"))
    structurally_valid = bool(
        envelope.get("mode") in {"cursor", "poll", "adapter"}
        and isinstance(envelope.get("request"), dict)
        and envelope.get("record_boundary")
        and envelope.get("valid") is True
    )
    if not structurally_valid:
        return False
    if envelope.get("mode") == "cursor":
        return envelope.get("continuation_verified") is True
    if envelope.get("mode") == "adapter":
        return bool(
            envelope.get("continuation_verified") is True
            and isinstance(envelope.get("adapter"), dict)
        )
    return True


def _probe_adapter_source_envelope(
    fetch_json,
    state: WatchState,
    *,
    request_facts: dict[str, Any],
    adapter_facts: dict[str, str],
    audit_id: str,
) -> dict[str, Any]:
    """Exercise the pinned pure adapter without committing its probe checkpoint."""

    from .source_adapter import (
        SourceAdapterError,
        accept_source_response,
        plan_source_request,
    )

    # The active Audit identity is already trusted runner state, but a new
    # watch is intentionally not persisted until its probe and parent checks
    # pass.  Use that typed identity for the pure adapter probe without
    # prematurely mutating or committing the watch.
    audit_id = str(
        audit_id or state.audit_root_task_id or state.prepare_root_task_id or ""
    ).strip()
    base = {
        "mode": "adapter",
        "record_boundary": "adapter_records",
        "request": request_facts,
        "adapter": adapter_facts,
        "valid": False,
    }
    if not audit_id:
        return {**base, "invalid_reason": "动态来源适配器缺少准确的命名 Audit 归属"}
    checkpoint: dict[str, Any] = {}
    request_count = 0
    # Probe with the same page budget the persisted watch will use.  A fixed
    # one-record probe changes the source protocol: overlap-capable adapters
    # may need several complete records before their opaque checkpoint can
    # advance.  Continuation is still exercised twice below, so a genuinely
    # repeated checkpoint remains rejected by ``accept_source_response``.
    probe_page_limit = max(1, int(state.tuning.page_limit or 1))
    try:
        plan = plan_source_request(
            owner_home=state.owner_home,
            audit_id=audit_id,
            source_url=state.source_url,
            request_facts=request_facts,
            adapter=adapter_facts,
            checkpoint=checkpoint,
            page_limit=probe_page_limit,
            now_unix=time.time(),
        )
        ok, payload, code = fetch_json(plan.request)
        request_count += 1
        if not ok:
            return {
                **base,
                "probe_error_code": str(code or "NETWORK_REQUEST_FAILED").strip().upper(),
                "probe_error": _probe_fetch_error("动态来源第一次探针请求失败", payload),
            }
        page = accept_source_response(
            owner_home=state.owner_home,
            audit_id=audit_id,
            adapter=adapter_facts,
            checkpoint=checkpoint,
            context=plan.context,
            response=payload,
            max_records=probe_page_limit,
        )
        if page.has_more:
            checkpoint = page.checkpoint
            plan = plan_source_request(
                owner_home=state.owner_home,
                audit_id=audit_id,
                source_url=state.source_url,
                request_facts=request_facts,
                adapter=adapter_facts,
                checkpoint=checkpoint,
                page_limit=probe_page_limit,
                now_unix=time.time(),
            )
            ok, payload, code = fetch_json(plan.request)
            request_count += 1
            if not ok:
                return {
                    **base,
                    "probe_error_code": str(code or "NETWORK_REQUEST_FAILED").strip().upper(),
                    "probe_error": _probe_fetch_error("动态来源第二次探针请求失败", payload),
                }
            accept_source_response(
                owner_home=state.owner_home,
                audit_id=audit_id,
                adapter=adapter_facts,
                checkpoint=checkpoint,
                context=plan.context,
                response=payload,
                max_records=probe_page_limit,
            )
    except SourceAdapterError as exc:
        return {
            **base,
            "probe_error_code": exc.code,
            "probe_error": str(exc),
        }
    return {
        **base,
        "valid": True,
        "continuation_verified": True,
        "probe_request_count": request_count,
    }


def _probe_http_source_envelope(
    fetch_json,
    source_url: str,
    *,
    params: dict[str, Any],
    requested_poll: bool,
    request_facts: dict[str, Any],
) -> dict[str, Any]:
    """Probe one HTTP response and keep only its structural record contract."""
    try:
        request = render_source_http_request(
            source_url,
            request_facts,
            cursor=None,
            page_size=None if requested_poll else 1,
        )
    except ValueError as exc:
        return {
            "mode": "poll" if requested_poll else "cursor",
            "record_boundary": "whole_response" if requested_poll else "array_item",
            "request": request_facts,
            "valid": False,
            "invalid_reason": f"来源请求无法构造: {exc}",
            "probe_error_code": "SOURCE_REQUEST_INVALID",
            "probe_error": f"来源请求无法构造: {exc}",
        }
    ok, payload, code = fetch_json(request)
    if not ok:
        failure_code = str(code or "NETWORK_REQUEST_FAILED").strip().upper()
        return {
            "mode": "poll" if requested_poll else "cursor",
            "record_boundary": "whole_response" if requested_poll else "array_item",
            "request": request_facts,
            "valid": False,
            "invalid_reason": "数据源探针请求失败",
            "probe_error_code": failure_code,
            "probe_error": _probe_fetch_error("数据源探针请求失败", payload),
        }
    if requested_poll:
        return {
            "mode": "poll",
            "record_boundary": "whole_response",
            "request": request_facts,
            "response_type": type(payload).__name__,
            "top_level_keys": (
                sorted(str(key) for key in payload)[:32] if isinstance(payload, dict) else []
            ),
            "valid": True,
        }
    if not isinstance(payload, dict):
        return {
            "mode": "cursor",
            "record_boundary": "array_item",
            "request": request_facts,
            "valid": False,
            "invalid_reason": "游标来源探针必须返回 JSON 对象信封",
        }
    cursor = _cursor_envelope_from_payload(payload, params)
    cursor["request"] = request_facts
    if cursor.get("valid") is not True:
        return cursor
    continuation_failure = _cursor_continuation_failure(
        fetch_json,
        source_url,
        request_facts=request_facts,
        first_request=request,
        first_payload=payload,
        envelope=cursor,
        params=params,
    )
    if continuation_failure:
        continuation_error, continuation_code = continuation_failure
        cursor["valid"] = False
        cursor["continuation_verified"] = False
        cursor["invalid_reason"] = continuation_error
        if continuation_code != "SOURCE_ENVELOPE_INVALID":
            cursor["probe_error_code"] = continuation_code
            cursor["probe_error"] = continuation_error
        return cursor
    cursor["continuation_verified"] = True
    cursor["probe_request_count"] = 2
    return cursor


def _cursor_continuation_failure(
    fetch_json,
    source_url: str,
    *,
    request_facts: dict[str, Any],
    first_request: SourceHttpRequest,
    first_payload: object,
    envelope: dict[str, Any],
    params: dict[str, Any],
) -> tuple[str, str] | None:
    """Prove that the learned request cursor actually controls continuation.

    A response field such as ``next_cursor`` does not imply that the next
    request uses a parameter with the same name.  The transport is publishable
    only after a second request rendered from the first committed position
    advances, or returns an empty caught-up page at that exact position.  This
    checks protocol mechanics only; it never interprets source records.
    """

    if not isinstance(first_payload, dict):
        return "游标来源第一次探针响应不是 JSON 对象", "SOURCE_ENVELOPE_INVALID"
    try:
        first_items = _cursor_probe_items(first_payload, envelope)
        first_next = _cursor_probe_next_position(first_payload, envelope)
    except ValueError as exc:
        return str(exc), "SOURCE_ENVELOPE_INVALID"
    binding = request_facts.get("cursor_binding")
    initial = int(binding.get("initial") or 0) if isinstance(binding, dict) else 0
    if first_items and first_next <= initial:
        return (
            f"第一次探针返回 {len(first_items)} 条完整记录，但响应游标 {first_next} "
            f"没有越过初始请求位置 {initial}",
            "SOURCE_ENVELOPE_INVALID",
        )
    # An empty source at its initial position still needs a distinct request to
    # prove that the configured binding is not ignored by the endpoint.
    continuation_cursor = first_next if first_next > initial else initial + 1
    try:
        second_request = render_source_http_request(
            source_url,
            request_facts,
            cursor=continuation_cursor,
            page_size=1,
        )
    except ValueError as exc:
        return f"无法构造第二次增量探针: {exc}", "SOURCE_REQUEST_INVALID"
    if second_request.url == first_request.url and second_request.data == first_request.data:
        return (
            "配置的请求游标没有改变第二次探针请求，无法证明增量续读",
            "SOURCE_ENVELOPE_INVALID",
        )
    ok, second_payload, code = fetch_json(second_request)
    if not ok:
        return (
            _probe_fetch_error(
                "第二次增量探针请求失败，尚未证明该来源能够续读",
                second_payload,
            ),
            str(code or "NETWORK_REQUEST_FAILED").strip().upper(),
        )
    if not isinstance(second_payload, dict):
        return "第二次增量探针响应不是 JSON 对象", "SOURCE_ENVELOPE_INVALID"
    second_envelope = _cursor_envelope_from_payload(second_payload, params)
    if second_envelope.get("valid") is not True:
        return (
            "第二次增量探针响应不再满足第一次确认的记录与游标结构",
            "SOURCE_ENVELOPE_INVALID",
        )
    if any(
        second_envelope.get(key) != envelope.get(key)
        for key in (
            "record_list_key",
            "cursor_field",
            "cursor_semantics",
            "cursor_position_semantics",
            "has_more_field",
        )
    ):
        return (
            "第二次增量探针的记录边界或游标结构与第一次不一致",
            "SOURCE_ENVELOPE_INVALID",
        )
    try:
        second_items = _cursor_probe_items(second_payload, envelope)
        second_next = _cursor_probe_next_position(second_payload, envelope)
    except ValueError as exc:
        return str(exc), "SOURCE_ENVELOPE_INVALID"
    if second_next < continuation_cursor:
        return (
            f"第二次探针请求位置为 {continuation_cursor}，响应游标却回退到 "
            f"{second_next}；当前 cursor_binding 未被来源正确应用",
            "SOURCE_ENVELOPE_INVALID",
        )
    if second_items and second_next <= continuation_cursor:
        return (
            f"第二次探针返回 {len(second_items)} 条完整记录，但响应游标 "
            f"{second_next} 没有越过请求位置 {continuation_cursor}；"
            "当前 cursor_binding 可能被接口忽略",
            "SOURCE_ENVELOPE_INVALID",
        )
    return None


def _probe_fetch_error(prefix: str, payload: object) -> str:
    """Preserve the typed transport failure without inventing source semantics."""

    detail = str(payload or "").strip()
    return f"{prefix}: {detail}" if detail else prefix


def _cursor_probe_items(
    payload: dict[str, Any],
    envelope: dict[str, Any],
) -> list[object]:
    field = str(envelope.get("record_list_key") or "")
    items = payload.get(field)
    if not field or not isinstance(items, list):
        raise ValueError("增量探针响应缺少已经确认的完整记录数组")
    return items


def _cursor_probe_next_position(
    payload: dict[str, Any],
    envelope: dict[str, Any],
) -> int:
    field = str(envelope.get("cursor_field") or "")
    raw = payload.get(field)
    parsed = _nonnegative_int(raw)
    if not field or parsed is None:
        raise ValueError("增量探针响应缺少已经确认的非负整数游标")
    semantics = str(envelope.get("cursor_semantics") or "")
    if semantics == "last_seen":
        return parsed + 1
    if semantics != "next_position":
        raise ValueError("增量探针响应使用了不支持的游标语义")
    return parsed


def _cursor_envelope_from_payload(
    payload: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    """Resolve an unambiguous cursor envelope without inspecting business text."""
    top_level_keys = sorted(str(key) for key in payload)[:32]
    try:
        declared_record = normalize_top_level_response_field(params.get("record_list_field"))
        declared_cursor = normalize_top_level_response_field(params.get("cursor_field"))
    except ValueError as exc:
        return _invalid_cursor_envelope(top_level_keys, str(exc))
    record_key = _record_list_key(payload, declared_record)
    cursor_key = _cursor_field(payload, declared_cursor)
    if not record_key:
        observed = [str(key) for key, value in payload.items() if isinstance(value, list)]
        reason = (
            f"record_list_field={declared_record!r} 不是顶层数组字段"
            if declared_record
            else "响应没有唯一可识别的顶层记录数组"
        )
        if observed:
            reason += f"；观察到的顶层数组字段: {observed}"
        return _invalid_cursor_envelope(
            top_level_keys,
            reason,
        )
    if not cursor_key:
        observed = [
            str(key) for key, value in payload.items() if _nonnegative_int(value) is not None
        ]
        reason = (
            f"cursor_field={declared_cursor!r} 不是顶层非负整数游标字段"
            if declared_cursor
            else "响应没有唯一可识别的顶层整数游标"
        )
        if observed:
            reason += f"；观察到的顶层整数候选: {observed}"
        return _invalid_cursor_envelope(
            top_level_keys,
            reason,
        )
    semantics = _cursor_semantics(
        payload,
        record_key=record_key,
        cursor_key=cursor_key,
        declared=str(params.get("cursor_semantics") or ""),
    )
    if not semantics:
        return _invalid_cursor_envelope(
            top_level_keys,
            "无法确定游标表示下一位置还是最后已见位置；请显式提供 cursor_semantics",
        )
    try:
        has_more_field = normalize_top_level_response_field(params.get("has_more_field"))
    except ValueError as exc:
        return _invalid_cursor_envelope(top_level_keys, str(exc))
    if has_more_field:
        if not isinstance(payload.get(has_more_field), bool):
            return _invalid_cursor_envelope(
                top_level_keys,
                f"has_more_field={has_more_field} 不存在或不是布尔值",
            )
    elif "has_more" in payload:
        if not isinstance(payload.get("has_more"), bool):
            return _invalid_cursor_envelope(
                top_level_keys,
                "has_more 存在但不是布尔值",
            )
        has_more_field = "has_more"
    list_keys = [str(key) for key, value in payload.items() if isinstance(value, list)]
    return {
        "mode": "cursor",
        "record_boundary": "array_item",
        "record_list_key": record_key,
        "cursor_field": cursor_key,
        "cursor_semantics": semantics,
        "cursor_position_semantics": _cursor_position_semantics(params),
        "has_more_field": has_more_field or None,
        "top_level_keys": top_level_keys,
        "valid": len(list_keys) >= 1,
    }


def _cursor_position_semantics(params: dict[str, Any]) -> str:
    """Return only the explicitly declared source-position arithmetic contract."""

    selected = str(params.get("cursor_position_semantics") or "").strip().lower()
    if selected in {"", "opaque"}:
        return "opaque"
    if selected == "contiguous_record_ordinal":
        return selected
    return "opaque"


def _record_list_key(payload: dict[str, Any], declared: str) -> str:
    if declared:
        return declared if isinstance(payload.get(declared), list) else ""
    if isinstance(payload.get("items"), list):
        return "items"
    keys = [str(key) for key, value in payload.items() if isinstance(value, list)]
    return keys[0] if len(keys) == 1 else ""


def _cursor_field(payload: dict[str, Any], declared: str) -> str:
    if declared:
        return declared if _nonnegative_int(payload.get(declared)) is not None else ""
    conventional = [
        key for key in ("next_cursor", "next") if _nonnegative_int(payload.get(key)) is not None
    ]
    if len(conventional) == 1:
        return conventional[0]
    if conventional:
        return ""
    candidates = [str(key) for key, value in payload.items() if _nonnegative_int(value) is not None]
    return candidates[0] if len(candidates) == 1 else ""


def _cursor_semantics(
    payload: dict[str, Any],
    *,
    record_key: str,
    cursor_key: str,
    declared: str,
) -> str:
    normalized = declared.strip().lower()
    if normalized in {"next_position", "last_seen"}:
        return normalized
    if cursor_key == "next_cursor":
        return "next_position"
    if cursor_key == "next":
        return "last_seen"
    records = payload.get(record_key)
    if not isinstance(records, list) or not records or not isinstance(records[-1], dict):
        return ""
    cursor_value = _nonnegative_int(payload.get(cursor_key))
    if cursor_value is None:
        return ""
    inferred: set[str] = set()
    for value in records[-1].values():
        item_value = _nonnegative_int(value)
        if item_value is None:
            continue
        if item_value == cursor_value:
            inferred.add("last_seen")
        if item_value + 1 == cursor_value:
            inferred.add("next_position")
    return next(iter(inferred)) if len(inferred) == 1 else ""


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _invalid_cursor_envelope(
    top_level_keys: list[str],
    reason: str,
) -> dict[str, Any]:
    return {
        "mode": "cursor",
        "record_boundary": "array_item",
        "top_level_keys": top_level_keys,
        "valid": False,
        "invalid_reason": reason,
    }


def _source_envelope_error(envelope: dict[str, Any]) -> str:
    reason = str(envelope.get("invalid_reason") or "无法确定数据源的完整记录与游标边界")
    if envelope.get("mode") == "adapter":
        return reason + "。请在当前命名 Audit 的 prepare 中修正并重新试通该来源适配器。"
    keys = [str(key) for key in envelope.get("top_level_keys") or [] if str(key)]
    if keys:
        reason += f"；探针观察到的顶层字段: {keys}"
    return (
        reason + "。record_list_field/cursor_field 若填写，必须是顶层字段原名而不是 JSONPath；"
        "能自动识别时可省略。"
    )


def _apply_open_overrides(
    state: WatchState,
    params: dict[str, Any],
    *,
    allow_terminal_reopen: bool = False,
) -> bool:
    """Apply one open without reviving a stopped run.

    Administrative close reasons fence retries inside the same run.  A newer
    typed ``audit_run_epoch`` is a fresh, explicitly started run and may reuse
    the retained source checkpoint after all lifecycle checks have passed.
    """

    if state.closed and state.close_reason in {
        "named_audit_clear",
        "audit_parent_inactive",
        "audit_run_superseded",
        "audit_source_membership_removed",
    } and not allow_terminal_reopen:
        return False
    _apply_window_override(state, params.get("watch_window_seconds"))
    state.closed = False
    # Re-open is a new live lifecycle boundary.  ``reopen_on_disk`` clears the
    # durable close marker, and the in-memory projection must expose the same
    # facts immediately; otherwise the open response says ``closed=False``
    # while still carrying the previous run's close time/reason/backlog.
    state.closed_at = 0.0
    state.close_reason = ""
    state.close_pending_records = 0
    return True


def _apply_file_record_boundary(
    state: WatchState,
    params: dict[str, Any],
) -> ToolHandlerOutcome | None:
    from .sources import source_kind

    raw = params.get("record_boundary")
    if source_kind(state.source_url, state.source_mode) != "file":
        if raw is not None:
            return _err(
                "record_boundary 目前只适用于 file 来源",
                "TOOL_INVALID_ARGUMENTS",
            )
        return None
    existing = (
        state.source_envelope.get("record_boundary")
        if state.source_envelope.get("mode") == "file"
        else None
    )
    if raw is None:
        boundary = existing if isinstance(existing, dict) else {"mode": "line"}
    else:
        boundary, error = _parse_file_record_boundary(raw)
        if error:
            return _err(error, "TOOL_INVALID_ARGUMENTS")
        assert boundary is not None
    if (
        isinstance(existing, dict)
        and boundary != existing
        and (state.cursor > 0 or state.file_fragment_bytes > 0)
    ):
        return _err(
            "已有文件游标或未完成片段时不能更换记录边界；请新建独立来源任务",
            "TOOL_INVALID_ARGUMENTS",
        )
    state.source_envelope = {
        "mode": "file",
        "record_boundary": boundary,
    }
    return None


def _parse_file_record_boundary(
    raw: object,
) -> tuple[dict[str, str] | None, str]:
    if not isinstance(raw, dict):
        return None, "record_boundary 必须是对象"
    extras = sorted(str(key) for key in raw if key not in {"mode", "delimiter"})
    if extras:
        return None, f"record_boundary 包含未知字段: {extras}"
    mode = str(raw.get("mode") or "").strip().lower()
    if mode == "line":
        if raw.get("delimiter") is not None:
            return None, "line 边界不能同时提供 delimiter"
        return {"mode": "line"}, ""
    if mode != "delimiter":
        return None, "record_boundary.mode 必须是 line 或 delimiter"
    delimiter = raw.get("delimiter")
    if not isinstance(delimiter, str) or not delimiter:
        return None, "delimiter 边界必须提供非空 delimiter 字符串"
    encoded = delimiter.encode("utf-8")
    if len(encoded) > 256:
        return None, "record_boundary.delimiter 不能超过 256 字节"
    return {"mode": "delimiter", "delimiter": delimiter}, ""


def _apply_audit_guarantee(
    tool: WatchStreamTool, state: WatchState, params: dict[str, Any]
) -> None:
    """/audit 保证档置位(单调棘轮:置上不因后续 open 缺参而降级——保证是用户级契约)。
    激活判据只认结构化事实,不再回扫 prompt/goal 文本:
    task_attributes 里的保证档标志是唯一入口——这是跨轮/跨 spawn 树可靠的信号:
    前台创建路(root_user_prompt 是用户原文时)由 gateway/orchestration
    一次性盖上,主代理自己和它委派的判读子代理/孙代理此后每轮都读得到(继承靠 state/attributes
    层,不靠 goal 文本)。gateway 只在消息以 /audit 命令开头时盖标；普通聊天里提到该词不会激活。
    模型工具参数也不能自行升级保证档，避免普通任务绕过用户的显式特殊模式选择。
    置位时快照引擎有损计数基线。"""
    requested = state.audit_guarantee or _audit_requested(tool)
    if not requested:
        return
    if not state.audit_guarantee:
        state.audit_guarantee = True
        state.audit_baseline = {
            key: int(state.engine.totals.get(key, 0) or 0)
            for key in ("suppressed", "overflow", "audit_throttled")
        }
    # Audit semantic authority lives in the named task objective.  Clear any
    # legacy per-source judgment state so a reopened old watch cannot silently
    # filter records or teach a replacement Agent a stale business rule.
    state.source_spec = None
    state.judgment_note = ""
    state.last_sample_digest = {}
    state.source_envelope = {
        key: value
        for key, value in state.source_envelope.items()
        if key
        in {
            "mode",
            "record_boundary",
            "request",
            "adapter",
            "response_type",
            "top_level_keys",
            "record_list_key",
            "cursor_field",
            "cursor_semantics",
            "has_more_field",
            "valid",
        }
    }
    state.engine.apply_spec(None)


# LLM: Persist only typed task lineage plus the opaque user objective; neither field is derived from source content.
# 函数用途: 把本次 Audit 的根编号和用户要求随 watch 保存，供后代接班、精确停止和无历史判读复用。
def _apply_audit_context(
    tool: WatchStreamTool,
    state: WatchState,
) -> ToolHandlerOutcome | None:
    if not state.audit_guarantee:
        return None
    root_task_id = _audit_root_task_id(tool.agent)
    if root_task_id and not state.audit_root_task_id:
        state.audit_root_task_id = root_task_id
    from ..common.audit_activation import (
        AUDIT_OBJECTIVE_ATTR,
        AUDIT_RUN_EPOCH_ATTR,
        AUDIT_RUN_PROMPT_ATTR,
    )
    from ..conversation.audit_requirements import audit_runtime_requirement_text

    attrs = _current_audit_attributes(tool.agent)
    run_epoch = max(
        0,
        int(attrs.get(AUDIT_RUN_EPOCH_ATTR) or 0) if isinstance(attrs, dict) else 0,
    )
    previous_epoch = max(0, int(state.audit_run_epoch or 0))
    if previous_epoch > run_epoch:
        return _err(
            "这个 watch 已属于同名 Audit 的更新运行轮次，旧轮不能复用。",
            "TOOL_PERMISSION_DENIED",
            reported_code="AUDIT_RUN_EPOCH_MISMATCH",
        )
    rollover = run_epoch > previous_epoch
    if rollover and previous_epoch > 0 and not state.closed:
        return _err(
            "这个 watch 的上一运行轮次仍未关闭，当前轮不能抢占它。",
            "TOOL_PERMISSION_DENIED",
            reported_code="AUDIT_PREVIOUS_RUN_ACTIVE",
        )
    objective = attrs.get(AUDIT_OBJECTIVE_ATTR) if isinstance(attrs, dict) else None
    objective_text = (
        audit_runtime_requirement_text(objective)
        if isinstance(objective, str) and objective.strip()
        else ""
    )
    run_prompt = attrs.get(AUDIT_RUN_PROMPT_ATTR) if isinstance(attrs, dict) else None
    run_prompt_text = run_prompt.strip() if isinstance(run_prompt, str) else ""
    from .source_worker import current_audit_source_task_goal

    source_goal = current_audit_source_task_goal(tool.agent)
    if not source_goal:
        return _err(
            "当前来源缺少创建该来源子代理时的完整任务说明，未启动采集。",
            "AUDIT_SOURCE_TASK_CONTEXT_UNAVAILABLE",
        )
    if not rollover and state.source_task_goal and state.source_task_goal != source_goal:
        return _err(
            "这个 watch 已钉住另一份来源任务说明；请停止后创建新的命名 Audit。",
            "TOOL_INVALID_ARGUMENTS",
            reported_code="AUDIT_SOURCE_BINDING_IMMUTABLE",
        )
    state.audit_run_epoch = run_epoch
    if objective_text:
        state.audit_objective = objective_text
    if rollover:
        # A continued named Audit keeps its source checkpoint and append-only
        # evidence, but run-scoped presentation, loss baseline and transient
        # health begin anew.  No source semantics are inferred here.
        state.audit_run_prompt = run_prompt_text
        state.source_task_goal = source_goal
        state.audit_baseline = {
            key: int(state.engine.totals.get(key, 0) or 0)
            for key in ("suppressed", "overflow", "audit_throttled")
        }
        state.last_error = ""
        state.last_error_code = ""
        state.ingest_window_started_at = time.time()
        state.ingest_window_start_records = int(state.cursor or 0)
        state.ingest_records_per_second = 0.0
        state.ingest_rate_updated_at = state.ingest_window_started_at
    elif run_prompt_text:
        state.audit_run_prompt = run_prompt_text
    state.source_task_goal = source_goal
    return None


# LLM: An Audit binds immutable source identity and opaque owner-owned
# references once; no source name or document content is interpreted here.
# 函数用途: 钉住本次 watch 的来源编号、说明/文档引用和结构配置版本，续跑变更时拒绝。
def _pin_source_binding(
    state: WatchState,
    params: dict[str, Any],
    *,
    resumed: bool,
) -> ToolHandlerOutcome | None:
    requested_source_id = str(params.get("source_id") or "").strip()
    if requested_source_id and not _SOURCE_ID_RE.fullmatch(requested_source_id):
        return _err(
            "source_id 只能包含字母、数字、点、下划线、冒号或短横线，且最长 128 字符。",
            "TOOL_INVALID_ARGUMENTS",
        )
    source_id = (
        requested_source_id
        or state.source_id
        or source_id_for(
            state.owner_home,
            state.source_url,
        )
    )
    profile_supplied = "source_profile_ref" in params
    profile_ref = str(params.get("source_profile_ref") or "").strip()
    documents_supplied = "document_refs" in params
    document_refs, document_error = _normalized_document_refs(params.get("document_refs"))
    if document_error:
        return _err(document_error, "TOOL_INVALID_ARGUMENTS")
    config_version = _source_config_version(state)
    if not state.audit_guarantee:
        state.source_id = source_id
        if profile_supplied:
            state.source_profile_ref = profile_ref
        if documents_supplied:
            state.document_refs = document_refs
        state.source_config_version = config_version
        return None
    pinned = bool(state.source_config_version)
    if resumed and pinned:
        conflicts: list[str] = []
        if requested_source_id and source_id != state.source_id:
            conflicts.append("source_id")
        if profile_supplied and profile_ref != state.source_profile_ref:
            conflicts.append("source_profile_ref")
        if documents_supplied and document_refs != state.document_refs:
            conflicts.append("document_refs")
        if config_version != state.source_config_version:
            conflicts.append("source_config_version")
        if conflicts:
            return _err(
                "运行中的来源绑定不可静默改写；请停止后创建新的命名 Audit。"
                f" 冲突字段: {','.join(conflicts)}",
                "TOOL_INVALID_ARGUMENTS",
                reported_code="AUDIT_SOURCE_BINDING_IMMUTABLE",
            )
        return None
    state.source_id = source_id
    state.source_profile_ref = profile_ref
    state.document_refs = document_refs
    state.source_config_version = config_version
    return None


def _normalized_document_refs(value: object) -> tuple[list[str], str]:
    if value is None:
        return [], ""
    if not isinstance(value, list):
        return [], "document_refs 必须是字符串数组。"
    if len(value) > 64:
        return [], "document_refs 最多 64 项。"
    refs: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            return [], "document_refs 的每一项都必须是字符串。"
        ref = raw.strip()
        if not ref or len(ref) > 2048:
            return [], "document_refs 不能包含空引用，单项最长 2048 字符。"
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs, ""


def _source_config_version(state: WatchState) -> str:
    from .source_binding import audit_source_config_version

    return audit_source_config_version(
        source_url=state.source_url,
        source_mode=state.source_mode,
        source_envelope=state.source_envelope,
        poll_query_seconds=state.tuning.poll_query_seconds,
    )


def _audit_requested(tool: WatchStreamTool) -> bool:
    from ..common.audit_activation import attributes_request_audit

    # 结构化标志(跨轮/跨子代理可靠):当前任务属性(子代理=task.attributes 经 runner
    # 设进上下文;主代理=gateway 建的 run_params.task_attributes)。
    return attributes_request_audit(_current_audit_attributes(tool.agent))


def _current_audit_attributes(agent: object) -> dict[str, Any] | None:
    """当前 runner 上下文的 task_attributes:子代理走线程本地上下文(run_flow 从 task.attributes
    设入),主代理走 _current_run_params.task_attributes(gateway 建)。两条都读,任一命中即可。"""
    from ..common.audit_activation import current_audit_attributes

    return current_audit_attributes(agent)


def _override_watch_window_from_audit_command(
    tool: WatchStreamTool,
    state: WatchState,
    params: dict[str, Any],
) -> None:
    """gateway 解析 /audit <N><d|h|m> 后写入结构化属性，据此钉住 watch 窗口。

    该值盖过模型自行传入的窗口，再走同一条 _apply_window_override；裸 /audit 不改窗口。
    watch 工具不读取用户原文，也不做自然语言推断。
    """
    from ..common.audit_activation import AUDIT_DEADLINE_ATTR, AUDIT_WINDOW_ATTR

    attrs = _current_audit_attributes(tool.agent)
    deadline = attrs.get(AUDIT_DEADLINE_ATTR) if isinstance(attrs, dict) else None
    try:
        deadline_value = float(deadline or 0.0)
    except (TypeError, ValueError):
        deadline_value = 0.0
    if deadline_value > 0:
        # An in-flight duplicate open keeps the durable start edge.  A closed
        # or already-finished watch is a new named-Audit run, however, and
        # ``_apply_window_override`` resets ``opened_at`` after this function
        # returns.  Calculate from the same prospective anchor here; otherwise
        # the idle time between runs is accidentally added to the new window.
        now = time.time()
        opened_at = float(state.opened_at or now)
        elapsed = now - opened_at
        old_window_done = (
            state.watch_window_seconds > 0
            and elapsed >= state.watch_window_seconds
        )
        anchor = now if state.closed or old_window_done else opened_at
        params["watch_window_seconds"] = max(
            1,
            math.ceil(deadline_value - anchor),
        )
        return
    seconds = attrs.get(AUDIT_WINDOW_ATTR) if isinstance(attrs, dict) else None
    if seconds:
        params["watch_window_seconds"] = seconds


def _apply_window_override(state: WatchState, raw_window: object) -> None:
    """带窗口的 open 且旧窗已走完(或 close 过)= 新一场盯守:窗口起点重置到现在。
    否则重开的盯守沿用旧 opened_at,窗口生下来就"已走完"——收割线程按窗口完成立即
    自停、模型看 window_complete=true 直接收工,重开静默空转(真机实锤:重启后二次
    任务 open 带 720s 窗,opened_at 还是 5200s 前)。
    窗口未走完的中途 open 不重置——续的还是原窗,语义不变。"""
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
        state.window_finalized_at = 0.0
    state.watch_window_seconds = new_window


def _collection_window_is_open(state: WatchState) -> bool:
    """Whether collection may still advance; pending records remain readable after it closes."""
    if state.closed:
        return False
    window = max(0, int(state.watch_window_seconds or 0))
    if window <= 0:
        return True
    return (
        time.time() - float(state.opened_at or 0.0) < window
        or float(state.window_finalized_at or 0.0) <= 0
    )


def _absorb_drain(state: WatchState, drain, aggregate: dict[str, int]) -> None:
    state.last_error = ""
    state.last_error_code = ""
    state.cursor = drain.cursor
    if isinstance(getattr(drain, "source_checkpoint", None), dict):
        state.source_checkpoint = deepcopy(drain.source_checkpoint)
    # file 源:aux_cursor=读后行号(轮转重读会回落,正确);其余源恒 0=不变。
    state.line_cursor = int(getattr(drain, "aux_cursor", 0) or 0)
    if float(getattr(drain, "fetched_at", 0.0) or 0.0) > 0:
        state.last_poll_at = float(drain.fetched_at)
    state.last_reached_end = drain.reached_end
    state.last_pull_at = time.time()
    state.totals["gap_events"] += drain.gap_events
    aggregate["seen"] += len(drain.events)
    aggregate["pages"] += drain.pages


def _status_payload(state: WatchState, *, agent: object | None = None) -> dict[str, Any]:
    from .harvester import harvester_block

    payload = {
        "ok": True,
        "action": "status",
        "watch_id": state.watch_id,
        "source_binding": source_binding_block(state),
        "source_url": state.source_url,
        "source_mode": state.source_mode,
        "last_error_code": state.last_error_code or None,
        "judgment_note": None if state.audit_guarantee else (state.judgment_note or None),
        "source_spec": (
            None
            if state.audit_guarantee
            else (dict(state.source_spec) if state.source_spec else None)
        ),
        "coverage": coverage_block(state, {}),
        "watch": watch_block(state),
        "harvester": harvester_block(state),
        "totals": {**state.totals, **state.engine.totals},
        # 内容过滤规则审计:建了哪条(spec 的 normal_* 条目)、各拦了多少、合计——
        # 减负可核算不静默。收割者在别的进程时按本进程最近载入的快照呈现(最终一致)。
        "content_rules": (
            {"hits_total": 0, "rules_count": 0, "rules": []}
            if state.audit_guarantee
            else content_rules_block(state.engine)
        ),
        # 档位一眼可见(默认档 false):status 能直接核对"这路到底在不在保证档"。
        "audit_guarantee": bool(state.audit_guarantee),
    }
    attach_audit_receipt(
        payload,
        state,
        include_objective=not _source_scoped_audit_actor(agent),
    )
    if state.audit_guarantee:
        from .source_worker import source_worker_facts

        payload["batch_context"] = _audit_batch_context_estimate(state, agent)
        payload["source_worker"] = source_worker_facts(agent, state)
    return payload


@dataclass(frozen=True)
class _VerdictToolRequest:
    raw_verdicts: object
    verdicts: list[dict[str, Any]]
    delivery_ref: str


def _verdict_tool_request(params: dict[str, Any]) -> _VerdictToolRequest | ToolHandlerOutcome:
    raw_verdicts = params.get("verdicts")
    verdicts, parse_error = _parse_verdicts(raw_verdicts)
    if parse_error:
        return _err(
            parse_error,
            "TOOL_INVALID_ARGUMENTS",
            reported_code="AUDIT_VERDICT_SHAPE_INVALID",
        )
    if not verdicts:
        return _err(
            "缺 verdicts:请为当前每个 candidate 逐条提交 [{"
            '"verdict_token":"vt-…",'
            '"verdict":"hit|clear|unsure",'
            '"score":0,"note":"判断理由"}]。clear 没有额外依据时可省略 note；'
            "首次判断同时传当前 delivery_ref，程序会按一次性令牌逐条绑定。"
            "可以先提交本轮已完成的部分；遗漏记录保持待判，重复或跨批令牌会拒绝整次提交。",
            "TOOL_PARAMETER_REQUIRED",
        )
    return _VerdictToolRequest(
        raw_verdicts=raw_verdicts,
        verdicts=verdicts,
        delivery_ref=str(params.get("delivery_ref") or "").strip(),
    )


def _bound_runtime_verdicts(
    tool: WatchStreamTool,
    state: WatchState,
    request: _VerdictToolRequest,
    run_id: str,
) -> tuple[list[dict[str, Any]], dict[str, int]] | ToolHandlerOutcome:
    from .harvester import (
        bind_current_delivery_verdict_tokens,
        validate_verdict_evidence_refs,
    )

    bound = bind_current_delivery_verdict_tokens(
        state,
        consumer=run_id,
        delivery_ref=request.delivery_ref or None,
        verdicts=request.verdicts,
    )
    if bound.get("ok") is not True:
        return _err(
            str(bound.get("error") or "当前交付无法绑定逐条结论"),
            "TOOL_INVALID_ARGUMENTS",
            reported_code=str(
                bound.get("error_code") or "AUDIT_DELIVERY_REF_INVALID"
            ),
        )
    observation = _verdict_output_observation(
        request.verdicts,
        raw_verdicts=request.raw_verdicts,
        delivery_ref=request.delivery_ref,
        watch_id=state.watch_id,
        settled_records=len(bound.get("verdicts") or []),
    )
    runtime_facts = _judge_runtime_facts(tool)
    verdicts = [
        {**row, **runtime_facts} for row in (bound.get("verdicts") or []) if isinstance(row, dict)
    ]
    if not verdicts:
        return verdicts, observation
    evidence = validate_verdict_evidence_refs(
        state,
        consumer=run_id,
        verdicts=verdicts,
        delivery_ref=request.delivery_ref or None,
    )
    if evidence.get("ok") is True:
        return verdicts, observation
    mismatches = [str(item) for item in (evidence.get("mismatch_ack_ids") or []) if str(item)]
    return _err(
        "verdict 的逐条身份与当前交付不一致；显式 source_ref/event_sha256 "
        "填错会拒绝，复核且没有 delivery_ref 时两者必须准确提供。"
        "若这是带当前 delivery_ref 的首次判断，请为本轮提交的每行原样保留相邻的"
        "verdict_token，并删除 ack_id/source_ref/event_sha256。"
        f"本次未写账，请核对：{mismatches}",
        "TOOL_INVALID_ARGUMENTS",
        reported_code="AUDIT_VERDICT_EVIDENCE_MISMATCH",
    )


def _render_verdict_tool_result(
    tool: WatchStreamTool,
    state: WatchState,
    request: _VerdictToolRequest,
    verdicts: list[dict[str, Any]],
    result: dict[str, Any],
    *,
    run_id: str,
) -> ToolHandlerOutcome:
    if not result.get("ok"):
        return _verdict_failure_result(result)
    from .source_worker import reconcile_audit_finding_outbox

    result["finding_projection"] = reconcile_audit_finding_outbox(tool.agent, run_id)
    result["correction_contract"] = (
        "若后续证据表明某条已签收判断有误，先对该条准确 ack_id/source_ref/"
        "event_sha256 再次调用 verdict，设 review=true 并提交更正后的 verdict/score/"
        "note；需要升级时可在同一复核行内携带 finding。record_finding 不会修改旧 verdict。"
    )
    payload = {"action": "verdict", "watch_id": state.watch_id, **result}
    attach_audit_receipt(
        payload,
        state,
        include_objective=not _source_scoped_audit_actor(tool.agent),
    )
    if result.get("unknown_ack_ids"):
        payload["unknown_note"] = (
            "unknown_ack_ids 里的令牌不在你(或任何在途批)的欠账里——ack_id 必须原样取自"
            "本轮 pull 候选行,别手编/串批;这些条目没有入账,请核对后随正确的 ack_id 重交。"
        )
    transition = _verdict_runtime_transition(tool, request, verdicts, result)
    return _ok_payload(payload, runtime_transition=transition)


def _verdict_runtime_transition(
    tool: WatchStreamTool,
    request: _VerdictToolRequest,
    verdicts: list[dict[str, Any]],
    result: dict[str, Any],
) -> dict[str, object] | None:
    acked_now = int(result.get("acked_now") or 0)
    if not (
        _source_scoped_audit_actor(tool.agent)
        and acked_now > 0
        and int(result.get("pending_remaining") or 0) == 0
        and not result.get("unknown_ack_ids")
    ):
        return None
    return _context_refresh_transition("durable_slice_committed")


def _verdict_failure_result(result: dict[str, Any]) -> ToolHandlerOutcome:
    reported_code = str(result.get("error_code") or "")
    message = str(result.get("error") or "结论未入账")
    details = [str(item).strip() for item in result.get("errors") or [] if str(item).strip()]
    if details:
        message = message + "；" + "；".join(details[:8])
    if not reported_code:
        return _err(message, "TOOL_INVALID_ARGUMENTS")
    if reported_code.endswith("_UNAVAILABLE"):
        error_code = "TOOL_UNAVAILABLE"
    elif any(value in reported_code for value in ("AUTHORIZATION", "WORKER", "LEASE")):
        error_code = "TOOL_PERMISSION_DENIED"
    else:
        error_code = "TOOL_INVALID_ARGUMENTS"
    return _err(
        message,
        error_code,
        reported_code=reported_code,
    )


def _parse_verdicts(
    raw: object,
) -> tuple[list[dict[str, Any]], str]:
    """Parse the verdict row array without interpreting its conclusions.

    A batch default is a separate top-level tool parameter. Escaped JSON remains
    compatible with providers that serialize native tool arguments as strings.
    """
    if raw is None:
        return [], ""
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [], "verdicts 不是有效 JSON"
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return [], "verdicts 必须是逐条判断数组"
    if any(not isinstance(row, dict) for row in raw):
        return [], "verdicts 数组中的每一项都必须是判断对象"
    return [dict(row) for row in raw], ""


# LLM: Batch sizing learns only the actual serialized protocol footprint of a
# successful model proposal. It does not inspect note text, scores or event
# fields for meaning, and it never turns a previous verdict into a new one.
# 函数用途: 计算本次 verdict 工具参数的机械字节量，供后续批次安全估算输出空间。
def _verdict_output_observation(
    verdicts: list[dict[str, Any]],
    *,
    raw_verdicts: object,
    delivery_ref: str,
    watch_id: str,
    settled_records: int | None = None,
) -> dict[str, int]:
    submitted_rows = list(verdicts)
    observed_records = (
        max(0, int(settled_records)) if settled_records is not None else len(verdicts)
    )
    if not submitted_rows or observed_records <= 0:
        return {
            "model_output_bytes": 0,
            "model_output_records": 0,
            "model_output_max_row_bytes": 0,
        }
    compact_rows = [
        json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for row in submitted_rows
    ]
    envelope: dict[str, Any] = {
        "action": "verdict",
        "watch_id": str(watch_id or ""),
    }
    # Preserve the actual provider-facing parameter shape. Some compatible
    # backends still return the array as an escaped JSON string; measuring the
    # reparsed list would under-count that model's real output size.
    if raw_verdicts is not None:
        envelope["verdicts"] = raw_verdicts
    if delivery_ref:
        envelope["delivery_ref"] = delivery_ref
    return {
        "model_output_bytes": len(
            json.dumps(
                envelope,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "model_output_records": observed_records,
        "model_output_max_row_bytes": max(len(row) for row in compact_rows),
    }


# LLM: Model/backend identity comes only from the trusted runtime object, never tool arguments.
# Every verdict caller stamps a fixed judge mode before the row reaches the append-only ledger.
# 函数用途: 给研判结果补上实际使用的模型和后端，方便以后复查时知道是谁判的。
def _judge_runtime_facts(
    tool: WatchStreamTool,
) -> dict[str, str]:
    backend = getattr(tool.agent, "backend", None)
    config = getattr(tool.agent, "config", None)
    return {
        "judge_mode": "agent_runtime",
        "judge_model": str(
            getattr(backend, "model_name", "") or getattr(config, "model_name", "") or ""
        ),
        "judge_backend": str(
            getattr(config, "model_backend", "")
            or (type(backend).__name__ if backend is not None else "")
        ),
    }


def _pull_from_spool(
    tool: WatchStreamTool,
    state: WatchState,
    max_wait: float,
    consumer: str,
    *,
    candidate_limit: int | None = None,
    source_authority: dict[str, object] | None = None,
) -> ToolHandlerOutcome | None:
    """后台连续摄取模式的 pull:确保收割者在跑,长轮询消费 spool 候选批。

    与 inline 模式的关键差别:等待期间【不持 state.lock】(收割线程每拍要锁);
    收割者起不来(且无别进程收割)时,spool 里只要还有已抬未判的候选就仍从 spool
    消费(不能因为线程起不来就静默跳到源游标 inline 路,把积压孤儿跳过去);
    积压清完才返回 None 回落 inline drain。
    """
    from .harvester import ensure_harvester

    ensured = ensure_harvester(
        state,
        tool._harvester_fetch(),
        on_records_ready=_audit_source_ready_callback(tool, state),
        on_window_finalized=_audit_source_finalized_callback(tool, state),
    )
    if ensured is None and not state.audit_guarantee and _unjudged_spool_backlog(state) <= 0:
        return None
    # /audit 保证档永不回落 inline drain:inline 路事件不进 durable 队列(判了没有逐条
    # 签收对账),违反"全量入队+判完才签收"。收割者起不来时如实空批(载荷 harvester
    # 块可见 off),慢=延迟,绝不换一条会漏账的路。
    remote = str((ensured or {}).get("mode") or "") == "remote"
    request = _SpoolPullRequest(
        tool=tool,
        state=state,
        consumer=consumer,
        candidate_limit=candidate_limit,
        source_authority=source_authority,
    )
    budget = _spool_pull_budget(request)
    records, backlog = _wait_for_spool_records(
        state,
        max_wait,
        remote=remote,
        consumer=consumer,
        batch_target_bytes=budget.target_bytes,
        candidate_limit=budget.record_limit,
        source_authority=source_authority,
        effective_audit_objective=_effective_audit_objective(tool.agent, state),
    )
    authorization_error = backlog.get("authorization_error")
    if isinstance(authorization_error, dict):
        return _spool_authorization_error(authorization_error)
    return _render_claimed_spool_pull(
        request,
        budget,
        records,
        backlog,
        remote=remote,
        harvester_ready=ensured is not None,
    )


@dataclass(frozen=True)
class _SpoolPullRequest:
    tool: WatchStreamTool
    state: WatchState
    consumer: str
    candidate_limit: int | None
    source_authority: dict[str, object] | None


@dataclass(frozen=True)
class _SpoolPullBudget:
    facts: dict[str, int | float | str]
    safe_consumer_tokens: int
    target_tokens: int
    target_bytes: int
    record_limit: int | None


def _spool_pull_budget(request: _SpoolPullRequest) -> _SpoolPullBudget:
    if not request.state.audit_guarantee:
        return _SpoolPullBudget({}, 0, 0, 0, request.candidate_limit)
    facts = _audit_batch_context_estimate(request.state, request.tool.agent)
    safe_tokens = max(1, int(facts.get("context_safe_batch_tokens") or 1))
    target_tokens = max(1, int(facts.get("safe_batch_tokens") or 1))
    record_limit = _effective_audit_record_limit(
        requested_records=request.candidate_limit,
        batch_context=facts,
    )
    # The byte watermark is only an early whole-record batching trigger.
    return _SpoolPullBudget(
        facts=facts,
        safe_consumer_tokens=safe_tokens,
        target_tokens=target_tokens,
        target_bytes=target_tokens * 3,
        record_limit=record_limit,
    )


def _spool_authorization_error(error: dict[str, Any]) -> ToolHandlerOutcome:
    reported_code = str(error.get("error_code") or "AUDIT_SOURCE_AUTHORIZATION_FAILED")
    return _err(
        str(error.get("error") or "来源工作者写账授权已失效"),
        (
            "TOOL_UNAVAILABLE"
            if reported_code.endswith("_UNAVAILABLE")
            else "TOOL_PERMISSION_DENIED"
        ),
        reported_code=reported_code,
    )


def _render_claimed_spool_pull(
    request: _SpoolPullRequest,
    budget: _SpoolPullBudget,
    records: list[dict],
    backlog: dict[str, Any],
    *,
    remote: bool,
    harvester_ready: bool,
) -> ToolHandlerOutcome:
    from .harvester import harvester_block

    state = request.state
    with state.lock:
        state.last_pull_at = time.time()
        if not remote:
            # 收割者在本进程:落一次快照(消费时间戳进盘,供 idle 判定和恢复观测)。
            persist_state(state)
        payload = _render_spool_pull(
            state,
            records,
            backlog,
            harvester_block(state),
            model_event_max_tokens=budget.target_tokens,
            requested_records=request.candidate_limit,
            effective_record_limit=budget.record_limit,
            safe_batch_tokens=budget.target_tokens,
            include_audit_objective=not _source_scoped_audit_actor(request.tool.agent),
        )
        if state.audit_guarantee:
            context_error = _commit_audit_pull_context(request, budget, records, payload)
            if context_error is not None:
                return context_error
    return _ok_payload(
        payload,
        preserve_prompt_output=state.audit_guarantee,
        runtime_transition=_empty_audit_pull_runtime_transition(
            request.tool,
            records,
            harvester_ready=harvester_ready,
        ),
    )


def _empty_audit_pull_runtime_transition(
    tool: WatchStreamTool,
    records: list[dict],
    *,
    harvester_ready: bool,
) -> dict[str, object] | None:
    """Yield one source-worker slice when its durable queue is empty.

    The collector and the Gateway's canonical 15-second collector recovery
    keep running without a model.  Their durable empty-to-nonempty callback
    wakes this exact logical source worker when complete records arrive.  Do
    not yield if collector ownership could not be established: in that case a
    later tool round may still recover collection instead of leaving a source
    position asleep forever.
    """

    if not (
        harvester_ready
        and not records
        and _source_scoped_audit_actor(tool.agent)
    ):
        return None
    return _context_refresh_transition("audit_source_waiting_for_records")


def _commit_audit_pull_context(
    request: _SpoolPullRequest,
    budget: _SpoolPullBudget,
    records: list[dict],
    payload: dict[str, Any],
) -> ToolHandlerOutcome | None:
    from .harvester import update_audit_batch_context

    batch_context = payload.get("batch_context")
    if not isinstance(batch_context, dict):
        return _err(
            "Audit 批次缺少上下文账，未向模型交付。",
            "AUDIT_CONSUMER_CONTEXT_UNAVAILABLE",
        )
    delivered_tokens = max(0, int(batch_context.get("estimated_input_tokens") or 0))
    delivered_bytes = max(0, int(batch_context.get("rendered_bytes") or 0))
    if records and not update_audit_batch_context(
        request.state,
        consumer=request.consumer,
        estimated_input_tokens=delivered_tokens,
        rendered_bytes=delivered_bytes,
        source_authority=request.source_authority,
    ):
        return _err(
            "Audit 批次已持久化，但在途上下文账更新失败；本次不继续交付，"
            "下次 pull 会从同一欠账批重投。",
            "AUDIT_CONSUMER_CONTEXT_UNAVAILABLE",
        )
    batch_context.update(_audit_pull_context_facts(budget, delivered_tokens))
    return None


def _audit_pull_context_facts(
    budget: _SpoolPullBudget,
    delivered_tokens: int,
) -> dict[str, int | float | str]:
    facts = budget.facts
    output_tokens = max(0, int(facts.get("model_output_budget_tokens") or 0))
    return {
        "context_limit_tokens": budget.safe_consumer_tokens,
        "context_limit_k_tokens": round(budget.safe_consumer_tokens / 1000.0, 2),
        "context_safe_batch_tokens": budget.safe_consumer_tokens,
        "model_output_budget_tokens": output_tokens,
        "model_output_budget_k_tokens": round(output_tokens / 1000.0, 2),
        "verdict_output_tokens_per_record": max(
            0,
            int(facts.get("verdict_output_tokens_per_record") or 0),
        ),
        "verdict_output_estimate_source": str(facts.get("verdict_output_estimate_source") or ""),
        "estimated_output_safe_records": max(
            0,
            int(facts.get("estimated_output_safe_records") or 0),
        ),
        "configured_batch_max_tokens": max(
            0,
            int(facts.get("configured_batch_max_tokens") or 0),
        ),
        "recovery_batch_max_tokens": max(
            0,
            int(facts.get("recovery_batch_max_tokens") or 0),
        ),
        "delivered_input_tokens": delivered_tokens,
    }


def _audit_batch_context_estimate(
    state: WatchState,
    agent: object | None,
) -> dict[str, int | float | str]:
    """Expose queue size and runtime-owned context estimates without reading content semantics."""
    from ..agent_core.model.call_runtime import max_output_tokens
    from ..agent_core.model.context_pressure import (
        model_visible_context_budget,
        safe_inline_tool_result_tokens,
    )
    from .harvester import read_spool_cursor, spool_unread, spool_unread_bytes

    unread_records = max(0, int(spool_unread(state)))
    unread_bytes = max(0, int(spool_unread_bytes(state)))
    budget = model_visible_context_budget(agent) if agent is not None else None
    safe_tokens = safe_inline_tool_result_tokens(agent) if agent is not None else 0
    output_tokens = max_output_tokens(agent) if agent is not None else 0
    cursor = read_spool_cursor(state)
    delivery_candidates = _audit_delivery_candidate_count(
        cursor,
        unread_candidates=unread_records,
    )
    output_budget = _audit_verdict_output_budget(
        cursor,
        unread_records=delivery_candidates,
        output_tokens=output_tokens,
    )
    input_budget = _audit_input_batch_budget(
        state,
        cursor,
        unread_records=unread_records,
        unread_bytes=unread_bytes,
        safe_tokens=safe_tokens,
    )
    return {
        "unread_records": unread_records,
        "delivery_candidate_records": delivery_candidates,
        "unread_bytes": unread_bytes,
        **input_budget,
        "context_window_tokens": int(budget.context_window_tokens if budget else 0),
        "compact_trigger_tokens": int(budget.compact_trigger_tokens if budget else 0),
        "current_context_tokens": int(budget.current_tokens if budget else 0),
        "remaining_to_compact_tokens": int(budget.remaining_to_compact_tokens if budget else 0),
        "model_output_budget_tokens": output_tokens,
        "model_output_budget_k_tokens": round(output_tokens / 1000.0, 2),
        **output_budget,
    }


def _audit_delivery_candidate_count(
    cursor: dict[str, Any],
    *,
    unread_candidates: int,
) -> int:
    """Return the rows the next model-facing delivery can actually contain.

    A partially settled Audit batch blocks fresh spool reads until every
    durable ``pending_acks`` entry is judged.  Budgeting a replay from only the
    fresh-unread counter therefore collapses a large pending batch to one row
    when just one newer record exists.  The in-flight debt is authoritative
    while present; otherwise the normal unread counter remains authoritative.
    """

    inflight = cursor.get("inflight")
    pending = inflight.get("pending_acks") if isinstance(inflight, dict) else None
    if isinstance(pending, list) and pending:
        return sum(1 for value in pending if str(value or "").strip())
    return max(0, int(unread_candidates or 0))


def _audit_verdict_output_budget(
    cursor: dict[str, Any],
    *,
    unread_records: int,
    output_tokens: int,
) -> dict[str, int | str]:
    # One unusually wide exception is reserved once; it is not multiplied by
    # every ordinary row. The per-record rate learns only serialized bytes.
    fixed_reserve = 1_024
    output_profile = (
        dict(cursor.get("verdict_output_profile") or {})
        if isinstance(cursor.get("verdict_output_profile"), dict)
        else {}
    )
    observed_output_records = max(
        0,
        int(output_profile.get("observed_records") or 0),
    )
    observed_output_bytes = max(
        0,
        int(output_profile.get("serialized_bytes") or 0),
    )
    observed_max_row_bytes = max(
        0,
        int(output_profile.get("max_row_bytes") or 0),
    )
    if observed_output_records and observed_output_bytes and observed_max_row_bytes:
        # estimate_tokens() elsewhere uses one token per roughly three UTF-8
        # bytes. Keep a 50% margin over the observed provider-facing average.
        # max_row_bytes is an additive exception reserve, not a per-record rate.
        # Do not retain the cold-start per-row reserve after the protocol has
        # produced durable evidence. If a later result shape grows
        # beyond the learned envelope, the still-pending delivery follows the
        # existing failure -> smaller replay path without losing or ACKing rows.
        observed_average_bytes = observed_output_bytes / observed_output_records
        verdict_output_tokens_per_record = max(
            1,
            math.ceil((observed_average_bytes * 1.5) / 3.0),
        )
        fixed_reserve += math.ceil(observed_max_row_bytes / 3.0)
        verdict_output_estimate_source = "observed_serialization"
    else:
        verdict_output_tokens_per_record = _AUDIT_INITIAL_VERDICT_OUTPUT_TOKENS_PER_RECORD
        verdict_output_estimate_source = "initial_protocol_reserve"
    estimated_output_safe_records = (
        max(
            1,
            (output_tokens - fixed_reserve) // verdict_output_tokens_per_record,
        )
        if unread_records and output_tokens > fixed_reserve
        else 0
    )
    return {
        "verdict_output_tokens_per_record": verdict_output_tokens_per_record,
        "verdict_output_estimate_source": verdict_output_estimate_source,
        "estimated_output_safe_records": min(
            unread_records,
            estimated_output_safe_records,
        ),
    }


def _audit_input_batch_budget(
    state: WatchState,
    cursor: dict[str, Any],
    *,
    unread_records: int,
    unread_bytes: int,
    safe_tokens: int,
) -> dict[str, int | float]:
    average_bytes = math.ceil(unread_bytes / unread_records) if unread_records else 0
    estimated_tokens_per_record = math.ceil(average_bytes / 3) if average_bytes else 0
    configured_ceiling = max(
        1,
        int(getattr(state.tuning, "guarantee_batch_max_tokens", 45_000) or 45_000),
    )
    recovery = (
        dict(cursor.get("batch_recovery") or {})
        if isinstance(cursor.get("batch_recovery"), dict)
        else {}
    )
    recovery_ceiling = max(0, int(recovery.get("max_batch_tokens") or 0))
    ceilings = [value for value in (safe_tokens, configured_ceiling, recovery_ceiling) if value > 0]
    effective_safe_tokens = min(ceilings) if ceilings else 0
    estimated_safe_records = (
        max(1, int((effective_safe_tokens * 3) / average_bytes))
        if unread_records and average_bytes and effective_safe_tokens
        else 0
    )
    return {
        "average_record_bytes": average_bytes,
        "estimated_tokens_per_record": estimated_tokens_per_record,
        "context_safe_batch_tokens": safe_tokens,
        "configured_batch_max_tokens": configured_ceiling,
        "recovery_batch_max_tokens": recovery_ceiling,
        "safe_batch_tokens": effective_safe_tokens,
        "safe_batch_k_tokens": round(effective_safe_tokens / 1000.0, 2),
        "estimated_safe_records": min(unread_records, estimated_safe_records),
    }


def _effective_audit_record_limit(
    *,
    requested_records: int | None,
    batch_context: dict[str, Any],
) -> int | None:
    """Bound a model request only by explicit transport resource budgets.

    Context bytes are enforced independently at complete-record boundaries and
    output capacity is learned from real serialized verdicts.  A runner work
    slice ending is a continuation boundary, not permission to mutate one
    durable debt into a tiny in-flight delivery.  The runner can therefore stop
    and resume from the same cursor without a near-expired slice pinning every
    later replay to one record.  No semantic rule or provider-name branch is
    involved.
    """
    limits: list[int] = []
    if requested_records is not None:
        limits.append(max(1, int(requested_records)))
    output_limit = max(
        0,
        int(batch_context.get("estimated_output_safe_records") or 0),
    )
    if output_limit > 0:
        limits.append(output_limit)
    return min(limits) if limits else None


def _resolved_pull_wait(state: WatchState, params: dict[str, Any]) -> float:
    """Resolve the time leg of the audit time/bytes batching race.

    An omitted audit wait gets a useful coalescing window. An explicit ``0`` is
    still an immediate diagnostic pull, and ordinary watches keep their legacy
    default. The byte threshold may wake the batch before this deadline.
    """
    default_wait = (
        min(
            _AUDIT_DEFAULT_BATCH_WAIT_SECONDS,
            float(state.tuning.max_wait_cap_seconds),
        )
        if state.audit_guarantee and "max_wait_seconds" not in params
        else 0.0
    )
    return _float_in(
        params.get("max_wait_seconds"),
        default_wait,
        float(state.tuning.max_wait_cap_seconds),
    )


def _pull_target_records(
    params: dict[str, Any],
) -> int | None | ToolHandlerOutcome:
    """Validate the Agent's optional per-call complete-record target."""
    if "target_records" not in params:
        return None
    raw = params.get("target_records")
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 100_000:
        return _err(
            "target_records 必须是 1 到 100000 的整数",
            "TOOL_INVALID_ARGUMENTS",
        )
    return raw


def _wait_for_spool_records(
    state: WatchState,
    max_wait: float,
    *,
    remote: bool,
    consumer: str = "",
    batch_target_bytes: int = 0,
    candidate_limit: int | None = None,
    source_authority: dict[str, object] | None = None,
    effective_audit_objective: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """长轮询 spool，按时间或累计体积任一门槛触发一批。

    正常记录不会因为体积门槛被截断；门槛只决定何时开始取批。单条超过门槛时仍整条
    交给后续 token 安全层处理。max_wait=0 的即取式 pull 也至少等一个收割节拍，避免
    收割者刚起步就空手返回。
    """
    from .harvester import (
        judge_quota,
        read_spool_cursor,
        read_spool_records,
        spool_unread,
        spool_unread_bytes,
    )

    deadline = time.time() + max(max_wait, state.tuning.poll_interval_seconds + 0.5)
    quota = judge_quota(state.tuning)
    while True:
        _refresh_if_remote(state, remote)
        now = time.time()
        window_complete = False
        if state.audit_guarantee:
            cursor = read_spool_cursor(state)
            inflight = cursor.get("inflight") if isinstance(cursor.get("inflight"), dict) else None
            has_pending = bool(
                inflight
                and isinstance(inflight.get("pending_acks"), list)
                and inflight.get("pending_acks")
            )
            window_complete = bool(
                state.watch_window_seconds > 0
                and now - float(state.opened_at or 0.0) >= state.watch_window_seconds
            )
            unread_count = spool_unread(state)
            unread_bytes = spool_unread_bytes(state)
            if not _audit_batch_ready(
                has_pending=has_pending,
                unread_bytes=unread_bytes,
                batch_target_bytes=batch_target_bytes,
                now=now,
                deadline=deadline,
                window_complete=window_complete,
            ):
                time.sleep(
                    min(
                        state.tuning.poll_interval_seconds,
                        max(0.1, deadline - now),
                    )
                )
                continue
            # Audit 的可取时机只由等待时间/累计体积决定。条数只是 Agent 对当前
            # 调用的领取意图；省略时取当前所有待判记录，再由字节/上下文边界截在
            # 完整记录之间。这样不会用固定 8/48 条人为压低吞吐。
            quota = candidate_limit or max(1, unread_count)
        # 消费口粮与判读反压同一把尺(judge_quota):直通批整批取走,别按旧上限剁成六截。
        records, backlog = read_spool_records(
            state,
            max_candidates=quota,
            consumer=consumer,
            max_bytes=batch_target_bytes,
            source_authority=source_authority,
            effective_audit_objective=effective_audit_objective,
        )
        if records or now >= deadline or window_complete:
            return records, backlog
        time.sleep(
            min(
                state.tuning.poll_interval_seconds,
                max(0.1, deadline - time.time()),
            )
        )


def _effective_audit_objective(agent: object, state: WatchState) -> str:
    """Read one exact active Audit objective; failures keep the prior batch config."""
    from ..common.audit_activation import AUDIT_OBJECTIVE_ATTR
    from ..conversation.audit_requirements import (
        audit_runtime_requirement_for_task,
        audit_runtime_requirement_text,
    )
    from ..conversation.authority import current_conversation_task_attributes

    task_id = str(state.audit_root_task_id or "").strip()
    published = audit_runtime_requirement_for_task(
        getattr(agent, "conversation_store", None),
        task_id,
    )
    if published:
        return published
    attrs = current_conversation_task_attributes(agent)
    task_projection = audit_runtime_requirement_text(attrs.get(AUDIT_OBJECTIVE_ATTR))
    return task_projection or audit_runtime_requirement_text(state.audit_objective)


# LLM: Audit batching is driven only by structured byte counters and clocks. Content never
# decides when a batch starts; requested record count only bounds one delivery after it is ready.
# 函数用途: 统一判断等待时间、累计体积或窗口结束是否已触发本轮研判。
def _audit_batch_ready(
    *,
    has_pending: bool,
    unread_bytes: int,
    batch_target_bytes: int,
    now: float,
    deadline: float,
    window_complete: bool,
) -> bool:
    return bool(
        has_pending
        or (batch_target_bytes > 0 and unread_bytes >= batch_target_bytes)
        or now >= deadline
        or window_complete
    )


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
            "系统不发新批；这些 ack_id 的结论全部提交后才签收。已提交的条目不会重新列入欠账。"
        )
    elif redelivered > 0:
        payload["redelivered_candidates"] = redelivered
        payload["redelivery_note"] = (
            f"本批 {redelivered} 条候选是上一任消费者取走后没确认判完的在途批,现在原样重投给你"
            "(接管续的不只是游标,还有这份缓冲区)。结论账本和 pending_ack_ids 是是否完成的"
            "权威事实，不能依据前任的自然语言说明猜测。"
        )
    gap = int(backlog.get("redelivery_gap_candidates") or 0)
    if gap > 0:
        payload["spool_redelivery_gap_candidates"] = gap


# LLM: This projection exposes every durable row in stream order; it never ranks or settles business meaning.
# 函数用途: 把持久队列中的完整候选批交给当前 Agent，只有单条超出上下文时才生成带标记视图。
def _render_spool_pull(
    state: WatchState,
    records: list[dict[str, Any]],
    backlog: dict[str, Any],
    harvester: dict[str, Any],
    model_event_max_tokens: int = 0,
    requested_records: int | None = None,
    effective_record_limit: int | None = None,
    safe_batch_tokens: int = 0,
    include_audit_objective: bool = True,
) -> dict[str, Any]:
    """spool 消费批 → 模型载荷；工具只交付记录，不自动判读、上报或决定委派。"""
    newest = records[-1] if records else {}
    delivery_ref = str(backlog.get("delivery_ref") or "").strip()
    durable_rows = [
        row for record in records for row in record.get("candidates") or [] if isinstance(row, dict)
    ]
    candidate_rows_this_call: list[dict[str, Any]] = []
    if state.audit_guarantee and delivery_ref:
        from .harvester import audit_verdict_token

        candidate_rows_this_call = [
            {
                "verdict_token": audit_verdict_token(
                    delivery_ref,
                    str(row.get("ack_id") or ""),
                ),
                **candidate_model_view(
                    row,
                    max_event_tokens=model_event_max_tokens,
                    include_triage=False,
                ),
            }
            for row in durable_rows
        ]
    else:
        candidate_rows_this_call = [
            candidate_model_view(
                row,
                max_event_tokens=model_event_max_tokens,
            )
            for row in durable_rows
        ]
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
        "source_binding": source_binding_block(state),
        "source_envelope": public_source_envelope(state.source_envelope),
        "source_spec_configured": bool(state.source_spec) and not state.audit_guarantee,
        "candidates": (
            candidate_rows_this_call
            if state.audit_guarantee
            else order_candidate_rows(candidate_rows_this_call)
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
    if state.audit_guarantee:
        from ..memory_archive import estimate_tokens

        rendered_bytes = len(
            json.dumps(
                candidate_rows_this_call,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        estimated_tokens = estimate_tokens(candidate_rows_this_call)
        payload["batch_context"] = {
            "requested_records": requested_records,
            "effective_record_limit": effective_record_limit,
            "delivered_records": len(candidate_rows_this_call),
            "rendered_bytes": rendered_bytes,
            "estimated_input_tokens": estimated_tokens,
            "estimated_input_k_tokens": round(estimated_tokens / 1000.0, 2),
            "safe_batch_tokens": max(0, int(safe_batch_tokens)),
            "safe_batch_k_tokens": round(max(0, int(safe_batch_tokens)) / 1000.0, 2),
            "bounded_by_complete_record_bytes": bool(
                requested_records is not None
                and len(candidate_rows_this_call) < requested_records
                and estimated_tokens >= max(1, int(safe_batch_tokens * 0.8))
            ),
        }
        if delivery_ref:
            payload["delivery_ref"] = delivery_ref
    if state.last_error:
        payload["last_source_error"] = state.last_error
    attach_audit_receipt(
        payload,
        state,
        include_objective=include_audit_objective,
    )
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
    attach_judgment_note(payload, state)
    # 高频命中类调查告警(spool 路):合并本消费批各记录的告警,与 inline pull 同契约;
    # 内容规则减负账同批汇总(命中数,零静默)。
    attach_frequent_hit_alert(
        payload,
        merge_frequent_hits([record.get("frequent_hits") or [] for record in records]),
    )
    attach_content_rules_count(payload, sum(int(r.get("normal_rule_hits") or 0) for r in records))
    # 续蹲/清账信号(spool 路补齐):此前只 inline 路调用,真机主路(spool 消费)拿不到
    attach_source_progress(payload)
    return payload


def _close_payload(state: WatchState) -> dict[str, Any]:
    payload = {
        "ok": True,
        "action": "close",
        "watch_id": state.watch_id,
        "source_binding": source_binding_block(state),
        "final_coverage": coverage_block(state, {}),
        "watch": watch_block(state),
    }
    # 不静默弃判(g8 不足4·末尾清账的账目半边):close 时 spool 还有已抬未确认判完的候选
    # (未读的 + 交付出去没 ack 的在途批),把数目如实亮进关闭回执——弃了多少一目了然;
    # 要盯完就先 pull 清账再 close(纯结构计数,不拦)。
    backlog = int(state.close_pending_records or 0)
    payload["spool_backlog_candidates_at_close"] = backlog
    attach_audit_receipt(payload, state)
    if backlog > 0:
        payload["discarded_backlog_note"] = (
            f"关闭时仍有 {backlog} 条已持久化记录没有确认结果。"
            "原始记录和 pending 覆盖状态会保留；因此当前覆盖账不能证明任务已完整处理。"
        )
    return payload


def _unjudged_spool_backlog(state: WatchState) -> int:
    """已抬进 spool 而未【确认判完】的候选数:未读 + 在途(交付出去没被确认判完)。
    与 audit_state.lane_unjudged_backlog 同一把尺(单消费者:一路一份读游标 ack 口径)。"""
    from .harvester import consumed_and_acked_on_disk

    written = int(state.totals.get("spool_candidates", 0) or 0)
    if written <= 0:
        return 0
    _consumed, acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    return max(0, written - acked)


def _audit_source_ready_callback(tool: object, state: WatchState):
    """Return the structured source wake callback only for named Audit watches."""

    if not state.audit_guarantee or not str(state.audit_root_task_id or "").strip():
        return None
    agent = getattr(tool, "agent", None)
    if agent is None:
        return None
    from .source_worker import wake_audit_source_worker

    return lambda ready_state: wake_audit_source_worker(agent, ready_state)


def _audit_source_finalized_callback(tool: object, state: WatchState):
    """Return the terminal projection callback only for named Audit watches."""

    if not state.audit_guarantee or not str(state.audit_root_task_id or "").strip():
        return None
    agent = getattr(tool, "agent", None)
    if agent is None:
        return None
    from .source_worker import settle_audit_source_worker

    return lambda finalized_state: settle_audit_source_worker(agent, finalized_state)


def _enriched_list_row(
    owner_home: Path,
    row: dict[str, Any],
    *,
    agent: object | None = None,
) -> dict[str, Any]:
    window = int(row.get("watch_window_seconds") or 0)
    elapsed = max(0.0, time.time() - float(row.get("opened_at") or 0.0))
    row["elapsed_seconds"] = round(elapsed, 1)
    row["window_complete"] = bool(window and elapsed >= window)
    if bool(row.get("audit_guarantee")):
        from .harvester import audit_receipt_facts
        from .watch_state import load_state

        state = load_state(
            owner_home,
            str(row.get("watch_id") or ""),
        )
        if state is not None:
            row["audit_receipt"] = audit_receipt_facts(state)
            from .source_worker import source_worker_facts

            row["source_worker"] = source_worker_facts(agent, state)
    return row


def _source_error_result(state: WatchState) -> ToolHandlerOutcome:
    body = {
        "ok": False,
        "watch_id": state.watch_id,
        "error": f"数据源拉取失败: {state.last_error}",
        "coverage": coverage_block(state, {}),
        "hint": "游标已持久化,源恢复后重新 pull 会从断点续读;连续失败可 status 查看并如实上报覆盖缺口。",
    }
    control_code, reported_code = _source_tool_error_codes(
        state.last_error_code or "NETWORK_REQUEST_FAILED"
    )
    return ToolHandlerOutcome(
        _TOOL_NAME,
        False,
        json.dumps(body, ensure_ascii=False),
        error_code=control_code,
        reported_error_code=reported_code,
    )


def _float_in(value: object, low: float, high: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return low
    return max(low, min(high, parsed))


def _ok_payload(
    payload: dict[str, Any],
    *,
    preserve_prompt_output: bool = False,
    runtime_transition: dict[str, object] | None = None,
) -> ToolHandlerOutcome:
    envelope = dict(payload)
    if preserve_prompt_output:
        # Audit batches are already bounded against the active model context and
        # preserve complete record boundaries.  Tell the shared projection layer
        # not to replace that safe batch with a generic head/tail preview.
        envelope["tool_output_policy"] = {"preserve_prompt_output": True}
    if runtime_transition:
        envelope["runtime_transition"] = dict(runtime_transition)
    return ToolHandlerOutcome(
        _TOOL_NAME,
        True,
        json.dumps(payload, ensure_ascii=False),
        result_envelope=envelope,
    )


def _context_refresh_transition(reason: str) -> dict[str, object]:
    return {
        "kind": "context_refresh",
        "reason": reason,
        "resume": "next_durable_slice",
    }


def _err(
    message: str,
    code: str,
    *,
    reported_code: str = "",
) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        _TOOL_NAME,
        False,
        json.dumps({"ok": False, "error": message}, ensure_ascii=False),
        error_code=code,
        reported_error_code=reported_code,
    )


_SOURCE_TOOL_CONTROL_ERRORS = {
    # The adapter owns source-specific diagnostics, while the shared tool
    # runtime owns the small stable control vocabulary.  Keep both, as 会话运行时
    # does for tool/runtime failures and 终端交互 does for input validation:
    # callers branch on the control class and repair from the reported cause.
    "SOURCE_ADAPTER_INVALID": "TOOL_INVALID_ARGUMENTS",
    "SOURCE_RECORD_KEYS_REQUIRED": "TOOL_INVALID_ARGUMENTS",
    "SOURCE_PAGE_TOO_LARGE": "TOOL_INVALID_ARGUMENTS",
    "SOURCE_ADAPTER_TIMEOUT": "TOOL_TIMEOUT",
    "SOURCE_ADAPTER_UNAVAILABLE": "TOOL_UNAVAILABLE",
}


def _source_tool_error_codes(reported_code: object) -> tuple[str, str]:
    """Return shared control code plus the exact Audit source diagnosis."""

    reported = str(reported_code or "UNKNOWN_ERROR").strip().upper()
    return _SOURCE_TOOL_CONTROL_ERRORS.get(reported, reported), reported


__all__ = ["WatchStreamTool"]
