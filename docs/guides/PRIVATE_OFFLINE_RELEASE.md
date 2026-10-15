# 临时离线发行包

状态：2026-10-13，R01–R21 的 `.20261013.4` 为上一版；用户已授权本轮 M01–M09/C01–C04/L01 修复推送 main 并打包。`.20261013.5` 是 14 项修复的隔离验收候选，随后追加模型等待刷新，交付版本改为 `0.3.0-tui-preview.20261013.6`。每次发布使用 main 的准确提交冻结源码，安装包 `release.json` 的 `base_commit` 和 `source_sha256` 是对应关系。本轮见 [核查与实施](../design/IM_CLOUD_SCHEDULE_LATENCY_REVIEW.md)，上一轮见 [本期交接](../tasks/IM_MONITOR_PERMISSIONS_IMPLEMENTATION.md)。各历史包验收不能冒充本次包的验收。

## 适用范围与内容

- 目标为无桌面的 Ubuntu 20.04、22.04、24.04，x86_64；实际机器信息为 Ubuntu 22.04.5 / appuser。
- 自包含 CPython 3.11、项目默认依赖及原 `scale,secrets` extra；不要求目标机安装 Python、pip、Docker、Node.js 或编译器。
- 业务模块转换成无源码 CPython 字节码，保留 `python -m`、子进程及原模块导入语义；不会随包交付业务 `.py` 源码。
  字节码可以被逆向，本格式不是加密或不可逆保护。配置、提示词、开放插件模板、第三方源码与许可证保持可读。
- 宿主源码不随包；用户账号、密钥、已有会话、开发记录和测试凭据不进入载荷。
- 原 bubblewrap 0.8.0 与原 openEuler 补丁在 Ubuntu 20.04 重新链接，带局部 libcap、对应源码和许可；
  系统 bubblewrap 优先，没有系统版本时使用随包版本。沙箱判定仍沿原合同，不绕过 namespace / AppArmor。
- 安装完全离线。模型请求仍需能访问用户配置的模型服务；额外安装插件或任务要求的软件，可能需要内网软件源。

## 目录与首次安装

账户的 Linux HOME 保持原样。安装位置通过 `--prefix` 显式指定，后续更新必须使用同一路径。

```text
/app/.my-agent/
|-- releases/          # 不可变程序版本；保留旧版本
|-- current            # 原子切换的当前版本链接
|-- bin/my-agent-preview  # 固定启动入口，不依赖登录 HOME
`-- data/              # 配置、密钥、会话、缓存、临时文件和工作文件
    `-- owners/local/main/  # 原 owner resolver 返回的默认工作目录；tasks 等业务文件在这里
        `-- workspace/     # 原运行时工作布局，Gateway 服务状态在这里
```

如果 appuser 无权在 /app 建目录，管理员只需准备一次：

```bash
sudo install -d -o appuser -g "$(id -gn appuser)" -m 750 /app/.my-agent
```

把 `.run` 上传到当前登录目录（例如 `~/`），首次安装目录 `/app/.my-agent` 保持为空；
随后以 appuser 安装。Ubuntu 标准安装自带所需 bash、tar、gzip 和 coreutils，无须另装运行时：

```bash
bash my-agent-版本-linux-x86_64.run --prefix /app/.my-agent
/app/.my-agent/bin/my-agent-preview --version
/app/.my-agent/bin/my-agent-preview --configure-model
/app/.my-agent/bin/my-agent-preview gateway start --ready-timeout 30
/app/.my-agent/bin/my-agent-preview
```

如需直接输入 `my-agent`，可为当前用户创建固定入口（不复制程序，后续更新仍跟随 current）：

```bash
mkdir -p ~/.local/bin
ln -sfn /app/.my-agent/bin/my-agent-preview ~/.local/bin/my-agent
export PATH="$HOME/.local/bin:$PATH"
```

将 PATH 那一行放入自己的 shell 启动文件即可永久生效。已有命令或别名能正常使用时无需重复设置。

首装配置使用独立 Gateway 端口 18420，与普通安装的 8420 分开；用户可以在自己的配置里修改。
程序始终使用原配置解析器和 Gateway 启动链，没有第二个调度入口。`workspace_root` 保持默认空值，
由原 owner resolver 在 data 下确定用户目录；不创建 owner home 外的工作区，不默认开启 Full Access。

## 百炼与中文

交互配置会询问 Base URL、模型 ID 和不回显的 API Key。当前用户提供的接口为：

- Base URL：`https://dashscope.aliyuncs.com/compatible-mode/v1`
- 模型：`qwen3.7-max`
- Key：只在服务器本地输入，不在命令行或分发包里保存。

