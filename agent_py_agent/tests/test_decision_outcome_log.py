"""决策结果日志：decide() 的每个结果按接入点落一行结构化记录（无正文），审计按点位汇总。

2026-09-26 真机：TUI 里的模型用 audit_records 只看到选模型的观察，就断言其余接入点"没接线"。实际是其余点位的结果没有
任何持久记录，冷却跳过更不留痕；选模型每次最先超时又把整条连接冷却到 300 秒。本测试锁定：成功、超时、点位冷却都会落日志，
日志不含状态、题目或候选正文，审计的 decision 主题按点位给出次数。
"""
import json
import time
from types import SimpleNamespace

from agent_py_agent.agent.conversation import decision_outcome_log, decision_service
from agent_py_agent.agent.conversation.decision_outcome_log import (
    SCHEMA,
    append_decision_outcome,
    decision_outcome_row,
    decision_outcome_summary,
)
from agent_py_agent.agent.tooling.audit_records_tool import AuditQuery, _decision_owner_report
from agent_py_agent.tests.test_decision_curator_plugin_concurrency import (  # noqa: F401  复用本地假决策服务与宿主夹具
    configured,
    decide_background,
    decide_foreground,
    fresh_host,
    lanes,
)


# 函数用途: 造一个只有结构化字段的决策结果替身。
def _outcome(status="success", reason=""):
    return SimpleNamespace(mode="observe", status=status, reason=reason)


# 函数用途: 造一个决策阶段身份替身。
def _stage():
    return SimpleNamespace(scope="thread", thread_id="thread-1", run_id="run-1", task_id="task-1", experiment=False)


def test_row_keeps_only_structured_facts():
    row = decision_outcome_row(_stage(), "recall", _outcome("deadline", "provider_failed"), 1.2345)

    assert set(row) == {"schema", "created_at", "point", "scope", "mode", "status", "result_category", "reason",
                        "elapsed_ms", "thread_id", "run_id", "task_id", "experiment", "blocking"}
    assert (row["point"], row["status"], row["reason"], row["elapsed_ms"]) == ("recall", "deadline", "provider_failed", 1234)
    # 结果对象没有 blocking 字段（旧替身、skipped 行）时按同步记；后台 observe 的结果带 blocking=False 原样投影。
    assert row["blocking"] is True
    background = SimpleNamespace(mode="observe", status="success", reason="", blocking=False)
    assert decision_outcome_row(_stage(), "recall", background, 0.5)["blocking"] is False
    # 没有响应、也没有丢弃标记的旧替身：类别如实写“未记录可选结果”，不按状态猜成选中。
    assert row["result_category"] == "no_selection_recorded"


# 函数用途: 造一个带逐题答案的决策响应（selected 候选或非选择取值都由 value 决定）。
def _response(*values, errors=()):
    from agent_py_agent.agent.backends.decision_protocol import DecisionAnswer, DecisionResponse

    answers = tuple(DecisionAnswer(question_id=f"q{index}", kind="choice", value=value,
                                   error_code=errors[index] if index < len(errors) else "")
                    for index, value in enumerate(values))
    return DecisionResponse(binding=None, input_digest="d", requested_model="jev-latest", model="jev-1.13.0",
                            answers=answers, _usage_json=b"{}")


