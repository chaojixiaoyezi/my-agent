# 当前模型目录参考快照

更新时间：2026-08-27

## 用途与边界

这份文件只解决一个问题：给后续模型目录设计留一份可追溯、可更新的当前型号参考，避免型号散落在代码
分支、价格表和示例里。它不是运行时 allowlist，不参与模型路由、权限、超时、计费或 endpoint 选择；当前
`model_name` 仍允许用户填写 provider 实际支持的未知型号，provider `/models` 与模型详情仍是运行事实。

任何型号能否使用，都必须以用户配置的 endpoint、凭据权限和 provider 实时发现为准。本文不保存
API Key、base URL 私有值或用户套餐信息。

## 2026-08-27 已核对的代表型号

| Provider | 参考型号 | 需要保留的协议差异 |
|---|---|---|
| OpenAI | `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna` | 三者均支持 reasoning；OpenAI 官方目录给出 1.05M context、128K max output。请求参数必须继续按 provider 能力投影，不能只按型号字符串猜。 |
| Anthropic | `claude-opus-5`、`claude-sonnet-5`、`claude-fable-5`、`claude-mythos-5` | 通道运行时 当前目录把这些型号标为 1M context、128K output；thinking 是否可关闭和 effort 映射不同，后续应进入声明式 compat。 |
| MiniMax | `MiniMax-M3`、`MiniMax-M2.7`、`MiniMax-M2.7-highspeed` | M3 使用原生 Anthropic thinking；M2.7 兼容流可能使用 `reasoning_content`，不能共用一条按名字硬猜的解析分支。 |
| DeepSeek | `deepseek-v4-pro`、`deepseek-v4-flash` | thinking 工具续轮需要正确回放或剥离 `reasoning_content`；旧 `deepseek-chat/reasoner` 不应继续作为新配置示例。 |
| Moonshot | `kimi-k3`、`kimi-k2.7-code`、`kimi-k2.7-code-highspeed` | K3 使用 `reasoning_effort`；K2.7 thinking 常开且要求省略若干采样/thinking 参数。 |
| Qwen | `qwen3.7-plus`、`qwen3.7-max`、`qwen3.6-plus`、`qwen3-coder-plus`、`qwen3-coder-next` | 型号能否使用取决于 Standard/Coding Plan endpoint；目录存在不等于当前凭据可用。 |
| Z.AI | `glm-5.2`、`glm-5.1` | GLM-5.2 的 reasoning 档位与 endpoint 套餐有关；未知 `glm-5*` 应允许 provider 发现后透传。 |

## 后续运行时目录方向

未来若落运行时 catalog，应是一份声明式数据而不是散落的 `if model_name == ...`：

- 标准键保存 `provider + model_id`，endpoint 与凭据继续只来自用户本地 provider 配置。
- 合并优先级建议为：用户显式配置 > provider 实时发现 > 随版本发布的种子元数据。
- 最小字段包括 `status/replaced_by`、输入模态、reasoning、context、max output、thinking 映射、compat、
  pricing source 与 checked_at。
- 未知型号默认透传；缺少价格只影响估算，不得阻止调用或改变任务状态。
- Gateway 启动时生成一次不可变目录快照，单个 run 不因后台目录刷新而中途换协议。
- 远端目录只能更新已安装 provider 的模型元数据，不能下发 base URL、headers 或认证信息。

## 来源

- [OpenAI 官方模型目录](https://developers.openai.com/api/docs/models)
- [通道运行时 MiniMax provider](（外部资料链接已移出发布文档）)
- [通道运行时 DeepSeek provider](（外部资料链接已移出发布文档）)
- [通道运行时 Anthropic provider](（外部资料链接已移出发布文档）)
- [通道运行时 Moonshot provider](（外部资料链接已移出发布文档）)
- [通道运行时 Qwen provider](（外部资料链接已移出发布文档）)
- [通道运行时 Z.AI provider](（外部资料链接已移出发布文档）)
- [通道运行时 模型目录刷新边界](（外部资料链接已移出发布文档）)

本文只记录上述页面在更新时间的公开事实；价格、可用性和型号生命周期变化时必须重新核对。
