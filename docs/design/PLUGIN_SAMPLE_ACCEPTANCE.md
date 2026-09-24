# 简易插件样本与真实 TUI 验收计划

状态：计划已记录，10 个样本均待实施，未安装或实测第三方插件。公开资料核对日期：2026-09-20。
本计划是 [合并重构顺序](MAINTAINABILITY_AND_JEV_REVIEW.md#下一轮结构整理顺序待实施) 的第 10 步；整体排期以该文档为准。
Audit/摄取重构已移出本轮，剩余 TUI 整理保留。此处仅借鉴社区插件的功能，自行编写适合 my-agent 的 Python 简易版。

## 解决问题与实施边界

单个只读工具只能证明注册链可用，不能证明插件接口足以支持文件、进程、配置、Skill、展示和并发装卸。
用 10 个小而完整的包验证这些差异；每个包只有明确的最小业务功能，但必须实际运行、有独立身份并能完整撤销。
不以空命令、固定返回值或只显示已安装的壳凑数，也不为某个样本给核心加入业务特判。
第 4—6 步的首个只读插件优先使用 workspace-peek，纳入这 10 个包，后续扩展同一个实现。

插件管理继续使用 `/plugins`，业务入口使用 `/plugins@插件ID [动作] [参数]`。
默认只显式安装本地包；停用时不导入实现、不启动进程、不订阅事件、不注入模型目录。
现有任务、历史、记忆、权限与工具执行仍由核心负责；插件配置和自有文件使用宿主管理的 owner 作用域。
UI 插件提交可校验的文本、表格、状态等展示描述；由核心渲染，插件 Python 不在 TUI 进程内执行。
第 9 步只开放有作用域、可撤销的只读快照/事件订阅；队列有界，旧代次拒绝，停用清除面板、回调和定时刷新。
订阅或渲染异常只影响该插件，不能卡住核心输入；展示层不能发明运行状态或代替取消、审批、调度。

## 热度口径与随机抽样

先通过 GitHub 官方仓库 API 按 `topic:dsh-plugin` 和 Star 查询，再筛选能缩成小功能的候选。
这里是候选池内抽样，不是全生态 Top 10 排名；Star 是查询时的仓库关注度，不是安装数或质量证明。
候选池按以下顺序固定，查询时均超过 100 Star：

```text
dsh-market/dsh-market
omdsh-dev/DSH-better-sidebar
chuspeeism/dashi-taskboard
bowenliang123/dsh-context
Devin-AXIS/deepseek-design
LiPu-jpg/Openwrite
omdsh-dev/dsh-browser
ccch1mneyyy/working-activity
Aisland-SJL/dsh-worktable
omdsh-dev/dsh-genui
lire1131/dsh-undo-savepoint
liustack/modlens
NanmiCoder/dsh-agent-teams
PC2005-cloud/dsh-pet
```

以 `random.Random(20260920).sample(candidates, 10)` 抽样。抽中的 dashi-taskboard 虽声明相关 topic，
但本轮主分支 README 与根 package.json 只核对到 Codex 看板入口，未证实其 DSH 插件入口，因此不纳入。
使用同一随机生成器从未抽中的有序候选列表 `choice` 补入 dsh-worktable；不据此断言被排除项目完全不支持 DSH。
下表按实施用途排列，来源链接指向作者仓库；功能范围及命令是本项目提案，不是第三方插件现有命令。

## 10 个最小功能包

| 来源与当日 Star | 自有插件 ID / 命令示例 | 简易版实际做什么 | 主要验证点 |
| --- | --- | --- | --- |
| [dsh-context](https://github.com/bowenliang123/dsh-context) · 1,447 | `context-inspector`：`/plugins@context-inspector show` | 查看当前会话的上下文组成与工具/Skill 来源清单；只读，不主动压缩或改写历史 | 作用域快照、只读命令、来源定位；用量等遥测只用于显示，不进入模型请求或调度 |
| [DSH-better-sidebar](https://github.com/omdsh-dev/DSH-better-sidebar) · 3,683 | `workspace-peek`：`/plugins@workspace-peek show ./README.md` | 文件树和文本/Markdown 预览，支持有界读取及继续翻页 | 文件权限、长输出游标、声明式预览、插件工具与普通中文调用 |
| [dsh-worktable](https://github.com/Aisland-SJL/dsh-worktable) · 652 | `worktable-lite`：`/plugins@worktable-lite list` | 展示当前 owner 的会话/任务列表，定位到原会话；仅保存自己的显示偏好 | 多会话隔离、公共状态投影、配置持久化；不新建任务账本或 Gateway |
| [working-activity](https://github.com/ccch1mneyyy/working-activity) · 660 | `activity-line`：`/plugins@activity-line show` | 在面板显示当前工具、耗时和最近结构化进展，随事件更新 | 只读订阅、事件顺序、队列边界及停用回收；不解析模型自述决定状态 |
| [dsh-pet](https://github.com/PC2005-cloud/dsh-pet) · 692 | `status-pet`：`/plugins@status-pet show --style whale` | 用文字/符号显示工作、等待、空闲状态，提供外观配置 | 展示贡献、设置生效、面板重绘及撤销；不用第三方动画素材，不加余额请求或自动对话 |
| [dsh-genui](https://github.com/omdsh-dev/dsh-genui) · 468 | `genui-lite`：`/plugins@genui-lite table --path data.json`（导出用 `export --output x.html`） | 将结构化数据渲染成表格、简单图表，可导出独立 HTML 文件 | 参数 schema、结构化渲染、产物引用、Skill 随插件装卸；首版不做完整 Web UI 框架 |
| [deepseek-design](https://github.com/Devin-AXIS/deepseek-design) · 1,368 | `design-lite`：`/plugins@design-lite create --template card` | 按模板生成一个 HTML 设计文件，并支持修改标题、颜色等局部字段 | 多动作命令、写权限、文件读回、Skill 按需加载；不做 PPT/视频或完整可视化编辑器 |
| [ModLens](https://github.com/liustack/modlens) · 3,998 | `image-text`：`/plugins@image-text read ./sample.png` | 提取图片文字与结构化结果；需要模型视觉理解时经原模型链使用官方 MiniMax-M3 | 可选依赖隔离、图片输入、密钥引用复用、失败与取消；不另建模型执行器 |
| [dsh-undo-savepoint](https://github.com/lire1131/dsh-undo-savepoint) · 160 | `savepoint-lite`：`/plugins@savepoint-lite save ./sample.txt` | 对明确指定的测试文件创建、列出和恢复快照，恢复前核对当前版本 | 多步读写、配置/数据归属、冲突与权限；不快照或改写核心源码、私密配置和正式历史 |
| [dsh-browser](https://github.com/omdsh-dev/dsh-browser) · 701 | `browser-lite`：`/plugins@browser-lite open <测试地址>` | 在专属浏览器上下文中打开受控页面、读取元素、点击和填写测试表单 | 多工具会话、真实外部进程、取消、超时、断连及精确回收；不接管用户日常登录态 |

网页型参考只借鉴用途：TUI 中提供简易文本/表格展示，需要网页的结果输出成可检查的文件。
常规真实 LLM 调用使用官方 MiniMax-M2.7；视觉功能使用官方 MiniMax-M3，复用 M2.7 的私有 provider/密钥引用，只切模型名。
本地 OCR 可用于文字提取，但不能冒充 M3 视觉验收；必须从真实 TUI 输入图片，并核对实际模型与图像请求事实。
OCR/浏览器依赖在其独立环境显式准备，核心环境不安装这些依赖；缺依赖必须显示不可用，不能算功能通过。
声明、实现、配置与 Skill 都从各包注册，不能靠核心按上述 ID 写分支。

## 分批实现与验收

1. 第一批 3 个：workspace-peek、context-inspector、savepoint-lite，验证读取、展示、写入及状态归属。
2. 第二批 4 个：worktable-lite、activity-line、status-pet、genui-lite，验证多会话、可撤销订阅及展示组合。
3. 第三批 3 个：design-lite、image-text、browser-lite，验证 Skill、产物、可选依赖和外部进程。
4. 最后做混装、连续长任务、部分停用、故障卸载和全部卸载的综合回归，核对核心基线。

接口稳定后，不同插件包的实现和独立 TUI 场景可以并行；核心协议、注册和公共 TUI 入口保持单一写入负责人。
发现接口缺口先修通用边界，再继续插件实现；不让每个包各自塞一套旁路或复制核心状态。

每个包必须通过：

- 从真实 TUI 安装、查看帮助/配置、启用、执行实际功能、停用、再启用和卸载；安装成功与启用成功分别留证。
- 提供工具/模型任务的包同时用普通中文需求触发，不能只靠显式 slash 成功；纯展示包通过实际 TUI 操作验证，不强行调用模型。
- 结果读取实际文件、页面、公开状态、工具结果与退出事实；不以模型说完成为证据，不由测试者代写任务产物。
- 包内按功能覆盖参数错误、缺依赖/配置、执行失败、阻塞/断连及清理失败；故障由测试包或受控服务制造，不写核心业务特判。
- 至少一条业务执行与该包停用/卸载交错；纯展示包验证在途刷新/订阅撤销，执行包验证进程或调用的有界回收。
- owner 隔离、命令冲突、目录代次、旧快照撤销、Skill 移除和恢复后的配置均有对应证据；卸载不抹去已发生的历史和用户产物。

组合验收只用一台机器的一个 Gateway，真实 TUI 分成插件任务、普通内置任务、管理三个角色，可各开多个独立会话。
覆盖连续多文件处理、浏览器多步骤任务、主子代理协作、Compact 后继续、回合中断/恢复及长任务中装卸。
Goal 暂停、中断当前回合、停止任务资源仍是三个不同控制动作，插件管理不得混用它们。
开始前报告真实 TUI 编号、tmux 会话/窗口与查看命令，核对官方 provider/端点及 Gateway 进程身份；测试后用相同身份验证未因装卸重启。
全部卸载后用新的基线会话复测内置工具、模型配置与目录；历史会话中真实插件调用记录保留。
任一包失败单列，不以其余包通过抵消；记录开发验证和真实验收各自范围，不提前编造 TUI 编号或通过数量。
测试只用合成文件、测试页面和明确归属的资源；原始日志、截图、个人路径、凭据和配置留仓库外。

## 阅读证据与适用边界

本轮读取上述作者仓库 README 和包声明，未做全量源码审计、安装或运行；不以 Star 宣称兼容或质量。
除根 package.json 外，实际插件包还核对了以下入口，避免将项目仓库与可安装插件混为一谈：

- working-activity：`packages/activity/working-activity/package.json` 的 DSH 包，根包是 pi 版本。
- deepseek-design：`packages/deepseek-idesign/package.json`，只参考 Design，不把同仓 PPT/Video 算作额外样本。
- dsh-pet：`dsh-pet/package.json`；dsh-worktable：`01_content/package.json`。
- dsh-browser：`packages/browser/bridge-browser/package.json`，不以同名 npm 包推断它就是本项目。

版本与授权需在实际实现涉及第三方依赖时再次核对；本计划采用独立实现，不复制第三方代码、动画或主题素材。
