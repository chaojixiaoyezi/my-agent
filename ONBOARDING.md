# ONBOARDING — 新 LLM / 新开发者开工指南

这份文档是你的唯一入口。读完它，你就知道怎么跑起来、怎么改代码、怎么提交。

---

## 第一步：跑起来（5 分钟）

```bash
cd /Users/xiaoyezi/my_agent/my-agent
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -e ".[dev]"
my-agent --help
```

看到帮助信息就说明安装成功了。

---

## 第二步：读文档（按顺序）

| 顺序 | 文件 | 读什么 | 时间 |
|:---:|------|--------|:---:|
| 1 | **本文件** (ONBOARDING.md) | 全局地图 | 5 min |
| 2 | [AGENTS.md](AGENTS.md) | LLM 开发者的行为规范：语言、注释、配置、自学习约束 | 10 min |
| 3 | [docs/development/DEVELOPMENT_RULES.md](docs/development/DEVELOPMENT_RULES.md) | 编码规则：版本兼容、导入纪律、命名、尺寸限制、写入边界 | 10 min |
| 4 | [docs/architecture/BOUNDARY_RULES.md](docs/architecture/BOUNDARY_RULES.md) | 分层导入矩阵：谁能导入谁、禁止模式 | 5 min |
| 5 | [docs/architecture/MODULE_OWNERSHIP.md](docs/architecture/MODULE_OWNERSHIP.md) | 每个模块的职责和状态：新增代码放哪里 | 查表 |

不需要全部读完再开工。读完前 3 步就可以开始改代码了，遇到具体模块再查第 4、5 步。

---

## 第三步：跑测试（提交前必做）

```bash
# 快速测试（PR 默认）
python3 -m pytest -q -m "not slow and not e2e" --tb=short

# 架构护栏
python3 -m pytest agent_py_agent/tests/test_architecture_guardrails.py -q

# 代码规模检查
python3 scripts/check_code_size.py --mode warn

# 脏文件检查
python3 scripts/check_clean_package.py .

# 语法检查
python3 -m compileall -q agent_py_agent scripts

# lint
ruff check agent_py_agent scripts
```

全部通过才能提交。

---

## 第四步：提交

```bash
git add <your-changed-files>
git commit -m "type: 简短描述"
git push
```

提交后 CI 会自动跑 Lint + Test。看 https://github.com/chaojixiaoyezi/my-agent/actions 确认全绿。

---

## 项目结构速览

```text
my-agent/
├── agent_py_agent/          # Python 包，核心代码
│   ├── cli/                 # CLI 命令层（解析参数、调用 agent、格式化输出）
│   ├── agent/
│   │   ├── agent_core/      # 主代理调度循环和运行时执行
│   │   ├── subagents/       # 子代理完整生命周期管理
│   │   ├── gateway_parts/   # gateway 进程和请求处理
│   │   ├── memory_store/    # 记忆物理存储（JSONL）
│   │   ├── memory_routing/  # 记忆路由规则
│   │   ├── memory_archive/  # 记忆归档、压缩、快照
│   │   ├── tooling/         # 外部工具安全封装（文件、shell、web）
│   │   ├── settings/        # 配置定义和加载
│   │   ├── session/         # 会话管理
│   │   ├── adapter/         # 通道适配器（QQ、飞书）
│   │   └── ...
│   └── tests/               # 测试目录
├── scripts/                 # 开发辅助脚本和治理检查工具
├── docs/                    # 项目文档
│   ├── architecture/        # 架构文档（边界规则、模块归属、重构计划）
│   ├── development/         # 开发规范（编码、测试、提交、review）
│   ├── decisions/           # 架构决策记录（ADR）
│   └── ...
├── pyproject.toml           # Python 打包和工具配置
└── .github/workflows/       # CI（Lint + Test + Full Tests）
```

**导入方向**：`cli/` → `agent_core/` → `subagents/` → `memory_store/`（单向，不许反向）

---

## 关键规则速记

1. **不许 `import *`** — 每个 import 必须写明符号名
2. **不许新文件叫 `utils.py` / `common.py` / `helpers.py`** — 用具体名字
3. **新文件不超过 400 行，新函数不超过 100 行，类不超过 250 行（Mixin 不超过 200 行）** — 超了就拆
4. **参数超过 8 个的函数必须用 dataclass bundling 模式** — 用 `params: SomeParams` 而不是展开 kwargs
5. **所有文件写入必须走 `tooling/filesystem_write.py`** — 不许直接 `Path.write_text()`
5. **Python 3.10+** — f-string 里不许用反斜杠
6. **中文写注释和文档，英文写代码**

---

## 遇到问题

| 问题 | 去哪看 |
|------|--------|
| 这个模块是干嘛的？ | [MODULE_OWNERSHIP.md](docs/architecture/MODULE_OWNERSHIP.md) |
| 这个文件能不能导入那个文件？ | [BOUNDARY_RULES.md](docs/architecture/BOUNDARY_RULES.md) |
| 测试怎么分层？ | [TESTING_POLICY.md](TESTING_POLICY.md) |
| 打包要注意什么？ | [CLEAN_PACKAGE_POLICY.md](CLEAN_PACKAGE_POLICY.md) |
| 代码尺寸超标了？ | [CODE_SIZE_POLICY.md](CODE_SIZE_POLICY.md) |
| 要做大的架构变更？ | 先写 ADR，放在 [docs/decisions/](docs/decisions/) |
