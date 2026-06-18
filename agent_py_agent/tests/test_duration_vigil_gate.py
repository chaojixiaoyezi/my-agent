
from __future__ import annotations

"""防回归:uncontracted 路径下「持续值守类长任务时长未达即引导继续」的一次性软门。

根因(日志运营 2 小时真机实锤):给主代理「用 log_ops 持续监控研判,运营至少 2 小时,每隔约
5 分钟 wait→poll 研判,持续到约 2 小时后再收尾」这类**无终点、靠时长驱动**的值守任务,主代理
只写了个中间值班笔记 shift_notes.md 到 work/,就被 uncontracted closeout「见产物可打开即判完成」
判完成、提前 5 轮 13 分钟退出。本门只在三要素同时成立时引导一次(明确持续值守意图 + 可解析的
时长要求 + 实际运行远未达标),且幂等放行绝不卡死;普通一次性任务(无值守意图/无时长)永不触发。
本测试锁死「该引导的引导、不该引导的(普通任务/时长够了/二次)放行」象限,并验证运行时长取自
work/timeline.jsonl 首条 created_at。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.delivery_closeout.duration_vigil_gate import (
    duration_vigil_rework,
)


def _request(prompt: str, tool_context: list | None = None, archive_tool_calls: list | None = None):
    return SimpleNamespace(
        params=SimpleNamespace(
            root_user_prompt=prompt,
            user_prompt=prompt,
            tool_context=tool_context if tool_context is not None else [],
            archive_tool_calls=archive_tool_calls if archive_tool_calls is not None else [],
        ),
    )


def _vigil_calls(n: int) -> list:
    """造 n 条成功的「值守进展」工具调用(wait/poll 等),用来驱动节流逻辑。"""
    tools = ["wait", "log_alert_poll", "log_source_query", "log_monitor_status"]
    return [{"tool": tools[i % len(tools)], "ok": True} for i in range(n)]


def _report(workspace_root: Path) -> dict:
    return {"artifacts": [], "workspace_root": str(workspace_root), "report_ref": "r.json", "ok": True}


def _seed_timeline(workspace_root: Path, *, started_minutes_ago: float) -> None:
    """在 work/timeline.jsonl 写一条 run_workspace_saved 首事件,起跑时间 = 现在 - N 分钟。"""
    work = workspace_root / "work"
    work.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
    work.joinpath("timeline.jsonl").write_text(
        json.dumps({"event_type": "run_workspace_saved", "created_at": started.isoformat()}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --- the case the regression is about: guides once -----------------------------


def test_blocks_when_vigil_intent_and_duration_and_elapsed_too_short(tmp_path):
    # 要求持续值班 2 小时,但才跑了 13 分钟 → 引导继续(不放行完成)。
    _seed_timeline(tmp_path, started_minutes_ago=13)
    req = _request("用 log_ops 工具持续监控研判,运营至少 2 小时,每隔约 5 分钟 wait 然后 log_alert_poll 拉候选研判")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is True
    assert report["ok"] is False
    assert report["duration_vigil_gate"]["finding"] == "VIGIL_DURATION_NOT_REACHED"
    # 注入一条「继续值班」软引导(带双语义:别交付、继续 wait→poll→研判→status)。
    assert any("[duration-vigil-rework]" in str(item) for item in req.params.tool_context)
    joined = "\n".join(str(item) for item in req.params.tool_context)
    assert "持续值守" in joined and "wait" in joined


def test_briefing_prompt_not_gated(tmp_path):
    """备课 prompt(含'长期持续监控'但本轮'先不要值班、等我确认')是一次性备课/汇报 run,门必须放行,
    不能把备课逼进值守循环(实测曾误判卡死 161 分钟)。"""
    _seed_timeline(tmp_path, started_minutes_ago=5)
    req = _request("我有10个API要做长期持续监控。先一起分析每个API、定好方案,汇报给我等我确认。这一轮先不要进入长时间值班。")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is False  # 备课不被误判为值守
    assert "duration_vigil_gate" not in report  # 没拦,不写拦截报告


def test_real_vigil_still_gated_despite_briefing_words(tmp_path):
    """真值守 prompt(直接'现在开始持续值班至少2小时')不含'先备课/等确认'否定信号,门照常拦。"""
    _seed_timeline(tmp_path, started_minutes_ago=10)
    req = _request("现在正式开始持续值班,运营至少 2 小时,每隔约5分钟 wait 然后 log_alert_poll 研判,坚持到2小时再收尾")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is True  # 真值守仍拦


def test_english_on_duty_phrasing_triggers(tmp_path):
    _seed_timeline(tmp_path, started_minutes_ago=5)
    req = _request("Keep monitoring the logs and stay on duty for 2 hours, poll every 5 minutes.")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is True


def test_minutes_unit_required_duration(tmp_path):
    # 要求值守 90 分钟,才跑 10 分钟 → 引导继续。
    _seed_timeline(tmp_path, started_minutes_ago=10)
    req = _request("持续值班监控日志 90 分钟,期间不断 poll 研判候选")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is True


# --- 持续拦(时长未达就每次都拦)+ 防紧密循环节流 -------------------------------


def test_persistently_blocks_while_under_duration_with_new_progress(tmp_path):
    # 时长未达 + 主代理被拦后正经做了新值守动作(wait/poll 累计涨了) → 继续拦,把它留在岗位上。
    _seed_timeline(tmp_path, started_minutes_ago=13)
    # 第一次拦时已有 2 次值守进展,标记里记 progress_at_block=2。
    marker = (
        "[duration-vigil-rework]\n"
        + json.dumps({"progress_at_block": 2, "finding": "VIGIL_DURATION_NOT_REACHED"}, sort_keys=True)
        + "\n继续值班循环。"
    )
    # 现在累计 4 次值守进展(>2)→ 说明被拦后又 wait→poll 了 → 应继续拦(不放行)。
    req = _request(
        "持续监控研判,运营至少 2 小时",
        tool_context=[marker],
        archive_tool_calls=_vigil_calls(4),
    )
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is True
    assert report["ok"] is False


def test_throttle_allows_when_no_new_progress_since_last_block(tmp_path):
    # 防紧密循环烧 token:被拦后**没干任何新值守活**就又想交付(累计进展没涨) → 放行,不陪空转。
    _seed_timeline(tmp_path, started_minutes_ago=13)
    marker = (
        "[duration-vigil-rework]\n"
        + json.dumps({"progress_at_block": 4, "finding": "VIGIL_DURATION_NOT_REACHED"}, sort_keys=True)
        + "\n继续值班循环。"
    )
    # 累计仍是 4(== progress_at_block)→ 紧密空转 → 安全阀放行。
    req = _request(
        "持续监控研判,运营至少 2 小时",
        tool_context=[marker],
        archive_tool_calls=_vigil_calls(4),
    )
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is False
    assert report["ok"] is True


def test_first_block_records_progress_baseline_in_marker(tmp_path):
    # 第一次拦截要把当前值守进展次数写进标记的 progress_at_block,供下次节流对比。
    _seed_timeline(tmp_path, started_minutes_ago=13)
    req = _request("持续监控研判,运营至少 2 小时", archive_tool_calls=_vigil_calls(3))
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is True
    marker = next(item for item in req.params.tool_context if "[duration-vigil-rework]" in str(item))
    payload = json.loads(str(marker)[str(marker).find("{"): str(marker).find("}") + 1])
    assert payload["progress_at_block"] == 3


def test_guidance_demands_wait_to_throttle_token_burn(tmp_path):
    # 引导话术必须明确要求"先用 wait 等待再继续",这样低频值守不烧 token。
    _seed_timeline(tmp_path, started_minutes_ago=13)
    req = _request("持续监控研判,运营至少 2 小时", archive_tool_calls=_vigil_calls(1))
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is True
    joined = "\n".join(str(item) for item in req.params.tool_context)
    assert "wait" in joined and "周期性" in joined


# --- duration satisfied → allow -----------------------------------------------


def test_allows_when_elapsed_meets_required_duration(tmp_path):
    # 要求 2 小时,已跑 118 分钟(>90% of 120) → 时长够了,放行完成。
    _seed_timeline(tmp_path, started_minutes_ago=118)
    req = _request("持续监控研判,运营至少 2 小时")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is False
    assert report["ok"] is True


# --- not the gate's business: must NOT block (no false positives) --------------


def test_normal_oneshot_report_task_not_blocked(tmp_path):
    # 普通一次性任务:写报告,无持续值守意图、无时长要求 → 永不触发。
    _seed_timeline(tmp_path, started_minutes_ago=1)
    req = _request("帮我写一份市场调研报告,放到 output 目录")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is False


def test_passing_mention_of_monitoring_without_duration_not_blocked(tmp_path):
    # 提了"监控"但既无持续值守语气也无时长要求 → 不触发(避免误伤)。
    _seed_timeline(tmp_path, started_minutes_ago=1)
    req = _request("帮我看一下监控面板现在有没有告警,有的话总结一下")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is False


def test_vigil_intent_but_no_duration_not_blocked(tmp_path):
    # 有持续值守语气但没给任何时长 → 解析不出目标秒数,不触发(时长门必须两要素齐)。
    _seed_timeline(tmp_path, started_minutes_ago=1)
    req = _request("持续值班盯着日志,有情况叫我")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is False


def test_no_timeline_means_conservative_allow(tmp_path):
    # 取不到起跑点(无 timeline)→ 保守放行,绝不凭空卡完成中的任务。
    req = _request("持续监控研判,运营至少 2 小时")
    report = _report(tmp_path)  # no timeline seeded
    assert duration_vigil_rework(req, report) is False


def test_started_epoch_taken_from_first_timeline_event(tmp_path):
    # 续跑会 append 新事件;起跑点必须取**首条**(最早),不被后续事件干扰。
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    early = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    late = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    work.joinpath("timeline.jsonl").write_text(
        json.dumps({"event_type": "run_workspace_saved", "created_at": early}, sort_keys=True) + "\n"
        + json.dumps({"event_type": "closeout", "created_at": late}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    req = _request("持续监控研判,运营至少 30 分钟")
    report = _report(tmp_path)
    # 以首条(20 分钟前)为基准,30 分钟要求未达标 → 引导;若误用末条(2 分钟前)同样会引导,
    # 故再补一个反例:要求 18 分钟,首条 20 分钟前已超 → 放行(证明用的是首条而非末条)。
    assert duration_vigil_rework(req, report) is True
    report2 = _report(tmp_path)
    req2 = _request("持续监控研判,运营至少 18 分钟")
    assert duration_vigil_rework(req2, report2) is False


# --- wiring: gate fires end-to-end through uncontracted closeout ----------------


def _vigil_closeout_params(tmp_path: Path, prompt: str):
    """构造一个 uncontracted 收口入参:work/ 顶层写了个中间值班笔记(真实产物),含 timeline。"""
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    work = tmp_path / "work"
    output = tmp_path / "output"
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    # 主代理写的中间值班笔记(就是真机里那个 shift_notes.md,落在 work/ 顶层)。
    note = work / "shift_notes.md"
    note.write_text("# 值班笔记\n\n02:13 已启动 daemon，候选 922，no_loss。\n", encoding="utf-8")
    # 起跑点:13 分钟前(远未到 2 小时)。
    started = (datetime.now(timezone.utc) - timedelta(minutes=13)).isoformat()
    work.joinpath("timeline.jsonl").write_text(
        json.dumps({"event_type": "run_workspace_saved", "created_at": started}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ToolLoopExecuteParams(
        user_prompt=prompt,
        root_user_prompt=prompt,
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes={
            "run_workspace": {
                "task_root": str(tmp_path),
                "output_dir": str(output),
                "work_dir": str(work),
            }
        },
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[
            {"tool": "write_file", "ok": True, "parameters": {"tool": "write_file", "path": str(note)}}
        ],
    )


def test_uncontracted_closeout_blocks_vigil_task_before_duration(tmp_path):
    """真机形态回归:持续值守 2 小时任务,写个 work/ 值班笔记就被判完成、提前退出。

    接入后:uncontracted 不再发 [MAIN_AGENT_DELIVERY_COMPLETE],而是引导继续值班循环。
    """
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        uncontracted_task_output_closeout_response,
    )

    prompt = "用 log_ops 工具持续监控研判，运营至少 2 小时，每隔约 5 分钟 wait 然后 log_alert_poll 拉候选研判，持续到约 2 小时后再收尾。"
    params = _vigil_closeout_params(tmp_path, prompt)
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))
    request = SimpleNamespace(agent=agent, params=params, backend="test")

    response = uncontracted_task_output_closeout_response(request, tmp_path)
    # 被引导继续 → 返回 None(非终态),不发交付完成。
    assert response is None
    # 软引导注入了「继续值班」提醒。
    assert any("[duration-vigil-rework]" in str(item) for item in params.tool_context)
    joined = "\n".join(str(item) for item in params.tool_context)
    assert "wait" in joined and "log_alert_poll" in joined


def test_uncontracted_closeout_allows_normal_task_no_false_positive(tmp_path):
    """对照:普通一次性任务(无持续值守意图/无时长)写产物 → 正常发交付完成,绝不误伤。"""
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        uncontracted_task_output_closeout_response,
    )

    prompt = "帮我整理一份本周安全告警摘要，写到 work 目录。"
    params = _vigil_closeout_params(tmp_path, prompt)
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))
    request = SimpleNamespace(agent=agent, params=params, backend="test")

    response = uncontracted_task_output_closeout_response(request, tmp_path)
    assert response is not None
    assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in response.text
    assert not any("[duration-vigil-rework]" in str(item) for item in params.tool_context)


# --- 无限期值守(没时间预算,只认喊停) ---


def test_indefinite_blocks_regardless_of_elapsed(tmp_path):
    # 无限期值守:即使跑了 10 小时,没喊停就继续拦(没有时长目标可达标)。
    _seed_timeline(tmp_path, started_minutes_ago=600)
    req = _request("用 log_ops 长期持续值守监控这些源,没有期限,一直盯到我喊停为止,发现威胁就汇报")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is True
    assert report["duration_vigil_gate"]["finding"] == "VIGIL_INDEFINITE_NOT_STOPPED"
    assert report["duration_vigil_gate"]["mode"] == "indefinite"


def test_indefinite_intent_without_duration_still_blocks(tmp_path):
    # 无限期(无明确时长)也拦 —— 扩展了原"值守但无可解析时长→放行"的逻辑。
    _seed_timeline(tmp_path, started_minutes_ago=5)
    req = _request("帮我无限期持续盯着这些日志源,直到我说停")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is True
    assert report["duration_vigil_gate"]["mode"] == "indefinite"


def test_indefinite_released_on_stop_signal(tmp_path):
    # 无限期 + 用户喊停标记(网关写 work/stop_vigil.flag)→ 放行优雅收尾。
    _seed_timeline(tmp_path, started_minutes_ago=30)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "stop_vigil.flag").write_text("stop", encoding="utf-8")
    req = _request("用 log_ops 长期值守监控,没有期限,一直盯到我喊停")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is False  # 喊停 → 放行


def test_stop_signal_also_releases_duration_mode(tmp_path):
    # 喊停对有时长模式也尊重:用户中途喊停,即使没到时长也放行。
    _seed_timeline(tmp_path, started_minutes_ago=13)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "stop_vigil.flag").write_text("stop", encoding="utf-8")
    req = _request("用 log_ops 持续监控研判,运营至少 2 小时,每隔约 5 分钟 wait 然后 log_alert_poll")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is False  # 喊停 → 放行


def test_duration_mode_not_misdetected_as_indefinite(tmp_path):
    # 有明确时长的值守不被误判为无限期(走时长模式,达标即放行,而非永久拦)。
    _seed_timeline(tmp_path, started_minutes_ago=200)  # 200 分钟 >> 2 小时*0.9=108
    req = _request("用 log_ops 持续监控研判,运营至少 2 小时,每隔约 5 分钟 wait 然后 log_alert_poll")
    report = _report(tmp_path)
    assert duration_vigil_rework(req, report) is False  # 时长达标放行,不是无限期永久拦
