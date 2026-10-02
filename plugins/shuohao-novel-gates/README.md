# shuohao-novel-gates：五阶段只读质量门

将固定上游 shuohao-skills 的改编大纲、美术、剧本、分镜和角色校验做成一个
`plugin_package.v6` Node 插件。只用 Node 标准库，要求 Gateway 的 PATH 中有 Node 18+；
不安装 npm 依赖、不联网、不调用出图服务，也不写工作区。

状态（2026-10-02）：已合入集成分支。ae 审查后修了一处门判定（只给 cast、不给 outline 时剧本的 `refs-characters`
不再错报通过），并在隔离环境完成真实 TUI 安装/确认/调用和一次真实模型自然调用，
见[验收记录](../../docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c14-第一批两个插件的审查与真实验收2026-10-02)。
C14 后续（分支 `worker/ds1-c14-followups`）把按数据内容跳过的门从"报通过"改为"报跳过"，与上游一致：
cast 没有名字 → no-names、大纲没有 props 字段 → prop-cap、shots 为空数组 → shot-recipe，且 shots 输入要求非空数组。
当前执行环境的 `/bin/ps` 启动拒绝和嵌套 Seatbelt 拒绝分别保留为宿主测试失败和沙箱测试跳过，
不关闭宿主出生身份校验，也不以普通子进程测试冒充沙箱验证。完整命令和结果见
[TESTS](../../TESTS.md)。

## 工具与操作

所有工具声明 `requested_effect: read_only`，`path` 为必填文件路径。
相对路径只按宿主**本次**下发的 cwd 解释；绝对路径同样需要本次读取授权。

| 工具 | operation | 参考参数与边界 | 门总数 |
| --- | --- | --- | --- |
| `outline_check` | `validate` / `checkup` | `stage=skeleton/beats/full` 控制 validate 结构完整度，默认 full | 14 |
| `art_check` | `validate` / `checkup` | 可选 `cast`；缺失时 no-names 门标跳过 | 11 |
| `script_check` | `validate` / `checkup` | 可选 `outline`、`art`、`cast`；缺失依赖的相关门标跳过 | 10 |
| `storyboard_check` | `validate` / `checkup` / `stats` | 校验必须提供 `script`，可选 `outline`、`art`、`cast`、`shots`（逐张卡片的路径列表） | 校验 17 |
| `cast_check` | 仅 `validate` | 必须提供 `book` 原文；可选 `lang`、`style`，逐字核对角色引文 | 0，返回角色数和问题列表 |

前四阶段默认 `checkup`，角色默认 `validate`。没有给每个阶段虚构 stats 或 checkup：
只有分镜 CLI 有独立 `stats` 子命令，统计 `path` 指定的**已有门日志**（JSONL），不是镜头/资产数量。
坏日志行返回 `ignored_lines`，不追加或生成日志。

## 回执口径

- 校验结果包含 `stage`、`operation`、布尔 `passed`、`complete`、`problems`、`gates` 和 `counts`。
- 每道门保留 `id`、`label`、`detail`、`upstream_ok`，公开 `status=passed/failed/skipped`、
  `passed=true/false/null` 和缺依赖的 `reason`；跳过不计入通过数。
- `counts` 分开记录 `total/passed/failed/skipped`；角色另有 `character_count` 和问题数。
- 顶层 `passed=true` 只代表已执行的检查没有失败；有 skipped 时 `complete=false`，
  **不能据此声称全部门已验**。按数据内容跳过的门与上游一致：cast 没有名字 → no-names 跳过、
  大纲没有 props 字段 → prop-cap 跳过、未传 `shots`（或空数组）→ shot-recipe 跳过；
  跳过只报 `status=skipped`，不计入 passed，也不计入 failed。`shots` 参数 schema 要求非空数组
  （`minItems: 1`），与上游 CLI 对 --shots 目录至少一张卡的要求一致。完整私有 shot-recipes 卡库未迁入。
- `validate` 包含上游结构校验问题，`checkup` 返回门报告。门不通过仍是成功计算的业务结果；
  路径/参数/上下文错误才是 MCP `isError=true`，用稳定结构化错误码说明。
- MCP 工具 `content[0].text` 是 JSON 文本。宿主命令 `output` 还有一层 JSON 信封，
  必须用 `json.loads` 分层解码，不在转义字符串里搜索 `"passed":true`。

## 构建与本人确认

在仓库根目录执行，输出放可重建的 tmp 目录：

```bash
PY=~/.my-agent/releases/claude-tools/ci-venv-312/bin/python
PYTHONPATH=$PWD PYTHONDONTWRITEBYTECODE=1 "$PY" scripts/build_plugin_files_package.py \
  --declaration plugins/shuohao-novel-gates/declaration.json \
  --files-root plugins/shuohao-novel-gates \
  --output tmp/shuohao-novel-gates.zip
```

