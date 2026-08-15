# 产物格式打开器：兜底与运行时扩展

产物验收分三层（见 `agent/contracts/artifact_openers.py` 与 `artifact_acceptance.py`）：

- **第 0 层（格式无关）**：文件存在、非空、非占位/报错残桩。任何格式都过这层，未知格式也过。
- **第 1 层（打开器注册表）**：`{格式: 打开器}`。打开器只把 bytes 变结构化视图，或返回"打不开"finding，不做内容校验。
- **第 2 层（通用检查器）**：吃打开器视图 + 合同声明字段（`required_columns` / `required_sections` / `required_strings` / `min_size` / `required_files` 等）做校验。

## 运行时遇到未登记格式怎么办

不拒绝、不崩。行为对齐 长期助手 / 工具运行时 / 终端应用：

1. **合同声明了结构要求** → 用**通用兜底打开器** `open_fallback` 按"长相"尽量校验：
   - 能解码的字节当文本，跑 `required_strings` / `required_sections` / `min_size` 等声明检查；
   - zip 包（PK 头）暴露成员名，跑 `required_files` 成员检查；
   - 纯二进制则只回退到第 0 层。
2. **没有结构声明** → 落第 0 层放行（存在/非空/残桩通过即可）。

通用兜底产出的是格式无关 finding（`ARTIFACT_REQUIRED_TEXT_MISSING` / `ARTIFACT_REQUIRED_MEMBER_MISSING` / `ARTIFACT_TOO_SMALL` 等），永不随格式增长。

## 不改源码、不重启加一种新格式的深度校验

往**打开器目录**丢一个脚本即可。目录解析顺序：

1. 环境变量 `MY_AGENT_OPENERS_DIR`（优先）
2. 默认 `~/.my-agent/openers/`

脚本写法（二选一）：

```python
# ~/.my-agent/openers/myfmt.py  —— register 钩子写法（推荐）
from agent_py_agent.agent.contracts.artifact_openers import OpenedArtifact, GenericView

def register(register_opener):
    def open_myfmt(path):
        # 打开成功 → 产视图；打不开 → return OpenedArtifact("myfmt", finding=ArtifactFinding(...))
        return OpenedArtifact("myfmt", view=GenericView(text=path.read_text(encoding="utf-8")))
    register_opener("myfmt", open_myfmt)
```

```python
# 或者直接导出 OPENERS dict
OPENERS = {"myfmt": open_myfmt}
```

加载规则（可靠保证）：

- 启动/刷新（`discover_openers(force=True)`）时扫描目录，逐脚本**隔离加载**；
- 任何脚本的导入/执行异常只记进 `OPENER_LOAD_ERRORS`，**绝不打断主链路**，已注册的好打开器不丢；
- **内置格式（csv/xlsx/pdf/docx/json）不可被插件覆盖**，保证已知格式行为可靠不变；
- 脚本产出的视图只要带 `text` 或 `zip_names`/`names` 属性，第 2 层通用检查器就能消费。

## 新增格式的成本

- 简单文本/zip 格式：**0 行**——通用兜底自动覆盖（合同声明结构要求即可深度校验）。
- 需要专属解析的格式：**一个十几行的打开器脚本**，丢进打开器目录，不改主代码、不重启。
- 进主仓的常用格式：在 `_BUILTIN_OPENERS` 注册一行（可选再加一个薄结构检查函数）。