def test_result_category_separates_selection_non_selection_and_unrecorded():
    from agent_py_agent.agent.conversation.decision_outcome_log import decision_result_category

    def with_response(value, errors=()):
        return SimpleNamespace(mode="observe", status="success", reason="", response=_response(value, errors=errors))

    # 选中了某个候选（value 是候选标识，不是宿主已知的非选择取值）。
    assert decision_result_category(with_response("candidate_3")) == "selected"
    # 各类非选择原样进后缀，不新造同义码。
    for value in ("not_needed", "no_match", "abstain", "need_data"):
        assert decision_result_category(with_response(value)) == f"non_selection:{value}"
    # 多题混答：只要有一题真的选了候选，就是选中，不因别的题说不需要而误报非选择。
    assert decision_result_category(SimpleNamespace(mode="observe", status="success", reason="",
                                                    response=_response("not_needed", "candidate_1"))) == "selected"
    # 多题全非选择：去重后按名排序、以 + 连接；重复与题序不产生新键（S1）。
    assert decision_result_category(SimpleNamespace(mode="observe", status="success", reason="",
                                                    response=_response("not_needed", "not_needed", "not_needed"))) == "non_selection:not_needed"
    assert decision_result_category(SimpleNamespace(mode="observe", status="success", reason="",
                                                    response=_response("not_needed", "no_match"))) == "non_selection:no_match+not_needed"
    assert decision_result_category(SimpleNamespace(mode="observe", status="success", reason="",
                                                    response=_response("no_match", "not_needed"))) == "non_selection:no_match+not_needed"
    # 逐题错误那题不算选择，也不算非选择（这里唯一一题就错了，于是没有可判定结果）。
    errored = SimpleNamespace(mode="observe", status="success", reason="",
                              response=_response("candidate_1", errors=("bad",)))
    assert decision_result_category(errored) == "no_selection_recorded"
    # 一题错、另一题真的选了候选：整行仍算选中。
    mixed_ok = SimpleNamespace(mode="observe", status="success", reason="",
                               response=_response("candidate_1", "candidate_2", errors=("bad",)))
    assert decision_result_category(mixed_ok) == "selected"
    assert decision_result_category(SimpleNamespace(mode="observe", status="success", reason="",
                                                    response=_response("", errors=("bad",)))) == "no_selection_recorded"
    # 没有响应（超时、冷却、跳过等消费前结果）不猜类别。
    assert decision_result_category(_outcome("deadline", "provider_failed")) == "no_selection_recorded"


# 函数用途: 造一个带真实信封绑定的 apply 结果替身（丢弃补充行要从绑定里取点位）。
def _bound_outcome(*, point="delivery_quality", value="candidate_1", may_apply=True, errors=()):
    from agent_py_agent.agent.backends.decision_protocol import (
        DecisionAnswer,
        DecisionBinding,
        DecisionResponse,
    )

    binding = DecisionBinding(point=point, owner_ref="owner-1", operation_id="op-1", policy_revision="policy-1",
                              candidates_revision="rev-1")
    response = DecisionResponse(binding=binding, input_digest="d", requested_model="jev-latest", model="jev-1.13.0",
                                answers=(DecisionAnswer(question_id="q0", kind="choice", value=value,
                                                        error_code=errors[0] if errors else ""),), _usage_json=b"{}")
    return SimpleNamespace(mode="apply", status="success", reason="", may_apply=may_apply, response=response)


def test_result_category_records_host_drops_with_the_existing_reason_code(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.decision_outcome_log import (
        decision_result_category,
        record_decision_dropped,
    )

    path = tmp_path / "outcomes.jsonl"
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_decision_outcomes_jsonl=path))
    outcome = _bound_outcome()
    # 类别推导：带 dropped_reason 的对象判成 dropped:<码>（写补充行时走的就是这条）。
    marked = SimpleNamespace(mode="apply", status="success", reason="", response=outcome.response,
                             dropped_reason="policy_changed")
    assert decision_result_category(marked) == "dropped:policy_changed"

    def rows():
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    # 消费者丢掉建议：另追加一行补充记录，带宿主原有原因码与同一点位/身份，不复制正文。
    record_decision_dropped(agent, _stage(), outcome, "policy_changed")
    row = rows()[0]
    assert (row["result_category"], row["record_kind"]) == ("dropped:policy_changed", "dropped")
    assert (row["point"], row["mode"], row["status"]) == ("delivery_quality", "apply", "success")
    assert (row["scope"], row["thread_id"], row["run_id"], row["task_id"]) == ("thread", "thread-1", "run-1", "task-1")
    assert set(row) == set(decision_outcome_row(_stage(), "delivery_quality", _outcome(), 0.0)) | {"record_kind"}
    # 空原因码不改判；本来就没有可采用的建议（非 apply、无响应）不记丢弃。
    record_decision_dropped(agent, _stage(), outcome, "")
    record_decision_dropped(agent, _stage(), SimpleNamespace(mode="observe", status="success", reason="",
                                                             may_apply=False, response=outcome.response), "deadline")
    # M1：模型没选（not_needed）时即使要丢弃也不追加 dropped 行——主行已是 non_selection:…，
    # 不能把“没选”报成“选了被丢”；真选中 + 期限过则仍然恰好一行 dropped（被丢弃是选中的子集）。
    record_decision_dropped(agent, _stage(), _bound_outcome(value="not_needed"), "adoption_deadline")
    assert len(rows()) == 1
    record_decision_dropped(agent, _stage(), _bound_outcome(value="candidate_9"), "adoption_deadline")
    assert len(rows()) == 2 and rows()[-1]["result_category"] == "dropped:adoption_deadline"
    # 所有题都带 error_code：类别是 no_selection_recorded（没有可判定结果），即使宿主想丢弃也不登记 dropped——
    # 守卫是 != "selected"，"没有可判定结果"和"非选择"一样都不放行（M1 的延伸，be 变异曾让这条存活）。
    record_decision_dropped(agent, _stage(), _bound_outcome(value="", errors=("bad_answer",)), "sources_changed")
    assert len(rows()) == 2
    # 写盘失败只记日志：调用返回 None、不抛异常，主链路结果不变。
    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(decision_outcome_log, "append_jsonl_capped", broken)
    assert record_decision_dropped(agent, _stage(), outcome, "deadline") is None


