"""审计 #21(low/i18n)真测:可配置 IANA 时区 + 周起始随 locale + CLI 时区标注 + 弃用 utcnow。

面向几十国客户:各自本地时区/夏令时/周起始约定不同,定时报表/对账/排班按服务器算会给错日期。
真造不同时区/坏时区/不同周起始,断言 now() 按用户墙钟、坏串安全回退不崩、周起始正确;真跑 prompt
日期注入按配置时区渲染带 %Z;benchmark 时间戳 tz-aware 可解析。学 长期助手 长期助手_time.py(env→
config→服务器本地,坏时区告警回退)。
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from agent_py_agent.agent.common import agent_time

# ---------- agent_time 核心 ----------

def test_now_uses_configured_zone(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_TIMEZONE", raising=False)
    tokyo = agent_time.now("Asia/Tokyo")
    assert tokyo.tzinfo is not None  # tz-aware
    assert tokyo.utcoffset() == datetime.now(ZoneInfo("Asia/Tokyo")).utcoffset()  # 东京墙钟偏移


def test_env_overrides_config_arg(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_TIMEZONE", "America/New_York")
    got = agent_time.now("Asia/Tokyo")  # env 应压过传入的配置时区
    assert got.utcoffset() == datetime.now(ZoneInfo("America/New_York")).utcoffset()


def test_bad_timezone_falls_back_safely(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_TIMEZONE", raising=False)
    assert agent_time.resolve_zone("Not/A_Zone") is None  # 坏串不抛、返回 None
    assert agent_time.resolve_zone("") is None  # 空=服务器本地
    fallback = agent_time.now("Mars/Phobos")  # 坏时区:回退服务器本地,仍 tz-aware,不崩
    assert fallback.tzinfo is not None


def test_week_start_by_locale() -> None:
    today = date(2026, 6, 17)  # 周三(weekday=2)
    assert agent_time.week_start_date(today, "monday").weekday() == 0  # 周一起始
    assert agent_time.week_start_date(today, "sunday").weekday() == 6  # 美国常用周日起始
    assert agent_time.week_start_date(today, "saturday").weekday() == 5  # 中东常用周六起始
    for ws in ("monday", "sunday", "saturday"):
        start = agent_time.week_start_date(today, ws)
        assert start <= today and (today - start).days < 7  # 本周起始落在过去 7 天内
    assert agent_time.week_start_date(today, "garbage").weekday() == 0  # 未知值退回 monday


# ---------- prompt 日期注入按配置时区 ----------

def test_prompt_context_renders_in_configured_timezone(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.prompting_parts.builder import _workspace_context_text

    monkeypatch.delenv("AGENT_TIMEZONE", raising=False)
    builder = SimpleNamespace(root=str(tmp_path), config=SimpleNamespace(timezone="Asia/Tokyo", week_start="monday"))
    text = _workspace_context_text(builder)
    expected_date = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    assert f"current_local_date: {expected_date}" in text  # 按东京时区算日期(非服务器本地)
    time_line = next(line for line in text.splitlines() if "current_local_time" in line)
    assert time_line.strip().endswith(("JST",)) or "JST" in time_line  # 带时区缩写 %Z(跨区不歧义)


def test_prompt_context_empty_timezone_is_server_local(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.prompting_parts.builder import _workspace_context_text

    monkeypatch.delenv("AGENT_TIMEZONE", raising=False)
    builder = SimpleNamespace(root=str(tmp_path), config=SimpleNamespace(timezone="", week_start="monday"))
    text = _workspace_context_text(builder)
    assert "current_local_date:" in text  # 空时区:服务器本地,不崩、照常渲染


# ---------- benchmark 弃用 utcnow 修复 ----------

def test_benchmark_timestamp_is_tz_aware_iso() -> None:
    # 真跑 run_speed_benchmark(echo 后端)走到 tested_at 生成处(审计 #21:弃用 utcnow→tz-aware)。
    # 顺带修了 run_speed_benchmark 的死导入 create_backend→get_backend,否则该函数根本跑不起来。
    from agent_py_agent.agent.model_speed.benchmark import SpeedBenchmarkParams, run_speed_benchmark
    from agent_py_agent.agent.settings import AgentConfig

    config = AgentConfig(model_backend="echo", model_name="echo")
    profile = run_speed_benchmark(config, params=SpeedBenchmarkParams(input_sizes=[100], output_size=10))
    assert profile.tested_at.endswith("Z")  # 保留 Z 后缀(向后兼容)
    parsed = datetime.fromisoformat(profile.tested_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None  # tz-aware,可解析(非弃用 utcnow 手拼)
