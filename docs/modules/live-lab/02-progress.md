# Live Lab Progress

## 2026-09-09 R224 测试说明与命名统一

测试助手采用 MY_AGENT_TEST_*，私有密码或 SSH 密钥，不保留源码写死凭据；历史实验源码目录走
MY_AGENT_CHECKOUT，公开历史路径和会话改中性占位。本次不执行旧故障注入、轮询或模型任务。
44项相关定向、Shell语法、SSH替身通过；单Gateway真实TUI ma-r224-brand 验证启动、帮助与状态，零新增模型请求。

## 2026-09-09 R223 静态错误和真实验收分层

启用 F821/F811 后修复 `main_agent_limit_cases._assert_storm_report/_assert_interrupt_honesty` 的未定义 lab：
调用方显式传入当前 LiveLab 实例，不从全局变量猜测试环境。测试辅助不能替被测 Agent 补业务文件。
本轮 87 项审计将定向故障注入与真实 TUI 分开记录；多路 TUI 共享一个 Gateway，测试说明与观察命令
见 R223 台账。没有外部 IM/Windows/跨 harness 公平对照的项目保持未验。

当前 Live Lab 已包含 `compact-stress` 真实压测入口，用来验证主代理在约 10MB 长文本、200K 上下文窗口、70% 自动 compact 条件下，能否多次 compact 后继续同一任务并产出完整报告。需要更长档位时可用 `MY_AGENT_COMPACT_STRESS_SIZE_MB=50` 单独跑。
该 suite 默认把 `tool_read_max_chars` 设为 100000，让 200K 上下文窗口下的长文本读取以较大页推进，避免测试人为制造过多模型往返；验收合同同时要求 `read_file` coverage ledger 连续覆盖源文件，防止只靠局部搜索或口头声明通过。
2026-06-07 起，compact stress 的固定事实 token 和章节标记只存在于 Live Lab fixture 里，用来验证长文本覆盖和 compact 续接；生产 runtime 不用这些自然语言 marker 做状态判断。
同日补充：自动 closeout 的软提示和 finalization 自动验收都必须服从显式 `target_coverage_contract` 的结构化完成状态；源文件覆盖未完成或 required artifact 尚未写出时，不因普通 `read_file` 进度反复自动 submit。

运行示例：

```bash
python3 scripts/live_agent_lab.py --suite compact-stress --real-llm --timeout 900
```

该 suite 会检查最终报告覆盖关键事实，并统计本轮运行目录里的 compact apply ledger；compact 次数不足会直接失败。

2026-06-05 的真实对照测试暴露了几个主链路要求：

- 默认 chat/cli/gateway 主链路不跑 delivery materializer。真实 compact 压测要依赖工具读取游标、coverage ledger、task work 草稿和外部校验，不把普通自然语言需求变成隐藏 `target_coverage_contract` 硬门。
- runtime fact / compact work state 必须保留已存在的显式 `target_coverage` 摘要；否则多轮 compact 后会只剩目标产物路径和自然语言 offset 提醒，覆盖合同会在恢复链里变弱。
- 长文本任务不应强迫模型纯顺序翻页。`search_text`/grep 可以作为章节定位器和范围索引，但 required `full_source_read` 的通过条件仍是已归档 `read_file`/`read_artifact` 窗口或 coverage ledger 证明。
- Live Lab 删除未使用的 stdout 文本 marker 断言。真实测试通过/失败只看命令 exit code、summary JSON、结构化产物和持久化状态，不再用模型自然语言回复里的固定短语判断子代理链路是否通过。

2026-06-26：移除 `log-analysis` suite 与 `log_analysis_replay` case（删除 `log_analysis_replay.py`/`log_analysis_replay_stages.py`）——随 log_analysis 安全日志子系统整体删除，Live Lab 不再覆盖该离线 replay 场景。

2026-07-10：真实模型模式增加真实 preflight，不再只检查 key 是否存在；错 key、空正文、echo 或请求异常
会在进入长 suite 前失败。新增 `tool-recovery` suite，用不存在的普通输入验证结构化失败、继续读取有效来源、
最终产物和“不得创建缺失输入”副作用边界。本地 Qwen 已通过 `main-artifact` 和 `tool-recovery`；完整证据
见 `docs/audits/REAL_LLM_24H_HARDENING_20260710.md`。

同日 current-wheel 复验：`tool-recovery` 133.13 秒通过；`real` suite 的 gateway ask 52.72 秒、
单子代理长链 275.72 秒通过，子代理最终 `DONE/VERIFIED` 且有 4 条工具证据。真实 suite 仍是隔离
harness 证据，不替代两台常驻服务实例的部署升级或 24 小时长稳。

2026-07-18：Skill policy 已删除无运行语义的 `pin_versions` 字段，compact-stress fixture 同步移除该键；
Skill 的逐轮稳定性由唯一 snapshot 内的 stable ID + SHA-256 保证，Live Lab 不再播种失真的配置。