def test_summary_counts_result_categories_and_marks_old_rows_unrecorded(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    now = time.time()
    rows = [{"point": "recall", "status": "success", "result_category": "selected"},
            {"point": "recall", "status": "success", "result_category": "non_selection:not_needed"},
            {"point": "curator", "status": "success", "result_category": "dropped:policy_changed"},
            {"point": "planning", "status": "success"},
            {"point": "delivery_quality", "status": "success", "result_category": "dropped:policy_changed",
             "record_kind": "dropped"}]
    path.write_text("".join(json.dumps({"schema": SCHEMA, "created_at": now, **row}) + "\n" for row in rows),
                    encoding="utf-8")

    summary = decision_outcome_summary(SimpleNamespace(owner_decision_outcomes_jsonl=path), since=now - 60)

    # 没有字段的旧记录归 unrecorded，不按状态或 reason 猜类别；等次数时按类别名排序。
    assert summary["result_categories"] == [
        {"category": "dropped:policy_changed", "calls": 2}, {"category": "non_selection:not_needed", "calls": 1},
        {"category": "selected", "calls": 1}, {"category": "unrecorded", "calls": 1}]
    # 逐点位类别计数（含 dropped 补充行）：recall 两条、curator 一条、planning 旧记录归 unrecorded、
    # delivery_quality 的 dropped 补充行单独计在其点位下。
    assert summary["result_categories_by_point"] == {
        "recall": {"selected": 1, "non_selection:not_needed": 1},
        "curator": {"dropped:policy_changed": 1},
        "planning": {"unrecorded": 1},
        "delivery_quality": {"dropped:policy_changed": 1}}
    assert [row["result_category"] for row in summary["recent"]] == [
        "selected", "non_selection:not_needed", "dropped:policy_changed", None, "dropped:policy_changed"]
    # 丢弃补充行不是一次独立调用：进类别统计与最近行，但不进按状态的调用统计。
    assert summary["points"] == {"recall": {"success": 2}, "curator": {"success": 1}, "planning": {"success": 1}}


def test_summary_categories_by_point_cover_full_window_beyond_recent_rows(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    now = time.time()
    rows = [
        # 低频点位的结果在 1 小时前：24 小时窗口内、但在最近 20 行之外。
        {"point": "external_material_order", "status": "success", "result_category": "selected", "created_at": now - 3600},
        {"point": "external_material_order", "status": "success", "result_category": "dropped:sources_changed",
         "record_kind": "dropped", "created_at": now - 3600},
    ]
    # 最近 20 行全是另一个点位，把 recent 窗口占满。
    rows += [{"point": "delivery_quality", "status": "success", "result_category": "selected",
              "created_at": now - index} for index in range(20)]
    path.write_text("".join(json.dumps({"schema": SCHEMA, **row}) + "\n" for row in rows), encoding="utf-8")

    summary = decision_outcome_summary(SimpleNamespace(owner_decision_outcomes_jsonl=path), since=now - 86400)

    # 逐点位类别唯一来源按时间窗口全部行（含 dropped 补充行）计算，不受最近行数限制。
    assert summary["result_categories_by_point"]["external_material_order"] == {
        "selected": 1, "dropped:sources_changed": 1}
    assert summary["result_categories_by_point"]["delivery_quality"] == {"selected": 20}
    # recent 只保留最近 20 行：低频点位不在里面，但类别统计仍在。
    assert [row["point"] for row in summary["recent"]] == ["delivery_quality"] * 20
    # 窗口更窄时（60 秒内）低频点位的结果被排除，类别统计随之消失。
    narrow = decision_outcome_summary(SimpleNamespace(owner_decision_outcomes_jsonl=path), since=now - 60)
    assert narrow["result_categories_by_point"].get("external_material_order") is None


def test_adoption_review_records_drop_with_reason_code(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.decision_protocol import (
        DecisionAnswer,
        DecisionBinding,
        DecisionResponse,
    )
    from agent_py_agent.agent.conversation.decision_service import (
        DecisionOutcome,
        DecisionStage,
        decision_outcome_is_current,
    )

    path = tmp_path / "outcomes.jsonl"
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_decision_outcomes_jsonl=path))
    now = time.monotonic()
    stage = DecisionStage(operation_id="op-1", owner_ref="owner-1", thread_id="t-1", run_id="r-1", task_id="k-1",
                          started_at=now, deadline=now + 60)

    def make_outcome(*, thread="t-1", deadline=now + 60, may_apply=True):
        binding = DecisionBinding(point="delivery_quality", owner_ref="owner-1", operation_id="op-1",
                                  policy_revision="policy-1", candidates_revision="rev-1",
                                  thread_id=thread, run_id="r-1", task_id="k-1")
        response = DecisionResponse(binding=binding, input_digest="d", requested_model="jev-latest", model="jev-1.13.0",
                                    answers=(DecisionAnswer(question_id="q0", kind="choice", value="candidate_1"),),
                                    _usage_json=b"{}")
        return DecisionOutcome(mode="apply", status="success", response=response, may_apply=may_apply,
                               connection_revision="conn-1", deadline=deadline)

    def rows():
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    # 身份不再匹配：不采用，并留下既有原因码。
    assert decision_outcome_is_current(agent, SimpleNamespace(), stage, make_outcome(thread="t-2")) is False
    assert rows()[-1]["result_category"] == "dropped:identity_changed"
    # 采用期限已过：登记 adoption_deadline，不误用发送阶段码。
    assert decision_outcome_is_current(agent, SimpleNamespace(), stage, make_outcome(deadline=0.0)) is False
    assert rows()[-1]["result_category"] == "dropped:adoption_deadline"
    # 设置/策略变化（_stale 给出的既有码）原样进，不新造同义码。
    monkeypatch.setattr(decision_service, "_stale", lambda *args: "policy_changed")
    assert decision_outcome_is_current(agent, SimpleNamespace(), stage, make_outcome()) is False
    assert rows()[-1]["result_category"] == "dropped:policy_changed"
    # 复核通过：可采用，且不写丢弃记录。
    monkeypatch.setattr(decision_service, "_stale", lambda *args: "")
    assert decision_outcome_is_current(agent, SimpleNamespace(), stage, make_outcome()) is True
    assert len(rows()) == 3
    # 没有可采用的建议（observe 结果）不算丢弃。
    assert decision_outcome_is_current(agent, SimpleNamespace(), stage, make_outcome(may_apply=False)) is False
    assert len(rows()) == 3


def test_result_category_label_translates_both_halves_and_tolerates_the_unknown():
    from agent_py_agent.agent.conversation.decision_outcome_log import result_category_label

    assert result_category_label("selected") == "选中了某个候选"
    assert result_category_label("non_selection:need_data") == "没有选择（need_data）"
    assert result_category_label("dropped:privacy_url") == "建议被宿主丢弃（privacy_url）"
    # 旧记录没有字段、或宿主写了没登记过的类别：显示“未记录”/原样显示，不拒绝、不改写。
    assert result_category_label(None) == "未记录" and result_category_label("") == "未记录"
    assert result_category_label("unrecorded") == "未记录"
    assert result_category_label("brand_new:thing") == "brand_new:thing"


def test_row_write_failure_never_changes_the_decision_result(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import decision_outcome_log
    from agent_py_agent.agent.conversation.decision_outcome_log import record_decision_skip

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(decision_outcome_log, "append_jsonl_capped", broken)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_decision_outcomes_jsonl=tmp_path / "outcomes.jsonl"))
    # 写盘失败只记日志：调用返回 None、不抛异常，调用方原有决策结果不变。
    assert append_decision_outcome(agent, decision_outcome_row(_stage(), "recall", _outcome(), 0)) is None
    record_decision_skip(agent, SimpleNamespace(error_code="", enabled_points=("recall",), scope="thread",
                                                thread_id="t", run_id="r", task_id="k", experiment=False),
                         "recall", "privacy_url")
    assert not (tmp_path / "outcomes.jsonl").exists()


