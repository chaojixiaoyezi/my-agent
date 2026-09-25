# 插件独立 Python 环境

状态：第 4 步内部准备器已接原 operation 资源声明和托管进程链，尚未接完整启用命令、发布或完成真实 TUI 验收。安装表仍是插件状态唯一权威；环境目录存在不等于启用。

## 准备与发布

环境准备只消费原安装包的不可变字节和宿主分配的候选身份，不重新打开用户来源，不扫描目录挑选版本。
候选位于 canonical owner 插件目录下，每次准备绑定固定地址。Python venv 和其中脚本含绝对路径，
因此不能先在临时地址安装再 rename；在最终地址准备，完整验证后才由原安装表发布引用。
准备失败不改现行安装/激活记录，也不自动启用。部分目录不证明成功，重试不得原地覆盖另一操作的候选。
环境准备结果不是第二套启用表、操作账或恢复状态机；持久结果仍经原宿主工具操作记录。
候选引用由原操作 ID、包摘要与宿主解释器指纹共同生成，只保存相对引用。失败候选保持不可用，
后续激活/卸载须凭原操作归属进行清理；本片没有通过目录扫描自动恢复的入口。
`plugin_environment_plan.py` 只读冻结原操作、包、插件、安装/配置版本与解释器指纹；完整
`plugin_environment_plan.v1` 作为单个规范 logical resource scope，由原执行器在 handler 前领取。
环境准备只接受这份已领取计划，不再接受一个裸 operation ID 后自行决定地址；解释器变化即拒绝。
这条声明证明本次准备身份，不代替唯一安装表的锁/CAS，也不把多个环境共用的包摘要单独锁住。

准备开始时冻结原 claim 的 holder/generation；原 ManagedOperationStore 在同一读事务检查
owner/task/run/attempt/tool、EXECUTING、未结算、输入摘要、epoch、完整声明和对应原锁。
本片仅服务不重入、不 reopen 的宿主管理操作；未来若允许重开，须由 coordinator 直接传原 claim，不能在旧 handler 迟到绑定时查询新代次。
绑定后不能重读当前 holder 补权，UNKNOWN、取消、丢锁和版本变化均不能继续准备；默认工具的
preclaim 权限检查保持原行为。进程记录和操作结果仍分别属于原 ProcessSessionStore 与原操作账。

## wheel 与依赖

- 仅接受包中声明且摘要已核对的本地 wheel；元数据和内部 ZIP 结构另行有界验证。
- wheel 名称、版本、Python 要求、平台标签和依赖读取 PyPA `packaging` 公共接口。
- 一个发行名称只能有一个 wheel；全部活动依赖及所需 extras 必须在固定集合内满足，缺失或版本冲突明确失败。
- 禁止直接 URL 依赖，包括当前 marker 未启用的声明；不调用索引、下载器、源码构建或安装脚本。
- 安装器使用标准 venv 自带 pip，关闭索引、依赖解析下载、缓存和字节码编译，以原 wheel 摘要逐项核对。
- 所有 wheel 一次安装。安装后校验在宿主只读完成，不启动已装插件的解释器来运行 `pip check`，避免 `.pth` 等启动代码在启用前运行。
- 插件不能覆盖环境的解释器、配置、安装器或其它 wheel 文件；文件内容、依赖有效性和激活授权是不同检查。
- RECORD 成员、强摘要与大小逐项核对，生成脚本也参与冲突检查；共享 namespace 允许不同文件共存。
- 内部 wheel 允许标准空目录，不放宽外包 v1 的规则；两层共用原 ZIP 预算与读取实现。

## 进程与权限

环境准备只运行宿主控制的 venv/pip 命令，不拼 Shell。子进程沿既有凭据擦除，再去除安装器和 Python 注入变量；
输入为空、输出留在候选中的私有有界日志，只有固定布局探测读取至多 64 KiB；等待响应原取消 token。
venv、布局探测和 pip 均复用既有 BackgroundLaunchRequest / ProcessSessionStore，先持久预留，再登记
host/child 的 PID 与出生标识；执行归属来自原 HostCommandBinding，不从路径墙或访问回退猜任务。
没有额外完成唤醒。同步等待期间仍复查原操作和期限，失败只停止已经取得的那个精确 session。
原进程启动信封当前使用 v4，显式携带 log 模式、单调截止时间与 stop_on_launcher_exit；准备进程交接后继续检查
启动宿主和同一期限，因此启动方崩溃不能让准备无限运行。普通长期后台默认零期限、交接后独立，行为保持。
未知协议或不完整模式/寿命字段拒绝，不为旧信封补默认值；一次性启动方和 host 必须同版。
session 当前 v3 区分任务与共享激活，但 venv/pip 始终保留原管理任务归属；stdio 与旧记录恢复见 [托管管道](MANAGED_PROCESS_STDIO.md)。
准备目录与配额必须来自原 owner 边界；新引用提交前再次核对原版本。退出未确认不能删除仍可能被写入的目录或宣称无副作用。
启动后异常同样进入精确收尾，身份取得失败或清理异常保留退出未确认，不无身份强杀。
原 quota 锁采用显式非阻塞准入，繁忙立即失败；旧调用方默认等待不变。取得后持锁覆盖本次准备，
以包含标准引导文件、解压副本和脚本包装的保守空间预留检查容量；不足时不创建候选。
非零配额时，同一 owner 的其它配额写入可能等待本次准备结束；实际多 TUI 并发仍须测量这一影响，不能预称完全无阻塞。
期限从准备入口起算，阶段切换和文件循环协作检查，不宣称能硬中断任意底层文件系统调用。
Python 进程与 venv 只解决依赖/崩溃隔离，不构成 OS 文件、网络或系统调用沙箱；实际插件运行边界另由激活链实现。
管理线程不得等待插件业务请求持有的锁。普通卸载不重启 Gateway、不停止共享任务或其他 MCP 客户端。
首片环境准备只支持具备原 no-follow/目录描述符能力的 POSIX 宿主。标准 venv 使用解释器符号链接，
不复制、替换核心解释器；安装依赖只写新 venv。当前 macOS Python 的复制式 venv 会使子解释器异常退出，
临时组件已验证标准链接式路径；不据此声称 Windows、所有 Python 发行版或 OS 沙箱已验。

