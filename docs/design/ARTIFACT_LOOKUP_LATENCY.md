# read_artifact 请求局部查找与安全打开

日期：2026-10-23。来源：开发任务 `dev_linux_lat_c1` 及其空路径/干净父目录补充规范；基线 `e1ab9441f3d78fa778de83155281e49abc272975`。

## 范围与唯一入口

本切片只修改 `agent_py_agent/agent/tooling/artifact.py` 和 `agent_py_agent/agent/memory_archive/artifact/reader.py`，新增两个测试文件和本说明。共享的路径发现、流式索引解码和 nofollow 原语保持不动；旧测试及根级 TESTS、CODEBASE_TREE、DESIGN_LEDGER 由集成者统一维护。

- `PreparedToolOutputArtifactRead` 是冻结的、请求局部的宿主 Python 值，包含 request、规范 root、规范 ref、一次发现的 index_paths 及命中记录浅副本。
- read/estimate 的 `prepared` 是 keyword-only，不进入模型 JSON Schema。只有 root/ref/run/task/request/scope 全部一致才复用；内容窗口 offset/max_chars/mode/query 可变。错配重新准备。
- 计划不是持久缓存、文件事务快照或权限凭证。下一请求重新发现目录、重新计算匹配；读取仍执行当前 scope 硬门、安全打开、完整正文哈希。

## 预算与查找次数

| 分支 | 预留前工作 | 正文读取 |
|---|---|---|
| max_chars > 0（含默认4000） | 直接按指定字符数 reserve，不扫描索引 | 自行 prepare 一次 |
| max_chars <= 0，预算关闭或无 run_id | reserve(0)，不为预算 estimate | 自行 prepare 一次 |
| max_chars <= 0，有效预算且有 run_id | prepare 一次，同一计划 estimate 后 reserve | 复用同一计划 |

有预留的读取始终在 finally 中 settle；成功按实际返回字符收费，失败/异常释放 pending。不将元数据 size_bytes 当作字符数、实际费用、授权或内容证明。

## 匹配兼容与查询内解析缓存

`_ArtifactIndexCandidate` 保存一次计算的 exact/basename 结果和延迟路径异常。扫描不保存无关历史对象，所有索引扫描完后再报告路径异常；索引 IO 失败整份丢弃，晚到 UTF-8 及 JSON 资源错误沿原优先级处理。

保留最后匹配、run/task/request 的旧 OR 选择、冒号 ref 兼容、current_run 的精确 run 硬门、空 path stub 和 .json 路径的唯一 basename 回退；两条相同 path 记录仍算两条，不去重成唯一。

单次 query 的 `_ArtifactPathCache` 只保存解析线索：

1. 空 path 的 `Path("").resolve()` 至多求值一次，仍保留路径形引用匹配当前目录的旧语义。
2. 规范 POSIX 绝对路径仅在父目录 realpath 不变且 stat 为真实目录后缓存“干净父目录”。叶 lstat 是普通非 symlink 或 ENOENT 时直接复用规范路径；symlink、非干净父目录及其它 OSError 回原 resolver。
3. 相对路径、~、..、双根、异常路径和 Windows 沿原实现。缓存不跨 query，不跳过最终安全打开。

仍严格解码历史中的无关行，不用命中即早退、裸字节 prescreen 或省 hash 来改变异常合同。无界历史扫描不承诺固定毫秒上界。

## 路径打开与兼容边界

发现的 index_paths 及其父目录只用于定位。逃出规范认证 root 的发现路径不被解析，也不能把已 resolve 到外 owner 的 tool_outputs 目录自授权。允许正文根由本次 index_paths 投影，不再另一次 glob。

规范 root 用于包含判断和相对路径计算，实际索引/正文打开保留原请求的宿主认证根，复用 `open_readonly_file_beneath` 的逐段 dirfd、nofollow、普通单链接文件与打开身份校验。认证根本身不能预置或中途变成 symlink；root 之上的祖先链须由宿主管理且不受业务写者修改。本文不宣称冻结了整个文件系统，也不把目录 stat 当作权限/内容缓存。

工具的默认根、宿主 `__artifact_read_root` 与 `__task_work_dir` 同样保留原路径，不能先 resolve 后再传给 reader，否则会把链接目标直接变成新的认证根。这里只展开用户家目录，不提前消除 symlink 身份。

索引通过只实现 exists/open 协议的局部 source 送给原流式解码器。安全 FD 以整数描述符附带原索引名，再交给原 Path.open/io.open 流包装和计量层；不会按该展示路径二次打开。正文解析和全文 hash 使用同一次安全打开读出的字节，不再普通 Path.read_text 重开。

根内 alias 解析到根内目标后可读；硬链接、非普通文件、unsafe parent 和严格 dirfd/nofollow 能力缺失时拒绝，不静默退回不安全打开。这对缺少严格原语的平台是明确的 fail-closed 行为，不能据本机测试宣称 Windows 已通过。

hash 合同保持 wrapper.sha256 优先、缺失时才取 record.sha256 的旧归档兼容；不是同时证明 index 和 wrapper 元数据一致。每次读取重新算完整 content hash，同大小并恢复 mtime 的篡改仍会被识别。