def test_append_writes_only_the_owner_path_and_stays_bounded(tmp_path, monkeypatch):
    path = tmp_path / "data" / "decision" / "outcomes.jsonl"
    # 没有规范路径的宿主（旧替身、无 owner 的入口）不写任何文件。
    append_decision_outcome(SimpleNamespace(home_paths=SimpleNamespace()), decision_outcome_row(_stage(), "recall", _outcome(), 0))
    assert not path.exists()

    monkeypatch.setattr(decision_outcome_log, "_MAX_RECORDS_COUNT", 3)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_decision_outcomes_jsonl=path))
    for index in range(5):
        append_decision_outcome(agent, decision_outcome_row(_stage(), f"p{index}", _outcome(), 0))

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["point"] for row in rows] == ["p2", "p3", "p4"]


def test_skip_rows_need_an_open_point_and_the_switch_and_carry_only_a_reason_code(tmp_path):
    from agent_py_agent.agent.backends.decision_protocol import (
        DecisionInputError,
        DecisionPrivacySkip,
    )
    from agent_py_agent.agent.conversation.decision_outcome_log import (
        material_or_skip,
        record_decision_skip,
    )

    path = tmp_path / "outcomes.jsonl"
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_decision_outcomes_jsonl=path),
                            config=SimpleNamespace(decision_skip_records_enabled=True))
    stage = SimpleNamespace(error_code="", enabled_points=("planning",), scope="thread", thread_id="thread-1",
                            run_id="run-1", task_id="task-1", experiment=False)

    def rows():
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    record_decision_skip(agent, stage, "planning", "request_too_long")
    assert [(row["point"], row["status"], row["reason"], row["thread_id"]) for row in rows()] == [
        ("planning", "skipped", "request_too_long", "thread-1")]
    # 点位未开启、阶段有错、或配置关闭时都不写。
    record_decision_skip(agent, stage, "delivery_quality", "request_too_long")
    record_decision_skip(agent, SimpleNamespace(**{**vars(stage), "error_code": "settings_busy"}), "planning", "x")
    agent.config.decision_skip_records_enabled = False
    record_decision_skip(agent, stage, "planning", "request_too_long")
    assert len(rows()) == 1
    agent.config.decision_skip_records_enabled = True
    # 材料准备遇到隐私跳过：返回 None 并记原因码；其它输入错误照常上抛。
    assert material_or_skip(agent, stage, "planning", lambda: ("state", "q", "rev")) == ("state", "q", "rev")

    def private():
        raise DecisionPrivacySkip("含 https://x.test/?k=secret-value")

    assert material_or_skip(agent, stage, "planning", private) is None
    assert rows()[-1]["reason"] == "privacy_url" and "secret-value" not in path.read_text(encoding="utf-8")

    def broken():
        raise DecisionInputError("坏材料")

    try:
        material_or_skip(agent, stage, "planning", broken)
    except DecisionInputError:
        pass
    else:
        raise AssertionError("non-privacy input errors must propagate")
    assert len(rows()) == 2


