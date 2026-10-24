# Linux shell 第一阶段提速与安全边界

日期：2026-10-23。基线：`e1ab9441f3d78fa778de83155281e49abc272975`。
来源：c3 开发任务及 `astra_linux_tool_latency_eval.md` §2/§3。

## 本次范围

- `path_access_policy.py`：冻结 `HostSandboxPaths`，每次 Full Access 准备只枚举一次 owner；普通 owner 不枚举他人。只读、隐藏、macOS 文件/目录正则复用原声明与叶函数。没有跨请求 TTL，后续调用重新发现新增 owner 和托管目录。
- `tooling/shell.py`：一次投影进入原 `AttemptSandboxSpec`；保留旧公开路径函数签名，不扩张 `_sandbox_exec` 参数。`ShellTool` 类本体未改。
- 新增 `tooling/process_pipe_pump.py`：POSIX 一条 reader 线程、一个 selector，监听两条输出和 self-pipe。空闲 `select()` 无固定 10ms 超时；每事件只读一块以保证流和停止通知公平。
- `tooling/process_output_capture.py`：唯一字节/EOF/错误账本，每流独立 4MiB 保留上限；超限继续 drain。短读、EAGAIN、真实 EOF、读错、UTF-8 replacement 独立报告。Windows 留双 reader 路径。
- `shell.py` 与 `attempt/process_run.py`：以 generation 条件等待替代输出等待的盲 sleep；在检查前采样 generation，防丢唤醒。前台仍先经 `finish_foreground_process` 按出生身份冻结/清理后代，再取得退出码。两流 EOF 不能当进程退出；同步 attempt 两流结束后保留有界 `proc.wait()`，不把末等待片内正常退出报成超时。
- 新增 `artifacts/registry_projection_cache.py` 并接 `registry.py`：每次仍通过原 no-follow reader 读取完整 raw。按可信词法根及安全打开目录的设备/inode、解析版本分区；只有完整 raw 相等才命中。每根只留最近一版，LRU 深层对象预算上限 8MiB，大投影完整解析但不缓存。IO、JSON 解析和体积核算均在缓存锁外。

原 JSON 容器上的字段转换先于冻结，不能把非法列表键冻成合法 tuple。投影保存全部历史行及解析错误，latest 与 operation 保留各自原错误口径。latest 返回独立记录/metadata/诊断；operation 重新判断本次读取中的历史匹配，不缓存“已提交”结论。目录身份检查不是用 stat 替代 raw 内容证明。

## 未改变的边界

未修改 `artifacts/shell_protection.py`、Landlock 启动器、隔离模型、端口集合、bwrap 协议或宿主账本耐久顺序。未用 mtime/size/inode 跳过产物内容 hash，没有减少 fsync，没有无沙箱重跑。未实现第二阶段 C helper、ruleset FD 缓存或 forkserver。

采集停止只停止本地读取，不是进程树已死的证明。capture 初始化/线程启动失败会释放自管道；前台初始化失败沿原出生身份终止入口清理并保留首个异常。身份不可核验时仍保留原失败，不另开裸 PID 杀进程旁路。

## 新增测试与复现

`test_linux_shell_latency.py`：单线程/单 selector、无空醒、generation 先发生变化、显式 stop、EAGAIN/短读、单流 EOF、Windows 分派兼容、事件等待接线、路径单次枚举/刷新/原投影对照、初始化失败清理、EOF 后期限观察、三种空备份集合。Windows 用例只模拟分派，不是 Windows 实机验证。

`test_registry_projection_cache.py`：同 raw 只 loads 一次但每次安全读；metadata 深层独立副本；同 stat 不同正文、原子替换、根目录换代、LRU/超大 raw、坏行诊断与旧解析器对照、当前读取拒绝不被旧缓存吞掉、非法容器转换及 latest/operation 不同字段错误合同。

任务指定回归（在工作树根执行）：

```bash
python3 -m pytest agent_py_agent/tests/test_landlock_launcher.py agent_py_agent/tests/test_sandbox_output_capture.py agent_py_agent/tests/test_shell_foreground_cleanup.py agent_py_agent/tests/test_host_files_access.py agent_py_agent/tests/test_artifact_registry.py agent_py_agent/tests/test_tools/test_shell_tool.py agent_py_agent/tests/test_linux_shell_latency.py agent_py_agent/tests/test_registry_projection_cache.py -q --tb=short
ruff check agent_py_agent/agent/tooling/shell.py agent_py_agent/agent/path_access_policy.py agent_py_agent/agent/tooling/process_output_capture.py agent_py_agent/agent/tooling/process_pipe_pump.py agent_py_agent/agent/attempt/process_run.py agent_py_agent/agent/artifacts/registry.py agent_py_agent/agent/artifacts/registry_projection_cache.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
```

## 性能观察口径

本地 macOS 微测，不是目标 2C4G Linux 或 Gateway 端到端时延。用 `git show <基线>:.../registry.py` 在独立模块中载入基线，当前实现与基线读取同一份 1000 行、112670 字节的临时登记表；每个暖查询各执行 20 次取中位数，均包含真实安全全读。

| 查询 | 基线暖中位数 | 本次暖中位数 |
| --- | ---: | ---: |
| latest report | 2.353ms | 2.119ms |
| operation 历史匹配 | 0.897ms | 0.416ms |

同次测量 latest 冷查询为基线 2.619ms、本次 19.571ms：冻结、字段转换和深层内存预算带来冷成本。不能把暖命中收益外推到持续变化的 raw、超预算文件或完整 run_command。总体 Linux 时延收益未验证。目标机应分别量沙箱准备、Python/Landlock/bwrap、业务命令、管道等待、产物 prepare/postcheck 与账本落盘，记录数据规模、冷暖与并发，不把工具总耗时归因于单个热点。

## 当前验证限制

当前局部命令为三文件加 `-k 'not process_waiters' -o addopts=''`，结果 **43 passed、2 deselected**；另单跑 `test_process_waiters_use_capture_events[attempt]` 得到 **1 passed**。这不等于被排除的真实 shell 等待/清理入口通过。当前九个改动 Python 文件 Ruff 通过，严格尺寸门禁 `hard=0、blocked=False`，差异格式检查通过；门禁生成的尺寸报告不随功能提交。

本地执行 `/bin/ps` 明确报 `Operation not permitted`，前台出生身份/僵尸核验无法取得真实事实。指定范围加 `-x -o addopts=''` 的检查得到 **45 passed、3 skipped、1 failed**，首个失败为 `test_natural_exit_cleans_child_after_outer_pipes_close[False-0]` 中出生身份为空；停止于首失败，后续未执行项不能记通过。更早不提前停止的相关检查持续遇到拒绝/超时，已停止，不作为全套结果。

真实 Linux 隔离、完整指定回归及实际 Windows 平台尚未验证。合流前须在具备原进程观测权限的目标环境运行上面未加 `-x` 的完整命令；不通过不能据此文档宣布完成验收。
