# 临时离线发行包

状态：2026-10-12，用户已授权将 L/T/IM 整合版本合入远端 main 并构建 `0.3.0-tui-preview.20261012.8`。本次发布使用 main 的准确提交冻结源码，安装包 `release.json` 的 `base_commit` 和 `source_sha256` 是对应关系。发布检查与未解决边界见 [整合交接](../tasks/L_T_IM_INTEGRATION_HANDOFF.md#主线发布追加2026-10-12)。历史 `.2` / `.5` 的验收不能冒充本次包的验收。

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
    `-- owners/local/main/  # 原 owner resolver 返回的默认工作目录
        `-- workspace/     # 原任务工作文件目录，沿现有产品布局创建
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

1. 等当前任务结束，退出本安装的所有 TUI；若运行飞书等独立通道，用固定入口执行 `adapter stop`，然后执行 `gateway stop`。
2. 上传新版 `.run`，再次执行 `bash 新包.run --prefix /app/.my-agent`。
3. 用同一固定入口查看 `--version`，执行 `gateway start --ready-timeout 30` 启动后台；原先运行飞书的安装再执行 `adapter start --channel feishu --daemon`，然后进入 TUI。

`gateway stop` 不等于 `adapter stop`；通道是独立进程，停止 Gateway 后可能继续占用旧版运行时。停止通道不删除飞书配置/凭据或记忆，更新期间连接暂时中断，恢复后须实测收发。

安装器先核对包 SHA256 与逐文件清单，再复制到新的版本目录，最后原子切换 current。
安装过程中发现本安装仍有活动 Python 进程时会停止更新并列出 PID，不自动杀任务。
同版本不同内容拒绝覆盖；传输损坏不切换当前版本。更新保留 data 内的配置、历史和工作文件原字节。
不自动变更 Linux HOME、不迁移其它 my-agent 安装；安装服务器只切换该 prefix 的发行版本，不操作开发仓库。
保留旧程序不等于数据库降级方案；本临时更新流程不承诺跨任意未来数据 schema 的自动回退。

## 构建与验收

构建使用冻结生产源码，经原 setuptools wheel 过滤开发专用模块；依赖只在构建机下载。
`scripts/private_release/prepare_runtime.sh` 准备运行时，`prepare_bwrap.sh` 重建同源沙箱，
`build.py` 生成参数展示快照、转换字节码、生成逐文件清单及自解压头。`installer.py` 和 `configure.py`
也编译进包。自解压头仅需 Ubuntu 自带 bash/tar/gzip/coreutils。

相对源码发行，只有三个产品适配点：Landlock 启动器读取真实模块 origin；包工作区识别 `.pyc` 入口；
参数读取方的展示元数据使用同源构建快照。快照不决定配置权限或运行路由。

校验分两层：源码继续走原 clean-package；有意编译的私有发行按 `release.json` 枚举的文件检查，
禁止 `__pycache__`、额外成员、外链、运行数据和业务 `.py`。不要将本格式的有意字节码误称为源码缓存，
也不要把普通源码包中的 pyc 当作允许项。

验收至少包括：断网首次安装、路径含空格、原位置升级、配置/历史字节不变、坏包不切换、活动进程阻止切换，
Ubuntu 三版 CLI 启动、UTF-8、真实 Gateway + TUI，以及原输入/停止修复回归。未完成项必须留在交接记录。