## 新依赖与参考边界

新增 `packaging>=24.2,<27`，仅使用 metadata、requirements、markers、version、specifier、tags 与 utils 公共接口。
它由 PyPA 维护，采用 Apache-2.0 或 BSD 双许可；不引用 pip 的私有 vendor 模块。
标准库可读取元数据文本，但不能完整裁决 PEP 440/508、extras 和平台标签；自行实现会增加错误和维护负担，故不采用。
标准库仍负责 ZIP、文件、进程、venv 和状态持久化；没有引入插件框架或依赖解析器。

已定向核读 Codex `578c1b2` 的 `core-plugins/src/store.rs::replace_plugin_root_atomically`，
借鉴先准备候选再发布的顺序，不能照搬普通目录 rename 到不可移动的 venv。模块索引命中该文件和 manager，合同索引未命中；未运行参考项目测试。

公开依据：

- [Python venv](https://docs.python.org/3/library/venv.html)：独立环境、解释器及不可移动边界。
- [pip 安全安装](https://pip.pypa.io/en/stable/topics/secure-installs/)与 [安装参数](https://pip.pypa.io/en/stable/cli/pip_install/)：固定本地 wheel、哈希与禁止源码/依赖安装。
- [Packaging 元数据](https://packaging.pypa.io/en/stable/metadata.html)、[依赖](https://packaging.pypa.io/en/stable/requirements.html)与 [标签](https://packaging.pypa.io/en/stable/tags.html)：标准声明验证。
- [Packaging 许可](https://github.com/pypa/packaging/blob/main/LICENSE)：双许可来源。
- [Wheel 规范](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)：RECORD、安装目录、脚本首行和标签约束。

## 验证边界

开发先用合成合法 wheel 检查缺依赖、冲突、extras、条件依赖、URL、平台/解释器不符、路径/压缩预算与文件覆盖。
临时目录可用标准 venv/pip 做组件验证，但不计真实产品验收。真实安装/启停/调用/卸载继续由多实际 TUI 发起，
与核心任务并行；不把环境创建成功当作 MCP 功能、精确撤销或完整第 4 步通过。
当前临时组件覆盖中文空格地址、两份本地 wheel、入口脚本与 headers、原安装表保持停用、
安装前后不执行 `.pth`/模块陷阱、同操作候选拒绝覆盖；取消/超时/出生标识故障/通信故障/清理失败用替身检查。
Unicode 规范等价路径按统一碰撞键拒绝，原名称保持；200 个入口脚本及签名成员的安装记录扩张已用临时组件验证，
安装后 RECORD 具有独立有界预算和完整目标/摘要校验，不再使用原文件大小加固定小余量。
预算按完整允许目标的相对路径字节、CSV 转义和逐行固定开销推导；4,000 个短入口的实际组件也已通过。
原配额竞争和空间拒绝均在创建候选前返回。产品启用授权、配置、激活 CAS、MCP 发布与卸载仍待接线。

本片源码集成用真实临时 RuntimeDB 和原 ToolExecutor 验证 claim 先于准备、三个进程归属一致、
原请求查询及重放不重新启动；操作/锁/版本矛盾应在候选写入前拒绝。原宿主链测试另覆盖交接后
启动方崩溃、无父级轮询的截止时间和普通后台回归。这些都是开发组件，不计真实 TUI 验收。

## 非 Python 包的环境（2026-09-25，本地实现）

v6 包不建 venv、不跑 pip：`prepare_plugin_environment` 转交 `plugin_files_environment.py`，在同一计划、候选地址、配额、
期限与原操作授权下，把随包文件按摘要排他解包到 `<环境>/files/`（可执行 0500、数据 0400，读回复核）。计划里的
`interpreter_fingerprint` 字段改由本机运行事实（入口类型、平台、系统解释器真实路径与摘要）生成，准备时重新解析必须一致；
解释器类型另写 `runtime.json` 固定解释器，每次启动复核。详见[任意语言插件](PLUGIN_ANY_LANGUAGE.md)。

## 插件进程 OS 沙箱试点（2026-09-25，本地实现）

上文“Python 进程与 venv 不构成 OS 沙箱”仍是默认事实。新增开关 `plugin_process_sandbox`（默认关）：打开后插件进程
（Python 与 v6 包同一入口）经平台沙箱启动，只能写自己的数据目录，读范围与网络不变，沙箱不可用则拒绝启动。
详见[插件进程 OS 沙箱](PLUGIN_PROCESS_SANDBOX.md)。
