# CODE SIZE POLICY

LLM: Enforce this policy with `scripts/check_code_size.py`.

给人看的解释：
这份规范控制文件、函数、类和入口门面的规模，防止项目继续长成大文件和万能 Manager。

## Limits

- 普通生产文件：理想不超过 250 行，超过 400 行必须说明，超过 600 行必须拆或登记豁免。
- CLI 注册文件：理想不超过 200 行，超过 300 行拆命令注册器，超过 400 行不允许继续膨胀。
- CLI 交互/TUI 文件：理想不超过 250 行，超过 400 行必须拆 session/router/renderer，超过 500 行列入优先整改。
- Service 文件：理想不超过 250 行，超过 350 行拆服务，超过 500 行不允许新增。
- Repository 文件：理想不超过 200 行，超过 300 行拆读写/索引/查询。
- Manager/Facade 文件：理想不超过 120 行，只允许组合、委托和兼容旧 API。
- 普通函数：理想不超过 40 行，超过 60 行评估拆分，超过 100 行必须拆或豁免。
- 普通类：理想不超过 150 行，超过 250 行评估职责，超过 350 行必须拆或豁免。
- Mixin 类：超过 200 行停止新增复杂逻辑，超过 250 行必须改 service。

## Complexity

- 新增函数圈复杂度目标不超过 10。
- 嵌套深度超过 3 层必须评估拆分。
- 参数超过 6 个应改 options/dataclass，超过 8 个必须拆。

## Forbidden

- 新增 `import *`。
- 新增 `utils.py`、`helpers.py`、`common.py`、`manager_extra.py` 等垃圾桶命名。
- 继续向 `cli/parser.py`、`cli/chat.py`、`agent/settings/config.py`、SubAgent mixin、Gateway runtime、Memory archive query/runtime 塞新功能。

## Exemptions

- 豁免必须登记到 `ARCHITECTURE_EXEMPTIONS.md`。
- 豁免必须包含原因、风险、拆分计划、负责人和过期日期。
- 禁止永久豁免。

## CI

- `python scripts/check_code_size.py --mode warn` 必须在 CI 中运行。
- 严格阶段再启用 `python scripts/check_code_size.py --mode strict`。