配置入口写原 `model_backend=openai_compatible`、`api_base`、`model_name` 和 `api_key`；
`/chat/completions` 和 Bearer 请求头由原后端处理。以实际账号开通的模型和允许访问的地址为准。
接口格式参考[阿里云官方说明](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions/)。

启动器显式启用 UTF-8，原 CLI 也对标准流设置 UTF-8。SSH 客户端须选择 UTF-8 和有中文字形的字体；
服务器 locale 是 UTF-8 但中文呈方框时，应检查客户端字体。客户端设置不会通过安装包修改。

## 同一路径更新

1. 等当前任务结束，退出本安装的所有 TUI；上传新版 `.run` 和配套 `SHA256SUMS`。
2. 执行 `sha256sum -c SHA256SUMS`，然后 `bash 新包.run --update --prefix /app/.my-agent`。
3. 用同一固定入口查看 `--version` 并进入 TUI。更新器自动保存并恢复原先开启的 Gateway 和 adapter，不必提前手工停止它们；原来关闭的服务继续保持关闭。

新版也可执行 `/app/.my-agent/bin/my-agent-preview update --package 新包.run`。
省略 `--prefix` 时自动识别当前账户的原安装；存在多个安装时要求明确选原路径，填成新的路径会拒绝建立第二套 Gateway。
深层自定义安装目录用显式 `--prefix` 更新，仍校验同一账户、路径与原安装标记，不要求登录 HOME 或环境变量保持不变。
从 `.20261012.8` 更新须使用新包的 `bash … --update` 入口；旧 CLI 不具备新版的离线 update 子命令。
旧版未声明排空协议，更新器沿原 CLI 执行 `gateway stop --stop-background --background-timeout 30`；因此先等任务结束。
新版 `drain.v1` 版本先排空请求再停止。更新器复用 canonical owner/runtime 服务目录，不用当前工作目录或安装器的 HOME 猜 PID 地址。
旧版心跳丢失启动通道时，更新器明确从原 registry 的 `channels[].name` 快照迁移；空、非法或冲突记录会在停服务前拒绝更新。

`gateway stop` 不等于 `adapter stop`；通道是独立进程，停止 Gateway 后可能继续占用旧版运行时。自动更新分别控制两者。若已手工停止服务，更新后按需执行 `gateway start --ready-timeout 30`、`adapter start --channel feishu --daemon`。更新期间连接暂时中断，停止不删除飞书配置/凭据或记忆，恢复后须实测收发。

安装器先核对包 SHA256 与逐文件清单，再复制到新的版本目录，最后原子切换 current。
停服务前发现本安装仍有交互 TUI 时拒绝更新；原服务控制完成后仍有活动旧进程则列出 PID 并拒绝切换，不按进程名称批量杀任务。
同版本不同内容拒绝覆盖；传输损坏不切换当前版本。更新保留 data 内的配置、历史和工作文件原字节。
不自动变更 Linux HOME、不迁移其它 my-agent 安装；安装服务器只切换该 prefix 的发行版本，不操作开发仓库。
保留旧程序不等于数据库降级方案；本临时更新流程不承诺跨任意未来数据 schema 的自动回退。

## 构建与验收

构建使用冻结生产源码，经原 setuptools wheel 过滤开发专用模块；依赖只在构建机下载。
`scripts/private_release/prepare_runtime.sh` 准备运行时，`prepare_bwrap.sh` 重建同源沙箱，
`build.py` 生成参数展示快照、转换字节码、生成逐文件清单及自解压头。`installer.py` 和 `configure.py`
也编译进包。自解压头仅需 Ubuntu 自带 bash/tar/gzip/coreutils。

