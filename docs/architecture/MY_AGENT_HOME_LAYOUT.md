# My-Agent Home Layout / 家目录设计

本文记录 `~/.my-agent/` 的长期目录约定。目标是让主代理、子代理、未来外部平台用户和群空间都有清楚边界：能共享公共能力，但不能互相污染记忆、任务和文件。

## 默认目录

```text
~/.my-agent/
  SOUL.md                         # 主账号人格文件；新会话可读，用来保持“像谁”
  USER.md                         # 主账号偏好；例如语言、风格、常用路径
  AGENTS.md                       # 新会话/clear 后应该先读什么、遵守什么
  memory.md                       # 关键记忆入口，只放重点和引用，不塞全文

  config/                         # agent_config.yaml、capability_config.yaml 等真实配置
  scripts/                        # my-agent 自己可复用的全局脚本，不属于某个任务
  skills/                         # 主账号全局 skill
  tools/                          # 主账号全局工具
  role_templates/                 # 主账号全局角色模板
  workflows/                      # 主账号全局工作流模板

  workspace/
    tasks/
      2026-05-13/
        购物网站-e2e-main/        # 某个任务自己的工作目录和产物目录

  memory/
    daily/                        # 按天存每日对话/事件流水，避免单个 memory.jsonl 无限变大
    raw/                          # 更接近原始事件的归档，按配置级别决定预览粒度
    hooks/                        # 关键节点快照，例如 compact 前、恢复点、gateway/run 完成点
    indexes/                      # 轻量索引卡片，用来快速找到事实源，不替代原始文件
    lessons/                      # 教训库，例如派工教训、工具教训；memory.md 只引用这里

  memory_archive/
    artifacts/                    # 大工具输出、外置正文、可恢复 artifact
    compact_applies/              # 手动/自动 compact apply 生成的恢复包和 ledger
    snapshots/                    # 权威恢复快照，compact/resume 前优先读
    tokens/                       # token 账本和上下文预算记录

  data/                           # 本地索引、gateway、兼容数据和旧路径迁移区
  providers/                      # QQ、飞书等外部平台接入后才懒创建
  logs/                           # 运行日志
  cache/                          # 可重建缓存
  tmp/                            # 临时文件
```

## 外部知识库

外部知识库先用一组扁平配置，不引入复杂 `query_order`：

```yaml
external_knowledge_index_file_name: "MY_AGENT_INDEX.md"
external_knowledge_directory_roots: []
external_knowledge_api_sources: []
external_knowledge_database_sources: []
```

查询顺序固定为目录、API、数据库。目录来源优先找 `MY_AGENT_INDEX.md`，再由索引指向小索引或正文。默认全空时不做任何额外扫描。

## Provider 用户/群空间

外部平台只在接入时创建自己的根目录，例如：

```text
~/.my-agent/providers/
  feishu/
    provider.yaml
    users/
      ou_123/
        SOUL.md / USER.md / AGENTS.md   # 未来可按用户单独生成
        tools/ skills/ role_templates/ workflows/
        workspace/ memory/ data/ sessions/
        downloads/ cache/ tmp/ trash/
    groups/
      oc_abc/
        tools/ skills/ role_templates/ workflows/
        workspace/ memory/ data/ sessions/
        downloads/ cache/ tmp/ trash/
```

原则：

- 主账号不放进 `providers/`，它使用 `~/.my-agent/` 根空间。
- 外部用户和群可以使用平台允许的公共 tools/skills/scripts/templates，但不能写主账号目录。
- 外部用户新增的 tools/skills/templates/workflows 只放自己空间。
- 群空间由群主/群管理员管理，只能拆自己的“群屋子”，不能越权到别的群、用户或 owner home。
- 删除默认进本空间 `trash/YYYY-MM-DD/`，30 天后再清理；第一版已经有 trash 目标和审计事件能力。
- 每个外部用户/群空间有 MiB 级容量配置，管理员可在后端配置里调整。

## Memory 区域说明

- `daily/`：按天流水，适合还原当天发生过什么。
- `raw/`：接近原始的事件归档。配置 level 越低越详细，level 越高越像恢复摘要。
- `hooks/`：关键节点快照，不只在崩溃时用；compact、恢复、gateway、runner 收尾都可以写。
- `indexes/`：目录卡片和轻量引用，应该分片/按天/按来源存，不把所有内容堆成一个大文件。
- `compact_applies/`：每次 `memory-compact --apply` 的非破坏性恢复包。它帮新会话恢复，不会删除旧记录。
- `snapshots/`：权威恢复快照。compact/resume 优先看它，再看 raw/hook。
- `artifacts/`：大正文或工具输出外置。默认读预览，必要时显式读取正文，避免 1G 日志直接进 prompt。
- `providers/*/users|groups/*/memory/`：外部用户/群自己的记忆空间，不和主账号长期记忆混用。

## 当前已实现

- `agent.user_space.home_layout`：解析 `MY_AGENT_HOME`、标准路径、任务工作区模板，并提供不覆盖已有入口文件的 `ensure_my_agent_home()` 初始化。
- `agent.user_space.provider_space`：懒创建 provider 根、外部用户/群空间、配额统计、同空间 trash 和 scoped audit。
- `agent.external_knowledge.config`：把后端配置转成外部知识库查询 bundle。
- `AgentConfig` / normalize / 默认 YAML 已有家目录、任务模板、外部知识库和 provider 空间配置字段。

## 后续接入

1. 安装/初始化命令创建 `~/.my-agent/` 基础文件。
2. memory 写入从旧 `data/memory.jsonl` 逐步迁到 `memory/daily/YYYY-MM-DD.jsonl`。
3. provider adapter 首次见到新平台/用户/群时调用 provider space resolver。
4. 工具/skill 执行网关读取 provider space scope，限制外部用户只能写自己的空间。
5. trash retention 后台任务按配置清理过期 trash，但审计事件继续保留。
