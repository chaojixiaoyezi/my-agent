# workspace-peek 首个自有插件

状态：第 4 步已有本地业务、标准安装包、独立 MCP 及原宿主完整装卸组件验证；发布部署和实际 TUI 验收待完成。

解决隔离插件只有注册壳、不能真正读取当前工作区的问题。本样本提供 UTF-8 文本/Markdown 有界预览和
有深度限制的目录树；不执行 Shell、网络请求或写用户文件。原宿主管理、审批和撤销仍负责全部控制。

## 唯一声明与构建

`plugins/workspace-peek/src/workspace_peek/declaration.json` 保存动作、参数、工具及设置 schema。
同一资源供 MCP `tools/list` 与开发构建器读取，不维护两份输入 schema。发行版本取实际 wheel 元数据，
归档加入 wheel 名称和 SHA256 后由原 PluginManifest/包/依赖闭包校验器验证。
SDK 精确版本在标准依赖中声明，全部 wheel 随包带齐；安装和启用阶段不构建或下载依赖。
开发构建仅对本仓自有工程执行，原 LICENSE/NOTICE 随标准 wheel 保留。输出明确指定，不能覆盖已有包。
标准后端 `setuptools>=77` 显式列入主工程开发依赖；离线构建不借本机偶然已安装的后端作为环境合同。
构建入口与命令见 [样本 README](../../plugins/workspace-peek/README.md)，无需新增第三方运行时依赖。

## 业务与权限

- `/plugins@workspace-peek show <path> [--bytes N] [--cursor TOKEN]`：按字节预算预览 UTF-8 文件，保留原换行；
  返回实际本页起止偏移、文件身份、是否到达 EOF 及下一页游标。空文件有效；非 UTF-8 或含 NUL 的内容明确拒绝。
- `/plugins@workspace-peek tree [path] [--depth N] [--limit N] [--cursor TOKEN]`：在明确深度和扫描预算内完整
  枚举并排序，再按页返回目录树。扫描预算包含拒绝项；超预算明确失败，不把任意目录前缀伪装成完整快照。
- 配置 `page_bytes`、`page_entries`、`scan_entries` 有明确默认值与上界；配置只影响读取和展示预算，不授予目录权限。
- 每次调用必须有宿主的 `workspace-read-context`；候选路径逐项经共用策略检查。空读取范围不回退 cwd。
- 从文件系统锚点逐段 no-follow 打开，拒绝路径链中的链接、`..` 和多链接/非普通文件；不先 resolve 后丢失链接事实。
  持有同一 fd 做 fstat/seek/read，目录通过 fd 枚举；缺少严格 dirfd/no-follow 能力的平台明确不可用，不降级普通 open。
- 每个目录子项都再检查权限；被拒绝项不暴露名称或内容，只有拒绝数量。目录链接不递归。
- 文件游标绑定 path、dev/ino/size/mtime/ctime 与 byte offset；UTF-8 分页扣除未完成字符的尾字节。
  目录游标绑定完整受限快照 digest、depth 与 offset；对象变化拒绝旧游标。游标仅是进度，不是授权或“全部已读”证明。

预览先使用原工具文本结果；第 9 步声明式展示仍待实现，不声称已有自定义 TUI 面板。
开发验证须覆盖真实标准 wheel/独立环境、实际插件协议与功能；实际 TUI 三路管理/业务/核心仍单独验收。