纯 Python 修复且依赖未变时，可从已校验上一版载荷复用 Ubuntu 20.04 同源 runtime/native；业务必须重新从准确 Git 提交构建 wheel，清理发行副本中的旧业务目录及旧 dist-info 后安装，不沿用旧业务 pyc。重生成清单前删除旧 release.json，防止它被纳入自身文件哈希。最终包继续执行 verify-only、原位置更新/数据字节保留、版本/新模块导入及 TUI smoke；复用运行时不等于跳过新包验证，也不把上一版三系统结果当本次重新验证。

相对源码发行，只有三个产品适配点：Landlock 启动器读取真实模块 origin；包工作区识别 `.pyc` 入口；
参数读取方的展示元数据使用同源构建快照。Landlock 启动与断网探测显式传入 `-B`，避免 `-I` 忽略环境变量后向发行运行时写标准库缓存。快照不决定配置权限或运行路由。

校验分两层：源码继续走原 clean-package；有意编译的私有发行按 `release.json` 枚举的文件检查，
禁止 `__pycache__`、额外成员、外链、运行数据和业务 `.py`。不要将本格式的有意字节码误称为源码缓存，
也不要把普通源码包中的 pyc 当作允许项。

验收至少包括：断网首次安装、路径含空格、原位置升级、配置/历史字节不变、坏包不切换、活动进程阻止切换，
Ubuntu 三版 CLI 启动、UTF-8、真实 Gateway + TUI，以及原输入/停止修复回归。未完成项必须留在交接记录。


## 20261014.6 依赖安全与锁图同步

本期22项及全卡取消的发行候选从 `.20261014.5` 改为 `0.3.0-tui-preview.20261014.6`。`.5`已冻结身份只作为被替代的候选证据，不把其源码hash或旧wheel身份标成`.6`；根完成本地严格gate并推送后，重新以准确干净main冻结源码、wheel与各平台清单。

GitHub实际23条开放Dependabot告警中，4条来自uv.lock：urllib3 2.7.0的3条（2 high、1 medium，修复2.8.0），cryptography 49.0.0的1条（high，修复50.0.0）。本次用原uv机制准确锁urllib3 2.8.0、cryptography 50.0.2及原依赖元数据/hash，不手填下载hash、不放宽pyproject范围。旧锁只有70包，其根metadata没有源码早已声明的computer-use/computer-use-observe extras，dev图也缺少后续已有声明。原uv解析补全为301包，只有上述两个既有包升级；其它新增节点是已声明可选/开发依赖的锁图同步，**不是新增默认依赖或把桌面执行器装入发行包**。发行仍选择原默认依赖与scale/secrets，不使用`--all-extras`。

`uv lock --check`必须对最终完整锁通过，不能交仅替换两个stanza却仍与pyproject漂移的半锁。uv0.11.2定点命令为：

```bash
uv lock --upgrade-package 'urllib3==2.8.0' --upgrade-package 'cryptography==50.0.2'
uv lock --check
uv export --locked --no-dev --extra scale --extra secrets --no-emit-project --format requirements.txt --output-file /tmp/my-agent-release-requirements.txt
```

完整锁已用 `uv lock --check` 核验且检查前后hash相同；原发行选择导出63个包要求，不包含computer-control-mcp、pyautogui、pyobjc、pytest或hypothesis。63是跨平台带marker的导出要求数，不是对每个平台实际安装数量或payload大小的断言。

根升级新候选后可运行以下兼容focused（本锁文件切片只做解析/导出检查，不运行服务或大套件）：

```bash
python -m pytest agent_py_agent/tests/test_task_credential_storage.py agent_py_agent/tests/test_feishu_crypto.py agent_py_agent/tests/test_feishu_transport_security.py agent_py_agent/tests/test_feishu_cli_backend.py -q --tb=short
```