包文件由 declaration 的显式清单决定，摘要由构建脚本计算。两次构建按字节相同；
README、conformance、自检、样例、镜头卡和 report 夹具不进 ZIP。

真实入口步骤（2026-10-02 已按此在隔离环境验收；安装包须放在 owner 可读范围内）：

1. 管理员输入 `/plugins install "<构建出的 ZIP 绝对路径>"`，安装后默认停用。
2. 输入 `/plugins enable shuohao-novel-gates`，读取解释器和包文件的确认回执。
3. 用户本人核对后输入回执给出的 `/plugins enable shuohao-novel-gates --confirm <确认码>`。
4. 例如 `/plugins@shuohao-novel-gates outline_check outline.json --operation validate`，
   或 `/plugins@shuohao-novel-gates cast_check cast.json --book book.txt`。
5. 分镜校验示例：`/plugins@shuohao-novel-gates storyboard_check storyboard.json --script script.json`；
   日志统计示例：`/plugins@shuohao-novel-gates storyboard_check .gates.jsonl --operation stats`。

Node 升级/替换导致已确认身份变化时，先停用，再按原回执重新本人确认；不自动追随新解释器。
模型自然使用也必须受原 Registry、权限与具体调用批准约束，安装或启用不等于扩大读取授权。

## 只读与来源边界

- `src/gates.js` 同进程调用原样 ESM 的导出计算函数，不进入 CLI main、storyboard logGates 或隐式文件读取助手。
  分镜调用等价于 `--no-log`，CLI 对照测试显式传该参数，不向用户工作区写 `.gates.jsonl`。
- 主文件、参考 JSON、原文、已有日志和每张卡片均先经过 `my-agent/workspace-read-context` v1；
  只取本次 `_meta`，不接收业务参数伪造上下文，不用环境/cwd 补授权。
- 打开时拒绝链接末段，复核真实目标和 dev/ino，只读普通文件、完整 UTF-8，单文件上限 16 MiB。
  Node 没有 openat，此打开后复核不具有 Python SDK 逐段目录描述符的同等竞态强度；
  它是协作式权限检查，不是 OS 沙箱。OS 沙箱仍由宿主统一控制。
- 上游整份 CLI 保留未暴露的写命令和 HTML renderer，但工具/参数白名单不提供它们；
  没有 chunk/seed/render/assemble/export/merge/report 写工具（M-B2 待实施）。
- 不迁 install.sh、付费出图路线、作者图片或完整私有卡库。
- 许可、固定提交、每个文件的来源路径/sha256 和改动说明见 [PROVENANCE](PROVENANCE.md) 与
  [UPSTREAM.json](UPSTREAM.json)。上游 LICENSE、NOTICE 原样随包；NOTICE 的旧样例路径不改。
- characters 源文件保留原样 NUL，用 `.gitattributes -text` 禁止文本转换；不清理字节、不改上游源码。
  源树 clean-package 已放行，工作区/暂存字节和 UPSTREAM 摘要相同，未增加忽略项绕门。

## 仓库验证与后续

```bash
PYTHONPATH=$PWD PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest \
  agent_py_agent/tests/test_shuohao_novel_gates.py \
  -q --tb=short -p no:cacheprovider --basetemp=/private/tmp/claude-501/m-sol2
```

测试覆盖真实 Node MCP、tools/list 与声明同源、74+20 读取向量、六份上游自检、
无工作区改写、越界与伪上下文拒绝、可复现打包及原样许可/NUL，以及三条跳过语义回归
（cast 无名字 → no-names、大纲无 props → prop-cap、shots 空数组 → shot-recipe）。
完整宿主安装→本人解释器确认→五注册工具和 report 平台沙箱测试保留，环境拒绝不改成通过。
收尾范围（2026-10-02，3a）：只跑本插件、跨语言样例等直接相关测试和 guards9，不再跑全仓或无文件列表的-x。
本分支聚焦结果（2026-10-02，ds1）：33 passed、1 failed、1 skipped——失败是宿主在带确认码启用处
（MCP 候选启动被沙箱环境拒绝，`plugin_endpoint_failed`，非本次改动路径），跳过是沙箱不可用；
宿主侧 141 个相关测试（确认回执、管理文案、目录提示、目录 schema）全过。
建议下一步：3a 在沙箱外重跑固定提交的完整文件，ae 可并行只读核许可和包装边界；
确认后再合入，并按迁移设计做真实 TUI 与一次真实模型自然调用。