## 测试入口与证据口径

新增文件：

- `agent_py_agent/tests/test_artifact_read_lookup_plan.py`：三种全读预算分支单次查找/发现、有限读拒绝零扫描、异常释放、计划防御性复制/冻结、六项身份错配重新准备、候选只匹配一次、空路径和父目录缓存每 query 重置。
- `agent_py_agent/tests/test_artifact_read_path_races.py`：发现目录逃逸、reader及真实工具三种根入口的预置 symlink、正文检查后 root/parent/leaf 替换、安全打开前/持FD后的索引替换、根内 alias、严格能力缺失和同大小同 mtime 篡改。真实硬链接测试保留；当前执行环境连普通临时文件的 os.link 也返回 EPERM，只对此明确准备失败报告 skip，不能计为真实硬链接验证通过，需 Linux 车道补验。

原四组合同在未修改的基线、现有 Python 3.12 测试环境中运行：209 passed。初次误用系统 Python 3.14 时，基线有 7 项失败（1 项 Path.resolve 链循环行为差异、6 项内存计量阈值）；该事实保留，不修改旧断言或共享模块来迁就环境。

新增查找测试原实现为 18 failed / 2 passed；路径测试原实现复现外部正文、逃逸索引与检查/打开竞态。硬链接最初失败在 os.link 准备阶段，普通临时文件探针同样 EPERM，不把它当产品缺陷红灯。

验收使用工作树根和正确解释器（现有 Python 3.12 环境，PYTHONPATH=.），不重装或修改全局环境：

2026-10-28 状态补充：以下为当时执行的历史命令，原结果保留；尺寸数值门已退役，今后复验不再运行该尺寸命令或生成/恢复报告，其余实际回归、Ruff、导入边界、文档同步与 clean-package 按现行 AGENTS 执行。

```bash
python3 -m pytest agent_py_agent/tests/test_artifact_read_lookup_plan.py agent_py_agent/tests/test_artifact_read_path_races.py agent_py_agent/tests/test_memory_artifact_read.py agent_py_agent/tests/test_tool_ref_hot_paths.py agent_py_agent/tests/test_tool_ref_scope_stream.py agent_py_agent/tests/test_tool_ref_hot_measurement.py -q --tb=short
ruff check agent_py_agent/agent/tooling/artifact.py agent_py_agent/agent/memory_archive/artifact/reader.py agent_py_agent/tests/test_artifact_read_lookup_plan.py agent_py_agent/tests/test_artifact_read_path_races.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
```

历史验收时生成的 CODE_SIZE_REPORT 不是本切片交付物，已恢复、不随提交带入；退役后不再生成或恢复。计量必须同时观察查找发现和权限根复核，不把返回字符数当总 IO，不把本地合成计数等同 2C4G Linux 线上延迟。

## 本机验证结果

解释器为 `/Users/xiaoyezi/my-agent-dsh/.venv/bin/python`（Python 3.12），所有命令在目标工作树根执行；pytest 显式 `PYTHONPATH=.` 且 `-o addopts=''`，没有删除、放宽或跳过任何旧测试。证据是本次命令的完整日志：

| 检查 | 实际结果 | 日志文件名 |
|---|---|---|
| 最终版本：两个新增文件和四组旧合同联合回归 | 242 passed, 1 skipped；16.99s | job-1792816378126497000.log |
| 最终版本：四文件 Ruff | exit 0，All checks passed | job-1792816378126497000.log |
| 最终版本：strict 尺寸门（按 CODE_SIZE_BASELINE） | exit 0，hard=0，blocked=False；既有 high-risk/soft 基线并非清零 | job-1792816432431188000.log |
| git diff --check（含五文件暂存差异） | exit 0；交付前与提交范围核对一起执行 | 本次命令回执 |

日志位于本次宿主执行记录的 `.background_jobs/` 下，非产品源码和提交交付物。生成的 CODE_SIZE_REPORT 已恢复至 HEAD。额外 AST 对比检查确认本次新增/修改的四文件所有定义（含内嵌函数）均有双层注释，最长产品函数按非空非注释行计为35行，没有超过60行。

唯一跳过项是原生硬链接：os.link 在测试准备阶段被 EPERM 拒绝，独立普通临时文件探针也复现；保留真实用例并标明 Linux 补验，不能把它写成硬链接防护已实测通过。这里没有运行 Linux 生产索引延迟基准、2C4G 负载测试、Windows 实机或部署后的端到端链路，均为未验证。

## 独立审查处理

独立只读审查覆盖需求、两产品文件diff、共享解码/nofollow依赖及两个新增测试，提出一条重要入口问题：工具 `_artifact_read_root` 提前 resolve 会丢失认证根 symlink 信息。父级通过真实 `ReadArtifactTool.execute` 复核，默认根拒绝、两个内部根参数仍错误成功（2 failed / 1 passed）；随后保留认证根原路径并在上列最终六组回归中验证三种入口均拒绝。审查发现已处理，没有用静态审查文字代替运行证据；本说明的结果以修复后日志为准。