def test_summary_counts_by_point_inside_the_window_and_counts_bad_rows(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    now = time.time()
    rows = [{"created_at": now - 10, "point": "recall", "status": "success"},
            {"created_at": now - 5, "point": "recall", "status": "cooldown"},
            {"created_at": now - 7200, "point": "planning", "status": "success"}]
    path.write_text("".join(json.dumps({"schema": SCHEMA, **row}) + "\n" for row in rows) + "{not json\n", encoding="utf-8")

    summary = decision_outcome_summary(SimpleNamespace(owner_decision_outcomes_jsonl=path), since=now - 3600)

    # 冷却跳过没发请求，单列在 not_sent，不计入 Jev 的超时率与失败率（2026-09-28 口径）
    assert summary["points"] == {"recall": {"success": 1}} and summary["not_sent"] == {"recall": {"cooldown": 1}}
    assert summary["unreadable_rows"] == 1 and len(summary["recent"]) == 2
    assert decision_outcome_summary(SimpleNamespace(), since=0)["available"] is False


def test_decide_logs_success_timeout_and_point_backoff_and_audit_reports_points(tmp_path, lanes):  # noqa: F811
    env = configured(tmp_path, lanes, background_timeout=0.2)
    path = tmp_path / "owner-data" / "decision" / "outcomes.jsonl"
    env.host.home_paths.owner_decision_outcomes_jsonl = path
    lanes.blocked = {"curator"}

    decide_background(env)
    decide_foreground(env)
    env.bg_stage = decision_service.begin_decision_stage(env.host, env.background, operation_id="curator-lease-2",
                                                         scope="owner_background")
    decide_background(env)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(row["point"], row["status"], row["reason"]) for row in rows] == [
        ("curator", "deadline", "provider_failed"), ("skill_tool", "success", ""), ("curator", "cooldown", "point_backoff")]
    # 请求材料里的 lane 标记只在发给决策服务的请求体里，日志里不能出现。
    assert "lane" not in path.read_text(encoding="utf-8")

    query = AuditQuery(topic="decision", scope="owner", thread_id="", since=0.0, limit=20)
    report = _decision_owner_report("alice", env.host, [env.thread.thread_id], query)
    assert report["points"]["points"] == {"curator": {"deadline": 1}, "skill_tool": {"success": 1}}
    assert report["points"]["not_sent"] == {"curator": {"point_backoff": 1}}
    # 只看当前会话：与用量、观察同一可信范围，后台 curator 行没有会话编号，不混进来。
    current = AuditQuery(topic="decision", scope="current_thread", thread_id=env.thread.thread_id, since=0.0, limit=20)
    report = _decision_owner_report("alice", env.host, [env.thread.thread_id], current)
    assert report["points"]["points"] == {"skill_tool": {"success": 1}}