本轮不改运行中的旧runtime。Linux准备目录已读到urllib3 2.8.0、cryptography50.0.2；Mac旧基座仍urllib32.7.0，不能仅克隆后`pip --no-deps`安装业务wheel就声称漏洞已修。根负责在新Mac候选和隔离验证venv同步到准确依赖版本，保留原旧实例，然后记录新payload实际METADATA和完整性清单、运行既定focused/字节码/native验收。锁文件更新本身不证明现有环境已升级。

剩余19条告警位于frontend/package-lock.json（14开发范围、5前端运行范围），本次wheel和离线包不包含frontend；它们仍影响前端开发或单独前端部署，等待另一个明确范围的修复，不改npm锁、不冒称全仓无漏洞。所列23条没有CLI Go清单项；这不能替代CLI二进制完整漏洞审查。真实供应商、代理配置和客户端渲染等原未验边界不因锁同步而改变。

建议下一步：根先核隔离验证venv/新Mac候选实际依赖，跑原凭据、加密事件和Feishu运输相关focused，再严格gate并重新冻结`.6`；可并行只读比对平台依赖版本与原发行清单，不自动扩大到前端升级或新专项测试。


## 20261014.7 观察依赖安全追加

最新 open Dependabot 从23条变为40条：前端仍19条（14开发、5前端运行），新增21条为MCP/Pillow。它们属于已声明的观察extra，但退役Mac `.6`实际装有MCP1.13.0/Pillow11.3.0，不能声称未入包；Linux stage未安装这两包。本机原配置保持 `computer_use_enabled=false`、`computer_use_observation_enabled=true`，保留观察能力。

观察extra的darwin/linux准确pins升级为MCP1.28.1（MIT、Python>=3.10）与Pillow12.3.0（MIT-CMU、Python>=3.10），官方元数据为 [MCP PyPI](https://pypi.org/pypi/mcp/1.28.1/json) 与 [Pillow PyPI](https://pypi.org/pypi/pillow/12.3.0/json)。原uv机制重新生成304包完整锁；其中新旧MCP/Pillow按互斥extra分支分别保存，新增PyJWT2.15.1，不用override篡改上游要求。

上游最新computer-control-mcp0.3.13仍精确要求旧MCP1.13.0/Pillow11.3.0。`tool.uv.conflicts`明确自动执行 `computer-use` 与 `computer-use-observe` 不能同时安装；原自动执行extra不删、不偷偷升级，但仍含已知安全告警，本期观察runtime不安装它。默认依赖与自动执行开关均不变，不能使用`--all-extras`。若要自动执行安装形态，必须单独解决上游兼容和漏洞，不能沿用本期观察验收结论。

隔离验证venv只更新MCP/Pillow和必要PyJWT，并卸除该验证克隆中未启用的旧自动执行器，`pip check`通过；原共享venv、实际旧runtime不动。退役Mac `.6`的实际METADATA没有computer-control-mcp。真实SDK测试经原computer_use_server完成stdio initialize/list_tools（仅observe_window，tools/call=0），替换桌面后端构造避免真实屏幕，合成PixelBuffer经原PNG编码、Pillow解码/重编码通过。SDK构造出现pydantic-settings的lifespan前向引用警告，未导致元数据协议失败，不把这次元数据验证等同真实屏幕/OCR或完整自动执行验收。

Mac ARM/Python3.12离线安装材料放本期artifact `dependency-wheels/observe-security`，`observe-security-manifest.json`逐wheel记录filename/name/version/SHA256，不覆盖urllib3清单。最终候选须离线安装该组并检查实际METADATA及pip check，再做准确新包字节码验收，不能复用旧候选身份或仅用锁证明已安装修复。前端19条和旧自动执行extra告警仍待修，不宣称全仓无漏洞。


构建工具安全基线追加：项目构建后端与dev依赖下限同步为 `setuptools>=83`，uv定点锁83.0.0；发行实际环境的pip需26.2.0、setuptools需83.0.0。pip不是项目运行依赖，不加进pyproject，升级由根在新候选/构建隔离环境执行并记录实际METADATA与wheel SHA；旧runtime与共享venv保持。该基线不改变默认业务依赖、不开启自动执行extra，旧上游执行器和前端仍有未修告警，不声称全仓无漏洞。
