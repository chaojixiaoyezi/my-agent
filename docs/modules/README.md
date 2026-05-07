# 模块文档四件套规范

这里按功能模块保存长期文档。目标不是把旧文档一次性搬家，而是给后续开发一个稳定位置：灵感、推进、初心、结构都能找到。

总导航：
- `LLM_GUIDE.md`：LLM 总入口，含开工前/收工后清单
- `docs/ROADMAP.md`：待做/进行中功能清单
- `docs/COMPLETED.md`：已落地功能清单
- `DESIGN_LEDGER.md`：设计决策主台账

## 标准结构

每个模块使用一个目录：

```text
docs/modules/<module>/
|-- 01-discussion.md   # 灵感碰撞 / 功能讨论记录
|-- 02-progress.md     # 开发推进记录：做了什么、解决什么、测了什么
|-- 03-purpose.md      # 初衷和想法
`-- 04-structure.md    # 结构树和详细说明
```

可以从 [_template/](./_template/) 复制四个模板开始写。

## 四件套分别写什么

- `01-discussion.md`：记录为什么想做、当时讨论过哪些方向、痛点是什么、哪些问题还没想清楚。
- `02-progress.md`：记录已完成、解决的问题、下一步、已跑测试、未跑测试、风险。测试要尽量写全面，不只写“跑过 pytest”。
- `03-purpose.md`：解释这个功能的初心、服务谁、为什么这样设计，而不是只列技术名词。
- `04-structure.md`：写模块结构、核心文件、数据流，以及给刚学编程的学生看的学习路径。

## 什么时候更新

- 新开一个功能模块时，先建四件套空壳，哪怕第一版只写索引和待补齐。
- 完成一个开发切片时，更新 `02-progress.md` 的“已完成 / 已跑测试 / 风险”。
- 发现新的用户痛点、设计取舍或失败样本时，更新 `01-discussion.md` 或 `03-purpose.md`。
- 新增核心文件、数据流变化或学习路径变化时，更新 `04-structure.md`。

## 代码、文档、注释同步门

后续改功能时，不能只改代码。最小同步要求：

- 改实现代码时，同一批 diff 必须更新对应模块的 `02-progress.md`，说明做了什么、解决什么、跑了什么测试、还有什么没跑。
- 改核心文件、数据流、入口、任务边界或新增/删除文件时，同一批 diff 必须更新对应模块的 `04-structure.md`。
- 改 module / class / def 行为、新增 class / def、调整重要参数时，同一个 Python 文件里必须同步更新定义上方注释。
- 功能代码的 module / class / function / method 必须使用双层注释：`LLM:` 给模型说明契约、调用方、副作用和修改边界；`模块用途:`、`函数用途:` 或 `类用途:` 给人用大白话说明用途、调用时机和修改注意事项。
- class / def 有装饰器时，注释放在装饰器上方；不能夹在装饰器和定义中间。
- 旧格式里的 `新手说明:`、`参数说明:`、`返回说明:` 可以继续保留，但不能替代 `LLM:` 和 `模块用途:` / `函数用途:` / `类用途:`。
- 如果只是修 typo 或纯测试数据，可以在提交说明里写明为什么没有代码文档同步；默认仍先跑检查脚本。

提交前运行：

```powershell
python scripts\check_doc_sync.py
```

如果只想检查已暂存内容：

```powershell
python scripts\check_doc_sync.py --staged
```

当前脚本先覆盖 `log-analysis`、`subagent`、`memory`、`gateway`、`live-lab` 这几个活跃模块。新模块进入开发时，要先建四件套，再把模块路径加入 `scripts/check_doc_sync.py` 的 `MODULE_RULES`。

## 测试记录怎么写

`02-progress.md` 必须保留这六个小节：

- 已完成
- 解决的问题
- 下一步
- 已跑测试
- 未跑测试
- 风险

写测试记录时，尽量说明测试覆盖了什么、没有覆盖什么。例如：

- 单元测试：覆盖模型序列化、边界值、错误输入。
- CLI 测试：覆盖命令参数、默认配置、失败提示。
- 手工闭环：覆盖真实命令从输入到输出的路径。
- 未跑项：说明原因，例如“本次只改文档，未跑 pytest”。

## 当前模块

| 模块 | 四件套入口 | 说明 |
| --- | --- | --- |
| subagent | [subagent/](subagent/) | 子代理、受控派工、质量契约、workflow 规划。 |
| log-analysis | [log-analysis/](log-analysis/) | 安全日志接入、查询、检测、case、报告和 analyst work order。 |
| memory | [memory/](memory/) | 长期记忆、规则路由、raw archive、恢复和诊断。 |
| gateway | [gateway/](gateway/) | 后台 gateway、本地请求队列、chat attach、恢复和 adapter。 |
| live-lab | [live-lab/](live-lab/) | 可见真实环境演练、离线 replay、suite/case 产物。 |

## 与旧文档的关系

- `DESIGN_LEDGER.md` 继续做全局设计台账，只放摘要和导航。
- `CODEBASE_TREE.md` 继续做全局目录树，只放结构说明和入口。
- 旧的 backlog、acceptance、evidence、runbook 不移动；模块四件套用链接引用它们。