def test_summary_can_be_limited_to_trusted_threads_and_drops_background_rows(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    rows = [{"thread_id": "thread-a", "point": "recall", "status": "success"},
            {"thread_id": "thread-b", "point": "recall", "status": "deadline"},
            {"thread_id": "", "point": "curator", "status": "success"}]
    path.write_text("".join(json.dumps({"schema": SCHEMA, "created_at": time.time(), **row}) + "\n" for row in rows),
                    encoding="utf-8")
    home = SimpleNamespace(owner_decision_outcomes_jsonl=path)

    # 当前会话范围只看自己的行：其它会话与没有会话编号的后台点位都不混入。
    assert decision_outcome_summary(home, since=0, thread_ids=["thread-a"])["points"] == {"recall": {"success": 1}}
    assert decision_outcome_summary(home, since=0)["points"] == {"recall": {"success": 1, "deadline": 1},
                                                                  "curator": {"success": 1}}


def test_rows_with_a_provider_response_record_requested_and_actual_model(tmp_path):
    from agent_py_agent.agent.backends.decision_protocol import DecisionResponse

    response = DecisionResponse(binding=None, input_digest="digest", requested_model="jev-latest", model="jev-1.13.0",
                                answers=(), _usage_json=b"{}")
    success = SimpleNamespace(mode="observe", status="success", reason="", response=response)
    row = decision_outcome_row(_stage(), "model_selection", success, 0.4)
    assert (row["requested_model"], row["model_version"]) == ("jev-latest", "jev-1.13.0")
    # 没有供应商响应的行（超时、冷却、替身）不带这两个键，不补猜。
    timed_out = SimpleNamespace(mode="observe", status="deadline", reason="provider_failed", response=None)
    assert {"requested_model", "model_version"}.isdisjoint(decision_outcome_row(_stage(), "recall", timed_out, 2.0))


def test_summary_counts_actual_versions_per_requested_alias_and_recent_rows_show_them(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    now = time.time()
    rows = [{"point": "model_selection", "status": "success", "requested_model": "jev-latest", "model_version": "jev-1.13.0"},
            {"point": "recall", "status": "success", "requested_model": "jev-latest", "model_version": "jev-1.13.0"},
            {"point": "recall", "status": "success", "requested_model": "jev-latest", "model_version": "jev-1.14.0"},
            {"point": "planning", "status": "success", "requested_model": "jev-1.13.0", "model_version": "jev-1.13.0"},
            {"point": "recall", "status": "deadline"},
            {"point": "recall", "status": "success", "model_version": 7}]
    path.write_text("".join(json.dumps({"schema": SCHEMA, "created_at": now, **row}) + "\n" for row in rows),
                    encoding="utf-8")

    summary = decision_outcome_summary(SimpleNamespace(owner_decision_outcomes_jsonl=path), since=now - 60)

    assert summary["model_versions"] == [
        {"requested_model": "jev-latest", "model_version": "jev-1.13.0", "calls": 2},
        {"requested_model": "jev-1.13.0", "model_version": "jev-1.13.0", "calls": 1},
        {"requested_model": "jev-latest", "model_version": "jev-1.14.0", "calls": 1}]
    assert [row.get("model_version") for row in summary["recent"]] == [
        "jev-1.13.0", "jev-1.13.0", "jev-1.14.0", "jev-1.13.0", None, None]
    assert "model_version" not in summary["recent"][4], "旧行、失败行不补这个键"
    assert decision_outcome_summary(SimpleNamespace(), since=0)["model_versions"] == []


def test_a_real_success_through_the_transport_logs_the_version_the_provider_returned(tmp_path, lanes):  # noqa: F811
    env = configured(tmp_path, lanes)
    path = tmp_path / "owner-data" / "decision" / "outcomes.jsonl"
    env.host.home_paths.owner_decision_outcomes_jsonl = path

    assert decide_foreground(env).status == "success"

    row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    # 档案请求的是 jev-test，假服务回的实际版本是 jev-resolved-test：两者分开记，审计按对计次。
    assert (row["requested_model"], row["model_version"]) == ("jev-test", "jev-resolved-test")
    query = AuditQuery(topic="decision", scope="owner", thread_id="", since=0.0, limit=20)
    report = _decision_owner_report("alice", env.host, [env.thread.thread_id], query)
    assert report["points"]["model_versions"] == [
        {"requested_model": "jev-test", "model_version": "jev-resolved-test", "calls": 1}]


def test_audit_recent_rows_carry_a_plain_language_category_label(tmp_path, lanes):  # noqa: F811
    env = configured(tmp_path, lanes)
    path = tmp_path / "owner-data" / "decision" / "outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    env.host.home_paths.owner_decision_outcomes_jsonl = path
    now = time.time()
    # 三种真实形态 + 一条没有字段的旧记录：最近行原样保留结构化类别，另配大白话标签给用户看。
    rows = [{"point": "recall", "status": "success", "result_category": "selected"},
            {"point": "recall", "status": "success", "result_category": "non_selection:need_data"},
            {"point": "planning", "status": "success", "result_category": "dropped:sources_changed"},
            {"point": "curator", "status": "success"}]
    path.write_text("".join(json.dumps({"schema": SCHEMA, "created_at": now, **row}) + "\n" for row in rows),
                    encoding="utf-8")

    query = AuditQuery(topic="decision", scope="owner", thread_id="", since=now - 60, limit=20)
    report = _decision_owner_report("alice", env.host, [env.thread.thread_id], query)

    assert [(row.get("result_category"), row["result_category_label"]) for row in report["points"]["recent"]] == [
        ("selected", "选中了某个候选"), ("non_selection:need_data", "没有选择（need_data）"),
        ("dropped:sources_changed", "建议被宿主丢弃（sources_changed）"), (None, "未记录")]
