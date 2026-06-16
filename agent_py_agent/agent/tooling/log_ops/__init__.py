
from __future__ import annotations

"""安全日志运营内置能力(log_ops)—— 两层分离:确定性采集 daemon + LLM 研判工具。

设计动机(见 MEMORY: Native Tool-Use Gap / 高吞吐安全日志监控):
高吞吐安全日志监控场景下,让 LLM 子代理常驻读全量日志会烧爆 token 且根本扛不住量。
所以这里把"扛量+不丢"和"研判"彻底分两层:

  第①层 确定性采集 daemon(本包 collector/archive/triage/daemon,零 LLM):
    独立 Python 进程,后台循环每 N 秒采集一轮。三种源各自记 offset/已处理文件集合/cursor
    到状态文件,重启从断点续采"不重不丢";每条原始日志全量 append 落 archive(一条不丢的
    根本保证);确定性规则库(关键词/正则)初筛命中即产候选告警 append 到候选队列。
    数天数月地跑,确定性循环不调一次 LLM。

  第②层 LLM 工具(本包 tools,模型可调用,精确 parameter_schema 对齐原生 tool_use):
    log_monitor_start/stop 起停 daemon、log_monitor_status 查不丢对账、log_alert_poll 拉新
    候选给主代理研判、log_source_query 在存档里确定性 grep 交叉验证、log_archive_stats 全量
    存档统计。研判工具返回的全是确定性真实结果(防幻觉:主代理判断必须基于这些证据)。

不丢的三道保证:
  1. collector 记断点(文件 byte offset / 文件夹已处理文件名集合 / API cursor)到状态文件,
     状态文件原子写(temp+replace),崩溃重启从断点续采,不重不丢。
  2. 每条采集到的原始日志先全量 append 到 archive 再做初筛 —— 即使初筛/研判漏了,原始日志
     都在 archive 里可 grep 回溯。
  3. log_monitor_status 做"已采集行数 vs 存档行数"对账,给出不丢证据。
"""

__all__ = [
    "collector",
    "store",
    "triage",
    "daemon",
    "manager",
    "tools",
]
